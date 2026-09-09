import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import func, select
from werkzeug.security import generate_password_hash

from app import create_app
from database_fixture import test_database
from dify_client import BailianClient
from models import AuditLog, Visitor, User


PW = 'Fixture-only-ai-chat-visitor-host-383!'
HASH = generate_password_hash(PW)
VISITOR_PHONE = '13900000000'
HOST_PHONE = '13800000123'
OTHER_HOST_PHONE = '13800000124'


class AiChatVisitorHostDisambiguationTests(unittest.TestCase):
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

    def chat(self, message, conversation_id=None):
        payload = {'message': message}
        if conversation_id:
            payload['conversation_id'] = conversation_id
        return self.client.post(
            '/ai/chat',
            json=payload,
            headers={'X-CSRF-Token': self.csrf()},
        )

    def make_house(self):
        building = self.business('building.save', {
            'community_id': 1, 'name': 'A栋', 'floors': 20,
        })['id']
        unit = self.business('unit.save', {
            'building_id': building, 'name': '1单元',
        })['id']
        return self.business('house.save', {
            'unit_id': unit,
            'room_no': 101,
            'area': '88',
            'usage': 'residential',
            'occupancy': 'vacant',
        })['id']

    def make_person(self, phone):
        return self.business('person.save', {
            'community_id': 1,
            'name': '王五',
            'phone': phone,
        })['id']

    def test_host_phone_followup_never_overwrites_visitor_phone(self):
        house_id = self.make_house()
        selected_host = self.make_person(HOST_PHONE)
        other_host = self.make_person(OTHER_HOST_PHONE)
        self.business('relation.bind', {
            'house_id': house_id,
            'person_id': selected_host,
            'kind': 'owner',
            'is_resident': True,
        })

        provider = BailianClient('http://agent.invalid', 'fixture-key', 'qwen-plus')
        self.app.extensions['dify'] = provider

        # Two same-name people are visible. The planner must stop locally before
        # any provider/tool write and preserve the visitor's own phone separately.
        provider_calls = []
        with patch.object(
            provider,
            '_request',
            side_effect=lambda *args, **kwargs: provider_calls.append((args, kwargs)),
        ):
            first = self.chat(
                f'登记访客李四来找王五，A栋101室，访客电话{VISITOR_PHONE}，明天下午两点'
            )

        self.assertEqual(first.status_code, 200, first.text[:1800])
        self.assertEqual(first.json.get('source'), 'planner', first.text[:1800])
        self.assertTrue(first.json.get('conversation_id'))
        self.assertEqual(provider_calls, [])
        self.assertIn('联系电话', first.json.get('answer', ''))
        with self.factory() as db:
            self.assertEqual(db.scalar(select(func.count(Visitor.id))), 0)

        # The follow-up phone belongs to the host, not the visitor. The complete
        # original visit request must resume and use this phone only to resolve
        # the same-name host. Final visitor.phone must remain VISITOR_PHONE.
        responses = iter([
            {'id': 'visitor-host-1', 'choices': [{'message': {'content': '我按住户联系电话核对被访人。'}}]},
            {'id': 'visitor-host-2', 'choices': [{'message': {'content': '我再核对目标房屋。'}}]},
            {'id': 'visitor-host-3', 'choices': [{'message': {'content': '对象已唯一，开始登记访客。'}}]},
            {'id': 'visitor-host-4', 'choices': [{'message': {'content': '访客登记已完成。'}}]},
        ])
        with patch.object(provider, '_request', side_effect=lambda *args, **kwargs: next(responses)):
            second = self.chat(
                f'住户联系电话{HOST_PHONE}',
                first.json['conversation_id'],
            )

        self.assertEqual(second.status_code, 200, second.text[:1800])
        self.assertNotEqual(second.json.get('source'), 'planner', second.text[:1800])
        self.assertEqual(second.json['conversation_id'], first.json['conversation_id'])

        with self.factory() as db:
            visitors = list(db.scalars(select(Visitor).where(Visitor.house_id == house_id)))
            self.assertEqual(len(visitors), 1)
            visitor = visitors[0]
            self.assertEqual(visitor.name, '李四')
            self.assertEqual(visitor.phone, VISITOR_PHONE)
            self.assertEqual(visitor.host_person_id, selected_host)
            self.assertNotEqual(visitor.host_person_id, other_host)
            audit = db.scalar(select(AuditLog).where(
                AuditLog.source == 'agent',
                AuditLog.action == 'visitor.create',
                AuditLog.status == 'success',
            ).order_by(AuditLog.id.desc()))
            self.assertIsNotNone(audit)


if __name__ == '__main__':
    unittest.main()
