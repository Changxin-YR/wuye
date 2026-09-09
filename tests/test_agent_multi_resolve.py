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


class MultiResolveVisitorTests(unittest.TestCase):
    def test_model_cannot_guess_or_switch_person_and_house_ids(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        callbacks = []
        responses = iter([
            {'id': 'multi-1', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('bad-1', 'execute', 'visitor.create', {
                    'house_id': 999, 'host_person_id': 998, 'name': '李四',
                    'phone': '13800000000', 'purpose': '拜访王五', 'expected_at': '2026-09-09T14:00',
                })],
            }}]},
            {'id': 'multi-2', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('bad-2', 'execute', 'visitor.create', {
                    'house_id': 997, 'host_person_id': 996, 'name': '李四',
                    'phone': '13800000000', 'purpose': '拜访王五', 'expected_at': '2026-09-09T14:00',
                })],
            }}]},
            {'id': 'multi-3', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('bad-3', 'execute', 'visitor.create', {
                    'house_id': 995, 'host_person_id': 994, 'name': '李四',
                    'phone': '13800000000', 'purpose': '拜访王五', 'expected_at': '2026-09-09T14:00',
                })],
            }}]},
            {'id': 'multi-4', 'choices': [{'message': {'role': 'assistant', 'content': '访客登记已完成。'}}]},
        ])

        def request(method, path, payload=None):
            return next(responses)

        def tool(args):
            callbacks.append(dict(args))
            params = json.loads(args['arguments_json'])
            if args['command'] == 'person.search':
                self.assertEqual(params, {'person_name': '王五'})
                self.assertNotIn('phone', params)
                return {'ok': True, 'code': 'SUCCESS', 'data': {'items': [{'id': 21, 'name': '王五'}]}, 'terminal': False}
            if args['command'] == 'house.search':
                self.assertEqual(params, {'building_name': 'A栋', 'room_no': 101})
                return {'ok': True, 'code': 'SUCCESS', 'data': {'items': [{'id': 31, 'building_name': 'A栋', 'room_no': 101}]}, 'terminal': False}
            self.assertEqual(args['command'], 'visitor.create')
            self.assertEqual(args['operation'], 'execute')
            self.assertEqual(params, {
                'house_id': 31,
                'host_person_id': 21,
                'name': '李四',
                'phone': '13800000000',
                'purpose': '拜访王五',
                'expected_at': '2026-09-09T14:00',
            })
            return {'ok': True, 'code': 'SUCCESS', 'data': {'status': 'executed', 'id': 41}, 'terminal': True}

        token = _PLANNER_HINT.set({
            'action': 'TOOL',
            'intent': 'visitor.create',
            'entity_status': 'RESOLVE_MULTI',
            'candidates': ['person.search', 'house.search', 'visitor.create'],
            'arguments': {
                'visitor_name': '李四', 'person_name': '王五',
                'building_name': 'A栋', 'room_no': 101,
                'phone': '13800000000', 'purpose': '拜访王五',
                'expected_at': '2026-09-09T14:00',
            },
            'tool_call': None,
        })
        try:
            with patch.object(client, '_request', side_effect=request):
                result = client.chat('登记访客李四来找王五，A栋101室，电话13800000000，明天下午两点', 'property:1:v1', tool_callback=tool)
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual([item['command'] for item in callbacks], ['person.search', 'house.search', 'visitor.create'])
        dumped = json.dumps(callbacks, ensure_ascii=False)
        for guessed in ('999', '998', '997', '996', '995', '994'):
            self.assertNotIn(guessed, dumped)
        self.assertEqual(result['execution_state'], 'EXECUTED')

    def test_ambiguous_host_stops_before_house_lookup_and_write(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        callbacks = []
        responses = iter([
            {'id': 'amb-1', 'choices': [{'message': {'role': 'assistant', 'content': '我先核对住户。'}}]},
            {'id': 'amb-2', 'choices': [{'message': {'role': 'assistant', 'content': '找到多位王五，请补充区分信息。'}}]},
        ])

        def tool(args):
            callbacks.append(dict(args))
            return {
                'ok': True, 'code': 'SUCCESS',
                'data': {'items': [{'id': 21, 'name': '王五'}, {'id': 22, 'name': '王五'}]},
                'terminal': False,
            }

        token = _PLANNER_HINT.set({
            'action': 'TOOL', 'intent': 'visitor.create', 'entity_status': 'RESOLVE_MULTI',
            'candidates': ['person.search', 'house.search', 'visitor.create'],
            'arguments': {
                'visitor_name': '李四', 'person_name': '王五', 'building_name': 'A栋', 'room_no': 101,
                'phone': '13800000000', 'purpose': '拜访王五', 'expected_at': '2026-09-09T14:00',
            }, 'tool_call': None,
        })
        try:
            with patch.object(client, '_request', side_effect=lambda *a, **k: next(responses)):
                result = client.chat('登记访客李四来找王五', 'property:1:v1', tool_callback=tool)
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual([item['command'] for item in callbacks], ['person.search'])
        self.assertEqual(result['execution_state'], 'LOOKUP_ONLY')


if __name__ == '__main__':
    unittest.main()
