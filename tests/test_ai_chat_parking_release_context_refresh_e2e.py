import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask, g
from sqlalchemy import select
from werkzeug.security import generate_password_hash

import agent_business_context as business_context
import agent_planner_state
from agent_planner import plan_request
from app import create_app
from database_fixture import test_database
from dify_client import BailianClient
from models import AiAction, ParkingSpace, ParkingUse, User


PW = 'Fixture-only-ai-chat-parking-refresh-563!'
HASH = generate_password_hash(PW)


class ParkingReleaseContextPlannerTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        business_context._CURRENT.clear()
        agent_planner_state._PENDING.clear()

    def plan(self, message, authorized, conversation_id=None):
        payload = {'message': message}
        if conversation_id:
            payload['conversation_id'] = conversation_id
        with self.app.test_request_context('/ai/chat', method='POST', json=payload):
            g.user = SimpleNamespace(id=17, auth_version=1)
            return plan_request(message, authorized, {})

    def test_contextual_repeat_reuses_visible_space_code_not_internal_relation_id(self):
        authorized = {'parking_use.search', 'parking.release'}
        first = self.plan('释放A-001车位，原因租期结束', authorized)
        self.assertEqual(first['intent'], 'parking.release')
        self.assertEqual(first['action'], 'CONFIRM')
        self.assertEqual(first['entity_status'], 'RESOLVE_FIRST')
        self.assertEqual(first['arguments']['space_code'], 'A-001')
        self.assertNotIn('id', first['arguments'])
        self.assertNotIn('version', first['arguments'])

        second = self.plan(
            '再释放刚才这个车位，原因重复测试',
            authorized,
            conversation_id='conv-parking-refresh',
        )
        self.assertEqual(second['intent'], 'parking.release')
        self.assertEqual(second['action'], 'CONFIRM')
        self.assertEqual(second['entity_status'], 'RESOLVE_FIRST')
        self.assertEqual(second['arguments']['space_code'], 'A-001')
        self.assertEqual(second['arguments']['reason'], '重复测试')
        self.assertNotIn('id', second['arguments'])
        self.assertNotIn('version', second['arguments'])
        self.assertEqual(second['candidates'], ['parking_use.search', 'parking.release'])


class AiChatParkingReleaseContextRefreshEndToEndTests(unittest.TestCase):
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

    def business(self, command, data):
        response = self.client.post(
            '/api/business/' + command,
            json={'data': data, 'confirmed': True},
            headers={'X-CSRF-Token': self.csrf()},
        )
        self.assertEqual(response.status_code, 200, response.text[:1800])
        return response.json

    def seed_active_parking_use(self):
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
        use_id = self.business('parking.assign', {
            'space_id': space_id, 'vehicle_id': vehicle_id,
        })['id']
        return space_id, use_id

    def test_release_confirm_then_stale_context_rechecks_state_without_second_pending(self):
        space_id, use_id = self.seed_active_parking_use()
        provider = BailianClient('http://agent.invalid', 'fixture-key', 'qwen-plus')
        self.app.extensions['dify'] = provider
        calls = []

        def request(*args, **kwargs):
            calls.append((args, kwargs))
            return {
                'id': f'parking-refresh-{len(calls)}',
                'choices': [{'message': {'content': '我按当前业务状态继续核对。'}}],
            }

        with patch.object(provider, '_request', side_effect=request):
            first = self.client.post(
                '/ai/chat',
                json={'message': '释放A-001车位，原因租期结束'},
                headers={'X-CSRF-Token': self.csrf()},
            )

        self.assertEqual(first.status_code, 200, first.text[:2500])
        pending = [
            item for item in first.json.get('actions', [])
            if item.get('command') == 'parking.release' and item.get('status') == 'pending'
        ]
        self.assertEqual(len(pending), 1, first.text[:2500])
        conversation_id = first.json['conversation_id']

        confirmed = self.client.post(
            f"/ai/actions/{pending[0]['id']}/confirm",
            json={},
            headers={'X-CSRF-Token': self.csrf()},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text[:2000])
        self.assertEqual(confirmed.json['status'], 'executed')

        with self.factory() as db:
            use = db.get(ParkingUse, use_id)
            self.assertEqual(use.status, 'ended')
            self.assertIsNotNone(use.end_at)
            self.assertEqual(db.get(ParkingSpace, space_id).status, 'available')
            first_action_count = len(db.scalars(select(AiAction).where(
                AiAction.command == 'parking.release'
            )).all())
            self.assertEqual(first_action_count, 1)

        calls_before_retry = len(calls)
        with patch.object(provider, '_request', side_effect=request):
            second = self.client.post(
                '/ai/chat',
                json={
                    'message': '再释放刚才这个车位，原因重复测试',
                    'conversation_id': conversation_id,
                },
                headers={'X-CSRF-Token': self.csrf()},
            )

        self.assertEqual(second.status_code, 200, second.text[:2500])
        self.assertEqual(second.json.get('source'), 'bailian', second.text[:2500])
        self.assertGreater(len(calls), calls_before_retry, 'stale retry must re-enter provider/resolver flow')
        second_pending = [
            item for item in second.json.get('actions', [])
            if item.get('command') == 'parking.release' and item.get('status') == 'pending'
        ]
        self.assertEqual(second_pending, [], second.text[:2500])

        with self.factory() as db:
            use = db.get(ParkingUse, use_id)
            self.assertEqual(use.status, 'ended')
            self.assertEqual(db.get(ParkingSpace, space_id).status, 'available')
            all_release_actions = db.scalars(select(AiAction).where(
                AiAction.command == 'parking.release'
            )).all()
            self.assertEqual(len(all_release_actions), 1)
            self.assertEqual(all_release_actions[0].status, 'executed')


if __name__ == '__main__':
    unittest.main()
