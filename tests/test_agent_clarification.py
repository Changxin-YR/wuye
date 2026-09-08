import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask, g

from agent_planner import clear_pending_plan, plan_request
from dify_client import BailianClient, _PLANNER_HINT


class PlannerClarificationTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.context = {
            'writable_communities': [
                {'id': 1, 'name': '春风苑'},
                {'id': 2, 'name': '滨河苑'},
            ]
        }

    def plan(self, text, conversation_id=None):
        payload = {'message': text}
        if conversation_id:
            payload['conversation_id'] = conversation_id
        with self.app.test_request_context('/ai/chat', method='POST', json=payload):
            g.user = SimpleNamespace(id=301, auth_version=1)
            return plan_request(text, {'notice.save'}, self.context)

    def tearDown(self):
        with self.app.test_request_context('/ai/chat', method='POST', json={'message': 'clear'}):
            g.user = SimpleNamespace(id=301, auth_version=1)
            clear_pending_plan()
        with self.app.test_request_context('/ai/chat', method='POST', json={'message': 'clear', 'conversation_id': 'conv-a'}):
            g.user = SimpleNamespace(id=301, auth_version=1)
            clear_pending_plan()

    def test_notice_scope_question_is_specific_and_pending_plan_survives(self):
        first = self.plan('发布公告：明天停水')
        self.assertEqual(first['action'], 'CLARIFY')
        self.assertIn('哪个小区', first['clarification_text'])
        second = self.plan('春风苑', 'conv-a')
        self.assertEqual(second['intent'], 'notice.save')
        self.assertEqual(second['action'], 'TOOL')
        self.assertEqual(second['arguments']['community_id'], 1)

    def test_clarification_text_is_additive_and_does_not_change_state_contract(self):
        result = self.plan('发布公告：明天停水')
        self.assertEqual(result['action'], 'CLARIFY')
        self.assertNotIn('pending_action', result)
        self.assertTrue(result['clarification_text'])


class ProviderClarificationTests(unittest.TestCase):
    def test_direct_provider_returns_planner_question_without_network_or_tools(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        calls = []
        token = _PLANNER_HINT.set({
            'action': 'CLARIFY',
            'intent': 'notice.save',
            'clarification_text': '这条公告要发布到哪个小区？直接告诉我小区名称即可。',
            'candidates': ['notice.save'],
        })
        try:
            with patch.object(client, '_request', side_effect=AssertionError('clarification must not call model')):
                result = client.chat('发布公告：明天停水', 'property:301:v1', tool_callback=lambda arg: calls.append(arg))
        finally:
            _PLANNER_HINT.reset(token)
        self.assertEqual(result['answer'], '这条公告要发布到哪个小区？直接告诉我小区名称即可。')
        self.assertEqual(result['execution_state'], 'NOT_EXECUTED')
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
