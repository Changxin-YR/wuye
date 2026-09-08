import unittest
from types import SimpleNamespace

from flask import Flask, g

from agent_planner import clear_pending_plan, plan_request
from agent_planner_state import visitor_expected_at


AUTHORIZED = {'visitor.create', 'person.search', 'house.search'}


class VisitorSlotPlannerTests(unittest.TestCase):
    def test_supplied_visitor_and_host_are_not_asked_twice(self):
        plan = plan_request('登记访客李四来找王五', AUTHORIZED, {})
        self.assertEqual(plan['intent'], 'visitor.create')
        self.assertEqual(plan['action'], 'CLARIFY')
        self.assertNotIn('visitor', plan['missing_fields'])
        self.assertNotIn('host_person', plan['missing_fields'])
        self.assertEqual(plan['arguments']['visitor_name'], '李四')
        self.assertEqual(plan['arguments']['person_name'], '王五')
        self.assertEqual(plan['arguments']['purpose'], '拜访王五')
        self.assertIn('house', plan['missing_fields'])
        self.assertIn('phone', plan['missing_fields'])
        self.assertIn('expected_at', plan['missing_fields'])

    def test_complete_business_facts_move_to_multi_resolve_not_internal_id_prompt(self):
        plan = plan_request(
            '登记访客李四来找王五，A栋101室，电话13800000000，明天下午两点',
            AUTHORIZED,
            {},
        )
        self.assertEqual(plan['action'], 'TOOL')
        self.assertEqual(plan['entity_status'], 'RESOLVE_MULTI')
        self.assertEqual(plan['missing_fields'], [])
        self.assertNotIn('id', plan['missing_fields'])
        self.assertNotIn('version', plan['missing_fields'])
        self.assertEqual(plan['arguments']['building_name'], 'A栋')
        self.assertEqual(plan['arguments']['room_no'], 101)
        self.assertEqual(plan['arguments']['phone'], '13800000000')
        self.assertTrue(plan['arguments']['expected_at'].endswith('T14:00'))

    def test_bare_clock_does_not_guess_the_visit_date(self):
        self.assertIsNone(visitor_expected_at('下午两点'))
        self.assertTrue(visitor_expected_at('明天下午两点').endswith('T14:00'))
        self.assertEqual(visitor_expected_at('2026-09-10 09:30'), '2026-09-10T09:30')


class VisitorSlotConversationTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.user = SimpleNamespace(id=811, auth_version=1)

    def plan(self, text, conversation_id=None):
        payload = {'message': text}
        if conversation_id:
            payload['conversation_id'] = conversation_id
        with self.app.test_request_context('/ai/chat', method='POST', json=payload):
            g.user = self.user
            return plan_request(text, AUTHORIZED, {})

    def tearDown(self):
        for cid in (None, 'visitor-conv'):
            payload = {'message': 'clear'}
            if cid:
                payload['conversation_id'] = cid
            with self.app.test_request_context('/ai/chat', method='POST', json=payload):
                g.user = self.user
                clear_pending_plan()

    def test_short_followup_keeps_previous_visitor_facts(self):
        first = self.plan('登记访客李四来找王五，A栋101室，电话13800000000')
        self.assertEqual(first['action'], 'CLARIFY')
        self.assertEqual(first['missing_fields'], ['expected_at'])
        self.assertIn('什么时候到', first['clarification_text'])

        second = self.plan('明天下午两点', 'visitor-conv')
        self.assertEqual(second['action'], 'TOOL')
        self.assertEqual(second['entity_status'], 'RESOLVE_MULTI')
        self.assertEqual(second['arguments']['visitor_name'], '李四')
        self.assertEqual(second['arguments']['person_name'], '王五')
        self.assertEqual(second['arguments']['building_name'], 'A栋')
        self.assertEqual(second['arguments']['room_no'], 101)
        self.assertEqual(second['arguments']['phone'], '13800000000')
        self.assertTrue(second['arguments']['expected_at'].endswith('T14:00'))


if __name__ == '__main__':
    unittest.main()
