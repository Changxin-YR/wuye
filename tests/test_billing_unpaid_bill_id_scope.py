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


class BillingUnpaidBillIdScopeTests(unittest.TestCase):
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
        response = self.app.test_client().post('/api/agent/tools', json={
            'request_token': token,
            'operation': 'lookup',
            'command': 'billing.unpaid',
            'arguments_json': json.dumps(args, ensure_ascii=False),
        })
        self.assertEqual(response.status_code, 200, response.text[:1200])
        return response.json['items']

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
        house = self.business('house.save', {
            'unit_id': unit,
            'room_no': room_no,
            'area': '90',
            'usage': 'residential',
            'occupancy': 'vacant',
        })['id']
        return building, house

    def make_bill(self, house_id, fee_id, period):
        return self.business('bill.create', {
            'house_id': house_id,
            'fee_item_id': fee_id,
            'period': period,
            'due_date': period + '-28',
        })['id']

    def test_bill_id_is_exact_filter_and_combines_with_other_filters(self):
        _, house_a = self.make_house('A栋', 101)
        _, house_b = self.make_house('B栋', 201)
        fee = self.business('fee.save', {
            'community_id': 1,
            'name': '物业费',
            'basis': 'fixed',
            'rate': '100',
        })['id']
        bill_a = self.make_bill(house_a, fee, '2026-09')
        bill_b = self.make_bill(house_b, fee, '2026-10')
        token = self.grant(1)

        rows = self.lookup(token, {'bill_id': bill_b})
        self.assertEqual([row['id'] for row in rows], [bill_b])

        mismatch_house = self.lookup(token, {'bill_id': bill_b, 'house_id': house_a})
        self.assertEqual(mismatch_house, [])

        mismatch_month = self.lookup(token, {'bill_id': bill_a, 'month': '2026-10'})
        self.assertEqual(mismatch_month, [])

    def test_bill_id_never_bypasses_building_data_scope(self):
        building_a, house_a = self.make_house('A栋', 101)
        _, house_b = self.make_house('B栋', 201)
        fee = self.business('fee.save', {
            'community_id': 1,
            'name': '物业费',
            'basis': 'fixed',
            'rate': '100',
        })['id']
        bill_a = self.make_bill(house_a, fee, '2026-09')
        bill_b = self.make_bill(house_b, fee, '2026-09')
        finance_id = self.business('staff.create', {
            'username': 'scopefinance_bill_lookup',
            'password': PW,
            'real_name': 'A栋财务',
            'phone': '13800000992',
            'role_codes': ['finance'],
            'scope_kind': 'building',
            'community_id': 1,
            'building_id': building_a,
        })['id']
        token = self.grant(finance_id)

        visible = self.lookup(token, {})
        self.assertEqual([row['id'] for row in visible], [bill_a])

        own = self.lookup(token, {'bill_id': bill_a})
        self.assertEqual([row['id'] for row in own], [bill_a])

        foreign = self.lookup(token, {'bill_id': bill_b})
        self.assertEqual(foreign, [])


if __name__ == '__main__':
    unittest.main()
