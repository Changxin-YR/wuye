import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import func, select
from werkzeug.security import generate_password_hash

from app import create_app
from database_fixture import test_database
from models import Inspection, User


PW = 'Fixture-only-inspection-guards-951!'
HASH = generate_password_hash(PW)


class InspectionCreateBusinessGuardTests(unittest.TestCase):
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
        self.building_id = self.business('building.save', {
            'community_id': 1, 'name': 'A栋', 'floors': 20,
        })['id']
        self.device_id = self.business('device.save', {
            'community_id': 1,
            'building_id': self.building_id,
            'code': 'P-GUARD-01',
            'name': '巡检门禁水泵',
            'category': 'pump',
            'location': 'A栋设备间',
            'status': 'normal',
        })['id']
        self.engineer_id = self.business('staff.create', {
            'username': 'inspection_guard_engineer',
            'password': PW,
            'real_name': '王工',
            'phone': '13800000861',
            'role_codes': ['engineer'],
            'scope_kind': 'building',
            'community_id': 1,
            'building_id': self.building_id,
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

    def business_response(self, command, data):
        return self.client.post(
            '/api/business/' + command,
            json={'data': data, 'confirmed': True},
            headers={'X-CSRF-Token': self.csrf()},
        )

    def business(self, command, data, expected=200):
        response = self.business_response(command, data)
        self.assertEqual(response.status_code, expected, response.text[:1800])
        return response.json

    @staticmethod
    def local_iso(delta):
        local_now = datetime.now(timezone.utc) + timedelta(hours=8) + delta
        return local_now.replace(tzinfo=None, second=0, microsecond=0).isoformat(timespec='minutes')

    def inspection_payload(self, due_at):
        return {
            'device_id': self.device_id,
            'assignee_id': self.engineer_id,
            'due_at': due_at,
            'checklist': '检查振动和温度',
        }

    def test_past_due_time_is_rejected_without_creating_task(self):
        response = self.business_response(
            'inspection.create',
            self.inspection_payload(self.local_iso(timedelta(hours=-2))),
        )
        self.assertEqual(response.status_code, 400, response.text[:1800])
        self.assertIn('时间', response.get_data(as_text=True))
        with self.factory() as db:
            self.assertEqual(db.scalar(select(func.count(Inspection.id))), 0)

    def test_identical_pending_assignment_is_rejected_instead_of_duplicated(self):
        payload = self.inspection_payload(self.local_iso(timedelta(days=1)))
        first = self.business_response('inspection.create', payload)
        self.assertEqual(first.status_code, 200, first.text[:1800])
        second = self.business_response('inspection.create', payload)
        self.assertEqual(second.status_code, 409, second.text[:1800])
        self.assertIn('巡检', second.get_data(as_text=True))
        with self.factory() as db:
            rows = list(db.scalars(select(Inspection).where(
                Inspection.device_id == self.device_id,
                Inspection.assignee_id == self.engineer_id,
                Inspection.status == 'pending',
            )))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].checklist, '检查振动和温度')


if __name__ == '__main__':
    unittest.main()
