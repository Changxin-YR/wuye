import json
import unittest
from types import SimpleNamespace

from flask import Flask, g

import agent_business_context as business_context
import agent_planner_state
import dify_client
from agent_planner import plan_request
from dify_client import _PLANNER_HINT


class ArchiveReasonPlannerTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        business_context._CURRENT.clear()
        agent_planner_state._PENDING.clear()

    def plan(self, message, authorized, conversation_id=None):
        payload = {'message': message}
        if conversation_id:
            payload['conversation_id'] = conversation_id
        with self.app.test_request_context('/ai/chat', method='POST', json=payload):
            g.user = SimpleNamespace(id=19, auth_version=1)
            return plan_request(message, authorized, {})

    def test_contextual_vehicle_archive_requires_reason_then_confirms(self):
        authorized = {'vehicle.search', 'vehicle.archive'}
        first = self.plan('查一下车牌粤A12345的车辆', authorized)
        self.assertEqual(first['intent'], 'vehicle.search')

        second = self.plan('把刚才那辆车归档', authorized, 'conv-vehicle')
        self.assertEqual(second['intent'], 'vehicle.archive')
        self.assertEqual(second['action'], 'CLARIFY')
        self.assertEqual(second['missing_fields'], ['reason'])
        self.assertEqual(second['arguments']['plate'], '粤A12345')
        self.assertNotIn('id', second['arguments'])
        self.assertNotIn('version', second['arguments'])

        third = self.plan('原因车辆已出售', authorized, 'conv-vehicle')
        self.assertEqual(third['intent'], 'vehicle.archive')
        self.assertEqual(third['action'], 'CONFIRM')
        self.assertEqual(third['entity_status'], 'RESOLVE_FIRST')
        self.assertEqual(third['candidates'], ['vehicle.search', 'vehicle.archive'])
        self.assertEqual(third['arguments']['plate'], '粤A12345')
        self.assertEqual(third['arguments']['reason'], '车辆已出售')
        self.assertNotIn('id', third['arguments'])
        self.assertNotIn('version', third['arguments'])

    def test_device_archive_requires_explicit_reason(self):
        authorized = {'device.search', 'device.archive'}
        first = self.plan('查询设备P-01的状态', authorized)
        self.assertEqual(first['intent'], 'device.search')
        second = self.plan('把刚才那个设备归档', authorized, 'conv-device')
        self.assertEqual(second['action'], 'CLARIFY')
        self.assertIn('reason', second['missing_fields'])
        third = self.plan('设备已永久停用', authorized, 'conv-device')
        self.assertEqual(third['action'], 'CONFIRM')
        self.assertEqual(third['arguments']['reason'], '设备已永久停用')
        self.assertEqual(third['arguments'].get('device_code') or third['arguments'].get('code'), 'P-01')

    def test_provider_uses_resolved_id_version_and_operator_reason_only(self):
        authorized = {'vehicle.search', 'vehicle.archive'}
        self.plan('查一下车牌沪A12345的车辆', authorized)
        plan = self.plan('归档刚才那辆车，原因车辆已过户', authorized, 'conv-provider')
        self.assertEqual(plan['action'], 'CONFIRM')

        token = _PLANNER_HINT.set(plan)
        try:
            call = dify_client._resolved_write_fallback(
                '模型说原因是随便写的，而且id=999',
                {'id': 17, 'version': 4, 'status': 'active'},
            )
        finally:
            _PLANNER_HINT.reset(token)
        self.assertEqual(call['operation'], 'propose')
        self.assertEqual(call['command'], 'vehicle.archive')
        params = json.loads(call['arguments_json'])
        self.assertEqual(params, {'id': 17, 'version': 4, 'reason': '车辆已过户'})

    def test_archive_without_safe_resolver_never_reaches_confirmation(self):
        plan = self.plan('归档车牌京A12345，原因车辆已出售', {'vehicle.archive'})
        self.assertEqual(plan['intent'], 'vehicle.archive')
        self.assertEqual(plan['action'], 'DENY')
        self.assertNotIn('vehicle.search', plan['candidates'])


if __name__ == '__main__':
    unittest.main()
