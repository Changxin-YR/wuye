import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import func, select
from werkzeug.security import generate_password_hash

from app import create_app
from database_fixture import test_database
from dify_client import BailianClient
from models import AiAction, AuditLog, Complaint, User


PW = 'Fixture-only-ai-chat-complaint-context-827!'
HASH = generate_password_hash(PW)


class AiChatComplaintContextLifecycleTests(unittest.TestCase):
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
        self.complaint_id = self.seed_complaint()

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

    def seed_complaint(self):
        building_id = self.business('building.save', {
            'community_id': 1, 'name': '23栋', 'floors': 20,
        })['id']
        unit_id = self.business('unit.save', {
            'building_id': building_id, 'name': '1单元',
        })['id']
        house_id = self.business('house.save', {
            'unit_id': unit_id, 'room_no': 311, 'area': '88',
            'usage': 'residential', 'occupancy': 'owner_occupied',
        })['id']
        return int(self.business('complaint.create', {
            'house_id': house_id,
            'title': '噪音投诉',
            'content': '晚上施工太吵',
            'category': '噪音',
        })['id'])

    def test_read_resolve_close_then_stale_context_cannot_close_again(self):
        read_responses = iter([
            {'id': 'complaint-read-1', 'choices': [{'message': {'content': '我先核对这条投诉。'}}]},
            {'id': 'complaint-read-2', 'choices': [{'message': {'content': '已查到投诉记录。'}}]},
        ])
        with patch.object(self.provider, '_request', side_effect=lambda *a, **k: next(read_responses)):
            first = self.chat(f'查询投诉#{self.complaint_id}状态')
        self.assertEqual(first.status_code, 200, first.text[:2000])
        conversation_id = first.json.get('conversation_id')
        self.assertTrue(conversation_id)

        resolve_responses = iter([
            {'id': 'complaint-resolve-1', 'choices': [{'message': {'content': '我重新读取刚才这条投诉。'}}]},
            {'id': 'complaint-resolve-2', 'choices': [{'message': {'content': '对象和当前状态已核对，记录处理结果。'}}]},
            {'id': 'complaint-resolve-3', 'choices': [{'message': {'content': '处理结果已保存。'}}]},
        ])
        with patch.object(self.provider, '_request', side_effect=lambda *a, **k: next(resolve_responses)):
            second = self.chat('刚才这条投诉处理结果是已上门整改', conversation_id)
        self.assertEqual(second.status_code, 200, second.text[:2200])
        self.assertTrue(any(
            item.get('command') == 'complaint.resolve' and item.get('status') == 'executed'
            for item in second.json.get('actions', [])
        ), second.text[:2200])

        with self.factory() as db:
            complaint = db.get(Complaint, self.complaint_id)
            self.assertEqual(complaint.status, 'resolved')
            self.assertEqual(complaint.resolution, '已上门整改')

        close_responses = iter([
            {'id': 'complaint-close-1', 'choices': [{'message': {'content': '我重新核对这条投诉当前状态。'}}]},
            {'id': 'complaint-close-2', 'choices': [{'message': {'content': '状态允许结案，生成确认单。'}}]},
            {'id': 'complaint-close-3', 'choices': [{'message': {'content': '请在确认单中确认后结案。'}}]},
        ])
        with patch.object(self.provider, '_request', side_effect=lambda *a, **k: next(close_responses)):
            third = self.chat('这个投诉回访住户确认后结案', conversation_id)
        self.assertEqual(third.status_code, 200, third.text[:2200])
        pending = [
            item for item in third.json.get('actions', [])
            if item.get('command') == 'complaint.close' and item.get('status') == 'pending'
        ]
        self.assertEqual(len(pending), 1, third.text[:2200])

        with self.factory() as db:
            complaint = db.get(Complaint, self.complaint_id)
            self.assertEqual(complaint.status, 'resolved')
            action = db.get(AiAction, pending[0]['id'])
            self.assertIsNotNone(action)

        confirmed = self.client.post(
            f"/ai/actions/{pending[0]['id']}/confirm",
            json={}, headers={'X-CSRF-Token': self.csrf()},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text[:1800])
        self.assertEqual(confirmed.json['status'], 'executed')

        with self.factory() as db:
            complaint = db.get(Complaint, self.complaint_id)
            self.assertEqual(complaint.status, 'closed')
            self.assertIn('已上门整改', complaint.resolution)
            self.assertIn('回访：住户确认', complaint.resolution)
            close_audit = db.scalar(select(AuditLog).where(
                AuditLog.source == 'agent',
                AuditLog.action == 'complaint.close',
                AuditLog.status == 'success',
            ).order_by(AuditLog.id.desc()))
            self.assertIsNotNone(close_audit)

        before_pending = None
        with self.factory() as db:
            before_pending = db.scalar(select(func.count(AiAction.id)).where(
                AiAction.command == 'complaint.close', AiAction.status == 'pending'
            ))

        stale_responses = iter([
            {'id': 'complaint-stale-1', 'choices': [{'message': {'content': '我再核对当前状态。'}}]},
            {'id': 'complaint-stale-2', 'choices': [{'message': {'content': '这条投诉当前已不处于可结案状态。'}}]},
        ])
        with patch.object(self.provider, '_request', side_effect=lambda *a, **k: next(stale_responses)):
            fourth = self.chat('这个投诉回访住户确认后结案', conversation_id)
        self.assertEqual(fourth.status_code, 200, fourth.text[:2200])
        self.assertFalse(any(
            item.get('command') == 'complaint.close' and item.get('status') == 'pending'
            for item in fourth.json.get('actions', [])
        ), fourth.text[:2200])

        with self.factory() as db:
            complaint = db.get(Complaint, self.complaint_id)
            self.assertEqual(complaint.status, 'closed')
            after_pending = db.scalar(select(func.count(AiAction.id)).where(
                AiAction.command == 'complaint.close', AiAction.status == 'pending'
            ))
            self.assertEqual(after_pending, before_pending)


if __name__ == '__main__':
    unittest.main()
