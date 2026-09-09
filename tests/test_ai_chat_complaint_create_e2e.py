import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import func, select
from werkzeug.security import generate_password_hash

from app import create_app
from database_fixture import test_database
from dify_client import BailianClient
from models import AuditLog, Complaint, User


PW = 'Fixture-only-ai-chat-complaint-421!'
HASH = generate_password_hash(PW)


class AiChatComplaintCreateEndToEndTests(unittest.TestCase):
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

    def chat(self, message):
        return self.client.post(
            '/ai/chat',
            json={'message': message},
            headers={'X-CSRF-Token': self.csrf()},
        )

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
        return self.business('house.save', {
            'unit_id': unit_id,
            'room_no': room_no,
            'area': '88',
            'usage': 'residential',
            'occupancy': 'owner_occupied',
        })['id']

    def test_chat_resolves_requested_house_and_persists_only_user_complaint_facts(self):
        target_house = self.make_house('23栋', 311)
        other_house = self.make_house('24栋', 311)
        provider = BailianClient('http://agent.invalid', 'fixture-key', 'qwen-plus')
        self.app.extensions['dify'] = provider
        responses = iter([
            {'id': 'complaint-e2e-1', 'choices': [{'message': {'content': '我先核对房屋。'}}]},
            {'id': 'complaint-e2e-2', 'choices': [{'message': {'content': '开始登记投诉。'}}]},
            {'id': 'complaint-e2e-3', 'choices': [{'message': {'content': '投诉已登记。'}}]},
        ])

        with patch.object(provider, '_request', side_effect=lambda *args, **kwargs: next(responses)):
            response = self.chat('23栋311投诉晚上施工太吵')

        self.assertEqual(response.status_code, 200, response.text[:1800])
        self.assertNotEqual(response.json.get('source'), 'planner', response.text[:1800])
        with self.factory() as db:
            rows = list(db.scalars(select(Complaint).order_by(Complaint.id)))
            self.assertEqual(len(rows), 1)
            complaint = rows[0]
            self.assertEqual(complaint.house_id, target_house)
            self.assertNotEqual(complaint.house_id, other_house)
            self.assertEqual(complaint.title, '噪音投诉')
            self.assertEqual(complaint.content, '晚上施工太吵')
            self.assertEqual(complaint.category, '噪音')
            self.assertEqual(complaint.status, 'open')

            self.assertEqual(
                db.scalar(select(func.count(Complaint.id)).where(Complaint.house_id == other_house)),
                0,
            )
            audit_row = db.scalar(select(AuditLog).where(
                AuditLog.source == 'agent',
                AuditLog.action == 'complaint.create',
                AuditLog.status == 'success',
            ).order_by(AuditLog.id.desc()))
            self.assertIsNotNone(audit_row)


if __name__ == '__main__':
    unittest.main()
