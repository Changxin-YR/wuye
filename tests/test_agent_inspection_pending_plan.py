import unittest
from types import SimpleNamespace

from flask import Flask, g

from agent_planner import clear_pending_plan, plan_request


class InspectionPendingPlanTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SECRET_KEY'] = 'inspection-pending-tests'
        self.authorized = {'device.search', 'inspection_staff.search', 'inspection.create'}

    def plan(self, text, auth_version=1):
        with self.app.test_request_context('/ai/chat', method='POST', json={'message': text}):
            g.user = SimpleNamespace(id=71, auth_version=auth_version)
            return plan_request(text, self.authorized, {})

    def clear(self):
        with self.app.test_request_context('/ai/chat', method='POST', json={'message': 'clear'}):
            g.user = SimpleNamespace(id=71, auth_version=1)
            clear_pending_plan()

    def tearDown(self):
        self.clear()

    def test_four_turn_inspection_request_keeps_business_facts_until_safe_resolution(self):
        first = self.plan('给P-01安排巡检')
        self.assertEqual((first['intent'], first['action']), ('inspection.create', 'CLARIFY'))
        self.assertEqual(first['arguments']['device_code'], 'P-01')
        self.assertEqual(first['missing_fields'], ['assignee', 'due_at', 'checklist'])

        second = self.plan('王工')
        self.assertEqual(second['action'], 'CLARIFY')
        self.assertEqual(second['arguments']['device_code'], 'P-01')
        self.assertEqual(second['arguments']['assignee_name'], '王工')
        self.assertEqual(second['missing_fields'], ['due_at', 'checklist'])

        third = self.plan('明天下午两点')
        self.assertEqual(third['action'], 'CLARIFY')
        self.assertEqual(third['arguments']['device_code'], 'P-01')
        self.assertEqual(third['arguments']['assignee_name'], '王工')
        self.assertIn('T14:00', third['arguments']['due_at'])
        self.assertEqual(third['missing_fields'], ['checklist'])

        fourth = self.plan('检查振动和温度')
        self.assertEqual(fourth['action'], 'TOOL')
        self.assertEqual(fourth['entity_status'], 'RESOLVE_MULTI')
        self.assertEqual(fourth['candidates'], ['device.search', 'inspection_staff.search', 'inspection.create'])
        self.assertEqual(fourth['arguments']['device_code'], 'P-01')
        self.assertEqual(fourth['arguments']['assignee_name'], '王工')
        self.assertIn('T14:00', fourth['arguments']['due_at'])
        self.assertEqual(fourth['arguments']['checklist'], '振动和温度')
        self.assertNotIn('device_id', fourth['arguments'])
        self.assertNotIn('assignee_id', fourth['arguments'])

    def test_auth_version_change_never_reuses_inspection_pending_state(self):
        self.assertEqual(self.plan('给P-01安排巡检', auth_version=1)['action'], 'CLARIFY')
        result = self.plan('王工', auth_version=2)
        self.assertEqual(result['intent'], 'unknown')
        self.assertEqual(result['action'], 'ANSWER')


if __name__ == '__main__':
    unittest.main()
