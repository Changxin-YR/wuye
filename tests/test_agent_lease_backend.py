import hashlib
import json
import secrets
import unittest
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import func, select
from werkzeug.security import generate_password_hash

from app import create_app
from database_fixture import test_database
from models import AiGrant, AuditLog, House, HousePerson, Lease, Person, User, utcnow


PW = 'Fixture-only-lease-311!'
HASH = generate_password_hash(PW)


def lease_dates():
    local_today = (datetime.now(timezone.utc) + timedelta(hours=8)).date()
    start = local_today - timedelta(days=1)
    end = local_today + timedelta(days=365)
    move_in = datetime.combine(local_today, time.min).isoformat(timespec='minutes')
    return start.isoformat(), end.isoformat(), move_in


class AgentLeaseBackendTests(unittest.TestCase):
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

    def grant(self):
        token = secrets.token_urlsafe(32)
        with self.factory() as db:
            user = db.scalar(select(User).where(User.username == 'admin'))
            db.add(AiGrant(
                id=str(uuid.uuid4()),
                user_id=user.id,
                auth_version=user.auth_version,
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                expires_at=utcnow() + timedelta(minutes=3),
            ))
            db.commit()
        return token

    def agent_tool(self, command, arguments, expected=200):
        response = self.app.test_client().post('/api/agent/tools', json={
            'request_token': self.grant(),
            'operation': 'execute',
            'command': command,
            'arguments_json': json.dumps(arguments, ensure_ascii=False),
        })
        self.assertEqual(response.status_code, expected, response.text[:1800])
        return response.json

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

    def test_lease_agent_write_persists_tenant_relation_occupancy_and_audit(self):
        house = self.make_house()
        tenant = self.make_person(1, '王五', '13800000123')
        second_tenant = self.make_person(1, '赵六', '13800000125')

        other_community = self.business('community.save', {
            'name': '第二小区',
            'address': '测试地址2号',
            'phone': '13800000999',
        })['id']
        outsider = self.make_person(other_community, '李四', '13800000124')
        start, end, move_in = lease_dates()

        # A model-selected existing person ID is still invalid when that person
        # belongs to another community. The failed attempt must have zero lease
        # side effects before the valid Agent operation is tried.
        bad = self.agent_tool('lease.create', {
            'house_id': house,
            'person_ids': [outsider],
            'start_date': start,
            'end_date': end,
            'move_in': move_in,
        }, expected=400)
        self.assertIn(bad.get('code'), {'VALIDATION_ERROR', 'BUSINESS_CONFLICT'})
        with self.factory() as db:
            self.assertEqual(db.scalar(select(func.count(Lease.id))), 0)
            self.assertEqual(
                db.scalar(select(func.count(HousePerson.id)).where(
                    HousePerson.house_id == house,
                    HousePerson.person_id == outsider,
                    HousePerson.kind == 'tenant',
                )),
                0,
            )
            self.assertEqual(db.get(House, house).occupancy, 'vacant')

        receipt = self.agent_tool('lease.create', {
            'house_id': house,
            'person_ids': [tenant],
            'start_date': start,
            'end_date': end,
            'move_in': move_in,
            'note': '现场核验入住',
        })
        self.assertEqual(receipt['status'], 'executed')

        with self.factory() as db:
            lease = db.scalar(select(Lease).where(Lease.house_id == house))
            self.assertIsNotNone(lease)
            self.assertEqual(lease.status, 'active')
            self.assertEqual(lease.active_key, house)
            self.assertEqual(lease.start_date.isoformat(), start)
            self.assertEqual(lease.end_date.isoformat(), end)
            self.assertEqual(db.get(House, house).occupancy, 'rented')

            relation = db.scalar(select(HousePerson).where(
                HousePerson.house_id == house,
                HousePerson.person_id == tenant,
                HousePerson.kind == 'tenant',
                HousePerson.status == 'active',
            ))
            self.assertIsNotNone(relation)
            self.assertEqual(relation.lease_id, lease.id)
            self.assertTrue(relation.is_resident)

            audit_row = db.scalar(select(AuditLog).where(
                AuditLog.source == 'agent',
                AuditLog.action == 'lease.create',
                AuditLog.status == 'success',
            ).order_by(AuditLog.id.desc()))
            self.assertIsNotNone(audit_row)

        # A house can have only one active lease. A second Agent call must fail
        # rather than creating another lease or tenant relation.
        duplicate = self.agent_tool('lease.create', {
            'house_id': house,
            'person_ids': [second_tenant],
            'start_date': start,
            'end_date': end,
            'move_in': move_in,
        }, expected=409)
        self.assertEqual(duplicate.get('code'), 'BUSINESS_CONFLICT')
        with self.factory() as db:
            self.assertEqual(
                db.scalar(select(func.count(Lease.id)).where(Lease.house_id == house)),
                1,
            )
            self.assertEqual(
                db.scalar(select(func.count(HousePerson.id)).where(
                    HousePerson.house_id == house,
                    HousePerson.kind == 'tenant',
                    HousePerson.status == 'active',
                )),
                1,
            )


if __name__ == '__main__':
    unittest.main()
