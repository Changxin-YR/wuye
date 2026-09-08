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


PW = 'Fixture-only-519!'
HASH = generate_password_hash(PW)


class AgentComplaintStaffScopeTests(unittest.TestCase):
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

    def lookup(self, token, args):
        return self.app.test_client().post('/api/agent/tools', json={
            'request_token': token,
            'operation': 'lookup',
            'command': 'complaint_staff.search',
            'arguments_json': json.dumps(args, ensure_ascii=False),
        })

    def create_building(self, name):
        return self.business('building.save', {
            'community_id': 1, 'name': name, 'floors': 20,
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

    def test_handler_lookup_filters_role_state_and_target_building(self):
        building_a = self.create_building('A栋')
        building_b = self.create_building('B栋')
        handler_a = self.create_staff('service_a', '李客服', '13800000301', ['customer_service'], building_a)
        self.create_staff('service_b', '李客服', '13800000302', ['customer_service'], building_b)
        self.create_staff('engineer_a', '李客服', '13800000303', ['engineer'], building_a)
        disabled = self.create_staff('disabled_service', '李客服', '13800000304', ['customer_service'], building_a)
        with self.factory() as db:
            db.get(User, disabled).active = False
            db.commit()

        response = self.lookup(self.grant(1), {
            'staff_name': '李客服', 'community_id': 1, 'building_id': building_a,
        })
        self.assertEqual(response.status_code, 200, response.text[:1200])
        self.assertEqual([row['id'] for row in response.json['items']], [handler_a])
        text = response.get_data(as_text=True)
        self.assertNotIn('password', text.lower())
        self.assertNotIn('auth_version', text)

    def test_building_handler_cannot_resolve_people_outside_own_scope(self):
        building_a = self.create_building('A栋')
        building_b = self.create_building('B栋')
        self.create_staff('service_a', '王客服', '13800000401', ['customer_service'], building_a)
        self.create_staff('service_b', '王客服', '13800000402', ['customer_service'], building_b)
        actor = self.create_staff('manager_a', 'A栋管家', '13800000403', ['building_manager'], building_a)
        token = self.grant(actor)

        in_scope = self.lookup(token, {
            'staff_name': '王客服', 'community_id': 1, 'building_id': building_a,
        })
        self.assertEqual(in_scope.status_code, 200, in_scope.text[:1200])
        self.assertEqual(len(in_scope.json['items']), 1)
        self.assertEqual(in_scope.json['items'][0]['username'], 'service_a')

        out_scope = self.lookup(token, {
            'staff_name': '王客服', 'community_id': 1, 'building_id': building_b,
        })
        self.assertIn(out_scope.status_code, {403, 404})
        self.assertNotIn('service_b', out_scope.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
