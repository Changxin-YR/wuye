import json
import unittest
from unittest.mock import patch

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


class ResolverFallbackTests(unittest.TestCase):
    def test_contextual_lookup_is_synthesized_before_final_write(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        callbacks = []
        payloads = []
        responses = iter([
            {'id': 'round-1', 'choices': [{'message': {'role': 'assistant', 'content': '我来处理。'}}]},
            {'id': 'round-2', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('call-final', 'execute', 'visitor.checkin', {'id': 9, 'version': 2})],
            }}]},
            {'id': 'round-3', 'choices': [{'message': {'role': 'assistant', 'content': '访客已登记进入。'}}]},
        ])

        def request(method, path, payload=None):
            payloads.append(payload)
            return next(responses)

        def tool(args):
            callbacks.append(dict(args))
            if args['operation'] == 'lookup':
                return {
                    'ok': True, 'code': 'SUCCESS',
                    'data': {'items': [{'id': 9, 'version': 2, 'status': 'registered'}]},
                    'terminal': False,
                }
            return {
                'ok': True, 'code': 'SUCCESS',
                'data': {'status': 'executed', 'verification': {'id': 9}},
                'terminal': True,
            }

        token = _PLANNER_HINT.set({
            'action': 'TOOL', 'intent': 'visitor.checkin',
            'entity_status': 'RESOLVE_FIRST',
            'candidates': ['visitor.search', 'visitor.checkin'],
            'arguments': {'status': 'registered'}, 'tool_call': None,
        })
        try:
            with patch.object(client, '_request', side_effect=request):
                result = client.chat('确认刚才的访客进入', 'property:1:v1', tool_callback=tool)
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual(len(callbacks), 2)
        self.assertEqual(callbacks[0]['operation'], 'lookup')
        self.assertEqual(callbacks[0]['command'], 'visitor.search')
        self.assertEqual(json.loads(callbacks[0]['arguments_json']), {'status': 'registered'})
        self.assertEqual(callbacks[1]['operation'], 'execute')
        self.assertEqual(callbacks[1]['command'], 'visitor.checkin')
        self.assertEqual(result['execution_state'], 'EXECUTED')
        second_messages = payloads[1]['messages']
        self.assertTrue(any(
            item.get('role') == 'assistant' and item.get('tool_calls')
            and item['tool_calls'][0].get('id') == 'planner-resolver'
            for item in second_messages
        ))
        self.assertTrue(any(
            item.get('role') == 'tool' and item.get('tool_call_id') == 'planner-resolver'
            for item in second_messages
        ))

    def test_provider_cannot_guess_write_target_before_lookup(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        callbacks = []
        responses = iter([
            {'id': 'guess-1', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('guessed-write', 'execute', 'visitor.checkin', {'id': 777, 'version': 1})],
            }}]},
            {'id': 'guess-2', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('real-write', 'execute', 'visitor.checkin', {'id': 9, 'version': 2})],
            }}]},
            {'id': 'guess-3', 'choices': [{'message': {'role': 'assistant', 'content': '访客已进入。'}}]},
        ])

        def request(method, path, payload=None):
            return next(responses)

        def tool(args):
            callbacks.append(dict(args))
            if args['operation'] == 'lookup':
                return {
                    'ok': True, 'code': 'SUCCESS',
                    'data': {'items': [{'id': 9, 'version': 2, 'status': 'registered'}]},
                    'terminal': False,
                }
            self.assertEqual(json.loads(args['arguments_json'])['id'], 9)
            return {'ok': True, 'code': 'SUCCESS', 'terminal': True}

        token = _PLANNER_HINT.set({
            'action': 'TOOL', 'intent': 'visitor.checkin',
            'entity_status': 'RESOLVE_FIRST',
            'candidates': ['visitor.search', 'visitor.checkin'],
            'arguments': {'status': 'registered'}, 'tool_call': None,
        })
        try:
            with patch.object(client, '_request', side_effect=request):
                result = client.chat('让刚才那个访客进来', 'property:1:v1', tool_callback=tool)
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual([item['command'] for item in callbacks], ['visitor.search', 'visitor.checkin'])
        self.assertNotIn('777', json.dumps(callbacks, ensure_ascii=False))
        self.assertEqual(result['execution_state'], 'EXECUTED')

    def test_provider_cannot_switch_target_after_resolver(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        callbacks = []
        responses = iter([
            {'id': 'bind-1', 'choices': [{'message': {'role': 'assistant', 'content': '先确认对象。'}}]},
            {'id': 'bind-2', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('wrong-after-lookup', 'execute', 'visitor.checkin', {'id': 88, 'version': 1})],
            }}]},
            {'id': 'bind-3', 'choices': [{'message': {'role': 'assistant', 'content': '已处理。'}}]},
        ])

        def tool(args):
            callbacks.append(dict(args))
            if args['operation'] == 'lookup':
                return {'ok': True, 'code': 'SUCCESS', 'data': {'items': [{'id': 9, 'version': 2}]}, 'terminal': False}
            params = json.loads(args['arguments_json'])
            self.assertEqual(params['id'], 9)
            self.assertEqual(params['version'], 2)
            return {'ok': True, 'code': 'SUCCESS', 'terminal': True}

        token = _PLANNER_HINT.set({
            'action': 'TOOL', 'intent': 'visitor.checkin', 'entity_status': 'RESOLVE_FIRST',
            'candidates': ['visitor.search', 'visitor.checkin'], 'arguments': {'status': 'registered'}, 'tool_call': None,
        })
        try:
            with patch.object(client, '_request', side_effect=lambda *a, **k: next(responses)):
                result = client.chat('让刚才那个访客进来', 'property:1:v1', tool_callback=tool)
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual([item['command'] for item in callbacks], ['visitor.search', 'visitor.checkin'])
        self.assertNotIn('88', json.dumps(callbacks, ensure_ascii=False))
        self.assertEqual(result['execution_state'], 'EXECUTED')

    def test_latest_payment_is_server_selected_and_prepared_for_confirmation(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        callbacks = []
        payloads = []
        responses = iter([
            {'id': 'pay-1', 'choices': [{'message': {'role': 'assistant', 'content': '我先找上一笔。'}}]},
            {'id': 'pay-2', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('wrong-payment', 'propose', 'payment.reverse', {'id': 999, 'version': 1, 'reason': '撤回'})],
            }}]},
            {'id': 'pay-3', 'choices': [{'message': {'role': 'assistant', 'content': '已准备冲销确认卡片。'}}]},
        ])

        def request(method, path, payload=None):
            payloads.append(payload)
            return next(responses)

        def tool(args):
            callbacks.append(dict(args))
            if args['operation'] == 'lookup':
                return {
                    'ok': True, 'code': 'SUCCESS',
                    'data': {'items': [
                        {'id': 12, 'version': 3, 'amount': '88.00'},
                        {'id': 11, 'version': 1, 'amount': '66.00'},
                    ]},
                    'terminal': False,
                }
            params = json.loads(args['arguments_json'])
            self.assertEqual(args['operation'], 'propose')
            self.assertEqual(args['command'], 'payment.reverse')
            self.assertEqual(params['id'], 12)
            self.assertEqual(params['version'], 3)
            return {'ok': True, 'code': 'CONFIRMATION_REQUIRED', 'terminal': True}

        token = _PLANNER_HINT.set({
            'action': 'CONFIRM', 'intent': 'payment.reverse', 'entity_status': 'RESOLVE_FIRST',
            'candidates': ['payment.search', 'payment.reverse'],
            'arguments': {'_resolve_strategy': 'latest'}, 'tool_call': None,
        })
        try:
            with patch.object(client, '_request', side_effect=request):
                result = client.chat('撤回上一笔收款记录', 'property:1:v1', tool_callback=tool)
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual([item['command'] for item in callbacks], ['payment.search', 'payment.reverse'])
        self.assertNotIn('999', json.dumps(callbacks, ensure_ascii=False))
        self.assertEqual(result['execution_state'], 'PENDING_CONFIRMATION')
        second_messages = json.dumps(payloads[1]['messages'], ensure_ascii=False)
        self.assertIn('"id": 12', second_messages)
        self.assertNotIn('"id": 11', second_messages)

    def test_ambiguous_resolver_result_stops_before_write(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        callbacks = []
        responses = iter([
            {'id': 'amb-1', 'choices': [{'message': {'role': 'assistant', 'content': '我来找一下。'}}]},
            {'id': 'amb-2', 'choices': [{'message': {'role': 'assistant', 'content': '找到多个登记访客，请确认具体哪一位。'}}]},
        ])

        def tool(args):
            callbacks.append(dict(args))
            return {
                'ok': True, 'code': 'SUCCESS',
                'data': {'items': [
                    {'id': 8, 'version': 1, 'name': '李四'},
                    {'id': 9, 'version': 2, 'name': '赵六'},
                ]},
                'terminal': False,
            }

        token = _PLANNER_HINT.set({
            'action': 'TOOL', 'intent': 'visitor.checkin',
            'entity_status': 'RESOLVE_FIRST',
            'candidates': ['visitor.search', 'visitor.checkin'],
            'arguments': {'status': 'registered'}, 'tool_call': None,
        })
        try:
            with patch.object(client, '_request', side_effect=lambda *a, **k: next(responses)):
                result = client.chat('确认刚才的访客进入', 'property:1:v1', tool_callback=tool)
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual(len(callbacks), 1)
        self.assertEqual(callbacks[0]['command'], 'visitor.search')
        self.assertEqual(result['execution_state'], 'LOOKUP_ONLY')

    def test_resolver_fallback_never_uses_a_write_candidate_as_lookup(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        calls = []
        token = _PLANNER_HINT.set({
            'action': 'TOOL', 'intent': 'visitor.checkin',
            'entity_status': 'RESOLVE_FIRST',
            'candidates': ['visitor.checkin'], 'arguments': {}, 'tool_call': None,
        })
        try:
            with patch.object(client, '_request', return_value={
                'id': 'no-resolver',
                'choices': [{'message': {'role': 'assistant', 'content': '缺少可用的查询能力。'}}],
            }):
                result = client.chat('确认刚才的访客进入', 'property:1:v1', tool_callback=lambda arg: calls.append(arg))
        finally:
            _PLANNER_HINT.reset(token)
        self.assertEqual(calls, [])
        self.assertEqual(result['execution_state'], 'NOT_EXECUTED')


if __name__ == '__main__':
    unittest.main()
