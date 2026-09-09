import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import func, select
from werkzeug.security import generate_password_hash

from app import create_app
from database_fixture import test_database
from dify_client import BailianClient
from models import AuditLog, Complaint, Inspection, User


PW = 'Fixture-only-ai-chat-staff-domains-541!'
HASH = generate_password_hash(PW)
COMPLAINT_TARGET_PHONE = '13800000301'
COMPLAINT_OTHER_PHONE = '13800000302'
INSPECTION_TARGET_PHONE = '13800000501'
INSPECTION_OTHER_PHONE = '13800000502'


class AiChatStaffDomainDisambiguationTests(unittest.TestCase):
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

    def create_staff(self, username, real_name, phone, role_codes, building_id):
        return self.business('staff.create', {
            'username': username,
            'password': PW,
            'real_name': real_name,
            'phone': phone,
            'role_codes': role_codes,
            'scope_kind': 'building',
            'community_id': 1,
            'building_id': building_id,
        })['id']

    def test_complaint_handler_phone_followup_resumes_original_assignment(self):
        building_id, house_id = self.create_scope()
        target = self.create_staff(
            'service_target', '李客服', COMPLAINT_TARGET_PHONE,
            ['customer_service'], building_id,
        )
        other = self.create_staff(
            'service_other', '李客服', COMPLAINT_OTHER_PHONE,
            ['customer_service'], building_id,
        )
        complaint_id = int(self.business('complaint.create', {
            'house_id': house_id,
            'title': '楼道噪音',
            'content': '夜间持续有噪音，请协调处理',
            'category': '邻里投诉',
        })['id'])

        first_responses = iter([
            {'id': 'complaint-staff-1', 'choices': [{'message': {'content': '我先核对投诉。'}}]},
            {'id': 'complaint-staff-2', 'choices': [{'message': {'content': '我再核对处理人员。'}}]},
            {'id': 'complaint-staff-3', 'choices': [{'message': {'content': '存在多位同名处理人员，需要补充信息。'}}]},
        ])
        with patch.object(self.provider, '_request', side_effect=lambda *args, **kwargs: next(first_responses)):
            first = self.chat(f'把投诉#{complaint_id}分给李客服')

        self.assertEqual(first.status_code, 200, first.text[:1800])
        self.assertTrue(first.json.get('conversation_id'))
        self.assertEqual(first.json.get('actions'), [])
        with self.factory() as db:
            complaint = db.get(Complaint, complaint_id)
            self.assertEqual(complaint.status, 'open')
            self.assertIsNone(complaint.assignee_id)

        second_responses = iter([
            {'id': 'complaint-staff-follow-1', 'choices': [{'message': {'content': '重新核对投诉。'}}]},
            {'id': 'complaint-staff-follow-2', 'choices': [{'message': {'content': '按联系电话核对处理人员。'}}]},
            {'id': 'complaint-staff-follow-3', 'choices': [{'message': {'content': '处理人员已唯一，执行分配。'}}]},
            {'id': 'complaint-staff-follow-4', 'choices': [{'message': {'content': '投诉分配已完成。'}}]},
        ])
        with patch.object(self.provider, '_request', side_effect=lambda *args, **kwargs: next(second_responses)):
            second = self.chat(
                f'投诉处理人员联系电话{COMPLAINT_TARGET_PHONE}',
                first.json['conversation_id'],
            )

        self.assertEqual(second.status_code, 200, second.text[:1800])
        self.assertEqual(second.json['conversation_id'], first.json['conversation_id'])
        with self.factory() as db:
            complaint = db.get(Complaint, complaint_id)
            self.assertEqual(complaint.status, 'assigned')
            self.assertEqual(complaint.assignee_id, target)
            self.assertNotEqual(complaint.assignee_id, other)
            audit = db.scalar(select(AuditLog).where(
                AuditLog.source == 'agent',
                AuditLog.action == 'complaint.assign',
                AuditLog.status == 'success',
            ).order_by(AuditLog.id.desc()))
            self.assertIsNotNone(audit)

    def test_inspector_phone_followup_resumes_original_inspection_creation(self):
        building_id, _ = self.create_scope()
        target = self.create_staff(
            'inspector_target', '王工', INSPECTION_TARGET_PHONE,
            ['engineer'], building_id,
        )
        other = self.create_staff(
            'inspector_other', '王工', INSPECTION_OTHER_PHONE,
            ['engineer'], building_id,
        )
        device_id = int(self.business('device.save', {
            'community_id': 1,
            'building_id': building_id,
            'code': 'P-01',
            'name': '循环水泵',
            'category': 'pump',
            'location': 'A栋设备间',
            'status': 'normal',
        })['id'])

        first_responses = iter([
            {'id': 'inspection-staff-1', 'choices': [{'message': {'content': '我先核对设备。'}}]},
            {'id': 'inspection-staff-2', 'choices': [{'message': {'content': '我再核对巡检人员。'}}]},
            {'id': 'inspection-staff-3', 'choices': [{'message': {'content': '存在多位同名巡检人员，需要补充信息。'}}]},
        ])
        request_text = '给P-01安排巡检，明天下午两点前完成，交给王工，检查振动和温度'
        with patch.object(self.provider, '_request', side_effect=lambda *args, **kwargs: next(first_responses)):
            first = self.chat(request_text)

        self.assertEqual(first.status_code, 200, first.text[:1800])
        self.assertTrue(first.json.get('conversation_id'))
        self.assertEqual(first.json.get('actions'), [])
        with self.factory() as db:
            self.assertEqual(
                db.scalar(select(func.count(Inspection.id)).where(Inspection.device_id == device_id)),
                0,
            )

        second_responses = iter([
            {'id': 'inspection-staff-follow-1', 'choices': [{'message': {'content': '重新核对设备。'}}]},
            {'id': 'inspection-staff-follow-2', 'choices': [{'message': {'content': '按联系电话核对巡检人员。'}}]},
            {'id': 'inspection-staff-follow-3', 'choices': [{'message': {'content': '巡检人员已唯一，创建任务。'}}]},
            {'id': 'inspection-staff-follow-4', 'choices': [{'message': {'content': '巡检任务已创建。'}}]},
        ])
        with patch.object(self.provider, '_request', side_effect=lambda *args, **kwargs: next(second_responses)):
            second = self.chat(
                f'巡检人员联系电话{INSPECTION_TARGET_PHONE}',
                first.json['conversation_id'],
            )

        self.assertEqual(second.status_code, 200, second.text[:1800])
        self.assertEqual(second.json['conversation_id'], first.json['conversation_id'])
        with self.factory() as db:
            rows = list(db.scalars(select(Inspection).where(Inspection.device_id == device_id)))
            self.assertEqual(len(rows), 1)
            inspection = rows[0]
            self.assertEqual(inspection.assignee_id, target)
            self.assertNotEqual(inspection.assignee_id, other)
            self.assertIn('振动', inspection.checklist)
            audit = db.scalar(select(AuditLog).where(
                AuditLog.source == 'agent',
                AuditLog.action == 'inspection.create',
                AuditLog.status == 'success',
            ).order_by(AuditLog.id.desc()))
            self.assertIsNotNone(audit)


if __name__ == '__main__':
    unittest.main()
