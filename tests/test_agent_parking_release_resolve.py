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


class ParkingReleaseResolveTests(unittest.TestCase):
    AUTHORIZED = {'parking_use.search', 'parking.release'}

    def test_complete_release_resolves_active_use_before_confirmation(self):
        plan = plan_request('释放A-001车位，原因租期结束', self.AUTHORIZED, {})
        self.assertEqual((plan['intent'], plan['action']), ('parking.release', 'CONFIRM'))
        self.assertEqual(plan['entity_status'], 'RESOLVE_FIRST')
        self.assertEqual(plan['candidates'], ['parking_use.search', 'parking.release'])
        self.assertEqual(plan['arguments']['space_code'], 'A-001')
        self.assertEqual(plan['arguments']['status'], 'active')
        self.assertEqual(plan['arguments']['reason'], '租期结束')

    def test_release_without_visible_relation_or_reason_stays_clarify(self):
        plan = plan_request('结束刚才的车位使用', self.AUTHORIZED, {})
        self.assertEqual((plan['intent'], plan['action']), ('parking.release', 'CLARIFY'))
        self.assertIn('parking_use', plan['missing_fields'])
        self.assertIn('reason', plan['missing_fields'])

    def test_model_cannot_guess_parking_use_id_version_or_reason(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        callbacks = []
        responses = iter([
            {'id': 'r-1', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('bad-1', 'propose', 'parking.release', {
                    'id': 999, 'version': 77, 'reason': '模型猜测原因',
                })],
            }}]},
            {'id': 'r-2', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('bad-2', 'propose', 'parking.release', {
                    'id': 998, 'version': 76, 'reason': '另一个原因',
                })],
            }}]},
            {'id': 'r-3', 'choices': [{'message': {'role': 'assistant', 'content': '需要你确认后才会释放。'}}]},
        ])

        def tool(args):
            callbacks.append(dict(args))
            params = json.loads(args['arguments_json'])
            if args['command'] == 'parking_use.search':
                self.assertEqual(args['operation'], 'lookup')
                self.assertEqual(params, {'space_code': 'A-001', 'status': 'active'})
                return {
                    'ok': True, 'code': 'SUCCESS',
                    'data': {'items': [{
                        'id': 51, 'version': 3, 'community_id': 1, 'building_id': 7,
                        'space_id': 31, 'vehicle_id': 41, 'status': 'active',
                    }]},
                    'terminal': False,
                }
            self.assertEqual(args['command'], 'parking.release')
            self.assertEqual(args['operation'], 'propose')
            self.assertEqual(params, {'id': 51, 'version': 3, 'reason': '租期结束'})
            return {
                'ok': True, 'code': 'CONFIRMATION_REQUIRED',
                'data': {'status': 'pending', 'id': 'action-1'},
                'terminal': True,
            }

        token = _PLANNER_HINT.set({
            'action': 'CONFIRM',
            'intent': 'parking.release',
            'entity_status': 'RESOLVE_FIRST',
            'candidates': ['parking_use.search', 'parking.release'],
            'arguments': {
                'space_code': 'A-001', 'status': 'active', 'reason': '租期结束',
                'id': 123456, 'version': 88,
            },
            'tool_call': {
                'operation': 'propose', 'command': 'parking.release',
                'arguments_json': json.dumps({'id': 123456, 'version': 88, 'reason': '伪造'}),
            },
        })
        try:
            with patch.object(client, '_request', side_effect=lambda *a, **k: next(responses)):
                result = client.chat(
                    '释放A-001车位，原因租期结束',
                    'property:1:v1',
                    tool_callback=tool,
                )
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual([item['command'] for item in callbacks], ['parking_use.search', 'parking.release'])
        dumped = json.dumps(callbacks, ensure_ascii=False)
        for guessed in ('999', '998', '77', '76', '123456', '88', '模型猜测原因', '另一个原因', '伪造'):
            self.assertNotIn(guessed, dumped)
        self.assertEqual(result['execution_state'], 'PENDING_CONFIRMATION')

    def test_ambiguous_active_use_stops_before_proposal(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        callbacks = []
        responses = iter([
            {'id': 'ra-1', 'choices': [{'message': {'role': 'assistant', 'content': '我先核对有效车位关系。'}}]},
            {'id': 'ra-2', 'choices': [{'message': {'role': 'assistant', 'content': '找到多条关系，请补充车牌。'}}]},
        ])

        def tool(args):
            callbacks.append(dict(args))
            self.assertEqual(args['command'], 'parking_use.search')
            return {
                'ok': True, 'code': 'SUCCESS',
                'data': {'items': [
                    {'id': 51, 'version': 1, 'status': 'active'},
                    {'id': 52, 'version': 1, 'status': 'active'},
                ]},
                'terminal': False,
            }

        token = _PLANNER_HINT.set({
            'action': 'CONFIRM', 'intent': 'parking.release', 'entity_status': 'RESOLVE_FIRST',
            'candidates': ['parking_use.search', 'parking.release'],
            'arguments': {'space_code': 'A-001', 'status': 'active', 'reason': '租期结束'},
            'tool_call': None,
        })
        try:
            with patch.object(client, '_request', side_effect=lambda *a, **k: next(responses)):
                result = client.chat(
                    '释放A-001车位，原因租期结束',
                    'property:1:v1',
                    tool_callback=tool,
                )
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual([item['command'] for item in callbacks], ['parking_use.search'])
        self.assertEqual(result['execution_state'], 'LOOKUP_ONLY')


if __name__ == '__main__':
    unittest.main()
