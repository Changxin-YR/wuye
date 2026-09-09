import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import func, select
from werkzeug.security import generate_password_hash

from app import create_app
from database_fixture import test_database
from dify_client import BailianClient
from models import AiAction, Bill, Payment, User

PW = 'Fixture-only-ai-chat-payment-refresh-901!'
HASH = generate_password_hash(PW)


class AiChatPaymentContextRefreshEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        url = 'sqlite+pysqlite:///' + str(Path(self.temp.name) / 'property.db')
        self.url = test_database(self, url)
        self.app = create_app({
            'TESTING': True,
            'DATABASE_URL': self.url,
            'SECRET_KEY': 'fixture',
            'UPLOAD_FOLDER': self.temp.name,
            'DIFY_API_KEY': '',
        })
        self.factory = self.app.extensions['db_session']
        self.client = self.app.test_client()
        with self.factory() as db:
            db.add(User(
                username='admin', password_hash=HASH, role=0,
                real_name='管理员', phone='13800000001',
            ))
            db.commit()
        self.client.get('/auth/login')
        with self.client.session_transaction() as session:
            csrf = session['csrf_token']
        response = self.client.post('/auth/login', data={
            'csrf_token': csrf,
            'username': 'admin',
            'password': PW,
        })
        self.assertEqual(response.status_code, 302)

    def tearDown(self):
        self.app.extensions['db_engine'].dispose()
        self.temp.cleanup()

    def csrf(self):
        self.client.get('/auth/login')
        with self.client.session_transaction() as session:
            return session['csrf_token']

    def business(self, command, data):
        response = self.client.post(
            '/api/business/' + command,
            json={'data': data, 'confirmed': True},
            headers={'X-CSRF-Token': self.csrf()},
        )
        self.assertEqual(response.status_code, 200, response.text[:1500])
        return response.json

    def seed_bill(self):
        building = self.business('building.save', {
            'community_id': 1, 'name': 'A栋', 'floors': 20,
        })['id']
        unit = self.business('unit.save', {
            'building_id': building, 'name': '1单元',
        })['id']
        house = self.business('house.save', {
            'unit_id': unit, 'room_no': 101, 'area': '100',
            'usage': 'residential', 'occupancy': 'vacant',
        })['id']
        fee = self.business('fee.save', {
            'community_id': 1, 'name': '物业费', 'basis': 'area', 'rate': '2.00',
        })['id']
        return self.business('bill.create', {
            'house_id': house,
            'fee_item_id': fee,
            'period': '2026-09',
            'due_date': '2026-09-30',
        })['id']

    @staticmethod
    def text_response(response_id, content):
        return {
            'id': response_id,
            'choices': [{'message': {'role': 'assistant', 'content': content}}],
        }

    def test_explicit_bill_read_full_payment_then_stale_context_cannot_collect_again(self):
        bill_id = self.seed_bill()
        provider = BailianClient('http://agent.invalid', 'fixture-key', 'qwen-plus')
        self.app.extensions['dify'] = provider

        responses = iter([
            self.text_response('read-1', '我先核验这张账单。'),
            self.text_response('read-2', '已找到这张账单。'),
            self.text_response('pay-1', '我会按已核验的信息生成收款确认单。'),
            self.text_response('pay-2', '收款确认单已生成，请由当前登录人员确认。'),
            self.text_response('retry-1', '我先重新核验这张账单的当前状态。'),
            self.text_response('retry-2', '这张账单当前已结清，不能重复登记收款。'),
        ])
        provider_calls = []

        def request(*args, **kwargs):
            provider_calls.append((args, kwargs))
            return next(responses)

        with patch.object(provider, '_request', side_effect=request):
            first = self.client.post(
                '/ai/chat',
                json={'message': f'查询账单#{bill_id}'},
                headers={'X-CSRF-Token': self.csrf()},
            )
            self.assertEqual(first.status_code, 200, first.text[:2000])
            conversation_id = first.json.get('conversation_id')
            self.assertTrue(conversation_id)
            self.assertEqual(first.json.get('actions'), [])

            second = self.client.post(
                '/ai/chat',
                json={
                    'message': '给刚才这张账单登记收款200元，现金，收据号 CASH-FULL-001',
                    'conversation_id': conversation_id,
                },
                headers={'X-CSRF-Token': self.csrf()},
            )
            self.assertEqual(second.status_code, 200, second.text[:2500])
            pending = [
                action for action in second.json.get('actions', [])
                if action.get('command') == 'payment.record'
            ]
            self.assertEqual(len(pending), 1, second.text[:2500])
            self.assertEqual(pending[0]['status'], 'pending')
            self.assertEqual(pending[0]['risk_level'], 'R3')
            self.assertEqual(pending[0]['execution_mode'], 'CONFIRM')

            with self.factory() as db:
                bill = db.get(Bill, bill_id)
                self.assertEqual((bill.paid_cents, bill.status), (0, 'unpaid'))
                action = db.get(AiAction, pending[0]['id'])
                payload = json.loads(action.payload)
                self.assertEqual(payload, {
                    'amount': '200.0',
                    'bill_id': bill_id,
                    'channel': 'cash',
                    'reference': 'CASH-FULL-001',
                    'version': bill.version,
                })

            confirmed = self.client.post(
                f"/ai/actions/{pending[0]['id']}/confirm",
                json={},
                headers={'X-CSRF-Token': self.csrf()},
            )
            self.assertEqual(confirmed.status_code, 200, confirmed.text[:2000])
            self.assertEqual(confirmed.json['status'], 'executed')

            with self.factory() as db:
                bill = db.get(Bill, bill_id)
                self.assertEqual((bill.amount_cents, bill.paid_cents, bill.status),
                                 (20000, 20000, 'paid'))
                self.assertEqual(
                    db.scalar(select(func.count(Payment.id)).where(Payment.bill_id == bill_id)),
                    1,
                )
                executed_action_count = db.scalar(select(func.count(AiAction.id)).where(
                    AiAction.command == 'payment.record',
                    AiAction.status == 'executed',
                ))
                self.assertEqual(executed_action_count, 1)

            retry = self.client.post(
                '/ai/chat',
                json={
                    'message': '给刚才这张账单再登记收款1元，现金，收据号 CASH-RETRY-001',
                    'conversation_id': conversation_id,
                },
                headers={'X-CSRF-Token': self.csrf()},
            )
            self.assertEqual(retry.status_code, 200, retry.text[:2500])
            retry_actions = [
                action for action in retry.json.get('actions', [])
                if action.get('command') == 'payment.record'
            ]
            self.assertEqual(retry_actions, [], retry.text[:2500])

        self.assertEqual(len(provider_calls), 6)
        with self.factory() as db:
            bill = db.get(Bill, bill_id)
            self.assertEqual((bill.paid_cents, bill.status), (20000, 'paid'))
            payments = list(db.scalars(select(Payment).where(Payment.bill_id == bill_id)))
            self.assertEqual(len(payments), 1)
            self.assertEqual((payments[0].amount_cents, payments[0].reference, payments[0].status),
                             (20000, 'CASH-FULL-001', 'posted'))
            self.assertEqual(
                db.scalar(select(func.count(AiAction.id)).where(
                    AiAction.command == 'payment.record',
                    AiAction.status == 'pending',
                )),
                0,
            )


if __name__ == '__main__':
    unittest.main()
