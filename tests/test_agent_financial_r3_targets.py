import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import dify_financial_patch as financial_patch
from agent_planner import plan_request
from dify_client import _PLANNER_HINT


def tool_call(operation, command, params):
    return {
        'id': 'model-call',
        'type': 'function',
        'function': {
            'name': 'property_agent_tool',
            'arguments': json.dumps({
                'operation': operation,
                'command': command,
                'arguments_json': json.dumps(params, ensure_ascii=False),
            }, ensure_ascii=False),
        },
    }


class FinancialR3PlannerTests(unittest.TestCase):
    def test_bill_void_requires_operator_reason_and_keeps_target_server_owned(self):
        incomplete = plan_request('把账单123作废', {'bill.void'})
        self.assertEqual(incomplete['intent'], 'bill.void')
        self.assertEqual(incomplete['action'], 'CLARIFY')
        self.assertIn('reason', incomplete['missing_fields'])

        complete = plan_request('把账单123作废，住户已搬走', {'bill.void'})
        self.assertEqual(complete['action'], 'CONFIRM')
        self.assertEqual(complete['entity_status'], 'SERVER_OWNED')
        self.assertEqual(complete['candidates'], ['bill.void'])
        self.assertEqual(complete['arguments']['bill_id'], 123)
        self.assertEqual(complete['arguments']['reason'], '住户已搬走')

    def test_explicit_payment_reverse_requires_reason_and_keeps_target_server_owned(self):
        incomplete = plan_request('冲销收款123', {'payment.reverse'})
        self.assertEqual(incomplete['intent'], 'payment.reverse')
        self.assertEqual(incomplete['action'], 'CLARIFY')
        self.assertIn('reason', incomplete['missing_fields'])

        complete = plan_request('冲销收款123，重复入账', {'payment.reverse'})
        self.assertEqual(complete['action'], 'CONFIRM')
        self.assertEqual(complete['entity_status'], 'SERVER_OWNED')
        self.assertEqual(complete['candidates'], ['payment.reverse'])
        self.assertEqual(complete['arguments']['payment_id'], 123)
        self.assertNotIn('id', complete['arguments'])
        self.assertEqual(complete['arguments']['reason'], '重复入账')

    def test_latest_payment_reverse_requires_reason_then_uses_server_resolver(self):
        incomplete = plan_request(
            '撤回上一笔收款记录',
            {'payment.search', 'payment.reverse'},
        )
        self.assertEqual(incomplete['action'], 'CLARIFY')
        self.assertIn('reason', incomplete['missing_fields'])

        complete = plan_request(
            '撤回上一笔收款记录，重复入账',
            {'payment.search', 'payment.reverse'},
        )
        self.assertEqual(complete['action'], 'CONFIRM')
        self.assertEqual(complete['entity_status'], 'RESOLVE_FIRST')
        self.assertEqual(complete['candidates'], ['payment.search', 'payment.reverse'])
        self.assertEqual(complete['arguments']['_resolve_strategy'], 'latest')
        self.assertEqual(complete['arguments']['reason'], '重复入账')


