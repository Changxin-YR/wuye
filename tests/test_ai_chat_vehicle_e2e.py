import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import func, select
from werkzeug.security import generate_password_hash

from app import create_app
from database_fixture import test_database
from dify_client import BailianClient
from models import AuditLog, HousePerson, User, Vehicle


PW = 'Fixture-only-ai-chat-vehicle-401!'
HASH = generate_password_hash(PW)


class AiChatVehicleEndToEndTests(unittest.TestCase):
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

    def make_house(self, building_name, room_no):
        building_id = self.business('building.save', {
            'community_id': 1,
            'name': building_name,
            'floors': 20,
        })['id']
        unit_id = self.business('unit.save', {
            'building_id': building_id,
            'name': '1单元',
        })['id']
        house_id = self.business('house.save', {
            'unit_id': unit_id,
            'room_no': room_no,
            'area': '88',
            'usage': 'residential',
            'occupancy': 'vacant',
        })['id']
        return building_id, house_id

    def make_resident(self, house_id, phone):
        person_id = self.business('person.save', {
            'community_id': 1,
            'name': '王五',
            'phone': phone,
        })['id']
        self.business('relation.bind', {
            'house_id': house_id,
            'person_id': person_id,
            'kind': 'owner',
            'is_resident': True,
        })
        return person_id

    def test_chat_uses_house_scope_to_choose_same_name_vehicle_owner_and_persists(self):
        _, target_house = self.make_house('A栋', 101)
        target_owner = self.make_resident(target_house, '13800000123')
        _, other_house = self.make_house('B栋', 201)
        other_owner = self.make_resident(other_house, '13800000124')

        with self.factory() as db:
            self.assertEqual(db.scalar(select(func.count(Vehicle.id))), 0)
            self.assertEqual(db.scalar(select(func.count(HousePerson.id)).where(
                HousePerson.person_id.in_([target_owner, other_owner]),
                HousePerson.is_resident.is_(True),
            )), 2)

        provider = BailianClient('http://agent.invalid', 'fixture-key', 'qwen-plus')
        self.app.extensions['dify'] = provider
        responses = iter([
            {'id': 'vehicle-e2e-1', 'choices': [{'message': {'content': '我先核对车辆登记房屋。'}}]},
            {'id': 'vehicle-e2e-2', 'choices': [{'message': {'content': '我再核对该房屋的车主。'}}]},
            {'id': 'vehicle-e2e-3', 'choices': [{'message': {'content': '对象已唯一，开始登记车辆。'}}]},
            {'id': 'vehicle-e2e-4', 'choices': [{'message': {'content': '车辆登记已完成。'}}]},
        ])
        calls = []

        def request(*args, **kwargs):
            calls.append((args, kwargs))
            return next(responses)

        with patch.object(provider, '_request', side_effect=request):
            response = self.client.post(
                '/ai/chat',
                json={'message': '登记车辆，车牌粤A12345，车主王五，A栋101室，车型Model3'},
                headers={'X-CSRF-Token': self.csrf()},
            )

        self.assertEqual(response.status_code, 200, response.text[:1800])
        self.assertNotEqual(response.json.get('source'), 'planner', response.text[:1800])
        self.assertEqual(len(calls), 4)
        self.assertTrue(any(
            action.get('command') == 'vehicle.save' and action.get('status') == 'executed'
            for action in response.json.get('actions', [])
        ))

        with self.factory() as db:
            vehicles = list(db.scalars(select(Vehicle).where(Vehicle.plate == '粤A12345')))
            self.assertEqual(len(vehicles), 1)
            vehicle = vehicles[0]
            self.assertEqual(vehicle.house_id, target_house)
            self.assertEqual(vehicle.person_id, target_owner)
            self.assertNotEqual(vehicle.person_id, other_owner)
            self.assertEqual(vehicle.model, 'Model3')

            audit = db.scalar(select(AuditLog).where(
                AuditLog.source == 'agent',
                AuditLog.action == 'vehicle.save',
                AuditLog.status == 'success',
            ).order_by(AuditLog.id.desc()))
            self.assertIsNotNone(audit)


if __name__ == '__main__':
    unittest.main()
