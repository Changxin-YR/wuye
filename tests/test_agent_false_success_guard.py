import json
import unittest
from unittest.mock import patch

from dify_client import BailianClient, _PLANNER_HINT


class UnresolvedWriteFalseSuccessTests(unittest.TestCase):
    def test_write_intent_without_fallback_cannot_claim_success(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        hint = {
            'action': 'TOOL',
            'intent': 'bill.create',
            'candidates': ['billing.unpaid', 'bill.create'],
            'tool_call': None,
        }
        token = _PLANNER_HINT.set(hint)
        try:
            with patch.object(client, '_request', return_value={
                'id': 'false-success',
                'choices': [{'message': {'content': 'A栋101物业费账单已成功生成。'}}],
            }):
                result = client.chat(
                    '生成A栋101的物业费账单',
                    'property:1:v1',
                    tool_callback=lambda _: {'ok': True},
                )
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual(result['execution_state'], 'NOT_EXECUTED')
        self.assertIn('没有完成任何业务写入', result['answer'])
        self.assertNotIn('已成功生成', result['answer'])

    def test_read_intent_can_fallback_to_lookup_without_mutation(self):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        hint = {'action': 'TOOL', 'intent': 'billing.unpaid', 'candidates': ['billing.unpaid'], 'tool_call': None}
        seen = []
        token = _PLANNER_HINT.set(hint)
        try:
            with patch.object(client, '_request', return_value={
                'id': 'read-answer',
                'choices': [{'message': {'content': '当前没有未缴账单。'}}],
            }):
                def callback(args):
                    seen.append(dict(args))
                    return {'ok': True, 'code': 'SUCCESS', 'data': {'items': []}, 'terminal': True}

                result = client.chat('查一下欠费', 'property:1:v1', tool_callback=callback)
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual(result['execution_state'], 'LOOKUP_ONLY')
        self.assertEqual(result['answer'], '当前没有未缴账单。')
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]['operation'], 'lookup')
        self.assertEqual(seen[0]['command'], 'billing.unpaid')
        self.assertEqual(json.loads(seen[0]['arguments_json']), {})


if __name__ == '__main__':
    unittest.main()
