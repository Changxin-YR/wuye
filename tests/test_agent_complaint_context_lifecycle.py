import json
import unittest
from types import SimpleNamespace

from flask import Flask, g

import agent_business_context as business_context
import agent_planner_state
import dify_client
from agent_planner import plan_request
from dify_client import _PLANNER_HINT


AUTHORIZED = {'complaint.search', 'complaint.resolve', 'complaint.close'}


class ComplaintContextPlannerTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        business_context._CURRENT.clear()
        agent_planner_state._PENDING.clear()

    def plan(self, message, conversation_id=None):
        payload = {'message': message}
        if conversation_id:
            payload['conversation_id'] = conversation_id
        with self.app.test_request_context('/ai/chat', method='POST', json=payload):
            g.user = SimpleNamespace(id=37, auth_version=1)
            return plan_request(message, AUTHORIZED, {})

    def test_cached_complaint_resolve_is_always_resolver_first(self):
        first = self.plan('查询投诉#23状态')
        self.assertEqual(first['intent'], 'complaint.search')
        self.assertEqual(first['arguments']['complaint_id'], 23)

        second = self.plan('刚才这条投诉处理结果是已上门整改', 'conv-complaint')
        self.assertEqual(second['intent'], 'complaint.resolve')
        self.assertEqual(second['action'], 'TOOL')
        self.assertEqual(second['entity_status'], 'RESOLVE_FIRST')
        self.assertEqual(second['candidates'], ['complaint.search', 'complaint.resolve'])
        self.assertEqual(second['arguments']['complaint_id'], 23)
        self.assertEqual(second['arguments']['resolution'], '已上门整改')
        self.assertNotIn('version', second['arguments'])

    def test_close_keeps_resolved_state_filter_and_requires_confirmation(self):
        self.plan('查询投诉#31状态')
        plan = self.plan('这个投诉回访住户确认后结案', 'conv-close')
        self.assertEqual(plan['action'], 'CONFIRM')
        self.assertEqual(plan['entity_status'], 'RESOLVE_FIRST')
        self.assertEqual(plan['arguments']['complaint_id'], 31)
        self.assertEqual(plan['arguments']['status'], 'resolved')
        self.assertEqual(plan['arguments']['resolution'], '住户确认')

    def test_provider_cannot_switch_resolved_target_version_or_resolution(self):
        self.plan('查询投诉#41状态')
        plan = self.plan('刚才这条投诉处理结果是已上门整改', 'conv-provider')
        token = _PLANNER_HINT.set(plan)
        try:
            fallback = dify_client._resolved_write_fallback(
                '模型尝试改成别的处理结果',
                {'id': 41, 'version': 7, 'status': 'open'},
            )
            fake_call = {
                'function': {
                    'arguments': json.dumps({
                        'operation': 'execute',
                        'command': 'complaint.resolve',
                        'arguments_json': json.dumps({
                            'id': 41,
                            'version': 999,
                            'resolution': '模型伪造结果',
                        }, ensure_ascii=False),
                    }, ensure_ascii=False),
                }
            }
            self.assertFalse(dify_client._call_matches_resolved_target(
                fake_call, {'id': 41, 'version': 7}
            ))
        finally:
            _PLANNER_HINT.reset(token)

        params = json.loads(fallback['arguments_json'])
        self.assertEqual(fallback['operation'], 'execute')
        self.assertEqual(fallback['command'], 'complaint.resolve')
        self.assertEqual(params, {
            'id': 41, 'version': 7, 'resolution': '已上门整改'
        })


if __name__ == '__main__':
    unittest.main()
