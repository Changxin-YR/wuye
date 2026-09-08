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


PW = 'Fixture-only-418!'
HASH = generate_password_hash(PW)


class AgentStaffScopeTests(unittest.TestCase):
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

    def raw_lookup(self, token, command, args):
        return self.app.test_client().post('/api/agent/tools', json={
            'request_token': token,
            'operation': 'lookup',
            'command': command,
            'arguments_json': json.dumps(args, ensure_ascii=False),
        })

    def create_building(self, name):
        return self.business('building.save', {
            'community_id': 1,
            'name': name,
            'floors': 20,
        })['id']

    def create_staff(self, username, real_name, phone, role_codes, building_id):
        return self.business('staff.create', {
            'username': username,
            'password': PW,
            'real_name': real_name,
            'phone': phone,
            'role_codes': role_codes,
            'scope_kind': 'building',
            'community_id': 1,
            'building_id': building_id,
        })['id']

    def test_staff_search_returns_only_active_eligible_worker_for_target_building(self):
        building_a = self.create_building('A栋')
        building_b = self.create_building('B栋')
        engineer_a = self.create_staff('engineer_a', '张师傅', '13800000101', ['engineer'], building_a)
        self.create_staff('engineer_b', '张师傅', '13800000102', ['engineer'], building_b)
        self.create_staff('cleaner_a', '张师傅', '13800000103', ['cleaner'], building_a)
        disabled_a = self.create_staff('disabled_a', '张师傅', '13800000104', ['engineer'], building_a)
        with self.factory() as db:
            db.get(User, disabled_a).active = False
            db.commit()

        token = self.grant(1)
        response = self.raw_lookup(token, 'staff.search', {
            'staff_name': '张师傅',
            'community_id': 1,
            'building_id': building_a,
        })
        self.assertEqual(response.status_code, 200, response.text[:1200])
        self.assertEqual([row['id'] for row in response.json['items']], [engineer_a])
        serialized = response.get_data(as_text=True)
        self.assertNotIn('password', serialized.lower())
        self.assertNotIn('auth_version', serialized)

    def test_building_dispatcher_cannot_search_repairers_outside_own_scope(self):
        building_a = self.create_building('A栋')
        building_b = self.create_building('B栋')
        self.create_staff('engineer_a', '李师傅', '13800000201', ['engineer'], building_a)
        self.create_staff('engineer_b', '李师傅', '13800000202', ['engineer'], building_b)
        dispatcher = self.create_staff(
            'dispatcher_a', 'A栋管家', '13800000203', ['building_manager'], building_a,
        )
        token = self.grant(dispatcher)

        in_scope = self.raw_lookup(token, 'staff.search', {
            'staff_name': '李师傅', 'community_id': 1, 'building_id': building_a,
        })
        self.assertEqual(in_scope.status_code, 200, in_scope.text[:1200])
        self.assertEqual(len(in_scope.json['items']), 1)
        self.assertEqual(in_scope.json['items'][0]['username'], 'engineer_a')

        out_of_scope = self.raw_lookup(token, 'staff.search', {
            'staff_name': '李师傅', 'community_id': 1, 'building_id': building_b,
        })
        self.assertIn(out_of_scope.status_code, {403, 404})
        self.assertNotIn('engineer_b', out_of_scope.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
