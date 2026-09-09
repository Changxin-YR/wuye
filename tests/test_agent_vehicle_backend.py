import hashlib
import json
import secrets
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import timedelta

from sqlalchemy import func, select
from werkzeug.security import generate_password_hash

from app import create_app
from database_fixture import test_database
from models import AiGrant, AuditLog, User, Vehicle, utcnow


PW = 'Fixture-only-vehicle-297!'
HASH = generate_password_hash(PW)


class AgentVehicleBackendTests(unittest.TestCase):
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

    def agent_tool(self, command, arguments, expected=200):
        response = self.app.test_client().post('/api/agent/tools', json={
            'request_token': self.grant(),
            'operation': 'execute',
            'command': command,
            'arguments_json': json.dumps(arguments, ensure_ascii=False),
        })
        self.assertEqual(response.status_code, expected, response.text[:1800])
        return response.json

    def make_house(self):
        building = self.business('building.save', {
            'community_id': 1, 'name': 'A栋', 'floors': 20,
        })['id']
        unit = self.business('unit.save', {
            'building_id': building, 'name': '1单元',
        })['id']
        house = self.business('house.save', {
            'unit_id': unit,
            'room_no': 101,
            'area': '88',
            'usage': 'residential',
            'occupancy': 'vacant',
        })['id']
        return house

    def make_person(self, name, phone):
        return self.business('person.save', {
            'community_id': 1, 'name': name, 'phone': phone,
        })['id']

    def test_vehicle_agent_write_persists_and_records_agent_audit(self):
        house = self.make_house()
        owner = self.make_person('王五', '13800000123')
        outsider = self.make_person('李四', '13800000124')
        self.business('relation.bind', {
            'house_id': house, 'person_id': owner, 'kind': 'owner',
        })

        receipt = self.agent_tool('vehicle.save', {
            'house_id': house,
            'person_id': owner,
            'plate': '粤A12345',
            'model': '测试车型',
        })
        self.assertEqual(receipt['status'], 'executed')

        with self.factory() as db:
            vehicle = db.scalar(select(Vehicle).where(Vehicle.plate == '粤A12345'))
            self.assertIsNotNone(vehicle)
            self.assertEqual((vehicle.house_id, vehicle.person_id), (house, owner))
            audit_row = db.scalar(select(AuditLog).where(
                AuditLog.source == 'agent',
                AuditLog.action == 'vehicle.save',
                AuditLog.status == 'success',
            ).order_by(AuditLog.id.desc()))
            self.assertIsNotNone(audit_row)

        bad = self.agent_tool('vehicle.save', {
            'house_id': house,
            'person_id': outsider,
            'plate': '粤A54321',
        }, expected=400)
        self.assertIn(bad.get('code'), {'VALIDATION_ERROR', 'BUSINESS_CONFLICT'})
        with self.factory() as db:
            self.assertEqual(
                db.scalar(select(func.count(Vehicle.id)).where(Vehicle.plate == '粤A54321')),
                0,
            )


if __name__ == '__main__':
    unittest.main()
