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


class InspectionCreateResolveTests(unittest.TestCase):
    AUTHORIZED = {'device.search', 'inspection_staff.search', 'inspection.create'}

    def test_incomplete_request_stays_in_clarification(self):
        plan = plan_request('给P-01安排巡检', self.AUTHORIZED, {})
        self.assertEqual(plan['intent'], 'inspection.create')
        self.assertEqual(plan['action'], 'CLARIFY')
        self.assertIn('assignee', plan['missing_fields'])
        self.assertIn('due_at', plan['missing_fields'])
        self.assertIn('checklist', plan['missing_fields'])
        self.assertTrue(plan.get('clarification_text'))

    def test_fully_specified_request_uses_two_server_resolvers(self):
        plan = plan_request(
            '给P-01安排巡检，明天下午两点前完成，交给王工，检查振动和温度',
            self.AUTHORIZED,
            {},
        )
        self.assertEqual(plan['action'], 'TOOL')
        self.assertEqual(plan['entity_status'], 'RESOLVE_MULTI')
        self.assertEqual(plan['candidates'], ['device.search', 'inspection_staff.search', 'inspection.create'])
        self.assertEqual(plan['arguments']['device_code'], 'P-01')
        self.assertEqual(plan['arguments']['assignee_name'], '王工')
        self.assertEqual(plan['arguments']['checklist'], '振动和温度')
        self.assertIn('T14:00', plan['arguments']['due_at'])

    def test_model_cannot_guess_device_or_assignee_ids(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        callbacks = []
        responses = iter([
            {'id': 'i-1', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('bad-1', 'execute', 'inspection.create', {
                    'device_id': 999, 'assignee_id': 998, 'due_at': '2099-01-01T00:00', 'checklist': '伪造',
                })],
            }}]},
            {'id': 'i-2', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('bad-2', 'execute', 'inspection.create', {
                    'device_id': 997, 'assignee_id': 996, 'due_at': '2099-01-01T00:00', 'checklist': '伪造',
                })],
            }}]},
            {'id': 'i-3', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('bad-3', 'execute', 'inspection.create', {
                    'device_id': 995, 'assignee_id': 994, 'due_at': '2099-01-01T00:00', 'checklist': '伪造',
                })],
            }}]},
            {'id': 'i-4', 'choices': [{'message': {'role': 'assistant', 'content': '巡检任务已创建。'}}]},
        ])

        def tool(args):
            callbacks.append(dict(args))
            params = json.loads(args['arguments_json'])
            if args['command'] == 'device.search':
                self.assertEqual(args['operation'], 'lookup')
                self.assertEqual(params, {'code': 'P-01'})
                return {
                    'ok': True, 'code': 'SUCCESS',
                    'data': {'items': [{'id': 41, 'community_id': 1, 'building_id': 7, 'code': 'P-01'}]},
                    'terminal': False,
                }
            if args['command'] == 'inspection_staff.search':
                self.assertEqual(args['operation'], 'lookup')
                self.assertEqual(params, {'staff_name': '王工', 'community_id': 1, 'building_id': 7})
                return {
                    'ok': True, 'code': 'SUCCESS',
                    'data': {'items': [{'id': 61, 'real_name': '王工'}]},
                    'terminal': False,
                }
            self.assertEqual(args['command'], 'inspection.create')
            self.assertEqual(args['operation'], 'execute')
            self.assertEqual(params, {
                'device_id': 41,
                'assignee_id': 61,
                'due_at': '2026-09-09T14:00',
                'checklist': '振动和温度',
            })
            return {
                'ok': True, 'code': 'SUCCESS',
                'data': {'status': 'executed', 'id': 71},
                'terminal': True,
            }

        token = _PLANNER_HINT.set({
            'action': 'TOOL',
            'intent': 'inspection.create',
            'entity_status': 'RESOLVE_MULTI',
            'candidates': ['device.search', 'inspection_staff.search', 'inspection.create'],
            'arguments': {
                'device_code': 'P-01',
                'assignee_name': '王工',
                'due_at': '2026-09-09T14:00',
                'checklist': '振动和温度',
                'device_id': 123456,
                'assignee_id': 654321,
            },
            'tool_call': {
                'operation': 'execute', 'command': 'inspection.create',
                'arguments_json': json.dumps({'device_id': 123456, 'assignee_id': 654321}),
            },
        })
        try:
            with patch.object(client, '_request', side_effect=lambda *a, **k: next(responses)):
                result = client.chat(
                    '给P-01安排巡检，明天下午两点前完成，交给王工，检查振动和温度',
                    'property:1:v1',
                    tool_callback=tool,
                )
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual(
            [item['command'] for item in callbacks],
            ['device.search', 'inspection_staff.search', 'inspection.create'],
        )
        dumped = json.dumps(callbacks, ensure_ascii=False)
        for guessed in ('999', '998', '997', '996', '995', '994', '123456', '654321'):
            self.assertNotIn(guessed, dumped)
        self.assertEqual(result['execution_state'], 'EXECUTED')


if __name__ == '__main__':
    unittest.main()
