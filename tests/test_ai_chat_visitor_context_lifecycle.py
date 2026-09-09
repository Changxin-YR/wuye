import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import func, select
from werkzeug.security import generate_password_hash

import agent_tools
import app as app_module
from app import create_app
from database_fixture import test_database
from dify_client import BailianClient
from models import AiAction, AuditLog, User, Visitor


PW = 'Fixture-only-ai-chat-visitor-context-642!'
HASH = generate_password_hash(PW)


class AiChatVisitorContextLifecycleTests(unittest.TestCase):
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
        self.visitor_id = self.seed_visitor()

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

    def seed_visitor(self):
        building_id = self.business('building.save', {
            'community_id': 1, 'name': '8栋', 'floors': 20,
        })['id']
        unit_id = self.business('unit.save', {
            'building_id': building_id, 'name': '1单元',
        })['id']
        house_id = self.business('house.save', {
            'unit_id': unit_id, 'room_no': 602, 'area': '96',
            'usage': 'residential', 'occupancy': 'owner_occupied',
        })['id']
        person_id = self.business('person.save', {
            'community_id': 1, 'name': '王五', 'phone': '13800000602',
        })['id']
        self.business('relation.bind', {
            'house_id': house_id, 'person_id': person_id,
            'kind': 'owner', 'is_resident': True,
        })
        return int(self.business('visitor.create', {
            'house_id': house_id,
            'host_person_id': person_id,
            'name': '李四',
            'phone': '13900000602',
            'purpose': '送文件',
            'expected_at': '2026-09-10T10:00:00',
        })['id'])

    def test_read_checkin_checkout_then_stale_context_cannot_checkout_again(self):
        read_responses = iter([
            {'id': 'visitor-read-1', 'choices': [{'message': {'content': '我先核对这条访客记录。'}}]},
            {'id': 'visitor-read-2', 'choices': [{'message': {'content': '已查到访客记录。'}}]},
        ])
        with patch.object(self.provider, '_request', side_effect=lambda *a, **k: next(read_responses)):
            first = self.chat(f'查询访客记录#{self.visitor_id}')
        self.assertEqual(first.status_code, 200, first.text[:2000])
        conversation_id = first.json.get('conversation_id')
        self.assertTrue(conversation_id)

        with self.factory() as db:
            visitor = db.get(Visitor, self.visitor_id)
            self.assertEqual(visitor.status, 'registered')
            self.assertIsNone(visitor.check_in)
            self.assertIsNone(visitor.check_out)

        lookup_calls = []
        perform_calls = []
        original_query = app_module.domain_query
        original_perform = agent_tools.perform

        def query_spy(db, actor, command, params):
            result = original_query(db, actor, command, params)
            if command == 'visitor.search':
                lookup_calls.append({'params': dict(params), 'result': result})
            return result

        def perform_spy(db, actor, grant, command, params, request_key=None):
            if command.startswith('visitor.'):
                perform_calls.append({'command': command, 'params': dict(params)})
            return original_perform(db, actor, grant, command, params, request_key=request_key)

        checkin_responses = iter([
            {'id': 'visitor-in-1', 'choices': [{'message': {'content': '我重新核对刚才的访客。'}}]},
            {'id': 'visitor-in-2', 'choices': [{'message': {'content': '状态允许入场，执行登记。'}}]},
            {'id': 'visitor-in-3', 'choices': [{'message': {'content': '访客已登记进入。'}}]},
        ])
        with (
            patch.object(app_module, 'domain_query', side_effect=query_spy),
            patch.object(agent_tools, 'perform', side_effect=perform_spy),
            patch.object(self.provider, '_request', side_effect=lambda *a, **k: next(checkin_responses)),
        ):
            second = self.chat('确认刚才的访客进入', conversation_id)
        self.assertEqual(second.status_code, 200, second.text[:2200])
        executed_checkin = any(
            item.get('command') == 'visitor.checkin' and item.get('status') == 'executed'
            for item in second.json.get('actions', [])
        )
        self.assertTrue(
            executed_checkin,
            f'lookup_calls={lookup_calls!r}; perform_calls={perform_calls!r}; response={second.text[:2200]}',
        )

        with self.factory() as db:
            visitor = db.get(Visitor, self.visitor_id)
            self.assertEqual(visitor.status, 'inside')
            self.assertIsNotNone(visitor.check_in)
            self.assertIsNone(visitor.check_out)

        checkout_responses = iter([
            {'id': 'visitor-out-1', 'choices': [{'message': {'content': '我重新核对刚才的访客当前状态。'}}]},
            {'id': 'visitor-out-2', 'choices': [{'message': {'content': '状态允许离场，执行登记。'}}]},
            {'id': 'visitor-out-3', 'choices': [{'message': {'content': '访客离场已记录。'}}]},
        ])
        with patch.object(self.provider, '_request', side_effect=lambda *a, **k: next(checkout_responses)):
            third = self.chat('刚才那个访客已经离开了', conversation_id)
        self.assertEqual(third.status_code, 200, third.text[:2200])
        self.assertTrue(any(
            item.get('command') == 'visitor.checkout' and item.get('status') == 'executed'
            for item in third.json.get('actions', [])
        ), third.text[:2200])

        with self.factory() as db:
            visitor = db.get(Visitor, self.visitor_id)
            self.assertEqual(visitor.status, 'left')
            self.assertIsNotNone(visitor.check_out)
            checkout_audits = db.scalar(select(func.count(AuditLog.id)).where(
                AuditLog.source == 'agent',
                AuditLog.action == 'visitor.checkout',
                AuditLog.status == 'success',
            ))
            self.assertEqual(checkout_audits, 1)
            executed_actions = db.scalar(select(func.count(AiAction.id)).where(
                AiAction.command == 'visitor.checkout', AiAction.status == 'executed'
            ))
            self.assertEqual(executed_actions, 1)

        stale_responses = iter([
            {'id': 'visitor-stale-1', 'choices': [{'message': {'content': '我再核对当前访客状态。'}}]},
            {'id': 'visitor-stale-2', 'choices': [{'message': {'content': '该访客已经离场，不能重复登记离场。'}}]},
        ])
        with patch.object(self.provider, '_request', side_effect=lambda *a, **k: next(stale_responses)):
            fourth = self.chat('刚才那个访客已经离开了', conversation_id)
        self.assertEqual(fourth.status_code, 200, fourth.text[:2200])
        self.assertFalse(any(
            item.get('command') == 'visitor.checkout'
            for item in fourth.json.get('actions', [])
        ), fourth.text[:2200])

        with self.factory() as db:
            visitor = db.get(Visitor, self.visitor_id)
            self.assertEqual(visitor.status, 'left')
            checkout_audits = db.scalar(select(func.count(AuditLog.id)).where(
                AuditLog.source == 'agent',
                AuditLog.action == 'visitor.checkout',
                AuditLog.status == 'success',
            ))
            self.assertEqual(checkout_audits, 1)
            executed_actions = db.scalar(select(func.count(AiAction.id)).where(
                AiAction.command == 'visitor.checkout', AiAction.status == 'executed'
            ))
            self.assertEqual(executed_actions, 1)


if __name__ == '__main__':
    unittest.main()
