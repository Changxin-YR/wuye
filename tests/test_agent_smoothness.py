import unittest
from types import SimpleNamespace

from flask import Flask, g

from agent_planner import clear_pending_plan, plan_request


ALL = {
    'notice.save', 'notice.read',
    'order.search', 'order.accept', 'order.progress', 'order.close', 'order.cancel',
    'complaint.search', 'complaint.resolve', 'complaint.close',
    'visitor.search', 'visitor.checkin', 'visitor.checkout', 'visitor.cancel',
    'parking.search', 'vehicle.search', 'parking.release',
    'device.search', 'device.save', 'device.archive',
    'inspection.search', 'inspection.complete',
    'payment.search', 'payment.reverse',
    'billing.unpaid', 'bill.void',
}


class SmoothContextPlannerTests(unittest.TestCase):
    def test_recent_visitor_is_resolved_before_asking_for_database_id(self):
        plan = plan_request('确认刚才的访客进入', ALL, {})
        self.assertEqual(plan['intent'], 'visitor.checkin')
        self.assertEqual(plan['action'], 'TOOL')
        self.assertEqual(plan['entity_status'], 'RESOLVE_FIRST')
        self.assertEqual(plan['arguments']['status'], 'registered')
        self.assertEqual(plan['candidates'], ['visitor.search', 'visitor.checkin'])

    def test_recent_visitor_checkout_uses_inside_status(self):
        plan = plan_request('刚才那个访客已经离开了', ALL, {})
        self.assertEqual(plan['intent'], 'visitor.checkout')
        self.assertEqual(plan['action'], 'TOOL')
        self.assertEqual(plan['arguments']['status'], 'inside')
        self.assertIn('visitor.search', plan['candidates'])

    def test_contextual_complaint_close_still_requires_confirmation(self):
        plan = plan_request('这个投诉回访确认后结案', ALL, {})
        self.assertEqual(plan['intent'], 'complaint.close')
        self.assertEqual(plan['action'], 'CONFIRM')
        self.assertEqual(plan['arguments']['status'], 'resolved')
        self.assertEqual(plan['candidates'], ['complaint.search', 'complaint.close'])

    def test_previous_payment_resolves_then_confirms(self):
        plan = plan_request('撤回上一笔收款记录', ALL, {})
        self.assertEqual(plan['intent'], 'payment.reverse')
        self.assertEqual(plan['action'], 'CONFIRM')
        self.assertEqual(plan['entity_status'], 'RESOLVE_FIRST')
        self.assertEqual(plan['candidates'], ['payment.search', 'payment.reverse'])

    def test_smoothness_never_expands_authorization(self):
        plan = plan_request('确认刚才的访客进入', {'visitor.search'}, {})
        self.assertNotEqual(plan['action'], 'TOOL')
        self.assertNotIn('visitor.checkin', plan.get('candidates', []))

    def test_device_code_survives_into_legacy_builder_compatibility_field(self):
        plan = plan_request('登记A栋水泵设备，编号P-01', ALL | {'building.search'}, {})
        self.assertEqual(plan['intent'], 'device.save')
        self.assertEqual(plan['arguments']['device_code'], 'P-01')
        self.assertEqual(plan['arguments']['code'], 'P-01')
        self.assertEqual(plan['arguments']['space_code'], 'P-01')


class ConversationPendingPlanTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.context = {'writable_communities': [{'id': 1, 'name': 'A小区'}, {'id': 2, 'name': 'B小区'}]}
        self.auth = {'notice.save'}

    def plan(self, text, conversation_id=None):
        payload = {'message': text}
        if conversation_id:
            payload['conversation_id'] = conversation_id
        with self.app.test_request_context('/ai/chat', method='POST', json=payload):
            g.user = SimpleNamespace(id=91, auth_version=1)
            return plan_request(text, self.auth, self.context)

    def clear(self, conversation_id=None):
        payload = {'message': 'clear'}
        if conversation_id:
            payload['conversation_id'] = conversation_id
        with self.app.test_request_context('/ai/chat', method='POST', json=payload):
            g.user = SimpleNamespace(id=91, auth_version=1)
            clear_pending_plan()

    def tearDown(self):
        self.clear()
        self.clear('conv-a')
        self.clear('conv-b')

    def test_initial_pending_plan_binds_to_returned_conversation(self):
        first = self.plan('发布公告：明天停水')
        self.assertEqual(first['action'], 'CLARIFY')
        second = self.plan('A小区', 'conv-a')
        self.assertEqual(second['action'], 'TOOL')
        self.assertEqual(second['intent'], 'notice.save')
        self.assertEqual(second['arguments']['community_id'], 1)
        other = self.plan('B小区', 'conv-b')
        self.assertEqual(other['action'], 'ANSWER')
        self.assertEqual(other['intent'], 'unknown')

    def test_user_can_cancel_pending_business_flow(self):
        self.assertEqual(self.plan('发布公告：明天停水')['action'], 'CLARIFY')
        cancelled = self.plan('算了', 'conv-a')
        self.assertEqual(cancelled['intent'], 'cancelled')
        self.assertEqual(cancelled['action'], 'ANSWER')
        after = self.plan('A小区', 'conv-a')
        self.assertEqual(after['intent'], 'unknown')


if __name__ == '__main__':
    unittest.main()
