import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask, g
from sqlalchemy import select
from werkzeug.security import generate_password_hash

import agent_business_context as business_context
import agent_planner_state
from agent_planner import plan_request
from app import create_app
from database_fixture import test_database
from dify_client import BailianClient
from models import AiAction, House, HousePerson, Lease, User


PW = 'Fixture-only-ai-chat-lease-refresh-581!'
HASH = generate_password_hash(PW)


def lease_dates():
    local_today = (datetime.now(timezone.utc) + timedelta(hours=8)).date()
    start = local_today - timedelta(days=30)
    end = local_today + timedelta(days=335)
    move_in = datetime.combine(local_today - timedelta(days=29), time.min).isoformat(timespec='minutes')
    return start.isoformat(), end.isoformat(), move_in


class LeaseCheckoutContextPlannerTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        business_context._CURRENT.clear()
        agent_planner_state._PENDING.clear()

    def plan(self, message, authorized, conversation_id=None):
        payload = {'message': message}
        if conversation_id:
            payload['conversation_id'] = conversation_id
        with self.app.test_request_context('/ai/chat', method='POST', json=payload):
            g.user = SimpleNamespace(id=19, auth_version=1)
            return plan_request(message, authorized, {})

    def test_contextual_repeat_reuses_visible_tenant_selector_and_active_state_only(self):
        authorized = {'lease.search', 'lease.checkout'}
        first = self.plan('王五已经搬走了，办理退租，原因合同到期', authorized)
        self.assertEqual(first['intent'], 'lease.checkout')
        self.assertEqual(first['action'], 'CONFIRM')
        self.assertEqual(first['entity_status'], 'RESOLVE_FIRST')
        self.assertEqual(first['arguments']['person_name'], '王五')
        self.assertEqual(first['arguments']['status'], 'active')
        self.assertEqual(first['arguments']['reason'], '合同到期')
        self.assertNotIn('id', first['arguments'])
        self.assertNotIn('lease_id', first['arguments'])
        self.assertNotIn('version', first['arguments'])

        second = self.plan(
            '刚才这个租户再退租一次，原因重复测试',
            authorized,
            conversation_id='conv-lease-refresh',
        )
        self.assertEqual(second['intent'], 'lease.checkout')
        self.assertEqual(second['action'], 'CONFIRM')
        self.assertEqual(second['entity_status'], 'RESOLVE_FIRST')
        self.assertEqual(second['arguments']['person_name'], '王五')
        self.assertEqual(second['arguments']['status'], 'active')
        self.assertEqual(second['arguments']['reason'], '重复测试')
        self.assertNotIn('id', second['arguments'])
        self.assertNotIn('lease_id', second['arguments'])
        self.assertNotIn('version', second['arguments'])
        self.assertEqual(second['candidates'], ['lease.search', 'lease.checkout'])


