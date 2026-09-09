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
from models import AuditLog, House, HousePerson, Lease, Person, User


PW = 'Fixture-only-ai-chat-lease-337!'
HASH = generate_password_hash(PW)


def lease_dates():
    local_today = (datetime.now(timezone.utc) + timedelta(hours=8)).date()
    start = local_today - timedelta(days=1)
    end = local_today + timedelta(days=365)
    move_in = datetime.combine(local_today, time.min).isoformat(timespec='minutes')
    return start.isoformat(), end.isoformat(), move_in


class AiChatLeaseEndToEndTests(unittest.TestCase):
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

    def chat(self, message):
        return self.client.post(
            '/ai/chat',
            json={'message': message},
            headers={'X-CSRF-Token': self.csrf()},
        )

    def make_house(self, community_id=1, building_name='A栋', room_no=101):
        building = self.business('building.save', {
            'community_id': community_id,
            'name': building_name,
            'floors': 20,
        })['id']
        unit = self.business('unit.save', {
            'building_id': building,
            'name': '1单元',
        })['id']
        return self.business('house.save', {
            'unit_id': unit,
            'room_no': room_no,
            'area': '88',
            'usage': 'residential',
            'occupancy': 'vacant',
        })['id']

    def make_person(self, community_id, name, phone):
        return self.business('person.save', {
            'community_id': community_id,
            'name': name,
            'phone': phone,
        })['id']

    def test_chat_resolves_new_tenant_inside_target_house_community_and_persists_full_lease(self):
        house_id = self.make_house(1, 'A栋', 101)
        target_tenant = self.make_person(1, '王五', '13800000123')

        other_community = self.business('community.save', {
            'name': '第二小区',
            'address': '测试地址2号',
            'phone': '13800000999',
        })['id']
        other_same_name = self.make_person(other_community, '王五', '13800000124')

        with self.factory() as db:
            self.assertEqual(db.scalar(select(func.count(HousePerson.id)).where(
                HousePerson.person_id.in_([target_tenant, other_same_name]),
            )), 0)
            self.assertEqual(db.get(House, house_id).occupancy, 'vacant')

        start, end, move_in = lease_dates()
        message = (
            f'给王五登记租户入住，A栋101室，租期{start}到{end}，'
            f'实际入住时间{move_in}'
        )

        # Only the external model transport is mocked. Planner, resolver,
        # /ai/chat, Agent Gateway, PropertyService and database writes are real.
        provider = BailianClient('http://agent.invalid', 'fixture-key', 'qwen-plus')
        responses = iter([
            {'id': 'lease-e2e-1', 'choices': [{'message': {'content': '我先核对房屋。'}}]},
            {'id': 'lease-e2e-2', 'choices': [{'message': {'content': '我再核对租户。'}}]},
            {'id': 'lease-e2e-3', 'choices': [{'message': {'content': '开始办理入住。'}}]},
            {'id': 'lease-e2e-4', 'choices': [{'message': {'content': '租户入住已登记。'}}]},
        ])
        self.app.extensions['dify'] = provider
        with patch.object(provider, '_request', side_effect=lambda *args, **kwargs: next(responses)):
            response = self.chat(message)

        self.assertEqual(response.status_code, 200, response.text[:1800])
        self.assertNotEqual(response.json.get('source'), 'planner', response.text[:1800])

        with self.factory() as db:
            leases = list(db.scalars(select(Lease).where(Lease.house_id == house_id)))
            self.assertEqual(len(leases), 1)
            lease = leases[0]
            self.assertEqual(lease.status, 'active')
            self.assertEqual(lease.start_date.isoformat(), start)
            self.assertEqual(lease.end_date.isoformat(), end)
            self.assertEqual(db.get(House, house_id).occupancy, 'rented')

            relation = db.scalar(select(HousePerson).where(
                HousePerson.house_id == house_id,
                HousePerson.person_id == target_tenant,
                HousePerson.kind == 'tenant',
                HousePerson.status == 'active',
            ))
            self.assertIsNotNone(relation)
            self.assertEqual(relation.lease_id, lease.id)
            self.assertTrue(relation.is_resident)

            self.assertEqual(db.scalar(select(func.count(HousePerson.id)).where(
                HousePerson.house_id == house_id,
                HousePerson.person_id == other_same_name,
                HousePerson.kind == 'tenant',
            )), 0)

            audit_row = db.scalar(select(AuditLog).where(
                AuditLog.source == 'agent',
                AuditLog.action == 'lease.create',
                AuditLog.status == 'success',
            ).order_by(AuditLog.id.desc()))
            self.assertIsNotNone(audit_row)


if __name__ == '__main__':
    unittest.main()
