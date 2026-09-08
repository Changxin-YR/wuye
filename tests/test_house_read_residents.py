import hashlib
import json
import secrets
import unittest
import uuid
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from app import create_app
from database_fixture import test_database
from dify_client import BailianClient
from models import AiGrant, User, utcnow


PW = 'Fixture-only-295!'
HASH = generate_password_hash(PW)


class HouseResidentReadTests(unittest.TestCase):
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

    def make_house(self, building_name='23栋', unit_name='3单元', room_no=311):
        building = self.business('building.save', {
            'community_id': 1,
            'name': building_name,
            'floors': 30,
        })['id']
        unit = self.business('unit.save', {
            'building_id': building,
            'name': unit_name,
        })['id']
        house = self.business('house.save', {
            'unit_id': unit,
            'room_no': room_no,
            'area': '90',
            'usage': 'residential',
            'occupancy': 'owner_occupied',
        })['id']
        return building, unit, house

    def make_residents(self, house):
        owner = self.business('person.save', {
            'community_id': 1,
            'name': '王五',
            'phone': '13800000123',
            'emergency_contact': '不应暴露',
            'note': '',
        })['id']
        family = self.business('person.save', {
            'community_id': 1,
            'name': '赵六',
            'phone': '13900000456',
            'emergency_contact': '也不应暴露',
            'note': '',
        })['id']
        self.business('relation.bind', {
            'house_id': house,
            'person_id': owner,
            'kind': 'owner',
            'is_resident': True,
        })
        self.business('relation.bind', {
            'house_id': house,
            'person_id': family,
            'kind': 'family',
            'is_resident': True,
        })
        return owner, family

    def test_targeted_house_read_returns_current_residents_with_masked_phone(self):
        _, _, house = self.make_house()
        self.make_residents(house)
        token = self.grant(1)

        result = self.lookup(token, 'house.search', {
            'building_name': '二十三号楼',
            'unit': '3',
            'room_no': 311,
        })
        self.assertEqual(len(result['items']), 1)
        row = result['items'][0]
        self.assertEqual(row['id'], house)
        residents = {item['name']: item for item in row['residents']}
        self.assertEqual(set(residents), {'王五', '赵六'})
        self.assertEqual(residents['王五']['kind'], 'owner')
        self.assertEqual(residents['赵六']['kind'], 'family')
        self.assertEqual(residents['王五']['phone'], '138****0123')
        self.assertEqual(residents['赵六']['phone'], '139****0456')
        self.assertNotIn('emergency_contact', residents['王五'])

    def test_broad_house_list_does_not_dump_resident_directory(self):
        self.make_house()
        token = self.grant(1)
        result = self.lookup(token, 'house.search', {'building_name': '23栋'})
        self.assertGreaterEqual(len(result['items']), 1)
        self.assertTrue(all('residents' not in row for row in result['items']))

    def test_nonresident_relation_is_not_reported_as_current_resident(self):
        _, _, house = self.make_house()
        contact = self.business('person.save', {
            'community_id': 1,
            'name': '联系人甲',
            'phone': '13700000777',
            'emergency_contact': '',
            'note': '',
        })['id']
        relation = self.business('relation.bind', {
            'house_id': house,
            'person_id': contact,
            'kind': 'contact',
            'is_resident': False,
        })
        token = self.grant(1)
        result = self.lookup(token, 'house.search', {
            'building_name': '23栋',
            'unit': '3单元',
            'room_no': 311,
        })
        self.assertEqual(result['items'][0]['residents'], [])
        self.assertTrue(relation['id'])

    def test_ai_chat_fallback_reads_target_house_and_only_sends_masked_residents_upstream(self):
        _, _, house = self.make_house()
        self.make_residents(house)
        client = BailianClient('http://agent.invalid', 'fixture-key', 'qwen-plus')
        payloads = []
        responses = iter([
            {'id': 'one', 'choices': [{'message': {'content': '我来查一下'}}]},
            {'id': 'two', 'choices': [{'message': {'content': '23栋311当前登记的居住人员是王五（业主）和赵六（家属）。'}}]},
        ])

        def fake_request(method, path, payload=None):
            payloads.append(payload)
            return next(responses)

        self.app.extensions['dify'] = client
        with patch.object(client, '_request', side_effect=fake_request):
            response = self.client.post(
                '/ai/chat',
                json={'message': '查一下23栋3单元311是谁住的'},
                headers={'X-CSRF-Token': self.csrf()},
            )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.json['source'], 'bailian')
        self.assertEqual(response.json['actions'], [])
        self.assertIn('王五', response.json['answer'])
        self.assertIn('赵六', response.json['answer'])
        self.assertEqual(len(payloads), 2)
        tool_messages = [row for row in payloads[1]['messages'] if row.get('role') == 'tool']
        self.assertEqual(len(tool_messages), 1)
        upstream_tool_text = tool_messages[0]['content']
        self.assertIn('王五', upstream_tool_text)
        self.assertIn('赵六', upstream_tool_text)
        self.assertIn('138****0123', upstream_tool_text)
        self.assertIn('139****0456', upstream_tool_text)
        self.assertNotIn('13800000123', upstream_tool_text)
        self.assertNotIn('13900000456', upstream_tool_text)
        self.assertNotIn('emergency_contact', upstream_tool_text)
        self.assertNotIn('不应暴露', upstream_tool_text)
        self.assertNotIn('也不应暴露', upstream_tool_text)


if __name__ == '__main__':
    unittest.main()