class AiChatLeaseCheckoutContextRefreshEndToEndTests(unittest.TestCase):
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

    def business(self, command, data):
        response = self.client.post(
            '/api/business/' + command,
            json={'data': data, 'confirmed': True},
            headers={'X-CSRF-Token': self.csrf()},
        )
        self.assertEqual(response.status_code, 200, response.text[:1800])
        return response.json

    def seed_active_lease(self):
        building_id = self.business('building.save', {
            'community_id': 1, 'name': 'A栋', 'floors': 20,
        })['id']
        unit_id = self.business('unit.save', {
            'building_id': building_id, 'name': '1单元',
        })['id']
        house_id = self.business('house.save', {
            'unit_id': unit_id, 'room_no': 101, 'area': '88',
            'usage': 'residential', 'occupancy': 'vacant',
        })['id']
        person_id = self.business('person.save', {
            'community_id': 1, 'name': '王五', 'phone': '13800000123',
        })['id']
        start, end, move_in = lease_dates()
        lease_id = self.business('lease.create', {
            'house_id': house_id,
            'person_ids': [person_id],
            'start_date': start,
            'end_date': end,
            'move_in': move_in,
            'note': '测试活动租约',
        })['id']
        return house_id, person_id, lease_id

    def test_checkout_confirm_then_stale_context_rechecks_active_lease_without_second_pending(self):
        house_id, person_id, lease_id = self.seed_active_lease()
        provider = BailianClient('http://agent.invalid', 'fixture-key', 'qwen-plus')
        self.app.extensions['dify'] = provider
        calls = []

        def request(*args, **kwargs):
            calls.append((args, kwargs))
            return {
                'id': f'lease-refresh-{len(calls)}',
                'choices': [{'message': {'content': '我按当前租约状态继续核对。'}}],
            }

        with patch.object(provider, '_request', side_effect=request):
            first = self.client.post(
                '/ai/chat',
                json={'message': '王五已经搬走了，办理退租，原因合同到期'},
                headers={'X-CSRF-Token': self.csrf()},
            )

        self.assertEqual(first.status_code, 200, first.text[:2500])
        pending = [
            item for item in first.json.get('actions', [])
            if item.get('command') == 'lease.checkout' and item.get('status') == 'pending'
        ]
        self.assertEqual(len(pending), 1, first.text[:2500])
        conversation_id = first.json['conversation_id']

        with self.factory() as db:
            lease = db.get(Lease, lease_id)
            relation = db.scalar(select(HousePerson).where(
                HousePerson.lease_id == lease_id,
                HousePerson.person_id == person_id,
            ))
            self.assertEqual(lease.status, 'active')
            self.assertEqual(relation.status, 'active')
            self.assertEqual(db.get(House, house_id).occupancy, 'rented')

        confirmed = self.client.post(
            f"/ai/actions/{pending[0]['id']}/confirm",
            json={},
            headers={'X-CSRF-Token': self.csrf()},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text[:2000])
        self.assertEqual(confirmed.json['status'], 'executed')

        with self.factory() as db:
            lease = db.get(Lease, lease_id)
            relation = db.scalar(select(HousePerson).where(
                HousePerson.lease_id == lease_id,
                HousePerson.person_id == person_id,
            ))
            self.assertEqual(lease.status, 'ended')
            self.assertIsNotNone(lease.move_out)
            self.assertEqual(relation.status, 'ended')
            self.assertIsNotNone(relation.end_at)
            self.assertIsNone(relation.active_key)
            self.assertEqual(db.get(House, house_id).occupancy, 'vacant')
            checkout_actions = db.scalars(select(AiAction).where(
                AiAction.command == 'lease.checkout'
            )).all()
            self.assertEqual(len(checkout_actions), 1)
            self.assertEqual(checkout_actions[0].status, 'executed')

        calls_before_retry = len(calls)
        with patch.object(provider, '_request', side_effect=request):
            second = self.client.post(
                '/ai/chat',
                json={
                    'message': '刚才这个租户再退租一次，原因重复测试',
                    'conversation_id': conversation_id,
                },
                headers={'X-CSRF-Token': self.csrf()},
            )

        self.assertEqual(second.status_code, 200, second.text[:2500])
        self.assertEqual(second.json.get('source'), 'bailian', second.text[:2500])
        self.assertGreater(len(calls), calls_before_retry, 'stale retry must re-enter active-lease resolver flow')
        second_pending = [
            item for item in second.json.get('actions', [])
            if item.get('command') == 'lease.checkout' and item.get('status') == 'pending'
        ]
        self.assertEqual(second_pending, [], second.text[:2500])

        with self.factory() as db:
            lease = db.get(Lease, lease_id)
            relation = db.scalar(select(HousePerson).where(
                HousePerson.lease_id == lease_id,
                HousePerson.person_id == person_id,
            ))
            self.assertEqual(lease.status, 'ended')
            self.assertEqual(relation.status, 'ended')
            self.assertEqual(db.get(House, house_id).occupancy, 'vacant')
            checkout_actions = db.scalars(select(AiAction).where(
                AiAction.command == 'lease.checkout'
            )).all()
            self.assertEqual(len(checkout_actions), 1)
            self.assertEqual(checkout_actions[0].status, 'executed')


if __name__ == '__main__':
    unittest.main()
