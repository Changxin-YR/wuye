import json
import unittest
from unittest.mock import patch

from dify_client import BailianClient, _PLANNER_HINT


class ResolverFallbackTests(unittest.TestCase):
    def test_contextual_lookup_is_synthesized_before_final_write(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        callbacks = []
        payloads = []
        responses = iter([
            {'id': 'round-1', 'choices': [{'message': {'role': 'assistant', 'content': '我来处理。'}}]},
            {
                'id': 'round-2',
                'choices': [{'message': {
                    'role': 'assistant',
                    'content': None,
                    'tool_calls': [{
                        'id': 'call-final',
                        'type': 'function',
                        'function': {
                            'name': 'property_agent_tool',
                            'arguments': json.dumps({
                                'operation': 'execute',
                                'command': 'visitor.checkin',
                                'arguments_json': json.dumps({'id': 9, 'version': 2}),
                            }, ensure_ascii=False),
                        },
                    }],
                }}],
            },
            {'id': 'round-3', 'choices': [{'message': {'role': 'assistant', 'content': '访客已登记进入。'}}]},
        ])

        def request(method, path, payload=None):
            payloads.append(payload)
            return next(responses)

        def tool(args):
            callbacks.append(dict(args))
            if args['operation'] == 'lookup':
                return {
                    'ok': True,
                    'code': 'SUCCESS',
                    'data': {'items': [{'id': 9, 'version': 2, 'status': 'registered'}]},
                    'terminal': False,
                }
            return {
                'ok': True,
                'code': 'SUCCESS',
                'data': {'status': 'executed', 'verification': {'id': 9}},
                'terminal': True,
            }

        token = _PLANNER_HINT.set({
            'action': 'TOOL',
            'intent': 'visitor.checkin',
            'entity_status': 'RESOLVE_FIRST',
            'candidates': ['visitor.search', 'visitor.checkin'],
            'arguments': {'status': 'registered'},
            'tool_call': None,
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
        resolver_assistant = next(
            item for item in second_messages
            if item.get('role') == 'assistant' and item.get('tool_calls')
            and item['tool_calls'][0].get('id') == 'planner-resolver'
        )
        resolver_tool = next(
            item for item in second_messages
            if item.get('role') == 'tool' and item.get('tool_call_id') == 'planner-resolver'
        )
        self.assertTrue(resolver_assistant)
        self.assertTrue(resolver_tool)

    def test_resolver_fallback_never_uses_a_write_candidate_as_lookup(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        calls = []
        token = _PLANNER_HINT.set({
            'action': 'TOOL',
            'intent': 'visitor.checkin',
            'entity_status': 'RESOLVE_FIRST',
            'candidates': ['visitor.checkin'],
            'arguments': {},
            'tool_call': None,
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
