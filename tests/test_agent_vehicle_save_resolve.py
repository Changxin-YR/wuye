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


class VehicleSaveResolveTests(unittest.TestCase):
    AUTHORIZED = {'house.search', 'person.search', 'vehicle.save'}

    def test_complete_business_request_uses_multi_resolution(self):
        plan = plan_request('登记车牌粤A12345，车主王五，A栋101室', self.AUTHORIZED, {})
        self.assertEqual((plan['intent'], plan['action']), ('vehicle.save', 'TOOL'))
        self.assertEqual(plan['entity_status'], 'RESOLVE_MULTI')
        self.assertEqual(plan['candidates'], ['house.search', 'person.search', 'vehicle.save'])
        self.assertEqual(plan['arguments']['plate'], '粤A12345')
        self.assertEqual(plan['arguments']['person_name'], '王五')
        self.assertEqual(plan['arguments']['building_name'], 'A栋')
        self.assertEqual(plan['arguments']['room_no'], 101)
        self.assertNotIn('house_id', plan['arguments'])
        self.assertNotIn('person_id', plan['arguments'])

    def test_model_cannot_guess_house_or_person_ids(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        callbacks = []
        responses = iter([
            {'id': 'v-1', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('bad-1', 'execute', 'vehicle.save', {
                    'house_id': 999, 'person_id': 998, 'plate': '粤A12345',
                })],
            }}]},
            {'id': 'v-2', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('bad-2', 'execute', 'vehicle.save', {
                    'house_id': 997, 'person_id': 996, 'plate': '粤A12345',
                })],
            }}]},
            {'id': 'v-3', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('bad-3', 'execute', 'vehicle.save', {
                    'house_id': 995, 'person_id': 994, 'plate': '粤A12345',
                })],
            }}]},
            {'id': 'v-4', 'choices': [{'message': {'role': 'assistant', 'content': '车辆登记已完成。'}}]},
        ])

        def tool(args):
            callbacks.append(dict(args))
            params = json.loads(args['arguments_json'])
            if args['command'] == 'house.search':
                self.assertEqual(args['operation'], 'lookup')
                self.assertEqual(params, {'building_name': 'A栋', 'room_no': 101})
                return {
                    'ok': True, 'code': 'SUCCESS',
                    'data': {'items': [{
                        'id': 31, 'community_id': 1, 'building_id': 7,
                        'building_name': 'A栋', 'room_no': 101,
                    }]},
                    'terminal': False,
                }
            if args['command'] == 'person.search':
                self.assertEqual(args['operation'], 'lookup')
                self.assertEqual(params, {
                    'person_name': '王五', 'community_id': 1, 'building_id': 7,
                })
                return {
                    'ok': True, 'code': 'SUCCESS',
                    'data': {'items': [{
                        'id': 41, 'community_id': 1, 'name': '王五',
                    }]},
                    'terminal': False,
                }
            self.assertEqual(args['command'], 'vehicle.save')
            self.assertEqual(args['operation'], 'execute')
            self.assertEqual(params, {
                'house_id': 31, 'person_id': 41, 'plate': '粤A12345',
            })
            return {
                'ok': True, 'code': 'SUCCESS',
                'data': {'status': 'executed', 'id': 51},
                'terminal': True,
            }

        token = _PLANNER_HINT.set({
            'action': 'TOOL',
            'intent': 'vehicle.save',
            'entity_status': 'RESOLVE_MULTI',
            'candidates': ['house.search', 'person.search', 'vehicle.save'],
            'arguments': {
                'building_name': 'A栋', 'room_no': 101,
                'person_name': '王五', 'plate': '粤A12345',
                'house_id': 123456, 'person_id': 654321,
            },
            'tool_call': {
                'operation': 'execute', 'command': 'vehicle.save',
                'arguments_json': json.dumps({
                    'house_id': 123456, 'person_id': 654321, 'plate': '粤A12345',
                }, ensure_ascii=False),
            },
        })
        try:
            with patch.object(client, '_request', side_effect=lambda *a, **k: next(responses)):
                result = client.chat(
                    '登记车牌粤A12345，车主王五，A栋101室',
                    'property:1:v1',
                    tool_callback=tool,
                )
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual(
            [item['command'] for item in callbacks],
            ['house.search', 'person.search', 'vehicle.save'],
        )
        dumped = json.dumps(callbacks, ensure_ascii=False)
        for guessed in ('999', '998', '997', '996', '995', '994', '123456', '654321'):
            self.assertNotIn(guessed, dumped)
        self.assertEqual(result['execution_state'], 'EXECUTED')

    def test_ambiguous_house_stops_before_person_lookup_and_write(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        callbacks = []
        responses = iter([
            {'id': 'va-1', 'choices': [{'message': {'role': 'assistant', 'content': '我先核对房屋。'}}]},
            {'id': 'va-2', 'choices': [{'message': {'role': 'assistant', 'content': '找到多套同号房屋，请补充小区或单元。'}}]},
        ])

        def tool(args):
            callbacks.append(dict(args))
            self.assertEqual(args['command'], 'house.search')
            return {
                'ok': True, 'code': 'SUCCESS',
                'data': {'items': [
                    {'id': 31, 'community_id': 1, 'building_id': 7, 'building_name': 'A栋', 'room_no': 101},
                    {'id': 32, 'community_id': 2, 'building_id': 8, 'building_name': 'A栋', 'room_no': 101},
                ]},
                'terminal': False,
            }

        token = _PLANNER_HINT.set({
            'action': 'TOOL', 'intent': 'vehicle.save', 'entity_status': 'RESOLVE_MULTI',
            'candidates': ['house.search', 'person.search', 'vehicle.save'],
            'arguments': {
                'building_name': 'A栋', 'room_no': 101,
                'person_name': '王五', 'plate': '粤A12345',
            },
            'tool_call': None,
        })
        try:
            with patch.object(client, '_request', side_effect=lambda *a, **k: next(responses)):
                result = client.chat(
                    '登记车牌粤A12345，车主王五，A栋101室',
                    'property:1:v1',
                    tool_callback=tool,
                )
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual([item['command'] for item in callbacks], ['house.search'])
        self.assertEqual(result['execution_state'], 'LOOKUP_ONLY')


if __name__ == '__main__':
    unittest.main()
