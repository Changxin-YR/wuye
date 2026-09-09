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


class OrderAssignmentResolveTests(unittest.TestCase):
    def test_model_cannot_guess_order_or_repairer_ids(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        callbacks = []
        responses = iter([
            {'id': 'assign-1', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('bad-1', 'execute', 'order.assign', {
                    'id': 999, 'version': 99, 'repairer_id': 998,
                })],
            }}]},
            {'id': 'assign-2', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('bad-2', 'execute', 'order.assign', {
                    'id': 997, 'version': 98, 'repairer_id': 996,
                })],
            }}]},
            {'id': 'assign-3', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('bad-3', 'execute', 'order.assign', {
                    'id': 995, 'version': 97, 'repairer_id': 994,
                })],
            }}]},
            {'id': 'assign-4', 'choices': [{'message': {'role': 'assistant', 'content': '工单已经派给张师傅。'}}]},
        ])

        def tool(args):
            callbacks.append(dict(args))
            params = json.loads(args['arguments_json'])
            if args['command'] == 'order.search':
                self.assertEqual(args['operation'], 'lookup')
                self.assertEqual(params, {'order_no': 'WO-20260908-001'})
                return {
                    'ok': True, 'code': 'SUCCESS',
                    'data': {'items': [{
                        'id': 51, 'version': 4, 'order_no': 'WO-20260908-001',
                        'community_id': 1, 'building_id': 7,
                    }]},
                    'terminal': False,
                }
            if args['command'] == 'staff.search':
                self.assertEqual(args['operation'], 'lookup')
                self.assertEqual(params, {
                    'staff_name': '张师傅', 'community_id': 1, 'building_id': 7,
                })
                return {
                    'ok': True, 'code': 'SUCCESS',
                    'data': {'items': [{'id': 61, 'real_name': '张师傅'}]},
                    'terminal': False,
                }
            self.assertEqual(args['command'], 'order.assign')
            self.assertEqual(args['operation'], 'execute')
            self.assertEqual(params, {'id': 51, 'version': 4, 'repairer_id': 61})
            return {
                'ok': True, 'code': 'SUCCESS',
                'data': {'status': 'executed', 'id': 51},
                'terminal': True,
            }

        token = _PLANNER_HINT.set({
            'action': 'TOOL',
            'intent': 'order.assign',
            'entity_status': 'RESOLVE_MULTI',
            'candidates': ['order.search', 'staff.search', 'order.assign'],
            'arguments': {
                'order_no': 'WO-20260908-001',
                'repairer_name': '张师傅',
                'repairer_id': 123456,
                'id': 654321,
            },
            'tool_call': {
                'operation': 'execute', 'command': 'order.assign',
                'arguments_json': json.dumps({'id': 654321, 'version': 1, 'repairer_id': 123456}),
            },
        })
        try:
            with patch.object(client, '_request', side_effect=lambda *a, **k: next(responses)):
                result = client.chat('把工单WO-20260908-001派给张师傅', 'property:1:v1', tool_callback=tool)
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual([item['command'] for item in callbacks], ['order.search', 'staff.search', 'order.assign'])
        dumped = json.dumps(callbacks, ensure_ascii=False)
        for guessed in ('999', '998', '997', '996', '995', '994', '123456', '654321'):
            self.assertNotIn(guessed, dumped)
        self.assertEqual(result['execution_state'], 'EXECUTED')

    def test_ambiguous_repairer_stops_before_write(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        callbacks = []
        responses = iter([
            {'id': 'amb-1', 'choices': [{'message': {'role': 'assistant', 'content': '先核对工单。'}}]},
            {'id': 'amb-2', 'choices': [{'message': {'role': 'assistant', 'content': '再核对维修人员。'}}]},
            {'id': 'amb-3', 'choices': [{'message': {'role': 'assistant', 'content': '找到多位同名维修人员，请补充信息。'}}]},
        ])

        def tool(args):
            callbacks.append(dict(args))
            if args['command'] == 'order.search':
                return {
                    'ok': True, 'code': 'SUCCESS',
                    'data': {'items': [{
                        'id': 51, 'version': 4, 'order_no': 'WO-1',
                        'community_id': 1, 'building_id': 7,
                    }]},
                    'terminal': False,
                }
            self.assertEqual(args['command'], 'staff.search')
            return {
                'ok': True, 'code': 'SUCCESS',
                'data': {'items': [
                    {'id': 61, 'real_name': '张师傅'},
                    {'id': 62, 'real_name': '张师傅'},
                ]},
                'terminal': False,
            }

        token = _PLANNER_HINT.set({
            'action': 'TOOL', 'intent': 'order.assign', 'entity_status': 'RESOLVE_MULTI',
            'candidates': ['order.search', 'staff.search', 'order.assign'],
            'arguments': {'order_no': 'WO-1', 'repairer_name': '张师傅'},
            'tool_call': None,
        })
        try:
            with patch.object(client, '_request', side_effect=lambda *a, **k: next(responses)):
                result = client.chat('把工单WO-1派给张师傅', 'property:1:v1', tool_callback=tool)
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual([item['command'] for item in callbacks], ['order.search', 'staff.search'])
        self.assertEqual(result['execution_state'], 'LOOKUP_ONLY')

    def test_missing_eligible_repairer_stops_before_write(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        callbacks = []
        responses = iter([
            {'id': 'missing-1', 'choices': [{'message': {'role': 'assistant', 'content': '先核对工单。'}}]},
            {'id': 'missing-2', 'choices': [{'message': {'role': 'assistant', 'content': '再核对维修人员。'}}]},
            {'id': 'missing-3', 'choices': [{'message': {'role': 'assistant', 'content': '当前范围没有找到可派单维修人员。'}}]},
        ])

        def tool(args):
            callbacks.append(dict(args))
            if args['command'] == 'order.search':
                return {
                    'ok': True, 'code': 'SUCCESS',
                    'data': {'items': [{
                        'id': 51, 'version': 4, 'order_no': 'WO-1',
                        'community_id': 1, 'building_id': 7,
                    }]},
                    'terminal': False,
                }
            self.assertEqual(args['command'], 'staff.search')
            return {'ok': True, 'code': 'SUCCESS', 'data': {'items': []}, 'terminal': False}

        token = _PLANNER_HINT.set({
            'action': 'TOOL', 'intent': 'order.assign', 'entity_status': 'RESOLVE_MULTI',
            'candidates': ['order.search', 'staff.search', 'order.assign'],
            'arguments': {'order_no': 'WO-1', 'repairer_name': '张师傅'},
            'tool_call': None,
        })
        try:
            with patch.object(client, '_request', side_effect=lambda *a, **k: next(responses)):
                result = client.chat('把工单WO-1派给张师傅', 'property:1:v1', tool_callback=tool)
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual([item['command'] for item in callbacks], ['order.search', 'staff.search'])
        self.assertEqual(result['execution_state'], 'LOOKUP_ONLY')


if __name__ == '__main__':
    unittest.main()
