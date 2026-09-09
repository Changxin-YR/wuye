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


class ParkingAssignmentResolveTests(unittest.TestCase):
    AUTHORIZED = {'parking.search', 'vehicle.search', 'parking.assign'}

    def test_complete_business_request_uses_multi_resolution(self):
        plan = plan_request('把A-001车位分给粤A12345', self.AUTHORIZED, {})
        self.assertEqual((plan['intent'], plan['action']), ('parking.assign', 'TOOL'))
        self.assertEqual(plan['entity_status'], 'RESOLVE_MULTI')
        self.assertEqual(plan['candidates'], ['parking.search', 'vehicle.search', 'parking.assign'])
        self.assertEqual(plan['arguments']['space_code'], 'A-001')
        self.assertEqual(plan['arguments']['plate'], '粤A12345')
        self.assertNotIn('space_id', plan['arguments'])
        self.assertNotIn('vehicle_id', plan['arguments'])

    def test_model_cannot_guess_space_or_vehicle_ids(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        callbacks = []
        responses = iter([
            {'id': 'p-1', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('bad-1', 'execute', 'parking.assign', {
                    'space_id': 999, 'vehicle_id': 998,
                })],
            }}]},
            {'id': 'p-2', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('bad-2', 'execute', 'parking.assign', {
                    'space_id': 997, 'vehicle_id': 996,
                })],
            }}]},
            {'id': 'p-3', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('bad-3', 'execute', 'parking.assign', {
                    'space_id': 995, 'vehicle_id': 994,
                })],
            }}]},
            {'id': 'p-4', 'choices': [{'message': {'role': 'assistant', 'content': '车位分配已完成。'}}]},
        ])

        def tool(args):
            callbacks.append(dict(args))
            params = json.loads(args['arguments_json'])
            if args['command'] == 'parking.search':
                self.assertEqual(args['operation'], 'lookup')
                self.assertEqual(params, {'space_code': 'A-001'})
                return {
                    'ok': True, 'code': 'SUCCESS',
                    'data': {'items': [{'id': 31, 'community_id': 1, 'building_id': 7, 'code': 'A-001'}]},
                    'terminal': False,
                }
            if args['command'] == 'vehicle.search':
                self.assertEqual(args['operation'], 'lookup')
                self.assertEqual(params, {'plate': '粤A12345'})
                return {
                    'ok': True, 'code': 'SUCCESS',
                    'data': {'items': [{'id': 41, 'community_id': 1, 'building_id': 7, 'plate': '粤A12345'}]},
                    'terminal': False,
                }
            self.assertEqual(args['command'], 'parking.assign')
            self.assertEqual(args['operation'], 'execute')
            self.assertEqual(params, {'space_id': 31, 'vehicle_id': 41})
            return {
                'ok': True, 'code': 'SUCCESS',
                'data': {'status': 'executed', 'id': 51},
                'terminal': True,
            }

        token = _PLANNER_HINT.set({
            'action': 'TOOL',
            'intent': 'parking.assign',
            'entity_status': 'RESOLVE_MULTI',
            'candidates': ['parking.search', 'vehicle.search', 'parking.assign'],
            'arguments': {
                'space_code': 'A-001',
                'plate': '粤A12345',
                'space_id': 123456,
                'vehicle_id': 654321,
            },
            'tool_call': {
                'operation': 'execute', 'command': 'parking.assign',
                'arguments_json': json.dumps({'space_id': 123456, 'vehicle_id': 654321}),
            },
        })
        try:
            with patch.object(client, '_request', side_effect=lambda *a, **k: next(responses)):
                result = client.chat(
                    '把A-001车位分给粤A12345',
                    'property:1:v1',
                    tool_callback=tool,
                )
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual(
            [item['command'] for item in callbacks],
            ['parking.search', 'vehicle.search', 'parking.assign'],
        )
        dumped = json.dumps(callbacks, ensure_ascii=False)
        for guessed in ('999', '998', '997', '996', '995', '994', '123456', '654321'):
            self.assertNotIn(guessed, dumped)
        self.assertEqual(result['execution_state'], 'EXECUTED')

    def test_ambiguous_space_stops_before_vehicle_lookup_and_write(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        callbacks = []
        responses = iter([
            {'id': 'pa-1', 'choices': [{'message': {'role': 'assistant', 'content': '我先核对车位。'}}]},
            {'id': 'pa-2', 'choices': [{'message': {'role': 'assistant', 'content': '找到多个同编号车位，请补充范围。'}}]},
        ])

        def tool(args):
            callbacks.append(dict(args))
            self.assertEqual(args['command'], 'parking.search')
            return {
                'ok': True, 'code': 'SUCCESS',
                'data': {'items': [
                    {'id': 31, 'community_id': 1, 'code': 'A-001'},
                    {'id': 32, 'community_id': 2, 'code': 'A-001'},
                ]},
                'terminal': False,
            }

        token = _PLANNER_HINT.set({
            'action': 'TOOL', 'intent': 'parking.assign', 'entity_status': 'RESOLVE_MULTI',
            'candidates': ['parking.search', 'vehicle.search', 'parking.assign'],
            'arguments': {'space_code': 'A-001', 'plate': '粤A12345'},
            'tool_call': None,
        })
        try:
            with patch.object(client, '_request', side_effect=lambda *a, **k: next(responses)):
                result = client.chat(
                    '把A-001车位分给粤A12345',
                    'property:1:v1',
                    tool_callback=tool,
                )
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual([item['command'] for item in callbacks], ['parking.search'])
        self.assertEqual(result['execution_state'], 'LOOKUP_ONLY')


if __name__ == '__main__':
    unittest.main()
