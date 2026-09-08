import unittest
from types import SimpleNamespace

from flask import Flask, g

from agent_planner import clear_pending_plan, plan_request


class PendingPlanTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SECRET_KEY'] = 'pending-plan-tests'
        self.authorized = {'notice.save', 'notice.read', 'order.search'}
        self.context = {'writable_communities': [{'id': 1, 'name': 'A小区'}, {'id': 2, 'name': 'B小区'}]}

    def plan(self, text, auth_version=1, context=None):
        with self.app.test_request_context('/ai/chat', method='POST', json={'message': text}):
            g.user = SimpleNamespace(id=7, auth_version=auth_version)
            return plan_request(text, self.authorized, context if context is not None else self.context)

    def clear(self):
        with self.app.test_request_context('/ai/chat', method='POST', json={'message': 'clear'}):
            g.user = SimpleNamespace(id=7, auth_version=1)
            clear_pending_plan()

    def tearDown(self):
        self.clear()

    def test_notice_scope_followup_fills_pending_without_overwriting_content(self):
        first = self.plan('发布公告：明天停水')
        self.assertEqual(first['action'], 'CLARIFY')
        self.assertEqual(first['intent'], 'notice.save')
        self.assertIn('community_id', first['missing_fields'])

        second = self.plan('A小区')
        self.assertEqual(second['action'], 'TOOL')
        self.assertEqual(second['intent'], 'notice.save')
        self.assertEqual(second['arguments']['community_id'], 1)
        self.assertEqual(second['arguments']['notice_content'], '明天停水')
        self.assertNotEqual(second['arguments']['notice_content'], 'A小区')

    def test_new_business_intent_clears_old_pending_plan(self):
        self.assertEqual(self.plan('发布公告：明天停水')['action'], 'CLARIFY')
        result = self.plan('我的工单有哪些')
        self.assertEqual(result['intent'], 'order.search')
        self.assertEqual(result['action'], 'TOOL')

    def test_auth_version_change_does_not_reuse_pending_plan(self):
        self.assertEqual(self.plan('发布公告：明天停水', auth_version=1)['action'], 'CLARIFY')
        result = self.plan('A小区', auth_version=2)
        self.assertEqual(result['action'], 'ANSWER')
        self.assertEqual(result['intent'], 'unknown')


if __name__ == '__main__':
    unittest.main()
