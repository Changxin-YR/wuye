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


PW = 'Fixture-only-296!'
HASH = generate_password_hash(PW)


class BillingUnpaidPersonScopeTests(unittest.TestCase):
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

    def csrf(self):
        self.client.get('/auth/login')
        with self.client.session_transaction() as session:
            return session['csrf_token']

    def login(self, username):
        response = self.client.post('/auth/login', data={
            'csrf_token': self.csrf(),
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
                id=str(uuid.uuid4()),
                user_id=user_id,
                auth_version=user.auth_version,
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                expires_at=utcnow() + timedelta(minutes=3),
            ))
            db.commit()
        return token

    def lookup_response(self, token, args):
        return self.app.test_client().post('/api/agent/tools', json={
            'request_token': token,
            'operation': 'lookup',
            'command': 'billing.unpaid',
            'arguments_json': json.dumps(args, ensure_ascii=False),
        })

    def make_house(self, building_name, room_no):
        building = self.business('building.save', {
            'community_id': 1,
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
            'area': '90',
            'usage': 'residential',
            'occupancy': 'owner_occupied',
        })['id']

    def make_person(self, name, phone, house_id):
        person = self.business('person.save', {
            'community_id': 1,
            'name': name,
            'phone': phone,
            'emergency_contact': '',
            'note': '',
        })['id']
        self.business('relation.bind', {
            'house_id': house_id,
            'person_id': person,
            'kind': 'owner',
            'is_resident': True,
        })
        return person

    def make_bill(self, house_id, fee_id, period):
        return self.business('bill.create', {
            'house_id': house_id,
            'fee_item_id': fee_id,
            'period': period,
            'due_date': period + '-28',
        })['id']

    def prepare_two_people(self):
        house_a = self.make_house('A栋', 101)
        house_b = self.make_house('B栋', 201)
        wang = self.make_person('王五', '13800000123', house_a)
        zhao = self.make_person('赵六', '13900000456', house_b)
        fee = self.business('fee.save', {
            'community_id': 1,
            'name': '物业费',
            'basis': 'fixed',
            'rate': '100',
        })['id']
        bill_a = self.make_bill(house_a, fee, '2026-09')
        bill_b = self.make_bill(house_b, fee, '2026-09')
        return wang, zhao, bill_a, bill_b

    def test_planner_extracts_person_from_natural_unpaid_question(self):
        for text in ('查王五有没有欠费', '查一下王五是否欠费', '王五还有没有未缴物业费'):
            with self.subTest(text=text):
                plan = plan_request(text, {'billing.unpaid'})
                self.assertEqual((plan['intent'], plan['action']), ('billing.unpaid', 'TOOL'), plan)
                self.assertEqual(plan['arguments'].get('person_name'), '王五', plan)

    def test_person_unpaid_lookup_never_degrades_to_all_visible_bills(self):
        _, _, bill_a, bill_b = self.prepare_two_people()
        token = self.grant()
        response = self.lookup_response(token, {'person_name': '王五'})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual([row['id'] for row in response.json['items']], [bill_a])
        self.assertNotIn(bill_b, [row['id'] for row in response.json['items']])

    def test_unknown_person_is_not_treated_as_broad_unpaid_query(self):
        self.prepare_two_people()
        token = self.grant()
        response = self.lookup_response(token, {'person_name': '不存在'})
        self.assertEqual(response.status_code, 404, response.get_data(as_text=True))

    def test_same_name_requires_disambiguation_instead_of_unioning_bills(self):
        house_a = self.make_house('A栋', 101)
        house_b = self.make_house('B栋', 201)
        self.make_person('王五', '13800000123', house_a)
        self.make_person('王五', '13900000456', house_b)
        fee = self.business('fee.save', {
            'community_id': 1,
            'name': '物业费',
            'basis': 'fixed',
            'rate': '100',
        })['id']
        self.make_bill(house_a, fee, '2026-09')
        self.make_bill(house_b, fee, '2026-09')
        token = self.grant()
        response = self.lookup_response(token, {'person_name': '王五'})
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))

        narrowed = self.lookup_response(token, {'person_name': '王五', 'phone': '13800000123'})
        self.assertEqual(narrowed.status_code, 200, narrowed.get_data(as_text=True))
        self.assertEqual(len(narrowed.json['items']), 1)

    def test_provider_cannot_switch_person_or_building_on_unpaid_read(self):
        cases = (
            (
                {'person_name': '王五'},
                {'person_name': '赵六', 'building_name': 'B栋'},
                {'person_name': '王五'},
            ),
            (
                {'building_name': 'B栋'},
                {'building_name': 'A栋', 'month': '2025-01'},
                {'building_name': 'B栋'},
            ),
        )
        for planner_args, provider_args, expected in cases:
            with self.subTest(planner_args=planner_args):
                token = _PLANNER_HINT.set({
                    'action': 'TOOL',
                    'intent': 'billing.unpaid',
                    'candidates': ['billing.unpaid'],
                    'arguments': planner_args,
                })
                try:
                    call = _synthetic_call({
                        'operation': 'lookup',
                        'command': 'billing.unpaid',
                        'arguments_json': json.dumps(provider_args, ensure_ascii=False),
                    })
                    normalized = _normalize_read_call(call)
                finally:
                    _PLANNER_HINT.reset(token)
                outer = json.loads(normalized['function']['arguments'])
                self.assertEqual(json.loads(outer['arguments_json']), expected)


if __name__ == '__main__':
    unittest.main()
