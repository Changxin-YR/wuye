import hashlib
import json
import secrets
import unittest
import uuid
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from werkzeug.security import generate_password_hash

from agent_planner import plan_request
from app import create_app
from database_fixture import test_database
from dify_client import _PLANNER_HINT, _normalize_read_call, _synthetic_call
from models import AiGrant, User, utcnow


PW = 'Fixture-only-resident-directory-297!'
HASH = generate_password_hash(PW)


class BuildingResidentDirectoryTests(unittest.TestCase):
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

    def grant(self, user_id=1):
        token = secrets.token_urlsafe(32)
        with self.factory() as db:
            user = db.get(User, user_id)
            db.add(AiGrant(
                id=str(uuid.uuid4()), user_id=user_id, auth_version=user.auth_version,
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                expires_at=utcnow() + timedelta(minutes=3),
            ))
            db.commit()
        return token

    def lookup_response(self, token, args):
        return self.app.test_client().post('/api/agent/tools', json={
            'request_token': token,
            'operation': 'lookup',
            'command': 'person.search',
            'arguments_json': json.dumps(args, ensure_ascii=False),
        })

    def make_house(self, building_name, room_no, community_id=1):
        building = self.business('building.save', {
            'community_id': community_id,
            'name': building_name,
            'floors': 20,
        })['id']
        unit = self.business('unit.save', {
            'building_id': building,
            'name': '1单元',
        })['id']
        house = self.business('house.save', {
            'unit_id': unit,
            'room_no': room_no,
            'area': '90',
            'usage': 'residential',
            'occupancy': 'owner_occupied',
        })['id']
        return building, house

    def make_person(self, name, phone, house_id, community_id=1, *, is_resident=True, kind='owner'):
        person = self.business('person.save', {
            'community_id': community_id,
            'name': name,
            'phone': phone,
            'emergency_contact': '上游模型不应看到',
            'note': '',
        })['id']
        relation = self.business('relation.bind', {
            'house_id': house_id,
            'person_id': person,
            'kind': kind,
            'is_resident': is_resident,
        })
        return person, relation

    def test_planner_routes_broad_building_resident_request_to_person_permission(self):
        plan = plan_request('查一下23栋的住户信息', {'house.search', 'person.search'})
        self.assertEqual((plan['intent'], plan['action']), ('person.search', 'TOOL'), plan)
        self.assertEqual(plan['arguments'].get('building_name'), '23栋', plan)

        denied = plan_request('查一下23栋的住户信息', {'house.search'})
        self.assertEqual(denied['intent'], 'person.search', denied)
        self.assertEqual(denied['action'], 'DENY', denied)

        targeted_room = plan_request('查一下23栋311是谁住的', {'house.search', 'person.search'})
        self.assertEqual(targeted_room['intent'], 'house.search', targeted_room)

    def test_person_search_by_building_returns_only_current_residents_and_masks_phone(self):
        _, house_23 = self.make_house('23栋', 101)
        _, house_24 = self.make_house('24栋', 201)
        self.make_person('王五', '13800000123', house_23)
        self.make_person('赵六', '13900000456', house_24)
        self.make_person('联系人甲', '13700000777', house_23, is_resident=False, kind='contact')
        token = self.grant()

        response = self.lookup_response(token, {'building_name': '二十三号楼'})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        names = {row['name'] for row in response.json['items']}
        self.assertEqual(names, {'王五'})
        resident = response.json['items'][0]
        self.assertEqual(resident['phone'], '138****0123')
        self.assertNotIn('emergency_contact', resident)
        self.assertNotIn('上游模型不应看到', response.get_data(as_text=True))

    def test_building_scoped_actor_cannot_read_another_buildings_residents(self):
        building_23, house_23 = self.make_house('23栋', 101)
        _, house_24 = self.make_house('24栋', 201)
        self.make_person('王五', '13800000123', house_23)
        self.make_person('赵六', '13900000456', house_24)
        staff = self.business('staff.create', {
            'username': 'frontdesk23',
            'password': PW,
            'real_name': '23栋前台',
            'phone': '13600000233',
            'role_codes': ['frontdesk'],
            'scope_kind': 'building',
            'community_id': 1,
            'building_id': building_23,
        })['id']
        token = self.grant(staff)

        own = self.lookup_response(token, {'building_name': '23栋'})
        self.assertEqual(own.status_code, 200, own.get_data(as_text=True))
        self.assertEqual({row['name'] for row in own.json['items']}, {'王五'})

        foreign = self.lookup_response(token, {'building_name': '24栋'})
        self.assertEqual(foreign.status_code, 404, foreign.get_data(as_text=True))

    def test_same_building_name_across_communities_requires_community_disambiguation(self):
        community_2 = self.business('community.save', {
            'name': '第二小区',
            'address': '测试地址2',
            'phone': '02012345678',
        })['id']
        _, house_1 = self.make_house('23栋', 101, community_id=1)
        _, house_2 = self.make_house('23栋', 201, community_id=community_2)
        self.make_person('王五', '13800000123', house_1, community_id=1)
        self.make_person('李四', '13900000456', house_2, community_id=community_2)
        token = self.grant()

        ambiguous = self.lookup_response(token, {'building_name': '23栋'})
        self.assertEqual(ambiguous.status_code, 409, ambiguous.get_data(as_text=True))

        scoped = self.lookup_response(token, {'community_id': 1, 'building_name': '23栋'})
        self.assertEqual(scoped.status_code, 200, scoped.get_data(as_text=True))
        self.assertEqual({row['name'] for row in scoped.json['items']}, {'王五'})

    def test_provider_cannot_switch_building_target_on_resident_directory_read(self):
        token = _PLANNER_HINT.set({
            'action': 'TOOL',
            'intent': 'person.search',
            'candidates': ['person.search'],
            'arguments': {'building_name': '23栋'},
        })
        try:
            call = _synthetic_call({
                'operation': 'lookup',
                'command': 'person.search',
                'arguments_json': json.dumps({'building_name': '24栋', 'person_name': '赵六'}, ensure_ascii=False),
            })
            normalized = _normalize_read_call(call)
        finally:
            _PLANNER_HINT.reset(token)
        outer = json.loads(normalized['function']['arguments'])
        self.assertEqual(json.loads(outer['arguments_json']), {'building_name': '23栋'})


if __name__ == '__main__':
    unittest.main()
