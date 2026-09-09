import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import func, select
from werkzeug.security import generate_password_hash

from app import create_app
from database_fixture import test_database
from models import Person, User, Vehicle


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

    def test_person_archive_requires_active_vehicle_to_be_archived_first(self):
        building = self.business('building.save', {
            'community_id': 1, 'name': 'A栋', 'floors': 20,
        })['id']
        unit = self.business('unit.save', {
            'building_id': building, 'name': '1单元',
        })['id']
        house = self.business('house.save', {
            'unit_id': unit, 'room_no': 101, 'area': '88',
            'usage': 'residential', 'occupancy': 'vacant',
        })['id']
        person = self.business('person.save', {
            'community_id': 1, 'name': '王五', 'phone': '13800000123',
        })['id']
        relation = self.business('relation.bind', {
            'house_id': house, 'person_id': person,
            'kind': 'family', 'is_resident': True,
        })
        vehicle = self.business('vehicle.save', {
            'house_id': house, 'person_id': person,
            'plate': '粤A12345', 'model': '测试车型',
        })

        self.business('relation.end', {
            'id': relation['id'],
            'version': relation['record']['version'],
            'reason': '人员已搬离该房屋',
        })

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
            'reason': '人员搬离，车辆同步归档',
        })
        archived = self.business('person.archive', {
            'id': person,
            'version': 1,
            'reason': '车辆关系已处理，归档人员',
        })
        self.assertEqual(archived['id'], person)
        with self.factory() as db:
            self.assertTrue(db.get(Person, person).deleted)


if __name__ == '__main__':
    unittest.main()
