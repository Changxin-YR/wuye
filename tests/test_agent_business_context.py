import unittest
from types import SimpleNamespace

from flask import Flask, g

import agent_business_context as business_context
import agent_planner_state
from agent_business_context import repair_business_current_plan
from agent_planner import plan_request


class BusinessTargetMemoryTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        business_context._CURRENT.clear()
        agent_planner_state._PENDING.clear()

    def call(self, message, result, authorized, conversation_id=None, auth_version=1):
        payload = {'message': message}
        if conversation_id:
            payload['conversation_id'] = conversation_id
        with self.app.test_request_context('/ai/chat', method='POST', json=payload):
            g.user = SimpleNamespace(id=7, auth_version=auth_version)
            return repair_business_current_plan(message, result, authorized)

    def plan(self, message, authorized, conversation_id=None, auth_version=1):
        payload = {'message': message}
        if conversation_id:
            payload['conversation_id'] = conversation_id
        with self.app.test_request_context('/ai/chat', method='POST', json=payload):
            g.user = SimpleNamespace(id=7, auth_version=auth_version)
            return plan_request(message, authorized, {})

    def test_unbound_vehicle_read_binds_to_returned_conversation_and_resumes_archive(self):
        first = {
            'action': 'TOOL', 'intent': 'vehicle.search',
            'candidates': ['vehicle.search'], 'missing_fields': [],
            'entity_status': 'RESOLVED', 'arguments': {'plate': '浙A12345'},
        }
        self.call('查一下车牌浙A12345的车辆', first, {'vehicle.search'})

        second = {
            'action': 'CLARIFY', 'intent': 'vehicle.archive',
            'candidates': ['vehicle.archive'], 'missing_fields': ['vehicle'],
            'entity_status': 'MISSING', 'arguments': {},
        }
        repaired = self.call(
            '把刚才那辆车归档', second,
            {'vehicle.search', 'vehicle.archive'}, conversation_id='conv-1',
        )
        self.assertEqual(repaired['action'], 'TOOL')
        self.assertEqual(repaired['entity_status'], 'RESOLVE_FIRST')
        self.assertEqual(repaired['arguments']['plate'], '浙A12345')
        self.assertEqual(repaired['candidates'], ['vehicle.search', 'vehicle.archive'])

    def test_complaint_context_requires_confirmation_and_never_uses_vehicle_target(self):
        complaint = {
            'action': 'TOOL', 'intent': 'complaint.search',
            'candidates': ['complaint.search'], 'missing_fields': [],
            'entity_status': 'RESOLVED', 'arguments': {'complaint_id': 23},
        }
        self.call('查投诉#23', complaint, {'complaint.search'})
        close = {
            'action': 'CLARIFY', 'intent': 'complaint.close',
            'candidates': ['complaint.close'], 'missing_fields': ['complaint'],
            'entity_status': 'MISSING', 'arguments': {},
        }
        repaired = self.call(
            '把刚才这条投诉关闭', close,
            {'complaint.search', 'complaint.close'}, conversation_id='conv-2',
        )
        self.assertEqual(repaired['action'], 'CONFIRM')
        self.assertEqual(repaired['entity_status'], 'RESOLVE_FIRST')
        self.assertEqual(repaired['arguments']['complaint_id'], 23)
        self.assertEqual(repaired['arguments']['id'], 23)

        business_context._CURRENT.clear()
        vehicle = {
            'action': 'TOOL', 'intent': 'vehicle.search',
            'candidates': ['vehicle.search'], 'missing_fields': [],
            'entity_status': 'RESOLVED', 'arguments': {'plate': '粤B88888'},
        }
        self.call('查粤B88888', vehicle, {'vehicle.search'})
        untouched = self.call(
            '关闭刚才那条投诉', close,
            {'complaint.search', 'complaint.close'}, conversation_id='conv-x',
        )
        self.assertEqual(untouched['action'], 'CLARIFY')
        self.assertEqual(untouched['missing_fields'], ['complaint'])
        self.assertNotIn('plate', untouched['arguments'])

    def test_partial_bill_context_never_satisfies_missing_bill_target(self):
        broad_bill_read = {
            'action': 'TOOL', 'intent': 'billing.unpaid',
            'candidates': ['billing.unpaid'], 'missing_fields': [],
            'entity_status': 'RESOLVED', 'arguments': {'month': '2026-09'},
        }
        self.call('查本月欠费', broad_bill_read, {'billing.unpaid'})
        payment = {
            'action': 'CLARIFY', 'intent': 'payment.record',
            'candidates': ['payment.record'], 'missing_fields': ['bill'],
            'entity_status': 'MISSING', 'arguments': {},
        }
        repaired = self.call(
            '给刚才这张账单登记收款', payment,
            {'billing.unpaid', 'payment.record'}, conversation_id='conv-bill',
        )
        self.assertEqual(repaired['action'], 'CLARIFY')
        self.assertEqual(repaired['missing_fields'], ['bill'])
        self.assertEqual(repaired['arguments'].get('period'), '2026-09')
        self.assertNotIn('bill_id', repaired['arguments'])

    def test_auth_version_and_conversation_boundaries_do_not_reuse_target(self):
        vehicle = {
            'action': 'TOOL', 'intent': 'vehicle.search',
            'candidates': ['vehicle.search'], 'missing_fields': [],
            'entity_status': 'RESOLVED', 'arguments': {'plate': '沪A12345'},
        }
        self.call('查沪A12345', vehicle, {'vehicle.search'})
        archive = {
            'action': 'CLARIFY', 'intent': 'vehicle.archive',
            'candidates': ['vehicle.archive'], 'missing_fields': ['vehicle'],
            'entity_status': 'MISSING', 'arguments': {},
        }
        bound = self.call(
            '归档刚才那辆车', archive,
            {'vehicle.search', 'vehicle.archive'}, conversation_id='conv-a',
        )
        self.assertEqual(bound['arguments']['plate'], '沪A12345')

        other_conversation = self.call(
            '归档刚才那辆车', archive,
            {'vehicle.search', 'vehicle.archive'}, conversation_id='conv-b',
        )
        self.assertEqual(other_conversation['action'], 'CLARIFY')
        self.assertNotIn('plate', other_conversation['arguments'])

        changed_auth = self.call(
            '归档刚才那辆车', archive,
            {'vehicle.search', 'vehicle.archive'}, conversation_id='conv-a', auth_version=2,
        )
        self.assertEqual(changed_auth['action'], 'CLARIFY')
        self.assertNotIn('plate', changed_auth['arguments'])

    def test_real_planner_reuses_explicit_visitor_id_for_contextual_checkin(self):
        authorized = {'visitor.search', 'visitor.checkin'}
        first = self.plan('查询访客记录#23', authorized)
        self.assertEqual(first['intent'], 'visitor.search')
        self.assertEqual(first['arguments']['id'], 23)

        second = self.plan('确认刚才的访客进入', authorized, conversation_id='conv-visitor')
        self.assertEqual(second['intent'], 'visitor.checkin')
        self.assertEqual(second['action'], 'TOOL')
        self.assertEqual(second['entity_status'], 'RESOLVE_FIRST')
        self.assertEqual(second['arguments']['id'], 23)
        self.assertEqual(second['arguments']['status'], 'registered')
        self.assertEqual(second['candidates'], ['visitor.search', 'visitor.checkin'])
        self.assertNotIn('version', second['arguments'])
        self.assertNotIn('name', second['arguments'])
        self.assertNotIn('visitor_name', second['arguments'])
        self.assertNotIn('phone', second['arguments'])

    def test_real_planner_reuses_explicit_bill_for_contextual_void(self):
        authorized = {'bill.search', 'bill.void'}
        first = self.plan('查询账单#23', authorized)
        self.assertEqual(first['intent'], 'bill.search')
        self.assertEqual(first['arguments']['bill_id'], 23)

        second = self.plan(
            '把刚才这张账单作废，原因是重复出账',
            authorized,
            conversation_id='conv-bill-void',
        )
        self.assertEqual(second['intent'], 'bill.void')
        self.assertEqual(second['action'], 'CONFIRM')
        self.assertEqual(second['entity_status'], 'SERVER_OWNED')
        self.assertEqual(second['arguments']['bill_id'], 23)
        self.assertEqual(second['arguments']['reason'], '重复出账')
        self.assertNotIn('id', second['arguments'])
        self.assertNotIn('version', second['arguments'])

    def test_real_planner_reuses_vehicle_plate_without_exposing_internal_id(self):
        authorized = {'vehicle.search', 'vehicle.archive'}
        first = self.plan('查一下车牌粤A12345的车辆', authorized)
        self.assertEqual(first['intent'], 'vehicle.search')
        self.assertEqual(first['arguments']['plate'], '粤A12345')

        second = self.plan('把刚才那辆车归档', authorized, conversation_id='conv-real')
        self.assertEqual(second['intent'], 'vehicle.archive')
        self.assertEqual(second['arguments']['plate'], '粤A12345')
        self.assertNotIn('id', second['arguments'])
        self.assertNotIn('version', second['arguments'])
        self.assertIn('vehicle.search', second['candidates'])


if __name__ == '__main__':
    unittest.main()
