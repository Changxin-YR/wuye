import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import select
from werkzeug.security import generate_password_hash

from app import create_app
from database_fixture import test_database
from dify_client import BailianClient
from models import AuditLog, User, WorkOrder


PW = 'Fixture-only-ai-chat-order-staff-437!'
HASH = generate_password_hash(PW)
TARGET_PHONE = '13800000201'
OTHER_PHONE = '13800000202'


class AiChatOrderStaffDisambiguationTests(unittest.TestCase):
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
            '/ai/chat', json=payload,
            headers={'X-CSRF-Token': self.csrf()},
        )

    def create_scope(self):
        building = self.business('building.save', {
            'community_id': 1, 'name': 'A栋', 'floors': 20,
        })['id']
        unit = self.business('unit.save', {
            'building_id': building, 'name': '1单元',
        })['id']
        house = self.business('house.save', {
            'unit_id': unit, 'room_no': 101, 'area': '88',
            'usage': 'residential', 'occupancy': 'vacant',
        })['id']
        return building, house

    def create_engineer(self, username, phone, building_id):
        return self.business('staff.create', {
            'username': username,
            'password': PW,
            'real_name': '张师傅',
            'phone': phone,
            'role_codes': ['engineer'],
            'scope_kind': 'building',
            'community_id': 1,
            'building_id': building_id,
        })['id']

    def create_order(self, house_id):
        result = self.business('order.create', {
            'house_id': house_id,
            'title': '厨房漏水',
            'content': '厨房水管持续漏水，请安排维修',
            'type': '水电故障',
            'location': 'A栋101室厨房',
            'contact_name': '测试住户',
            'contact_phone': '13900000101',
        })
        with self.factory() as db:
            order = db.get(WorkOrder, int(result['id']))
            return order.id, order.order_no

    def test_staff_phone_followup_resumes_original_order_assignment(self):
        building_id, house_id = self.create_scope()
        target_worker = self.create_engineer('engineer_target', TARGET_PHONE, building_id)
        other_worker = self.create_engineer('engineer_other', OTHER_PHONE, building_id)
        order_id, order_no = self.create_order(house_id)

        provider = BailianClient('http://agent.invalid', 'fixture-key', 'qwen-plus')
        self.app.extensions['dify'] = provider

        first_responses = iter([
            {'id': 'order-staff-1', 'choices': [{'message': {'content': '我先核对工单。'}}]},
            {'id': 'order-staff-2', 'choices': [{'message': {'content': '我再核对维修人员。'}}]},
            {'id': 'order-staff-3', 'choices': [{'message': {'content': '存在多位同名维修人员，需要补充信息。'}}]},
        ])
        with patch.object(provider, '_request', side_effect=lambda *args, **kwargs: next(first_responses)):
            first = self.chat(f'把工单{order_no}派给张师傅')

        self.assertEqual(first.status_code, 200, first.text[:1800])
        self.assertTrue(first.json.get('conversation_id'))
        self.assertEqual(first.json.get('actions'), [])
        with self.factory() as db:
            order = db.get(WorkOrder, order_id)
            self.assertEqual(order.status, 0)
            self.assertIsNone(order.repairer_id)

        second_responses = iter([
            {'id': 'order-staff-follow-1', 'choices': [{'message': {'content': '我按联系电话重新核对工单。'}}]},
            {'id': 'order-staff-follow-2', 'choices': [{'message': {'content': '我按联系电话核对维修人员。'}}]},
            {'id': 'order-staff-follow-3', 'choices': [{'message': {'content': '维修人员已唯一，执行派单。'}}]},
            {'id': 'order-staff-follow-4', 'choices': [{'message': {'content': '工单派单已完成。'}}]},
        ])
        with patch.object(provider, '_request', side_effect=lambda *args, **kwargs: next(second_responses)):
            second = self.chat(
                f'维修人员联系电话{TARGET_PHONE}',
                first.json['conversation_id'],
            )

        self.assertEqual(second.status_code, 200, second.text[:1800])
        self.assertEqual(second.json['conversation_id'], first.json['conversation_id'])
        with self.factory() as db:
            order = db.get(WorkOrder, order_id)
            self.assertEqual(order.status, 1)
            self.assertEqual(order.repairer_id, target_worker)
            self.assertNotEqual(order.repairer_id, other_worker)
            audit = db.scalar(select(AuditLog).where(
                AuditLog.source == 'agent',
                AuditLog.action == 'order.assign',
                AuditLog.status == 'success',
            ).order_by(AuditLog.id.desc()))
            self.assertIsNotNone(audit)


if __name__ == '__main__':
    unittest.main()
