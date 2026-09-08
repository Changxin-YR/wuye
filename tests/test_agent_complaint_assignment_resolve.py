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


class ComplaintAssignmentResolveTests(unittest.TestCase):
    def test_planner_uses_complaint_staff_not_resident_person(self):
        plan = plan_request(
            '把投诉#12分给李客服',
            {'complaint.search', 'complaint_staff.search', 'complaint.assign'},
            {},
        )
        self.assertEqual(plan['action'], 'TOOL')
        self.assertEqual(plan['intent'], 'complaint.assign')
        self.assertEqual(plan['entity_status'], 'RESOLVE_MULTI')
        self.assertEqual(plan['candidates'], ['complaint.search', 'complaint_staff.search', 'complaint.assign'])
        self.assertEqual(plan['arguments']['complaint_id'], 12)
        self.assertEqual(plan['arguments']['assignee_name'], '李客服')
        self.assertNotIn('person.search', plan['candidates'])

    def test_model_cannot_guess_complaint_or_handler_ids(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        callbacks = []
        responses = iter([
            {'id': 'complaint-1', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('bad-1', 'execute', 'complaint.assign', {
                    'id': 999, 'version': 99, 'assignee_id': 998,
                })],
            }}]},
            {'id': 'complaint-2', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('bad-2', 'execute', 'complaint.assign', {
                    'id': 997, 'version': 98, 'assignee_id': 996,
                })],
            }}]},
            {'id': 'complaint-3', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('bad-3', 'execute', 'complaint.assign', {
                    'id': 995, 'version': 97, 'assignee_id': 994,
                })],
            }}]},
            {'id': 'complaint-4', 'choices': [{'message': {'role': 'assistant', 'content': '投诉已经分派。'}}]},
        ])

        def tool(args):
            callbacks.append(dict(args))
            params = json.loads(args['arguments_json'])
            if args['command'] == 'complaint.search':
                self.assertEqual(args['operation'], 'lookup')
                self.assertEqual(params, {'id': 12})
                return {
                    'ok': True, 'code': 'SUCCESS',
                    'data': {'items': [{
                        'id': 12, 'version': 3, 'community_id': 1, 'building_id': 7,
                    }]},
                    'terminal': False,
                }
            if args['command'] == 'complaint_staff.search':
                self.assertEqual(args['operation'], 'lookup')
                self.assertEqual(params, {
                    'staff_name': '李客服', 'community_id': 1, 'building_id': 7,
                })
                return {
                    'ok': True, 'code': 'SUCCESS',
                    'data': {'items': [{'id': 61, 'real_name': '李客服'}]},
                    'terminal': False,
                }
            self.assertEqual(args['command'], 'complaint.assign')
            self.assertEqual(args['operation'], 'execute')
            self.assertEqual(params, {'id': 12, 'version': 3, 'assignee_id': 61})
            return {
                'ok': True, 'code': 'SUCCESS',
                'data': {'status': 'executed', 'id': 12},
                'terminal': True,
            }

        token = _PLANNER_HINT.set({
            'action': 'TOOL',
            'intent': 'complaint.assign',
            'entity_status': 'RESOLVE_MULTI',
            'candidates': ['complaint.search', 'complaint_staff.search', 'complaint.assign'],
            'arguments': {
                'complaint_id': 12,
                'assignee_name': '李客服',
                'id': 654321,
                'assignee_id': 123456,
            },
            'tool_call': {
                'operation': 'execute', 'command': 'complaint.assign',
                'arguments_json': json.dumps({'id': 654321, 'version': 1, 'assignee_id': 123456}),
            },
        })
        try:
            with patch.object(client, '_request', side_effect=lambda *a, **k: next(responses)):
                result = client.chat('把投诉#12分给李客服', 'property:1:v1', tool_callback=tool)
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual(
            [item['command'] for item in callbacks],
            ['complaint.search', 'complaint_staff.search', 'complaint.assign'],
        )
        dumped = json.dumps(callbacks, ensure_ascii=False)
        for guessed in ('999', '998', '997', '996', '995', '994', '123456', '654321'):
            self.assertNotIn(guessed, dumped)
        self.assertEqual(result['execution_state'], 'EXECUTED')

    def test_contextual_complaint_never_defaults_to_latest_when_ambiguous(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        callbacks = []
        responses = iter([
            {'id': 'ctx-1', 'choices': [{'message': {'role': 'assistant', 'content': '先核对投诉。'}}]},
            {'id': 'ctx-2', 'choices': [{'message': {'role': 'assistant', 'content': '有多条投诉，请确认。'}}]},
        ])

        def tool(args):
            callbacks.append(dict(args))
            self.assertEqual(args['command'], 'complaint.search')
            self.assertEqual(json.loads(args['arguments_json']), {})
            return {
                'ok': True, 'code': 'SUCCESS',
                'data': {'items': [
                    {'id': 12, 'version': 1, 'community_id': 1, 'building_id': 7},
                    {'id': 13, 'version': 1, 'community_id': 1, 'building_id': 7},
                ]},
                'terminal': False,
            }

        token = _PLANNER_HINT.set({
            'action': 'TOOL', 'intent': 'complaint.assign', 'entity_status': 'RESOLVE_MULTI',
            'candidates': ['complaint.search', 'complaint_staff.search', 'complaint.assign'],
            'arguments': {'assignee_name': '李客服'},
            'tool_call': None,
        })
        try:
            with patch.object(client, '_request', side_effect=lambda *a, **k: next(responses)):
                result = client.chat('把刚才那条投诉分给李客服', 'property:1:v1', tool_callback=tool)
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual([item['command'] for item in callbacks], ['complaint.search'])
        self.assertEqual(result['execution_state'], 'LOOKUP_ONLY')

    def test_ambiguous_handler_stops_before_write(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        callbacks = []
        responses = iter([
            {'id': 'amb-1', 'choices': [{'message': {'role': 'assistant', 'content': '核对投诉。'}}]},
            {'id': 'amb-2', 'choices': [{'message': {'role': 'assistant', 'content': '核对处理人。'}}]},
            {'id': 'amb-3', 'choices': [{'message': {'role': 'assistant', 'content': '同名人员不唯一。'}}]},
        ])

        def tool(args):
            callbacks.append(dict(args))
            if args['command'] == 'complaint.search':
                return {
                    'ok': True, 'code': 'SUCCESS',
                    'data': {'items': [{'id': 12, 'version': 3, 'community_id': 1, 'building_id': 7}]},
                    'terminal': False,
                }
            self.assertEqual(args['command'], 'complaint_staff.search')
            return {
                'ok': True, 'code': 'SUCCESS',
                'data': {'items': [
                    {'id': 61, 'real_name': '李客服'},
                    {'id': 62, 'real_name': '李客服'},
                ]},
                'terminal': False,
            }

        token = _PLANNER_HINT.set({
            'action': 'TOOL', 'intent': 'complaint.assign', 'entity_status': 'RESOLVE_MULTI',
            'candidates': ['complaint.search', 'complaint_staff.search', 'complaint.assign'],
            'arguments': {'complaint_id': 12, 'assignee_name': '李客服'},
            'tool_call': None,
        })
        try:
            with patch.object(client, '_request', side_effect=lambda *a, **k: next(responses)):
                result = client.chat('把投诉#12分给李客服', 'property:1:v1', tool_callback=tool)
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual([item['command'] for item in callbacks], ['complaint.search', 'complaint_staff.search'])
        self.assertEqual(result['execution_state'], 'LOOKUP_ONLY')


if __name__ == '__main__':
    unittest.main()
