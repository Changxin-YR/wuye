import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import func, select
from werkzeug.security import generate_password_hash

from app import create_app
from database_fixture import test_database
from models import HousePerson, Person, User, Vehicle, utcnow


PW = 'Fixture-only-person-archive-401!'
HASH = generate_password_hash(PW)


class PersonArchiveVehicleGuardTests(unittest.TestCase):
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

    def make_bundle(self, room_no=101, plate='粤A12345'):
        building = self.business('building.save', {
            'community_id': 1, 'name': f'A{room_no}栋', 'floors': 20,
        })['id']
        unit = self.business('unit.save', {
            'building_id': building, 'name': '1单元',
        })['id']
        house = self.business('house.save', {
            'unit_id': unit, 'room_no': room_no, 'area': '88',
            'usage': 'residential', 'occupancy': 'vacant',
        })['id']
        person = self.business('person.save', {
            'community_id': 1, 'name': f'王{room_no}', 'phone': f'1380000{room_no:04d}',
        })['id']
        relation = self.business('relation.bind', {
            'house_id': house, 'person_id': person,
            'kind': 'family', 'is_resident': True,
        })
        vehicle = self.business('vehicle.save', {
            'house_id': house, 'person_id': person,
            'plate': plate, 'model': '测试车型',
        })
        return house, person, relation, vehicle

    def test_relation_end_requires_active_vehicle_to_be_archived_first(self):
        _, person, relation, vehicle = self.make_bundle()

        blocked = self.business('relation.end', {
            'id': relation['id'],
            'version': relation['record']['version'],
            'reason': '人员已搬离该房屋',
        }, expected=409)
        self.assertIn('车辆', blocked.get('error', '') + blocked.get('message', ''))

        self.business('vehicle.archive', {
            'id': vehicle['id'],
            'version': vehicle['record']['version'],
            'reason': '人员搬离，车辆同步归档',
        })
        ended = self.business('relation.end', {
            'id': relation['id'],
            'version': relation['record']['version'],
            'reason': '车辆已处理，解除房屋关系',
        })
        self.assertEqual(ended['id'], relation['id'])

        archived = self.business('person.archive', {
            'id': person,
            'version': 1,
            'reason': '关系与车辆均已处理，归档人员',
        })
        self.assertEqual(archived['id'], person)
        with self.factory() as db:
            self.assertTrue(db.get(Person, person).deleted)

    def test_person_archive_blocks_legacy_unlinked_active_vehicle(self):
        _, person, relation, vehicle = self.make_bundle(102, '粤A54321')

        # Simulate legacy/inconsistent data created before dependency guards:
        # the housing relation is already ended while the vehicle is still active.
        with self.factory() as db:
            row = db.get(HousePerson, relation['id'])
            row.status = 'ended'
            row.end_at = utcnow()
            row.active_key = None
            db.commit()

        blocked = self.business('person.archive', {
            'id': person,
            'version': 1,
            'reason': '人员档案清理',
        }, expected=409)
        self.assertIn('车辆', blocked.get('error', '') + blocked.get('message', ''))

        with self.factory() as db:
            self.assertFalse(db.get(Person, person).deleted)
            active_vehicle = db.get(Vehicle, vehicle['id'])
            self.assertIsNotNone(active_vehicle)
            self.assertFalse(active_vehicle.deleted)
            self.assertEqual(active_vehicle.person_id, person)
            self.assertEqual(
                db.scalar(select(func.count(Vehicle.id)).where(
                    Vehicle.person_id == person,
                    Vehicle.deleted.is_(False),
                )),
                1,
            )

        self.business('vehicle.archive', {
            'id': vehicle['id'],
            'version': vehicle['record']['version'],
            'reason': '清理历史车辆引用',
        })
        archived = self.business('person.archive', {
            'id': person,
            'version': 1,
            'reason': '车辆关系已处理，归档人员',
        })
        self.assertEqual(archived['id'], person)


if __name__ == '__main__':
    unittest.main()
