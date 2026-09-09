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
from models import AiAction, AuditLog, Bill, Payment, User

PW = 'Fixture-only-ai-chat-payment-433!'
HASH = generate_password_hash(PW)


def tool_call(arguments):
    return {
        'id': 'malicious-payment', 'type': 'function',
        'function': {
            'name': 'property_agent_tool',
            'arguments': json.dumps({
                'operation': 'execute', 'command': 'payment.record',
                'arguments_json': json.dumps(arguments, ensure_ascii=False),
            }, ensure_ascii=False),
        },
    }


class AiChatPaymentConfirmationEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        url = 'sqlite+pysqlite:///' + str(Path(self.temp.name) / 'property.db')
        self.url = test_database(self, url)
        self.app = create_app({
            'TESTING': True, 'DATABASE_URL': self.url, 'SECRET_KEY': 'fixture',
            'UPLOAD_FOLDER': self.temp.name, 'DIFY_API_KEY': '',
        })
        self.factory = self.app.extensions['db_session']
        self.client = self.app.test_client()
        with self.factory() as db:
            db.add(User(username='admin', password_hash=HASH, role=0,
                        real_name='管理员', phone='13800000001'))
            db.commit()
        self.client.get('/auth/login')
        with self.client.session_transaction() as session:
            csrf = session['csrf_token']
        response = self.client.post('/auth/login', data={
            'csrf_token': csrf, 'username': 'admin', 'password': PW,
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
        unit = self.business('unit.save', {'building_id': building, 'name': '1单元'})['id']
        house = self.business('house.save', {
            'unit_id': unit, 'room_no': 101, 'area': '100',
            'usage': 'residential', 'occupancy': 'vacant',
        })['id']
        fee = self.business('fee.save', {
            'community_id': 1, 'name': '物业费', 'basis': 'area', 'rate': '2.00',
        })['id']
        return self.business('bill.create', {
            'house_id': house, 'fee_item_id': fee,
            'period': '2026-09', 'due_date': '2026-09-30',
        })['id']

    def test_chat_only_proposes_payment_and_browser_confirmation_executes_exact_user_facts(self):
        bill_id = self.seed_bill()
        with self.factory() as db:
            bill = db.get(Bill, bill_id)
            self.assertEqual((bill.amount_cents, bill.paid_cents, bill.status), (20000, 0, 'unpaid'))
            self.assertEqual(db.scalar(select(func.count(Payment.id))), 0)

        provider = BailianClient('http://agent.invalid', 'fixture-key', 'qwen-plus')
        self.app.extensions['dify'] = provider
        responses = iter([
            {'id': 'p1', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call({
                    'bill_id': 999999, 'version': 999, 'amount': '999999',
                    'channel': 'bank', 'reference': 'MODEL-GUESSED-REF',
                })],
            }}]},
            {'id': 'p2', 'choices': [{'message': {
                'role': 'assistant', 'content': '确认单已生成，我不会在用户确认前直接收款。',
            }}]},
            {'id': 'p3', 'choices': [{'message': {
                'role': 'assistant', 'content': '收款信息已生成待确认操作，请由当前登录人员确认后执行。',
            }}]},
        ])
        calls = []

        def request(*args, **kwargs):
            calls.append((args, kwargs))
            return next(responses)

        with patch.object(provider, '_request', side_effect=request):
            response = self.client.post(
                '/ai/chat',
                json={'message': f'账单{bill_id}已收款100元，现金，收据号 CASH-001'},
                headers={'X-CSRF-Token': self.csrf()},
            )
        self.assertEqual(response.status_code, 200, response.text[:2000])
        self.assertEqual(len(calls), 3)
        actions = [a for a in response.json.get('actions', []) if a.get('command') == 'payment.record']
        self.assertEqual(len(actions), 1, response.text[:2000])
        pending = actions[0]
        self.assertEqual((pending['status'], pending['risk_level'], pending['execution_mode']),
                         ('pending', 'R3', 'CONFIRM'))

        with self.factory() as db:
            bill = db.get(Bill, bill_id)
            self.assertEqual((bill.paid_cents, bill.status), (0, 'unpaid'))
            self.assertEqual(db.scalar(select(func.count(Payment.id))), 0)
            item = db.get(AiAction, pending['id'])
            payload = json.loads(item.payload)
            self.assertEqual(payload, {
                'amount': '100.0', 'bill_id': bill_id, 'channel': 'cash',
                'reference': 'CASH-001', 'version': bill.version,
            })
            dumped = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn('999999', dumped)
            self.assertNotIn('MODEL-GUESSED-REF', dumped)

        confirmed = self.client.post(
            f"/ai/actions/{pending['id']}/confirm", json={},
            headers={'X-CSRF-Token': self.csrf()},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text[:2000])
        self.assertEqual(confirmed.json['status'], 'executed')

        with self.factory() as db:
            payments = list(db.scalars(select(Payment).where(Payment.bill_id == bill_id)))
            self.assertEqual(len(payments), 1)
            payment = payments[0]
            self.assertEqual((payment.amount_cents, payment.channel, payment.reference, payment.status),
                             (10000, 'cash', 'CASH-001', 'posted'))
            bill = db.get(Bill, bill_id)
            self.assertEqual((bill.paid_cents, bill.status), (10000, 'partial'))
            self.assertIsNotNone(db.scalar(select(AuditLog).where(
                AuditLog.source == 'agent', AuditLog.action == 'payment.record',
                AuditLog.status == 'success')))
            self.assertIsNotNone(db.scalar(select(AuditLog).where(
                AuditLog.source == 'agent', AuditLog.action == 'ai_execute',
                AuditLog.status == 'success')))


if __name__ == '__main__':
    unittest.main()
