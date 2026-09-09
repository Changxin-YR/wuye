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
from models import AiAction, AuditLog, Bill, User

PW = 'Fixture-only-bill-void-refresh-331!'
HASH = generate_password_hash(PW)


def void_tool_call(params):
    return {
        'id': 'model-void-call',
        'type': 'function',
        'function': {
            'name': 'property_agent_tool',
            'arguments': json.dumps({
                'operation': 'execute',
                'command': 'bill.void',
                'arguments_json': json.dumps(params, ensure_ascii=False),
            }, ensure_ascii=False),
        },
    }


class AiChatBillVoidContextRefreshEndToEndTests(unittest.TestCase):
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

    @staticmethod
    def tool_response(response_id, params):
        return {
            'id': response_id,
            'choices': [{'message': {
                'role': 'assistant',
                'content': None,
                'tool_calls': [void_tool_call(params)],
            }}],
        }

    def test_read_void_confirm_then_stale_context_cannot_void_again(self):
        bill_id = self.seed_bill()
        with self.factory() as db:
            bill = db.get(Bill, bill_id)
            self.assertEqual((bill.status, bill.paid_cents, bill.void_reason), ('unpaid', 0, ''))

        provider = BailianClient('http://agent.invalid', 'fixture-key', 'qwen-plus')
        self.app.extensions['dify'] = provider
        responses = iter([
            self.text_response('read-1', '我先查询这张账单。'),
            self.text_response('read-2', '已找到这张账单。'),
            self.tool_response('void-1', {
                'id': 999999,
                'version': 999,
                'reason': 'MODEL-GUESSED-REASON',
            }),
            self.text_response('void-2', '账单作废确认单已生成，请由当前登录人员确认。'),
            self.tool_response('retry-1', {
                'id': 999999,
                'version': 999,
                'reason': 'MODEL-GUESSED-REASON',
            }),
            self.text_response('retry-2', '这张账单已经作废，不能重复作废。'),
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
                    'message': '把刚才这张账单作废，原因是重复出账',
                    'conversation_id': conversation_id,
                },
                headers={'X-CSRF-Token': self.csrf()},
            )
            self.assertEqual(second.status_code, 200, second.text[:2500])
            pending = [
                action for action in second.json.get('actions', [])
                if action.get('command') == 'bill.void'
            ]
            self.assertEqual(len(pending), 1, second.text[:2500])
            self.assertEqual((pending[0]['status'], pending[0]['risk_level'], pending[0]['execution_mode']),
                             ('pending', 'R3', 'CONFIRM'))

            with self.factory() as db:
                bill = db.get(Bill, bill_id)
                action = db.get(AiAction, pending[0]['id'])
                payload = json.loads(action.payload)
                self.assertEqual(payload, {
                    'id': bill_id,
                    'version': bill.version,
                    'reason': '重复出账',
                })
                dumped = json.dumps(payload, ensure_ascii=False)
                self.assertNotIn('999999', dumped)
                self.assertNotIn('MODEL-GUESSED-REASON', dumped)
                self.assertEqual(bill.status, 'unpaid')

            confirmed = self.client.post(
                f"/ai/actions/{pending[0]['id']}/confirm",
                json={},
                headers={'X-CSRF-Token': self.csrf()},
            )
            self.assertEqual(confirmed.status_code, 200, confirmed.text[:2000])
            self.assertEqual(confirmed.json['status'], 'executed')

            with self.factory() as db:
                bill = db.get(Bill, bill_id)
                self.assertEqual((bill.status, bill.paid_cents, bill.void_reason),
                                 ('void', 0, '重复出账'))
                self.assertEqual(db.scalar(select(func.count(AiAction.id)).where(
                    AiAction.command == 'bill.void',
                    AiAction.status == 'executed',
                )), 1)
                self.assertIsNotNone(db.scalar(select(AuditLog).where(
                    AuditLog.source == 'agent',
                    AuditLog.action == 'bill.void',
                    AuditLog.status == 'success',
                )))

            retry = self.client.post(
                '/ai/chat',
                json={
                    'message': '把刚才这张账单再作废一次，原因是重复出账',
                    'conversation_id': conversation_id,
                },
                headers={'X-CSRF-Token': self.csrf()},
            )
            self.assertEqual(retry.status_code, 200, retry.text[:2500])
            retry_actions = [
                action for action in retry.json.get('actions', [])
                if action.get('command') == 'bill.void'
            ]
            self.assertEqual(retry_actions, [], retry.text[:2500])

        self.assertEqual(len(provider_calls), 6)
        with self.factory() as db:
            bill = db.get(Bill, bill_id)
            self.assertEqual((bill.status, bill.void_reason), ('void', '重复出账'))
            self.assertEqual(db.scalar(select(func.count(AiAction.id)).where(
                AiAction.command == 'bill.void',
                AiAction.status == 'pending',
            )), 0)
            self.assertEqual(db.scalar(select(func.count(AiAction.id)).where(
                AiAction.command == 'bill.void',
                AiAction.status == 'executed',
            )), 1)


if __name__ == '__main__':
    unittest.main()
