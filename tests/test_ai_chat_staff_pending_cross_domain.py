import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import func, select
from werkzeug.security import generate_password_hash

from app import create_app
from database_fixture import test_database
from dify_client import BailianClient
from models import Complaint, Inspection, User


PW = 'Fixture-only-ai-chat-staff-cross-domain-617!'
HASH = generate_password_hash(PW)


class AiChatStaffPendingCrossDomainTests(unittest.TestCase):
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
        self.building_id, self.house_id = self.create_scope()

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

    def create_staff(self, username, real_name, phone, role_codes):
        return self.business('staff.create', {
            'username': username,
            'password': PW,
            'real_name': real_name,
            'phone': phone,
            'role_codes': role_codes,
            'scope_kind': 'building',
            'community_id': 1,
            'building_id': self.building_id,
        })['id']

    def create_complaint(self):
        return int(self.business('complaint.create', {
            'house_id': self.house_id,
            'title': '楼道噪音',
            'content': '夜间持续有噪音，请协调处理',
            'category': '邻里投诉',
        })['id'])

    def create_device(self):
        return int(self.business('device.save', {
            'community_id': 1,
            'building_id': self.building_id,
            'code': 'P-01',
            'name': '循环水泵',
            'category': 'pump',
            'location': 'A栋设备间',
            'status': 'normal',
        })['id'])

    def test_explicit_inspection_intent_replaces_ambiguous_complaint_pending(self):
        self.create_staff('service_a', '李客服', '13800000601', ['customer_service'])
        self.create_staff('service_b', '李客服', '13800000602', ['customer_service'])
        engineer = self.create_staff('engineer_one', '王工', '13800000603', ['engineer'])
        complaint_id = self.create_complaint()
        device_id = self.create_device()

        ambiguous = iter([
            {'id': 'cross-c1', 'choices': [{'message': {'content': '先核对投诉。'}}]},
            {'id': 'cross-c2', 'choices': [{'message': {'content': '再核对处理人员。'}}]},
            {'id': 'cross-c3', 'choices': [{'message': {'content': '同名处理人员，需要补充信息。'}}]},
        ])
        with patch.object(self.provider, '_request', side_effect=lambda *a, **k: next(ambiguous)):
            first = self.chat(f'把投诉#{complaint_id}分给李客服')
        self.assertEqual(first.status_code, 200, first.text[:1800])
        self.assertTrue(first.json.get('conversation_id'))

        inspection_responses = iter([
            {'id': 'cross-i1', 'choices': [{'message': {'content': '核对设备。'}}]},
            {'id': 'cross-i2', 'choices': [{'message': {'content': '核对巡检人员。'}}]},
            {'id': 'cross-i3', 'choices': [{'message': {'content': '创建巡检任务。'}}]},
            {'id': 'cross-i4', 'choices': [{'message': {'content': '巡检任务已创建。'}}]},
        ])
        with patch.object(self.provider, '_request', side_effect=lambda *a, **k: next(inspection_responses)):
            second = self.chat(
                '给P-01安排巡检，明天下午两点前完成，交给王工，检查振动和温度',
                first.json['conversation_id'],
            )
        self.assertEqual(second.status_code, 200, second.text[:1800])

        with self.factory() as db:
            complaint = db.get(Complaint, complaint_id)
            self.assertEqual(complaint.status, 'open')
            self.assertIsNone(complaint.assignee_id)
            rows = list(db.scalars(select(Inspection).where(Inspection.device_id == device_id)))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].assignee_id, engineer)
            self.assertIn('振动', rows[0].checklist)

    def test_explicit_complaint_intent_replaces_ambiguous_inspection_pending(self):
        self.create_staff('engineer_a', '王工', '13800000701', ['engineer'])
        self.create_staff('engineer_b', '王工', '13800000702', ['engineer'])
        handler = self.create_staff('service_one', '赵客服', '13800000703', ['customer_service'])
        complaint_id = self.create_complaint()
        device_id = self.create_device()

        ambiguous = iter([
            {'id': 'cross-i-a1', 'choices': [{'message': {'content': '先核对设备。'}}]},
            {'id': 'cross-i-a2', 'choices': [{'message': {'content': '再核对巡检人员。'}}]},
            {'id': 'cross-i-a3', 'choices': [{'message': {'content': '同名巡检人员，需要补充信息。'}}]},
        ])
        with patch.object(self.provider, '_request', side_effect=lambda *a, **k: next(ambiguous)):
            first = self.chat('给P-01安排巡检，明天下午两点前完成，交给王工，检查振动和温度')
        self.assertEqual(first.status_code, 200, first.text[:1800])
        self.assertTrue(first.json.get('conversation_id'))

        complaint_responses = iter([
            {'id': 'cross-c-a1', 'choices': [{'message': {'content': '核对投诉。'}}]},
            {'id': 'cross-c-a2', 'choices': [{'message': {'content': '核对处理人员。'}}]},
            {'id': 'cross-c-a3', 'choices': [{'message': {'content': '执行投诉分配。'}}]},
            {'id': 'cross-c-a4', 'choices': [{'message': {'content': '投诉已分配。'}}]},
        ])
        with patch.object(self.provider, '_request', side_effect=lambda *a, **k: next(complaint_responses)):
            second = self.chat(
                f'把投诉#{complaint_id}分给赵客服',
                first.json['conversation_id'],
            )
        self.assertEqual(second.status_code, 200, second.text[:1800])

        with self.factory() as db:
            complaint = db.get(Complaint, complaint_id)
            self.assertEqual(complaint.status, 'assigned')
            self.assertEqual(complaint.assignee_id, handler)
            self.assertEqual(
                db.scalar(select(func.count(Inspection.id)).where(Inspection.device_id == device_id)),
                0,
            )


if __name__ == '__main__':
    unittest.main()
