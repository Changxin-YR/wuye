import json
import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import select
from werkzeug.security import generate_password_hash

from app import create_app
from database_fixture import test_database
from dify_client import BailianClient
from models import AiAction, AuditLog, House, HousePerson, Lease, User


PW = 'Fixture-only-ai-chat-lease-checkout-521!'
HASH = generate_password_hash(PW)


def lease_dates():
    local_today = (datetime.now(timezone.utc) + timedelta(hours=8)).date()
    start = local_today - timedelta(days=30)
    end = local_today + timedelta(days=335)
    move_in = datetime.combine(local_today - timedelta(days=29), time.min).isoformat(timespec='minutes')
    return start.isoformat(), end.isoformat(), move_in


class AiChatLeaseCheckoutConfirmationEndToEndTests(unittest.TestCase):
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

    def test_chat_only_proposes_checkout_and_browser_confirmation_ends_exact_active_lease(self):
        house_id, person_id, lease_id = self.seed_active_lease()
        with self.factory() as db:
            lease = db.get(Lease, lease_id)
            relation = db.scalar(select(HousePerson).where(
                HousePerson.lease_id == lease_id,
                HousePerson.person_id == person_id,
                HousePerson.status == 'active',
            ))
            self.assertEqual(lease.status, 'active')
            self.assertIsNotNone(relation)
            self.assertEqual(db.get(House, house_id).occupancy, 'rented')

        provider = BailianClient('http://agent.invalid', 'fixture-key', 'qwen-plus')
        self.app.extensions['dify'] = provider
        responses = iter([
            {'id': 'lease-checkout-e2e-1', 'choices': [{'message': {'content': '我先核对王五当前的活动租约。'}}]},
            {'id': 'lease-checkout-e2e-2', 'choices': [{'message': {'content': '已生成退租确认单。'}}]},
            {'id': 'lease-checkout-e2e-3', 'choices': [{'message': {'content': '请由当前登录人员确认后办理退租。'}}]},
        ])
        calls = []

        def request(*args, **kwargs):
            calls.append((args, kwargs))
            return next(responses)

        with patch.object(provider, '_request', side_effect=request):
            response = self.client.post(
                '/ai/chat',
                json={'message': '王五已经搬走了，办理退租，原因合同到期'},
                headers={'X-CSRF-Token': self.csrf()},
            )

        self.assertEqual(response.status_code, 200, response.text[:2000])
        actions = [
            item for item in response.json.get('actions', [])
            if item.get('command') == 'lease.checkout'
        ]
        self.assertEqual(len(actions), 1, response.text[:2000])
        pending = actions[0]
        self.assertEqual(pending['status'], 'pending')
        self.assertEqual(pending['execution_mode'], 'CONFIRM')

        with self.factory() as db:
            lease = db.get(Lease, lease_id)
            relation = db.scalar(select(HousePerson).where(
                HousePerson.lease_id == lease_id,
                HousePerson.person_id == person_id,
            ))
            self.assertEqual(lease.status, 'active')
            self.assertEqual(relation.status, 'active')
            self.assertEqual(db.get(House, house_id).occupancy, 'rented')

            action = db.get(AiAction, pending['id'])
            payload = json.loads(action.payload)
            self.assertEqual(payload, {
                'id': lease_id,
                'reason': '合同到期',
                'version': lease.version,
            })

        confirmed = self.client.post(
            f"/ai/actions/{pending['id']}/confirm",
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
            self.assertIsNotNone(db.scalar(select(AuditLog).where(
                AuditLog.source == 'agent',
                AuditLog.action == 'lease.checkout',
                AuditLog.status == 'success',
            )))
            self.assertIsNotNone(db.scalar(select(AuditLog).where(
                AuditLog.source == 'agent',
                AuditLog.action == 'ai_execute',
                AuditLog.status == 'success',
            )))


if __name__ == '__main__':
    unittest.main()
