import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import select
from werkzeug.security import generate_password_hash

from app import create_app
from database_fixture import test_database
from dify_client import BailianClient
from models import Inspection, User


PW = 'Fixture-only-device-current-object-731!'
HASH = generate_password_hash(PW)


class AiChatDeviceCurrentObjectTests(unittest.TestCase):
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
        self.device_one = self.business('device.save', {
            'community_id': 1, 'building_id': self.building_id,
            'code': 'P-01', 'name': '一号循环水泵', 'category': 'pump',
            'location': 'A栋设备间', 'status': 'normal',
        })['id']
        self.device_two = self.business('device.save', {
            'community_id': 1, 'building_id': self.building_id,
            'code': 'P-02', 'name': '二号循环水泵', 'category': 'pump',
            'location': 'A栋设备间', 'status': 'normal',
        })['id']
        self.engineer = self.business('staff.create', {
            'username': 'engineer_current', 'password': PW,
            'real_name': '王工', 'phone': '13800000801',
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

    def test_exact_device_read_becomes_server_owned_current_object_for_followup_inspection(self):
        read_responses = iter([
            {'id': 'device-read-1', 'choices': [{'message': {'content': '我先核对设备。'}}]},
            {'id': 'device-read-2', 'choices': [{'message': {'content': 'P-01 当前状态正常。'}}]},
        ])
        with patch.object(self.provider, '_request', side_effect=lambda *a, **k: next(read_responses)):
            first = self.chat('查询设备P-01的状态')
        self.assertEqual(first.status_code, 200, first.text[:1800])
        self.assertTrue(first.json.get('conversation_id'))

        create_responses = iter([
            {'id': 'device-follow-1', 'choices': [{'message': {'content': '核对刚才的设备。'}}]},
            {'id': 'device-follow-2', 'choices': [{'message': {'content': '核对巡检人员。'}}]},
            {'id': 'device-follow-3', 'choices': [{'message': {'content': '创建巡检任务。'}}]},
            {'id': 'device-follow-4', 'choices': [{'message': {'content': '巡检任务已创建。'}}]},
        ])
        with patch.object(self.provider, '_request', side_effect=lambda *a, **k: next(create_responses)):
            second = self.chat(
                '给它安排巡检，明天下午两点前完成，交给王工，检查振动和温度',
                first.json['conversation_id'],
            )
        self.assertEqual(second.status_code, 200, second.text[:1800])

        with self.factory() as db:
            rows = list(db.scalars(select(Inspection).order_by(Inspection.id)))
            self.assertEqual(len(rows), 1)
            inspection = rows[0]
            self.assertEqual(inspection.device_id, self.device_one)
            self.assertNotEqual(inspection.device_id, self.device_two)
            self.assertEqual(inspection.assignee_id, self.engineer)
            self.assertIn('振动', inspection.checklist)
            self.assertIn('温度', inspection.checklist)


if __name__ == '__main__':
    unittest.main()
