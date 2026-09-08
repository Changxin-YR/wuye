import hashlib
import json
import secrets
import unittest
import uuid
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from app import create_app
from database_fixture import test_database
from dify_client import BailianClient
from models import AiGrant, Building, Community, Notice, User, utcnow


PW = 'Fixture-only-294!'
HASH = generate_password_hash(PW)


class NoticeReadBuildingScopeTests(unittest.TestCase):
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
            db.add(User(username='admin', password_hash=HASH, role=0, real_name='管理员', phone='13800000001'))
            db.commit()
        self.login('admin')

    def tearDown(self):
        self.app.extensions['db_engine'].dispose()
        self.temp.cleanup()

    def csrf(self, client=None):
        client = client or self.client
        client.get('/auth/login')
        with client.session_transaction() as session:
            return session['csrf_token']

    def login(self, username, client=None):
        client = client or self.client
        response = client.post('/auth/login', data={
            'csrf_token': self.csrf(client),
            'username': username,
            'password': PW,
        })
        self.assertEqual(response.status_code, 302, response.text)

    def business(self, command, data):
        response = self.client.post(
            '/api/business/' + command,
            json={'data': data, 'confirmed': True},
            headers={'X-CSRF-Token': self.csrf()},
        )
        self.assertEqual(response.status_code, 200, response.text[:1200])
        return response.json

    def grant(self, user_id):
        token = secrets.token_urlsafe(32)
        with self.factory() as db:
            user = db.get(User, user_id)
            db.add(AiGrant(
                id=str(uuid.uuid4()),
                user_id=user_id,
                auth_version=user.auth_version,
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                expires_at=utcnow() + timedelta(minutes=3),
            ))
            db.commit()
        return token

    def lookup_response(self, token, command, args):
        return self.app.test_client().post('/api/agent/tools', json={
            'request_token': token,
            'operation': 'lookup',
            'command': command,
            'arguments_json': json.dumps(args, ensure_ascii=False),
        })

    def lookup(self, token, command, args):
        response = self.lookup_response(token, command, args)
        self.assertEqual(response.status_code, 200, response.text[:1200])
        return response.json

    def create_building(self, community_id, name):
        return self.business('building.save', {
            'community_id': community_id,
            'name': name,
            'floors': 20,
        })['id']

    def create_notice(self, community_id, building_id, title):
        return self.business('notice.save', {
            'community_id': community_id,
            'building_id': building_id,
            'title': title,
            'content': title + '内容',
        })['id']

    def test_building_name_read_includes_community_notice_and_target_building_only(self):
        building3 = self.create_building(1, '3栋')
        building4 = self.create_building(1, '4栋')
        community_notice = self.create_notice(1, None, '全区停水')
        building3_notice = self.create_notice(1, building3, '3栋电梯检修')
        building4_notice = self.create_notice(1, building4, '4栋消防检查')
        token = self.grant(1)

        result = self.lookup(token, 'notice.read', {'building_name': '三栋'})
        ids = {row['id'] for row in result['items']}
        self.assertEqual(ids, {community_notice, building3_notice})
        self.assertNotIn(building4_notice, ids)

        by_id = self.lookup(token, 'notice.read', {'building_id': building3})
        self.assertEqual({row['id'] for row in by_id['items']}, {community_notice, building3_notice})

    def test_same_building_name_across_communities_requires_disambiguation(self):
        first = self.create_building(1, '3栋')
        second_community = self.business('community.save', {
            'name': '第二小区',
            'address': '测试地址2号',
            'phone': '057100000002',
        })['id']
        self.create_building(second_community, '3栋')
        self.create_notice(1, first, '第一小区3栋公告')
        token = self.grant(1)

        ambiguous = self.lookup_response(token, 'notice.read', {'building_name': '3号楼'})
        self.assertEqual(ambiguous.status_code, 409, ambiguous.text[:1200])

        scoped = self.lookup(token, 'notice.read', {'community_id': 1, 'building_name': '3号楼'})
        self.assertEqual([row['title'] for row in scoped['items']], ['第一小区3栋公告'])

    def test_building_scope_cannot_use_alias_to_read_another_building(self):
        building3 = self.create_building(1, '3栋')
        building4 = self.create_building(1, '4栋')
        self.create_notice(1, building3, '3栋公告')
        self.create_notice(1, building4, '4栋公告')
        staff_id = self.business('staff.create', {
            'username': 'notice_building_staff',
            'password': PW,
            'real_name': '3栋管家',
            'phone': '13800000992',
            'role_codes': ['building_manager'],
            'scope_kind': 'building',
            'community_id': 1,
            'building_id': building3,
        })['id']
        token = self.grant(staff_id)

        in_scope = self.lookup(token, 'notice.read', {'building_name': '三号楼'})
        self.assertEqual([row['title'] for row in in_scope['items']], ['3栋公告'])

        out_of_scope = self.lookup_response(token, 'notice.read', {'building_name': '四栋'})
        self.assertEqual(out_of_scope.status_code, 404, out_of_scope.text[:1200])


class NoticeReadPlannerFallbackTests(unittest.TestCase):
    def test_model_omission_keeps_planner_building_target(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        seen = []
        hint = {
            'action': 'TOOL',
            'intent': 'notice.read',
            'candidates': ['notice.read'],
            'arguments': {'community_id': 1, 'building_name': '3栋'},
            'tool_call': None,
        }
        from dify_client import _PLANNER_HINT
        token = _PLANNER_HINT.set(hint)
        responses = iter([
            {'id': 'one', 'choices': [{'message': {'content': '我来查一下'}}]},
            {'id': 'two', 'choices': [{'message': {'content': '已查询'}}]},
        ])
        try:
            with patch.object(client, '_request', side_effect=lambda *args, **kwargs: next(responses)):
                def callback(args):
                    seen.append(dict(args))
                    return {'ok': True, 'code': 'SUCCESS', 'data': {'items': []}, 'terminal': True}

                result = client.chat('查一下三栋的通知', 'property:1:v1', tool_callback=callback)
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual(result['execution_state'], 'LOOKUP_ONLY')
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]['operation'], 'lookup')
        self.assertEqual(seen[0]['command'], 'notice.read')
        self.assertEqual(
            json.loads(seen[0]['arguments_json']),
            {'community_id': 1, 'building_name': '3栋'},
        )


if __name__ == '__main__':
    unittest.main()
