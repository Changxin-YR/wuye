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


def tool_call(call_id, arguments):
    return {
        'id': call_id,
        'type': 'function',
        'function': {
            'name': 'property_agent_tool',
            'arguments': json.dumps({
                'operation': 'execute',
                'command': 'payment.record',
                'arguments_json': json.dumps(arguments, ensure_ascii=False),
            }, ensure_ascii=False),
        },
    }


class AiChatPaymentConfirmationEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        sqlite_url = 'sqlite+pysqlite:///' + str(Path(self.temp.name) / 'property.db')
        self.url = test_database(self, sqlite_url)
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
        self.login()

    def tearDown(self):
        self.app.extensions['db_engine'].dispose()
        self.temp.cleanup()

    def csrf(self):
        self.client.get('/auth/login')
        with self.client.session_transaction() as session:
            return session['csrf_token']

    def login(self):
        response = self.client.post('/auth/login', data={
            'csrf_token': self.csrf(),
            'username': 'admin',
            'password': PW,
        })
        self.assertEqual(response.status_code, 302, response.text)

    def business(self, command, data, expected=200):
        response = self.client.post(
            '/api/business/' + command,
            json={'data': data, 'confirmed': True},
            headers={'X-CSRF-Token': self.csrf()},
        )
        self.assertEqual(response.status_code, expected, response.text[:1800])
        return response.json

    def seed_bill(self):
        building_id = self.business('building.save', {
            'community_id': 1, 'name': 'A栋', 'floors': 20,
        })['id']
        unit_id = self.business('unit.save', {
            'building_id': building_id, 'name': '1单元',
        })['id']
        house_id = self.business('house.save', {
            'unit_id': unit_id, 'room_no': 101, 'area': '100',
            'usage': 'residential', 'occupancy': 'vacant',
        })['id']
        fee_id = self.business('fee.save', {
            'community_id': 1, 'name': '物业费', 'basis': 'area', 'rate': '2.00',
        })['id']
        bill_id = self.business('bill.create', {
            'house_id': house_id, 'fee_item_id': fee_id,
            'period': '2026-09', 'due_date': '2026-09-30',
        })['id']
        return bill_id

    def test_chat_only_proposes_payment_and_browser_confirmation_executes_exact_user_facts(self):
        bill_id = self.seed_bill()
        with self.factory() as db:
            bill = db.get(Bill, bill_id)
            self.assertEqual(bill.amount_cents, 20000)
            self.assertEqual(bill.paid_cents, 0)
            self.assertEqual(bill.status, 'unpaid')
            self.assertEqual(db.scalar(select(func.count(Payment.id))), 0)

        provider = BailianClient('http://agent.invalid', 'fixture-key', 'qwen-plus')
        self.app.extensions['dify'] = provider
        responses = iter([
            {
                'id': 'payment-e2e-1',
                'choices': [{'message': {
                    'role': 'assistant', 'content': None,
                    'tool_calls': [tool_call('malicious-payment', {
                        'bill_id': 999999,
                        'version': 999,
                        'amount': '999999',
                        'channel': 'bank',
                        'reference': 'MODEL-GUESSED-REF',
                    })],
                }}],
            },
            {
                'id': 'payment-e2e-2',
                'choices': [{'message': {
                    'role': 'assistant',
                    'content': '收款信息已生成待确认操作，请由当前登录人员确认后执行。',
                }}],
            },
        ])
        calls = []

        def request(*args, **kwargs):
            calls.append((args, kwargs))
            return next(responses)

        message = f'账单{bill_id}已收款100元，现金，收据号 CASH-001'
        with patch.object(provider, '_request', side_effect=request):
            response = self.client.post(
                '/ai/chat',
                json={'message': message},
                headers={'X-CSRF-Token': self.csrf()},
            )

        self.assertEqual(response.status_code, 200, response.text[:2000])
        self.assertNotEqual(response.json.get('source'), 'planner', response.text[:2000])
        self.assertEqual(len(calls), 2)
        pending_actions = [
            action for action in response.json.get('actions', [])
            if action.get('command') == 'payment.record'
        ]
        self.assertEqual(len(pending_actions), 1, response.text[:2000])
        pending = pending_actions[0]
        self.assertEqual(pending['status'], 'pending')
        self.assertEqual(pending['risk_level'], 'R3')
        self.assertEqual(pending['execution_mode'], 'CONFIRM')

        # The proposal dry-run must leave absolutely no financial side effect.
        with self.factory() as db:
            bill = db.get(Bill, bill_id)
            self.assertEqual(bill.paid_cents, 0)
            self.assertEqual(bill.status, 'unpaid')
            self.assertEqual(db.scalar(select(func.count(Payment.id))), 0)
            item = db.get(AiAction, pending['id'])
            self.assertIsNotNone(item)
            payload = json.loads(item.payload)
            self.assertEqual(payload, {
                'amount': '100.0',
                'bill_id': bill_id,
                'channel': 'cash',
                'reference': 'CASH-001',
                'version': bill.version,
            })
            dumped = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn('999999', dumped)
            self.assertNotIn('MODEL-GUESSED-REF', dumped)

        confirm_response = self.client.post(
            f"/ai/actions/{pending['id']}/confirm",
            json={},
            headers={'X-CSRF-Token': self.csrf()},
        )
        self.assertEqual(confirm_response.status_code, 200, confirm_response.text[:2000])
        self.assertEqual(confirm_response.json['status'], 'executed')

        with self.factory() as db:
            payments = list(db.scalars(select(Payment).where(Payment.bill_id == bill_id)))
            self.assertEqual(len(payments), 1)
            payment = payments[0]
            self.assertEqual(payment.amount_cents, 10000)
            self.assertEqual(payment.channel, 'cash')
            self.assertEqual(payment.reference, 'CASH-001')
            self.assertEqual(payment.status, 'posted')

            bill = db.get(Bill, bill_id)
            self.assertEqual(bill.paid_cents, 10000)
            self.assertEqual(bill.status, 'partial')

            domain_audit = db.scalar(select(AuditLog).where(
                AuditLog.source == 'agent',
                AuditLog.action == 'payment.record',
                AuditLog.status == 'success',
            ).order_by(AuditLog.id.desc()))
            self.assertIsNotNone(domain_audit)
            confirm_audit = db.scalar(select(AuditLog).where(
                AuditLog.source == 'agent',
                AuditLog.action == 'ai_execute',
                AuditLog.status == 'success',
            ).order_by(AuditLog.id.desc()))
            self.assertIsNotNone(confirm_audit)


if __name__ == '__main__':
    unittest.main()
