import hashlib
import json
import secrets
import unittest
import uuid
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from werkzeug.security import generate_password_hash

from app import create_app
from database_fixture import test_database
from models import AiGrant, User, utcnow


PW = 'Fixture-only-292!'
HASH = generate_password_hash(PW)


class BusinessQueryAliasTests(unittest.TestCase):
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
            db.add(User(username='admin', password_hash=HASH, role=0, real_name='管理员', phone='13800000001'))
            db.commit()
        self.login('admin')

    def tearDown(self):
        self.app.extensions['db_engine'].dispose()
        self.temp.cleanup()

    def csrf(self, client=None):
        client = client or self.client
        client.get('/auth/login')
        with client.session_transaction() as session:
            return session['csrf_token']

    def login(self, username, client=None):
        client = client or self.client
        response = client.post('/auth/login', data={
            'csrf_token': self.csrf(client),
            'username': username,
            'password': PW,
        })
        self.assertEqual(response.status_code, 302, response.text)

    def business(self, command, data):
        response = self.client.post(
            '/api/business/' + command,
            json={'data': data, 'confirmed': True},
            headers={'X-CSRF-Token': self.csrf()},
        )
        self.assertEqual(response.status_code, 200, response.text[:1200])
        return response.json

    def grant(self, user_id):
        token = secrets.token_urlsafe(32)
        with self.factory() as db:
            user = db.get(User, user_id)
            db.add(AiGrant(
                id=str(uuid.uuid4()),
                user_id=user_id,
                auth_version=user.auth_version,
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                expires_at=utcnow() + timedelta(minutes=3),
            ))
            db.commit()
        return token

    def lookup(self, token, command, args):
        response = self.app.test_client().post('/api/agent/tools', json={
            'request_token': token,
            'operation': 'lookup',
            'command': command,
            'arguments_json': json.dumps(args, ensure_ascii=False),
        })
        self.assertEqual(response.status_code, 200, response.text[:1200])
        return response.json

    def make_house(self, building_name, room_no):
        building = self.business('building.save', {
            'community_id': 1,
            'name': building_name,
            'floors': 20,
        })['id']
        unit = self.business('unit.save', {
            'building_id': building,
            'name': '3单元',
        })['id']
        house = self.business('house.save', {
            'unit_id': unit,
            'room_no': room_no,
            'area': '90',
            'usage': 'residential',
            'occupancy': 'vacant',
        })['id']
        return building, unit, house

    def test_house_building_and_unit_aliases_resolve_same_rows(self):
        building, unit, house = self.make_house('A栋', 101)
        token = self.grant(1)

        houses = self.lookup(token, 'house.search', {
            'building_name': 'A号楼',
            'unit': '3',
            'room_no': 101,
        })
        self.assertEqual([row['id'] for row in houses['items']], [house])

        buildings = self.lookup(token, 'building.search', {'building_name': 'A号楼'})
        self.assertEqual([row['id'] for row in buildings['items']], [building])

        units = self.lookup(token, 'unit.search', {'building_id': building, 'unit': '3'})
        self.assertEqual([row['id'] for row in units['items']], [unit])

    def test_alias_matching_never_expands_building_data_scope(self):
        building_a, _, house_a = self.make_house('A栋', 101)
        self.make_house('B栋', 101)
        staff_id = self.business('staff.create', {
            'username': 'building_staff',
            'password': PW,
            'real_name': 'A栋管家',
            'phone': '13800000991',
            'role_codes': ['building_manager'],
            'scope_kind': 'building',
            'community_id': 1,
            'building_id': building_a,
        })['id']
        token = self.grant(staff_id)

        in_scope = self.lookup(token, 'house.search', {
            'building_name': 'A号楼',
            'unit': '3',
            'room_no': 101,
        })
        self.assertEqual([row['id'] for row in in_scope['items']], [house_a])

        out_of_scope = self.lookup(token, 'house.search', {
            'building_name': 'B号楼',
            'unit': '3',
            'room_no': 101,
        })
        self.assertEqual(out_of_scope['items'], [])


if __name__ == '__main__':
    unittest.main()
