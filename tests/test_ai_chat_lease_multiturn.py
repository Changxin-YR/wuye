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
from models import AuditLog, House, HousePerson, Lease, User


PW = 'Fixture-only-ai-chat-lease-multiturn-347!'
HASH = generate_password_hash(PW)


def lease_dates():
    local_today = (datetime.now(timezone.utc) + timedelta(hours=8)).date()
    start = local_today - timedelta(days=1)
    end = local_today + timedelta(days=365)
    move_in = datetime.combine(local_today, time.min).isoformat(timespec='minutes')
    return start.isoformat(), end.isoformat(), move_in


class AiChatLeaseMultiTurnTests(unittest.TestCase):
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

    def chat(self, message, conversation_id=None):
        payload = {'message': message}
        if conversation_id:
            payload['conversation_id'] = conversation_id
        return self.client.post(
            '/ai/chat',
            json=payload,
            headers={'X-CSRF-Token': self.csrf()},
        )

    def make_house(self):
        building = self.business('building.save', {
            'community_id': 1, 'name': 'A栋', 'floors': 20,
        })['id']
        unit = self.business('unit.save', {
            'building_id': building, 'name': '1单元',
        })['id']
        return self.business('house.save', {
            'unit_id': unit,
            'room_no': 101,
            'area': '88',
            'usage': 'residential',
            'occupancy': 'vacant',
        })['id']

    def make_person(self):
        return self.business('person.save', {
            'community_id': 1,
            'name': '王五',
            'phone': '13800000123',
        })['id']

    def test_clarify_then_followup_reuses_business_context_and_executes_real_lease(self):
        house_id = self.make_house()
        tenant_id = self.make_person()

        provider = BailianClient('http://agent.invalid', 'fixture-key', 'qwen-plus')
        self.app.extensions['dify'] = provider

        def forbidden(*args, **kwargs):
            raise AssertionError('incomplete lease clarification must stay local')

        with patch.object(provider, '_request', side_effect=forbidden):
            first = self.chat('给王五登记租户入住，A栋101室')

        self.assertEqual(first.status_code, 200, first.text[:1800])
        self.assertEqual(first.json['source'], 'planner')
        self.assertIn('租期', first.json['answer'])
        self.assertNotIn('house_id', first.json['answer'])
        self.assertNotIn('person_id', first.json['answer'])
        self.assertEqual(first.json['actions'], [])
        conversation_id = first.json['conversation_id']
        self.assertTrue(conversation_id)

        start, end, move_in = lease_dates()
        responses = iter([
            {'id': 'lease-mt-1', 'choices': [{'message': {'content': '我先核对房屋。'}}]},
            {'id': 'lease-mt-2', 'choices': [{'message': {'content': '我再核对租户。'}}]},
            {'id': 'lease-mt-3', 'choices': [{'message': {'content': '业务信息已齐全，开始办理。'}}]},
            {'id': 'lease-mt-4', 'choices': [{'message': {'content': '租户入住已登记。'}}]},
        ])
        calls = []

        def request(*args, **kwargs):
            calls.append((args, kwargs))
            return next(responses)

        with patch.object(provider, '_request', side_effect=request):
            second = self.chat(
                f'租期{start}到{end}，实际入住时间{move_in}',
                conversation_id,
            )

        self.assertEqual(second.status_code, 200, second.text[:1800])
        self.assertNotEqual(second.json.get('source'), 'planner', second.text[:1800])
        self.assertEqual(second.json['conversation_id'], conversation_id)
        self.assertEqual(len(calls), 4)

        with self.factory() as db:
            lease = db.scalar(select(Lease).where(Lease.house_id == house_id))
            self.assertIsNotNone(lease)
            self.assertEqual(lease.status, 'active')
            self.assertEqual(lease.start_date.isoformat(), start)
            self.assertEqual(lease.end_date.isoformat(), end)
            self.assertEqual(db.get(House, house_id).occupancy, 'rented')

            relation = db.scalar(select(HousePerson).where(
                HousePerson.house_id == house_id,
                HousePerson.person_id == tenant_id,
                HousePerson.kind == 'tenant',
                HousePerson.status == 'active',
            ))
            self.assertIsNotNone(relation)
            self.assertEqual(relation.lease_id, lease.id)

            audit_row = db.scalar(select(AuditLog).where(
                AuditLog.source == 'agent',
                AuditLog.action == 'lease.create',
                AuditLog.status == 'success',
            ).order_by(AuditLog.id.desc()))
            self.assertIsNotNone(audit_row)


if __name__ == '__main__':
    unittest.main()
