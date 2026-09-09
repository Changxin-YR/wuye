import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import select
from werkzeug.security import generate_password_hash

from app import create_app
from database_fixture import test_database
from dify_client import BailianClient
from models import AiAction, AuditLog, User, Vehicle


PW = 'Fixture-only-ai-chat-vehicle-context-913!'
HASH = generate_password_hash(PW)


class AiChatVehicleContextArchiveTests(unittest.TestCase):
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
        self.provider = BailianClient('http://agent.invalid', 'fixture-key', 'qwen-plus')
        self.app.extensions['dify'] = self.provider
        self.vehicle_id = self.seed_vehicle()

    def tearDown(self):
        self.app.extensions['db_engine'].dispose()
        self.temp.cleanup()

    def csrf(self):
        self.client.get('/auth/login')
        with self.client.session_transaction() as session:
            return session['csrf_token']

    def login(self):
        response = self.client.post('/auth/login', data={
            'csrf_token': self.csrf(), 'username': 'admin', 'password': PW,
        })
        self.assertEqual(response.status_code, 302, response.text)

    def business(self, command, data):
        response = self.client.post(
            '/api/business/' + command,
            json={'data': data, 'confirmed': True},
            headers={'X-CSRF-Token': self.csrf()},
        )
        self.assertEqual(response.status_code, 200, response.text[:1800])
        return response.json

    def chat(self, message, conversation_id=None):
        payload = {'message': message}
        if conversation_id:
            payload['conversation_id'] = conversation_id
        return self.client.post(
            '/ai/chat', json=payload,
            headers={'X-CSRF-Token': self.csrf()},
        )

    def seed_vehicle(self):
        building = self.business('building.save', {
            'community_id': 1, 'name': 'A栋', 'floors': 20,
        })['id']
        unit = self.business('unit.save', {
            'building_id': building, 'name': '1单元',
        })['id']
        house = self.business('house.save', {
            'unit_id': unit, 'room_no': 101, 'area': '88',
            'usage': 'residential', 'occupancy': 'owner_occupied',
        })['id']
        person = self.business('person.save', {
            'community_id': 1, 'name': '王五', 'phone': '13800000123',
        })['id']
        self.business('relation.bind', {
            'house_id': house, 'person_id': person,
            'kind': 'owner', 'is_resident': True,
        })
        return int(self.business('vehicle.save', {
            'house_id': house, 'person_id': person,
            'plate': '粤A12345', 'model': 'Model3',
        })['id'])

    def test_query_then_context_archive_requires_reason_confirmation_and_exact_target(self):
        read_responses = iter([
            {'id': 'vehicle-context-read-1', 'choices': [{'message': {'content': '我先核对这辆车。'}}]},
            {'id': 'vehicle-context-read-2', 'choices': [{'message': {'content': '已查到粤A12345的车辆记录。'}}]},
        ])
        with patch.object(self.provider, '_request', side_effect=lambda *a, **k: next(read_responses)):
            first = self.chat('查询车牌粤A12345的车辆')

        self.assertEqual(first.status_code, 200, first.text[:2000])
        conversation_id = first.json.get('conversation_id')
        self.assertTrue(conversation_id)
        with self.factory() as db:
            vehicle = db.get(Vehicle, self.vehicle_id)
            self.assertFalse(vehicle.deleted)
            self.assertEqual(vehicle.status, 'active')

        # A contextual target is remembered, but archive reason is not invented.
        second = self.chat('把刚才那辆车归档', conversation_id)
        self.assertEqual(second.status_code, 200, second.text[:2000])
        self.assertEqual(second.json.get('source'), 'planner')
        self.assertEqual(second.json.get('actions'), [])
        self.assertIn('原因', second.json.get('answer', ''))
        with self.factory() as db:
            vehicle = db.get(Vehicle, self.vehicle_id)
            self.assertFalse(vehicle.deleted)
            self.assertEqual(vehicle.status, 'active')

        archive_responses = iter([
            {'id': 'vehicle-context-archive-1', 'choices': [{'message': {'content': '我重新核对刚才的车辆。'}}]},
            {'id': 'vehicle-context-archive-2', 'choices': [{'message': {'content': '车辆已唯一，生成归档确认单。'}}]},
            {'id': 'vehicle-context-archive-3', 'choices': [{'message': {'content': '请确认后归档该车辆。'}}]},
        ])
        with patch.object(self.provider, '_request', side_effect=lambda *a, **k: next(archive_responses)):
            third = self.chat('原因车辆已出售', conversation_id)

        self.assertEqual(third.status_code, 200, third.text[:2200])
        actions = [
            item for item in third.json.get('actions', [])
            if item.get('command') == 'vehicle.archive'
        ]
        self.assertEqual(len(actions), 1, third.text[:2200])
        pending = actions[0]
        self.assertEqual(pending['status'], 'pending')
        self.assertEqual(pending['execution_mode'], 'CONFIRM')

        with self.factory() as db:
            vehicle = db.get(Vehicle, self.vehicle_id)
            self.assertFalse(vehicle.deleted)
            self.assertEqual(vehicle.status, 'active')
            action = db.get(AiAction, pending['id'])
            self.assertEqual(json.loads(action.payload), {
                'id': self.vehicle_id,
                'version': vehicle.version,
                'reason': '车辆已出售',
            })

        confirmed = self.client.post(
            f"/ai/actions/{pending['id']}/confirm",
            json={}, headers={'X-CSRF-Token': self.csrf()},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text[:2000])
        self.assertEqual(confirmed.json['status'], 'executed')

        with self.factory() as db:
            vehicle = db.get(Vehicle, self.vehicle_id)
            self.assertTrue(vehicle.deleted)
            self.assertEqual(vehicle.status, 'archived')
            archive_audit = db.scalar(select(AuditLog).where(
                AuditLog.source == 'agent',
                AuditLog.action == 'vehicle.archive',
                AuditLog.status == 'success',
            ).order_by(AuditLog.id.desc()))
            self.assertIsNotNone(archive_audit)
            self.assertEqual(archive_audit.detail, '车辆已出售')
            execute_audit = db.scalar(select(AuditLog).where(
                AuditLog.source == 'agent',
                AuditLog.action == 'ai_execute',
                AuditLog.status == 'success',
            ).order_by(AuditLog.id.desc()))
            self.assertIsNotNone(execute_audit)


if __name__ == '__main__':
    unittest.main()
