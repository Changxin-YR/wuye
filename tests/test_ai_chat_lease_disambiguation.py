import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import func, select
from werkzeug.security import generate_password_hash

from app import create_app
from database_fixture import test_database
from dify_client import BailianClient
from models import House, HousePerson, Lease, User


PW = 'Fixture-only-ai-chat-lease-disambiguation-359!'
HASH = generate_password_hash(PW)


def lease_dates():
    local_today = (datetime.now(timezone.utc) + timedelta(hours=8)).date()
    start = local_today - timedelta(days=1)
    end = local_today + timedelta(days=365)
    move_in = datetime.combine(local_today, time.min).isoformat(timespec='minutes')
    return start.isoformat(), end.isoformat(), move_in


class AiChatLeaseDisambiguationTests(unittest.TestCase):
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

    def make_person(self, phone):
        return self.business('person.save', {
            'community_id': 1,
            'name': '王五',
            'phone': phone,
        })['id']

    def test_same_name_resolver_stops_write_then_phone_followup_resumes_original_lease(self):
        house_id = self.make_house()
        selected_id = self.make_person('13800000123')
        other_id = self.make_person('13800000124')
        start, end, move_in = lease_dates()
        full_request = (
            f'给王五登记租户入住，A栋101室，租期{start}到{end}，'
            f'实际入住时间{move_in}'
        )

        provider = BailianClient('http://agent.invalid', 'fixture-key', 'qwen-plus')
        self.app.extensions['dify'] = provider

        # The first turn may safely use scoped read resolvers. It must stop after
        # person.search returns multiple candidates and must never call lease.create.
        first_responses = iter([
            {'id': 'lease-dis-1', 'choices': [{'message': {'content': '我先核对目标房屋。'}}]},
            {'id': 'lease-dis-2', 'choices': [{'message': {'content': '我再核对同小区租户。'}}]},
            {'id': 'lease-dis-3', 'choices': [{'message': {'content': '找到多个同名租户，请补充联系电话。'}}]},
        ])
        with patch.object(provider, '_request', side_effect=lambda *args, **kwargs: next(first_responses)):
            first = self.chat(full_request)

        self.assertEqual(first.status_code, 200, first.text[:1800])
        self.assertNotEqual(first.json.get('source'), 'planner', first.text[:1800])
        self.assertTrue(first.json['conversation_id'])
        self.assertEqual(first.json['actions'], [])
        self.assertTrue('多个' in first.json['answer'] or '同名' in first.json['answer'])
        self.assertIn('联系电话', first.json['answer'])

        with self.factory() as db:
            self.assertEqual(db.scalar(select(func.count(Lease.id)).where(Lease.house_id == house_id)), 0)
            self.assertEqual(db.get(House, house_id).occupancy, 'vacant')

        second_responses = iter([
            {'id': 'lease-da-1', 'choices': [{'message': {'content': '我先核对房屋。'}}]},
            {'id': 'lease-da-2', 'choices': [{'message': {'content': '我按联系电话核对租户。'}}]},
            {'id': 'lease-da-3', 'choices': [{'message': {'content': '对象已唯一，开始办理。'}}]},
            {'id': 'lease-da-4', 'choices': [{'message': {'content': '租户入住已登记。'}}]},
        ])
        with patch.object(provider, '_request', side_effect=lambda *args, **kwargs: next(second_responses)):
            second = self.chat('联系电话13800000123', first.json['conversation_id'])

        self.assertEqual(second.status_code, 200, second.text[:1800])
        self.assertNotEqual(second.json.get('source'), 'planner', second.text[:1800])
        self.assertEqual(second.json['conversation_id'], first.json['conversation_id'])

        with self.factory() as db:
            lease = db.scalar(select(Lease).where(Lease.house_id == house_id))
            self.assertIsNotNone(lease)
            self.assertEqual(lease.status, 'active')
            relation = db.scalar(select(HousePerson).where(
                HousePerson.house_id == house_id,
                HousePerson.person_id == selected_id,
                HousePerson.kind == 'tenant',
                HousePerson.status == 'active',
            ))
            self.assertIsNotNone(relation)
            self.assertEqual(relation.lease_id, lease.id)
            self.assertEqual(db.scalar(select(func.count(HousePerson.id)).where(
                HousePerson.house_id == house_id,
                HousePerson.person_id == other_id,
                HousePerson.kind == 'tenant',
            )), 0)
            self.assertEqual(db.get(House, house_id).occupancy, 'rented')


if __name__ == '__main__':
    unittest.main()
