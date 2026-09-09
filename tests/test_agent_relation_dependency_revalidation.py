import hashlib
import json
import secrets
import unittest
import uuid
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import select
from werkzeug.security import generate_password_hash

from app import create_app
from database_fixture import test_database
from models import AiGrant, HousePerson, User, utcnow


PW = 'Fixture-only-agent-relation-409!'
HASH = generate_password_hash(PW)


class AgentRelationDependencyRevalidationTests(unittest.TestCase):
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

    def propose(self, command, arguments, expected=200):
        response = self.app.test_client().post('/api/agent/tools', json={
            'request_token': self.grant(),
            'operation': 'propose',
            'command': command,
            'arguments_json': json.dumps(arguments, ensure_ascii=False),
        })
        self.assertEqual(response.status_code, expected, response.text[:1800])
        return response

    def confirm(self, action_id, expected=200):
        response = self.client.post(
            f'/ai/actions/{action_id}/confirm',
            json={},
            headers={'X-CSRF-Token': self.csrf()},
        )
        self.assertEqual(response.status_code, expected, response.text[:1800])
        return response

    def make_resident(self, room_no, name, phone):
        building = self.business('building.save', {
            'community_id': 1, 'name': f'R{room_no}栋', 'floors': 20,
        })['id']
        unit = self.business('unit.save', {
            'building_id': building, 'name': '1单元',
        })['id']
        house = self.business('house.save', {
            'unit_id': unit, 'room_no': room_no, 'area': '88',
            'usage': 'residential', 'occupancy': 'vacant',
        })['id']
        person = self.business('person.save', {
            'community_id': 1, 'name': name, 'phone': phone,
        })['id']
        relation = self.business('relation.bind', {
            'house_id': house, 'person_id': person,
            'kind': 'family', 'is_resident': True,
        })
        return house, person, relation

    def test_existing_vehicle_dependency_blocks_agent_proposal(self):
        house, person, relation = self.make_resident(201, '王五', '13800000201')
        self.business('vehicle.save', {
            'house_id': house, 'person_id': person,
            'plate': '粤B12001', 'model': '测试车型',
        })

        response = self.propose('relation.end', {
            'id': relation['id'],
            'version': relation['record']['version'],
            'reason': '人员搬离',
        }, expected=409)
        self.assertIn('车辆', response.get_json().get('error', ''))
        with self.factory() as db:
            row = db.get(HousePerson, relation['id'])
            self.assertEqual(row.status, 'active')
            self.assertIsNotNone(row.active_key)

    def test_dependency_added_after_proposal_is_rechecked_on_confirm(self):
        house, person, relation = self.make_resident(202, '李四', '13800000202')
        proposal = self.propose('relation.end', {
            'id': relation['id'],
            'version': relation['record']['version'],
            'reason': '人员搬离',
        })
        self.assertEqual(proposal.json.get('status'), 'pending')
        action_id = proposal.json['id']

        visitor = self.business('visitor.create', {
            'house_id': house,
            'host_person_id': person,
            'name': '访客乙',
            'phone': '13900000202',
            'purpose': '拜访住户',
            'expected_at': '2030-01-01T10:00',
        })
        blocked = self.confirm(action_id, expected=409)
        self.assertIn('访客', blocked.get_json().get('error', ''))

        with self.factory() as db:
            row = db.get(HousePerson, relation['id'])
            self.assertEqual(row.status, 'active')
            self.assertIsNotNone(row.active_key)

        self.business('visitor.cancel', {
            'id': visitor['id'],
            'version': visitor['record']['version'],
        })
        # The failed confirmation must leave the action retryable; after the
        # dependency is resolved, the same proposal is revalidated and may run.
        confirmed = self.confirm(action_id)
        self.assertEqual(confirmed.json.get('status'), 'executed')
        with self.factory() as db:
            row = db.get(HousePerson, relation['id'])
            self.assertEqual(row.status, 'ended')
            self.assertIsNone(row.active_key)


if __name__ == '__main__':
    unittest.main()
