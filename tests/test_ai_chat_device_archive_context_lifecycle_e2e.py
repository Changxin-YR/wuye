import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import select
from werkzeug.security import generate_password_hash

from app import create_app
from database_fixture import test_database
from dify_client import BailianClient
from models import AiAction, AuditLog, Device, Inspection, User


PW = 'Fixture-only-ai-chat-device-archive-947!'
HASH = generate_password_hash(PW)


class AiChatDeviceArchiveContextLifecycleTests(unittest.TestCase):
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
        self.building_id = self.business('building.save', {
            'community_id': 1, 'name': 'A栋', 'floors': 20,
        })['id']
        self.engineer_id = self.business('staff.create', {
            'username': 'device_archive_engineer', 'password': PW,
            'real_name': '王工', 'phone': '13800000851',
            'role_codes': ['engineer'], 'scope_kind': 'building',
            'community_id': 1, 'building_id': self.building_id,
        })['id']

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

    def seed_device(self, code, name):
        return int(self.business('device.save', {
            'community_id': 1,
            'building_id': self.building_id,
            'code': code,
            'name': name,
            'category': 'pump',
            'location': 'A栋设备间',
            'status': 'normal',
        })['id'])

    def query_device(self, code):
        read_responses = iter([
            {'id': f'{code}-read-1', 'choices': [{'message': {'content': '我先核对设备。'}}]},
            {'id': f'{code}-read-2', 'choices': [{'message': {'content': f'{code} 当前状态已核对。'}}]},
        ])
        with patch.object(self.provider, '_request', side_effect=lambda *a, **k: next(read_responses)):
            response = self.chat(f'查询设备{code}的状态')
        self.assertEqual(response.status_code, 200, response.text[:2000])
        self.assertEqual(response.json.get('source'), 'bailian', response.text[:2000])
        self.assertTrue(response.json.get('conversation_id'))
        return response.json['conversation_id']

    def test_read_archive_confirm_then_stale_context_cannot_archive_again(self):
        device_id = self.seed_device('P-01', '一号循环水泵')
        conversation_id = self.query_device('P-01')

        missing_reason = self.chat('把刚才那个设备归档', conversation_id)
        self.assertEqual(missing_reason.status_code, 200, missing_reason.text[:2000])
        self.assertEqual(missing_reason.json.get('source'), 'planner')
        self.assertEqual(missing_reason.json.get('actions'), [])
        self.assertIn('原因', missing_reason.json.get('answer', ''))

        archive_responses = iter([
            {'id': 'device-archive-1', 'choices': [{'message': {'content': '我重新核对刚才的设备。'}}]},
            {'id': 'device-archive-2', 'choices': [{'message': {'content': '设备唯一，生成归档确认单。'}}]},
            {'id': 'device-archive-3', 'choices': [{'message': {'content': '请确认后归档该设备。'}}]},
        ])
        with patch.object(self.provider, '_request', side_effect=lambda *a, **k: next(archive_responses)):
            proposed = self.chat('设备已永久停用', conversation_id)

        self.assertEqual(proposed.status_code, 200, proposed.text[:2500])
        pending = [
            item for item in proposed.json.get('actions', [])
            if item.get('command') == 'device.archive' and item.get('status') == 'pending'
        ]
        self.assertEqual(len(pending), 1, proposed.text[:2500])

        with self.factory() as db:
            device = db.get(Device, device_id)
            self.assertFalse(device.deleted)
            action = db.get(AiAction, pending[0]['id'])
            self.assertEqual(json.loads(action.payload), {
                'id': device_id,
                'version': device.version,
                'reason': '设备已永久停用',
            })

        confirmed = self.client.post(
            f"/ai/actions/{pending[0]['id']}/confirm",
            json={}, headers={'X-CSRF-Token': self.csrf()},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text[:2000])
        self.assertEqual(confirmed.json['status'], 'executed')

        with self.factory() as db:
            device = db.get(Device, device_id)
            self.assertTrue(device.deleted)
            archive_actions = db.scalars(select(AiAction).where(
                AiAction.command == 'device.archive'
            )).all()
            self.assertEqual(len(archive_actions), 1)
            self.assertEqual(archive_actions[0].status, 'executed')
            archive_audits = db.scalars(select(AuditLog).where(
                AuditLog.source == 'agent',
                AuditLog.action == 'device.archive',
                AuditLog.status == 'success',
            )).all()
            self.assertEqual(len(archive_audits), 1)
            self.assertEqual(archive_audits[0].detail, '设备已永久停用')

        retry_calls = []

        def retry_response(*args, **kwargs):
            retry_calls.append((args, kwargs))
            return {
                'id': f'device-archive-retry-{len(retry_calls)}',
                'choices': [{'message': {'content': '我重新核对当前设备状态。'}}],
            }

        with patch.object(self.provider, '_request', side_effect=retry_response):
            retried = self.chat('把刚才那个设备再归档一次，原因重复测试', conversation_id)

        self.assertEqual(retried.status_code, 200, retried.text[:2500])
        self.assertEqual(retried.json.get('source'), 'bailian', retried.text[:2500])
        self.assertGreater(len(retry_calls), 0, 'stale retry must re-enter the device resolver flow')
        second_pending = [
            item for item in retried.json.get('actions', [])
            if item.get('command') == 'device.archive' and item.get('status') == 'pending'
        ]
        self.assertEqual(second_pending, [], retried.text[:2500])

        with self.factory() as db:
            self.assertTrue(db.get(Device, device_id).deleted)
            archive_actions = db.scalars(select(AiAction).where(
                AiAction.command == 'device.archive'
            )).all()
            self.assertEqual(len(archive_actions), 1)
            archive_audits = db.scalars(select(AuditLog).where(
                AuditLog.source == 'agent',
                AuditLog.action == 'device.archive',
                AuditLog.status == 'success',
            )).all()
            self.assertEqual(len(archive_audits), 1)

    def test_pending_inspection_blocks_archive_before_confirmation_action_exists(self):
        device_id = self.seed_device('P-02', '二号循环水泵')
        due_at = (datetime.now(timezone.utc) + timedelta(days=1, hours=8)).replace(tzinfo=None).isoformat(timespec='minutes')
        inspection_id = int(self.business('inspection.create', {
            'device_id': device_id,
            'assignee_id': self.engineer_id,
            'due_at': due_at,
            'checklist': '检查振动、温度和是否漏水',
        })['id'])
        conversation_id = self.query_device('P-02')

        calls = []

        def conflict_response(*args, **kwargs):
            calls.append((args, kwargs))
            return {
                'id': f'device-conflict-{len(calls)}',
                'choices': [{'message': {'content': '我按当前设备和巡检状态核对归档条件。'}}],
            }

        with patch.object(self.provider, '_request', side_effect=conflict_response):
            response = self.chat('把刚才那个设备归档，原因设备已淘汰', conversation_id)

        self.assertEqual(response.status_code, 200, response.text[:2500])
        self.assertEqual(response.json.get('source'), 'bailian', response.text[:2500])
        self.assertGreater(len(calls), 0)
        pending = [
            item for item in response.json.get('actions', [])
            if item.get('command') == 'device.archive' and item.get('status') == 'pending'
        ]
        self.assertEqual(pending, [], response.text[:2500])

        with self.factory() as db:
            device = db.get(Device, device_id)
            inspection = db.get(Inspection, inspection_id)
            self.assertFalse(device.deleted)
            self.assertEqual(inspection.status, 'pending')
            self.assertEqual(len(db.scalars(select(AiAction).where(
                AiAction.command == 'device.archive'
            )).all()), 0)
            self.assertEqual(len(db.scalars(select(AuditLog).where(
                AuditLog.source == 'agent',
                AuditLog.action == 'device.archive',
                AuditLog.status == 'success',
            )).all()), 0)


if __name__ == '__main__':
    unittest.main()
