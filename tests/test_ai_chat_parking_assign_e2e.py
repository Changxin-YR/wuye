import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import func, select
from werkzeug.security import generate_password_hash

from app import create_app
from database_fixture import test_database
from dify_client import BailianClient
from models import AuditLog, ParkingSpace, ParkingUse, User, Vehicle


PW = 'Fixture-only-ai-chat-parking-409!'
HASH = generate_password_hash(PW)


class AiChatParkingAssignEndToEndTests(unittest.TestCase):
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

    def seed_vehicle_and_space(self):
        building_id = self.business('building.save', {
            'community_id': 1, 'name': 'A栋', 'floors': 20,
        })['id']
        unit_id = self.business('unit.save', {
            'building_id': building_id, 'name': '1单元',
        })['id']
        house_id = self.business('house.save', {
            'unit_id': unit_id, 'room_no': 101, 'area': '88',
            'usage': 'residential', 'occupancy': 'vacant',
        })['id']
        person_id = self.business('person.save', {
            'community_id': 1, 'name': '王五', 'phone': '13800000123',
        })['id']
        self.business('relation.bind', {
            'house_id': house_id, 'person_id': person_id,
            'kind': 'owner', 'is_resident': True,
        })
        vehicle_id = self.business('vehicle.save', {
            'house_id': house_id, 'person_id': person_id,
            'plate': '粤A12345', 'model': 'Model3',
        })['id']
        space_id = self.business('parking.save', {
            'community_id': 1, 'building_id': building_id,
            'code': 'A-001', 'location': '地下A区001',
        })['id']
        return vehicle_id, space_id

    def test_chat_resolves_business_identifiers_and_persists_parking_use(self):
        vehicle_id, space_id = self.seed_vehicle_and_space()
        with self.factory() as db:
            self.assertEqual(db.scalar(select(func.count(ParkingUse.id))), 0)
            self.assertEqual(db.get(ParkingSpace, space_id).status, 'available')

        provider = BailianClient('http://agent.invalid', 'fixture-key', 'qwen-plus')
        self.app.extensions['dify'] = provider
        responses = iter([
            {'id': 'parking-e2e-1', 'choices': [{'message': {'content': '我先核对车位。'}}]},
            {'id': 'parking-e2e-2', 'choices': [{'message': {'content': '我再核对车辆。'}}]},
            {'id': 'parking-e2e-3', 'choices': [{'message': {'content': '对象已唯一，开始分配车位。'}}]},
            {'id': 'parking-e2e-4', 'choices': [{'message': {'content': '车位分配已完成。'}}]},
        ])
        calls = []

        def request(*args, **kwargs):
            calls.append((args, kwargs))
            return next(responses)

        with patch.object(provider, '_request', side_effect=request):
            response = self.client.post(
                '/ai/chat',
                json={'message': '把A-001车位分给粤A12345'},
                headers={'X-CSRF-Token': self.csrf()},
            )

        self.assertEqual(response.status_code, 200, response.text[:1800])
        self.assertNotEqual(response.json.get('source'), 'planner', response.text[:1800])
        self.assertEqual(len(calls), 4)
        self.assertTrue(any(
            action.get('command') == 'parking.assign' and action.get('status') == 'executed'
            for action in response.json.get('actions', [])
        ))

        with self.factory() as db:
            uses = list(db.scalars(select(ParkingUse).where(ParkingUse.status == 'active')))
            self.assertEqual(len(uses), 1)
            use = uses[0]
            self.assertEqual(use.space_id, space_id)
            self.assertEqual(use.vehicle_id, vehicle_id)
            self.assertEqual(use.active_space, space_id)
            self.assertEqual(use.active_vehicle, vehicle_id)
            self.assertEqual(db.get(ParkingSpace, space_id).status, 'occupied')
            self.assertEqual(db.get(Vehicle, vehicle_id).plate, '粤A12345')

            audit = db.scalar(select(AuditLog).where(
                AuditLog.source == 'agent',
                AuditLog.action == 'parking.assign',
                AuditLog.status == 'success',
            ).order_by(AuditLog.id.desc()))
            self.assertIsNotNone(audit)


if __name__ == '__main__':
    unittest.main()
