import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import select
from werkzeug.security import generate_password_hash

from app import create_app
from database_fixture import test_database
from models import House, HousePerson, Lease, User, utcnow


PW = 'Fixture-only-lease-occupancy-619!'
HASH = generate_password_hash(PW)


class LeaseCheckoutOccupancyTests(unittest.TestCase):
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

    def make_house(self, building_name, room_no):
        building = self.business('building.save', {
            'community_id': 1, 'name': building_name, 'floors': 20,
        })['id']
        unit = self.business('unit.save', {
            'building_id': building, 'name': '1单元',
        })['id']
        return int(self.business('house.save', {
            'unit_id': unit,
            'room_no': room_no,
            'area': '88',
            'usage': 'residential',
            'occupancy': 'vacant',
        })['id'])

    def make_person(self, name, phone):
        return int(self.business('person.save', {
            'community_id': 1, 'name': name, 'phone': phone,
        })['id'])

    def make_lease(self, house_id, tenant_id):
        today = date.today()
        return int(self.business('lease.create', {
            'house_id': house_id,
            'person_ids': [tenant_id],
            'start_date': today.isoformat(),
            'end_date': (today + timedelta(days=365)).isoformat(),
            'move_in': utcnow().isoformat() + 'Z',
        })['id'])

    def checkout(self, lease_id):
        with self.factory() as db:
            version = db.get(Lease, lease_id).version
        self.business('lease.checkout', {
            'id': lease_id,
            'version': version,
            'reason': '租约结束并完成交接',
        })

    def test_checkout_restores_owner_occupied_when_other_residents_remain(self):
        house = self.make_house('A栋', 101)
        owner = self.make_person('业主甲', '13800000611')
        tenant = self.make_person('租户乙', '13800000612')
        self.business('relation.bind', {
            'house_id': house, 'person_id': owner, 'kind': 'owner',
        })
        lease = self.make_lease(house, tenant)
        self.assertEqual(self._house_occupancy(house), 'rented')

        self.checkout(lease)

        with self.factory() as db:
            self.assertEqual(db.get(House, house).occupancy, 'owner_occupied')
            owner_relation = db.scalar(select(HousePerson).where(
                HousePerson.house_id == house,
                HousePerson.person_id == owner,
                HousePerson.status == 'active',
            ))
            tenant_relation = db.scalar(select(HousePerson).where(
                HousePerson.lease_id == lease,
                HousePerson.person_id == tenant,
            ))
            self.assertIsNotNone(owner_relation)
            self.assertTrue(owner_relation.is_resident)
            self.assertIsNotNone(tenant_relation)
            self.assertEqual(tenant_relation.status, 'ended')
            self.assertFalse(tenant_relation.is_resident)

    def test_checkout_leaves_house_vacant_when_no_other_resident_remains(self):
        house = self.make_house('B栋', 202)
        tenant = self.make_person('租户丙', '13800000613')
        lease = self.make_lease(house, tenant)
        self.checkout(lease)

        with self.factory() as db:
            self.assertEqual(db.get(House, house).occupancy, 'vacant')
            remaining = list(db.scalars(select(HousePerson).where(
                HousePerson.house_id == house,
                HousePerson.status == 'active',
                HousePerson.is_resident.is_(True),
            )))
            self.assertEqual(remaining, [])

    def _house_occupancy(self, house_id):
        with self.factory() as db:
            return db.get(House, house_id).occupancy


if __name__ == '__main__':
    unittest.main()
