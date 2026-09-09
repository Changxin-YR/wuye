import json
import unittest
from unittest.mock import patch

from agent_planner import plan_request
from dify_client import BailianClient, _PLANNER_HINT


def tool_call(call_id, command, arguments):
    return {
        'id': call_id,
        'type': 'function',
        'function': {
            'name': 'property_agent_tool',
            'arguments': json.dumps({
                'operation': 'execute',
                'command': command,
                'arguments_json': json.dumps(arguments, ensure_ascii=False),
            }, ensure_ascii=False),
        },
    }


class ComplaintCreatePlannerTests(unittest.TestCase):
    AUTHORIZED = {'house.search', 'complaint.create'}

    def test_natural_complaint_uses_house_resolver_and_preserves_user_content(self):
        plan = plan_request('23栋311投诉晚上施工太吵', self.AUTHORIZED, {})
        self.assertEqual((plan['intent'], plan['action']), ('complaint.create', 'TOOL'))
        self.assertEqual(plan['entity_status'], 'RESOLVE_MULTI')
        self.assertEqual(plan['candidates'], ['house.search', 'complaint.create'])
        self.assertEqual(plan['arguments']['building_name'], '23栋')
        self.assertEqual(plan['arguments']['room_no'], 311)
        self.assertEqual(plan['arguments']['complaint_content'], '晚上施工太吵')
        self.assertEqual(plan['arguments']['complaint_category'], '噪音')
        self.assertNotIn('house_id', plan['arguments'])

    def test_missing_house_or_content_is_clarified_without_guessing(self):
        missing_house = plan_request('登记投诉：晚上施工太吵', self.AUTHORIZED, {})
        self.assertEqual(missing_house['intent'], 'complaint.create')
        self.assertEqual(missing_house['action'], 'CLARIFY')
        self.assertIn('house', missing_house['missing_fields'])
        self.assertNotIn('house_id', missing_house['arguments'])

        missing_content = plan_request('23栋311登记投诉', self.AUTHORIZED, {})
        self.assertEqual(missing_content['intent'], 'complaint.create')
        self.assertEqual(missing_content['action'], 'CLARIFY')
        self.assertIn('content', missing_content['missing_fields'])


class ComplaintCreateProviderTests(unittest.TestCase):
    AUTHORIZED = {'house.search', 'complaint.create'}

    def test_model_cannot_guess_or_switch_complaint_house(self):
        plan = plan_request('23栋311投诉晚上施工太吵', self.AUTHORIZED, {})
        self.assertEqual(plan['entity_status'], 'RESOLVE_MULTI')
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        callbacks = []
        responses = iter([
            {'id': 'c1', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('bad-1', 'complaint.create', {
                    'house_id': 999, 'title': '伪造标题', 'content': '伪造内容', 'category': '伪造',
                })],
            }}]},
            {'id': 'c2', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('bad-2', 'complaint.create', {
                    'house_id': 998, 'title': '另一个标题', 'content': '另一个内容', 'category': '其他',
                })],
            }}]},
            {'id': 'c3', 'choices': [{'message': {'role': 'assistant', 'content': '投诉已登记。'}}]},
        ])

        def tool(args):
            callbacks.append(dict(args))
            params = json.loads(args['arguments_json'])
            if args['command'] == 'house.search':
                self.assertEqual(args['operation'], 'lookup')
                self.assertEqual(params, {'building_name': '23栋', 'room_no': 311})
                return {
                    'ok': True, 'code': 'SUCCESS',
                    'data': {'items': [{
                        'id': 31, 'community_id': 1, 'building_id': 7,
                        'building_name': '23栋', 'room_no': 311,
                    }]},
                    'terminal': False,
                }
            self.assertEqual(args['command'], 'complaint.create')
            self.assertEqual(args['operation'], 'execute')
            self.assertEqual(params, {
                'house_id': 31,
                'title': '噪音投诉',
                'content': '晚上施工太吵',
                'category': '噪音',
            })
            return {
                'ok': True, 'code': 'SUCCESS',
                'data': {'status': 'executed', 'id': 61},
                'terminal': True,
            }

        poisoned = dict(plan)
        poisoned['arguments'] = {**plan['arguments'], 'house_id': 123456}
        token = _PLANNER_HINT.set(poisoned)
        try:
            with patch.object(client, '_request', side_effect=lambda *a, **k: next(responses)):
                result = client.chat('23栋311投诉晚上施工太吵', 'property:1:v1', tool_callback=tool)
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual([item['command'] for item in callbacks], ['house.search', 'complaint.create'])
        dumped = json.dumps(callbacks, ensure_ascii=False)
        for guessed in ('999', '998', '123456', '伪造标题', '伪造内容', '另一个标题', '另一个内容'):
            self.assertNotIn(guessed, dumped)
        self.assertEqual(result['execution_state'], 'EXECUTED')

    def test_ambiguous_house_stops_before_complaint_write(self):
        plan = plan_request('23栋311投诉晚上施工太吵', self.AUTHORIZED, {})
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        callbacks = []
        responses = iter([
            {'id': 'a1', 'choices': [{'message': {'role': 'assistant', 'content': '核对房屋。'}}]},
            {'id': 'a2', 'choices': [{'message': {'role': 'assistant', 'content': '找到多套同号房屋，请补充小区或单元。'}}]},
        ])

        def tool(args):
            callbacks.append(dict(args))
            self.assertEqual(args['command'], 'house.search')
            return {
                'ok': True, 'code': 'SUCCESS',
                'data': {'items': [
                    {'id': 31, 'community_id': 1, 'building_id': 7, 'building_name': '23栋', 'room_no': 311},
                    {'id': 32, 'community_id': 2, 'building_id': 8, 'building_name': '23栋', 'room_no': 311},
                ]},
                'terminal': False,
            }

        token = _PLANNER_HINT.set(plan)
        try:
            with patch.object(client, '_request', side_effect=lambda *a, **k: next(responses)):
                result = client.chat('23栋311投诉晚上施工太吵', 'property:1:v1', tool_callback=tool)
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual([item['command'] for item in callbacks], ['house.search'])
        self.assertEqual(result['execution_state'], 'LOOKUP_ONLY')


if __name__ == '__main__':
    unittest.main()
