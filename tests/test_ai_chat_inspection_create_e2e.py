import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import select
from werkzeug.security import generate_password_hash

from app import create_app
from database_fixture import test_database
from dify_client import BailianClient
from models import AuditLog, Device, Inspection, User


PW = 'Fixture-only-ai-chat-inspection-341!'
HASH = generate_password_hash(PW)


def expected_due_at():
    local_today = (datetime.now(timezone.utc) + timedelta(hours=8)).date()
    return datetime.combine(local_today + timedelta(days=1), time(hour=14, minute=0))


class AiChatInspectionCreateEndToEndTests(unittest.TestCase):
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

    def create_building(self, name):
        return self.business('building.save', {
            'community_id': 1,
            'name': name,
            'floors': 20,
        })['id']

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

    def test_chat_resolves_only_eligible_in_scope_inspector_and_persists_task(self):
        building_a = self.create_building('A栋')
        building_b = self.create_building('B栋')

        inspector_a = self.create_staff(
            'engineer_a', '王工', '13800000501', ['engineer'], building_a,
        )
        inspector_b = self.create_staff(
            'engineer_b', '王工', '13800000502', ['engineer'], building_b,
        )
        service_a = self.create_staff(
            'service_a', '王工', '13800000503', ['customer_service'], building_a,
        )

        device_id = self.business('device.save', {
            'community_id': 1,
            'building_id': building_a,
            'code': 'P-01',
            'name': '循环水泵',
            'category': 'pump',
            'location': 'A栋设备间',
            'status': 'normal',
        })['id']

        provider = BailianClient('http://agent.invalid', 'fixture-key', 'qwen-plus')
        responses = iter([
            {'id': 'inspection-e2e-1', 'choices': [{'message': {'content': '我先核对设备。'}}]},
            {'id': 'inspection-e2e-2', 'choices': [{'message': {'content': '我再核对巡检人员。'}}]},
            {'id': 'inspection-e2e-3', 'choices': [{'message': {'content': '开始创建巡检任务。'}}]},
            {'id': 'inspection-e2e-4', 'choices': [{'message': {'content': '巡检任务已创建。'}}]},
        ])
        self.app.extensions['dify'] = provider

        message = '给P-01安排巡检，明天下午两点前完成，交给王工，检查振动和温度'
        with patch.object(provider, '_request', side_effect=lambda *args, **kwargs: next(responses)):
            response = self.chat(message)

        self.assertEqual(response.status_code, 200, response.text[:1800])
        self.assertNotEqual(response.json.get('source'), 'planner', response.text[:1800])

        with self.factory() as db:
            rows = list(db.scalars(select(Inspection).where(Inspection.device_id == device_id)))
            self.assertEqual(len(rows), 1)
            inspection = rows[0]
            self.assertEqual(inspection.status, 'pending')
            self.assertEqual(inspection.assignee_id, inspector_a)
            self.assertNotEqual(inspection.assignee_id, inspector_b)
            self.assertNotEqual(inspection.assignee_id, service_a)
            self.assertEqual(inspection.checklist, '振动和温度')
            self.assertEqual(inspection.due_at, expected_due_at())

            device = db.get(Device, device_id)
            self.assertEqual(device.building_id, building_a)
            self.assertEqual(device.code, 'P-01')

            audit_row = db.scalar(select(AuditLog).where(
                AuditLog.source == 'agent',
                AuditLog.action == 'inspection.create',
                AuditLog.status == 'success',
            ).order_by(AuditLog.id.desc()))
            self.assertIsNotNone(audit_row)


if __name__ == '__main__':
    unittest.main()
