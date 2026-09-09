import unittest
from tempfile import TemporaryDirectory

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from werkzeug.exceptions import HTTPException
from werkzeug.security import generate_password_hash

from app import create_app
from models import Community, User
from property_service import PropertyService
from agent_tools import agent_request_key


class AgentIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.app = create_app({'TESTING': True, 'DATABASE_URL': 'sqlite+pysqlite:///:memory:', 'SECRET_KEY': 'test', 'UPLOAD_FOLDER': self.temp.name})
        self.engine = self.app.extensions['db_engine']
        self.db = self.app.extensions['db_session']
        with self.db() as db:
            db.add(User(id=1, username='admin', password_hash=generate_password_hash('pw'), role=0))
            db.commit()

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    def test_domain_request_key_replays_and_rejects_changed_payload(self):
        payload = {'name': '幂等小区', 'address': '测试地址', 'phone': '13800000000'}
        with self.db() as db:
            actor = db.get(User, 1)
            first = PropertyService(db, actor, source='agent').run('community.save', payload, request_key='agent:1:test:community')
            db.commit()
            second = PropertyService(db, actor, source='agent').run('community.save', payload, request_key='agent:1:test:community')
            self.assertEqual(first, second)
            with self.assertRaises(HTTPException) as ctx:
                PropertyService(db, actor, source='agent').run('community.save', {'name': '不同小区', 'address': '测试地址', 'phone': '13800000000'}, request_key='agent:1:test:community')
            self.assertEqual(ctx.exception.code, 409)
            self.assertEqual(db.query(Community).filter(Community.name == '幂等小区').count(), 1)

    def test_agent_key_is_stable_and_bounded(self):
        key = agent_request_key(42, 'conversation-' + 'x' * 36, 'request-' + 'y' * 60, 'order.create')
        self.assertLessEqual(len(key), 100)
        self.assertEqual(key, agent_request_key(42, 'conversation-' + 'x' * 36, 'request-' + 'y' * 60, 'order.create'))
