import json
import unittest
from unittest.mock import patch

from agent_planner import plan_request
from dify_client import BailianClient, _PLANNER_HINT


def tool_call(call_id, operation, command, arguments):
    return {
        'id': call_id,
        'type': 'function',
        'function': {
            'name': 'property_agent_tool',
            'arguments': json.dumps({
                'operation': operation,
                'command': command,
                'arguments_json': json.dumps(arguments, ensure_ascii=False),
            }, ensure_ascii=False),
        },
    }


class LeaseCheckoutResolveTests(unittest.TestCase):
    AUTHORIZED = {'lease.search', 'lease.checkout'}

    def test_complete_checkout_resolves_active_lease_before_confirmation(self):
        plan = plan_request('王五已经搬走了，办理退租，原因合同到期', self.AUTHORIZED, {})
        self.assertEqual((plan['intent'], plan['action']), ('lease.checkout', 'CONFIRM'))
        self.assertEqual(plan['entity_status'], 'RESOLVE_FIRST')
        self.assertEqual(plan['candidates'], ['lease.search', 'lease.checkout'])
        self.assertEqual(plan['arguments']['person_name'], '王五')
        self.assertEqual(plan['arguments']['status'], 'active')
        self.assertEqual(plan['arguments']['reason'], '合同到期')
        self.assertNotIn('id', plan['arguments'])
        self.assertNotIn('version', plan['arguments'])

    def test_checkout_without_reason_stays_clarify(self):
        plan = plan_request('王五已经搬走了，办理退租', self.AUTHORIZED, {})
        self.assertEqual((plan['intent'], plan['action']), ('lease.checkout', 'CLARIFY'))
        self.assertIn('reason', plan['missing_fields'])
        self.assertEqual(plan['arguments']['person_name'], '王五')
        self.assertNotIn('id', plan['arguments'])
        self.assertNotIn('version', plan['arguments'])

    def test_model_cannot_guess_lease_id_version_or_reason(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        callbacks = []
        responses = iter([
            {'id': 'lc-1', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('bad-lc-1', 'propose', 'lease.checkout', {
                    'id': 999, 'version': 77, 'reason': '模型猜测原因',
                })],
            }}]},
            {'id': 'lc-2', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('bad-lc-2', 'propose', 'lease.checkout', {
                    'id': 998, 'version': 76, 'reason': '另一个原因',
                })],
            }}]},
            {'id': 'lc-3', 'choices': [{'message': {'role': 'assistant', 'content': '需要当前登录人员确认后才会办理退租。'}}]},
        ])

        def tool(args):
            callbacks.append(dict(args))
            params = json.loads(args['arguments_json'])
            if args['command'] == 'lease.search':
                self.assertEqual(args['operation'], 'lookup')
                self.assertEqual(params, {'person_name': '王五', 'status': 'active'})
                return {
                    'ok': True, 'code': 'SUCCESS',
                    'data': {'items': [{
                        'id': 51, 'version': 3, 'house_id': 31,
                        'status': 'active',
                    }]},
                    'terminal': False,
                }
            self.assertEqual(args['command'], 'lease.checkout')
            self.assertEqual(args['operation'], 'propose')
            self.assertEqual(params, {'id': 51, 'version': 3, 'reason': '合同到期'})
            return {
                'ok': True, 'code': 'CONFIRMATION_REQUIRED',
                'data': {'status': 'pending', 'id': 'action-lease-1'},
                'terminal': True,
            }

        token = _PLANNER_HINT.set({
            'action': 'CONFIRM',
            'intent': 'lease.checkout',
            'entity_status': 'RESOLVE_FIRST',
            'candidates': ['lease.search', 'lease.checkout'],
            'arguments': {
                'person_name': '王五', 'status': 'active', 'reason': '合同到期',
                'id': 123456, 'version': 88,
            },
            'tool_call': {
                'operation': 'propose', 'command': 'lease.checkout',
                'arguments_json': json.dumps({
                    'id': 123456, 'version': 88, 'reason': '伪造',
                }, ensure_ascii=False),
            },
        })
        try:
            with patch.object(client, '_request', side_effect=lambda *a, **k: next(responses)):
                result = client.chat(
                    '王五已经搬走了，办理退租，原因合同到期',
                    'property:1:v1',
                    tool_callback=tool,
                )
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual([item['command'] for item in callbacks], ['lease.search', 'lease.checkout'])
        dumped = json.dumps(callbacks, ensure_ascii=False)
        for guessed in ('999', '998', '77', '76', '123456', '88', '模型猜测原因', '另一个原因', '伪造'):
            self.assertNotIn(guessed, dumped)
        self.assertEqual(result['execution_state'], 'PENDING_CONFIRMATION')

    def test_ambiguous_active_leases_stop_before_proposal(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        callbacks = []
        responses = iter([
            {'id': 'lca-1', 'choices': [{'message': {'role': 'assistant', 'content': '我先核对活动租约。'}}]},
            {'id': 'lca-2', 'choices': [{'message': {'role': 'assistant', 'content': '找到多条活动租约，请补充房屋信息。'}}]},
        ])

        def tool(args):
            callbacks.append(dict(args))
            self.assertEqual(args['command'], 'lease.search')
            return {
                'ok': True, 'code': 'SUCCESS',
                'data': {'items': [
                    {'id': 51, 'version': 1, 'house_id': 31, 'status': 'active'},
                    {'id': 52, 'version': 1, 'house_id': 32, 'status': 'active'},
                ]},
                'terminal': False,
            }

        token = _PLANNER_HINT.set({
            'action': 'CONFIRM', 'intent': 'lease.checkout', 'entity_status': 'RESOLVE_FIRST',
            'candidates': ['lease.search', 'lease.checkout'],
            'arguments': {'person_name': '王五', 'status': 'active', 'reason': '合同到期'},
            'tool_call': None,
        })
        try:
            with patch.object(client, '_request', side_effect=lambda *a, **k: next(responses)):
                result = client.chat(
                    '王五已经搬走了，办理退租，原因合同到期',
                    'property:1:v1',
                    tool_callback=tool,
                )
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual([item['command'] for item in callbacks], ['lease.search'])
        self.assertEqual(result['execution_state'], 'LOOKUP_ONLY')


if __name__ == '__main__':
    unittest.main()