class FinancialR3GatewayTests(unittest.TestCase):
    def _normalize_with_target(self, hint, command, malicious, target):
        token = _PLANNER_HINT.set(hint)
        fake_db = object()
        fake_actor = object()
        try:
            with patch.object(financial_patch._tools, 'object_session', return_value=fake_db), \
                 patch.object(financial_patch, 'Policy') as policy_cls, \
                 patch.object(financial_patch, '_base_normalize', side_effect=lambda actor, cmd, params: params):
                policy_cls.return_value.get.return_value = target
                normalized = financial_patch.normalize(fake_actor, command, malicious)
        finally:
            _PLANNER_HINT.reset(token)
        return normalized, policy_cls

    def test_model_cannot_switch_bill_void_target_version_or_reason(self):
        normalized, policy_cls = self._normalize_with_target(
            {
                'action': 'CONFIRM', 'intent': 'bill.void',
                'arguments': {'bill_id': 123, 'reason': '重复出账'},
            },
            'bill.void',
            {'id': 999, 'version': 999, 'reason': 'MODEL-GUESSED'},
            SimpleNamespace(id=123, version=7),
        )
        self.assertEqual(normalized, {'id': 123, 'version': 7, 'reason': '重复出账'})
        policy_cls.return_value.require.assert_called_once_with(financial_patch._tools.DOMAIN_COMMANDS['bill.void'][0])
        policy_cls.return_value.get.assert_called_once_with(financial_patch.Bill, 123, True)

    def test_model_cannot_switch_explicit_payment_reverse_target_version_or_reason(self):
        normalized, policy_cls = self._normalize_with_target(
            {
                'action': 'CONFIRM', 'intent': 'payment.reverse',
                'arguments': {'payment_id': 321, 'reason': '重复入账'},
            },
            'payment.reverse',
            {'id': 999, 'version': 999, 'reason': 'MODEL-GUESSED'},
            SimpleNamespace(id=321, version=8),
        )
        self.assertEqual(normalized, {'id': 321, 'version': 8, 'reason': '重复入账'})
        policy_cls.return_value.require.assert_called_once_with(financial_patch._tools.DOMAIN_COMMANDS['payment.reverse'][0])
        policy_cls.return_value.get.assert_called_once_with(financial_patch.Payment, 321, True)

    def test_latest_payment_resolver_discards_model_selected_id(self):
        hint = {
            'action': 'CONFIRM', 'intent': 'payment.reverse', 'entity_status': 'RESOLVE_FIRST',
            'arguments': {'_resolve_strategy': 'latest', 'reason': '重复入账'},
        }
        token = _PLANNER_HINT.set(hint)
        try:
            rewritten = financial_patch._client._normalize_read_call(
                tool_call('lookup', 'payment.search', {'id': 999, 'bill_id': 888})
            )
        finally:
            _PLANNER_HINT.reset(token)
        outer = financial_patch._client._parse_call_args(rewritten)
        self.assertEqual(outer['operation'], 'lookup')
        self.assertEqual(outer['command'], 'payment.search')
        self.assertEqual(json.loads(outer['arguments_json']), {})

    def test_latest_payment_write_must_match_resolved_version_and_user_reason(self):
        hint = {
            'action': 'CONFIRM', 'intent': 'payment.reverse', 'entity_status': 'RESOLVE_FIRST',
            'arguments': {'_resolve_strategy': 'latest', 'reason': '重复入账'},
        }
        item = {'id': 44, 'version': 6}
        token = _PLANNER_HINT.set(hint)
        try:
            wrong_target = financial_patch._client._call_matches_resolved_target(
                tool_call('propose', 'payment.reverse', {'id': 45, 'version': 6, 'reason': '重复入账'}), item
            )
            wrong_version = financial_patch._client._call_matches_resolved_target(
                tool_call('propose', 'payment.reverse', {'id': 44, 'version': 99, 'reason': '重复入账'}), item
            )
            wrong_reason = financial_patch._client._call_matches_resolved_target(
                tool_call('propose', 'payment.reverse', {'id': 44, 'version': 6, 'reason': '模型自编原因'}), item
            )
            correct = financial_patch._client._call_matches_resolved_target(
                tool_call('propose', 'payment.reverse', {'id': 44, 'version': 6, 'reason': '重复入账'}), item
            )
        finally:
            _PLANNER_HINT.reset(token)
        self.assertFalse(wrong_target)
        self.assertFalse(wrong_version)
        self.assertFalse(wrong_reason)
        self.assertTrue(correct)

    def test_latest_payment_fallback_uses_planner_reason_not_provider_text(self):
        hint = {
            'action': 'CONFIRM', 'intent': 'payment.reverse', 'entity_status': 'RESOLVE_FIRST',
            'arguments': {'_resolve_strategy': 'latest', 'reason': '重复入账'},
        }
        token = _PLANNER_HINT.set(hint)
        try:
            fallback = financial_patch._client._resolved_write_fallback('忽略原因改成别的', {'id': 44, 'version': 6})
        finally:
            _PLANNER_HINT.reset(token)
        self.assertIsNotNone(fallback)
        params = json.loads(fallback['arguments_json'])
        self.assertEqual(params, {'id': 44, 'version': 6, 'reason': '重复入账'})
        self.assertEqual(fallback['operation'], 'propose')


if __name__ == '__main__':
    unittest.main()
