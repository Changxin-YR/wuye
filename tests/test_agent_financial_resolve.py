import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import dify_financial_patch as financial_patch
from agent_planner import plan_request
from dify_client import BailianClient, _PLANNER_HINT


def completion(message, response_id='financial-test'):
    return {'id': response_id, 'choices': [{'message': message}]}


def malicious_write(command, arguments):
    return {
        'tool_calls': [{
            'id': 'model-guessed-write',
            'type': 'function',
            'function': {
                'name': 'property_agent_tool',
                'arguments': json.dumps({
                    'operation': 'propose',
                    'command': command,
                    'arguments_json': json.dumps(arguments, ensure_ascii=False),
                }, ensure_ascii=False),
            },
        }],
    }


class FinancialProviderResolveTests(unittest.TestCase):
    def run_client(self, plan, responses, callback):
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        token = _PLANNER_HINT.set(plan)
        try:
            with patch.object(client, '_request', side_effect=lambda *args, **kwargs: next(responses)):
                return client.chat('财务测试', 'property:1:v1', tool_callback=callback)
        finally:
            _PLANNER_HINT.reset(token)

    def test_bill_create_ignores_model_guessed_house_and_fee_ids(self):
        plan = plan_request(
            '给A栋101生成2026-09物业费账单，到期日2026-09-30',
            {'house.search', 'fee.search', 'bill.create'},
        )
        self.assertEqual(plan['action'], 'CONFIRM')
        self.assertEqual(plan['entity_status'], 'RESOLVE_MULTI')

        responses = iter([
            completion(malicious_write('bill.create', {
                'house_id': 999, 'fee_item_id': 998, 'period': '2035-01', 'due_date': '2035-01-01'
            }), 'one'),
            completion(malicious_write('bill.create', {
                'house_id': 997, 'fee_item_id': 996, 'period': '2036-01', 'due_date': '2036-01-01'
            }), 'two'),
            completion(malicious_write('bill.create', {
                'house_id': 995, 'fee_item_id': 994, 'period': '2037-01', 'due_date': '2037-01-01'
            }), 'three'),
            completion({'content': '已生成待确认账单'}, 'four'),
        ])
        calls = []

        def callback(args):
            calls.append(args)
            command = args['command']
            if command == 'house.search':
                return {
                    'ok': True, 'code': 'SUCCESS',
                    'data': {'items': [{'id': 11, 'community_id': 3, 'version': 4}]},
                    'terminal': False,
                }
            if command == 'fee.search':
                return {
                    'ok': True, 'code': 'SUCCESS',
                    'data': {'items': [{'id': 22, 'community_id': 3, 'version': 2}]},
                    'terminal': False,
                }
            if command == 'bill.create':
                params = json.loads(args['arguments_json'])
                self.assertEqual(params, {
                    'house_id': 11,
                    'fee_item_id': 22,
                    'period': '2026-09',
                    'due_date': '2026-09-30',
                })
                return {
                    'ok': True, 'code': 'CONFIRMATION_REQUIRED',
                    'data': {'status': 'pending'}, 'terminal': False,
                }
            self.fail('unexpected command: ' + command)

        result = self.run_client(plan, responses, callback)
        self.assertEqual([item['command'] for item in calls], ['house.search', 'fee.search', 'bill.create'])
        self.assertEqual(result['execution_state'], 'PENDING_CONFIRMATION')

    def test_bill_batch_ignores_model_guessed_building_and_fee_ids(self):
        plan = plan_request(
            '给23栋生成2026-09物业费账单，到期日2026-09-30',
            {'building.search', 'fee.search', 'bill.batch'},
        )
        self.assertEqual(plan['action'], 'CONFIRM')
        self.assertEqual(plan['entity_status'], 'RESOLVE_MULTI')

        responses = iter([
            completion(malicious_write('bill.batch', {
                'building_id': 900, 'fee_item_id': 901, 'period': '2035-01', 'due_date': '2035-01-01'
            }), 'one'),
            completion(malicious_write('bill.batch', {
                'building_id': 902, 'fee_item_id': 903, 'period': '2036-01', 'due_date': '2036-01-01'
            }), 'two'),
            completion(malicious_write('bill.batch', {
                'building_id': 904, 'fee_item_id': 905, 'period': '2037-01', 'due_date': '2037-01-01'
            }), 'three'),
            completion({'content': '已生成批量账单确认'}, 'four'),
        ])
        calls = []

        def callback(args):
            calls.append(args)
            command = args['command']
            if command == 'building.search':
                return {
                    'ok': True, 'code': 'SUCCESS',
                    'data': {'items': [{'id': 31, 'community_id': 7, 'version': 1}]},
                    'terminal': False,
                }
            if command == 'fee.search':
                return {
                    'ok': True, 'code': 'SUCCESS',
                    'data': {'items': [{'id': 32, 'community_id': 7, 'version': 1}]},
                    'terminal': False,
                }
            if command == 'bill.batch':
                params = json.loads(args['arguments_json'])
                self.assertEqual(params, {
                    'building_id': 31,
                    'fee_item_id': 32,
                    'period': '2026-09',
                    'due_date': '2026-09-30',
                })
                return {
                    'ok': True, 'code': 'CONFIRMATION_REQUIRED',
                    'data': {'status': 'pending'}, 'terminal': False,
                }
            self.fail('unexpected command: ' + command)

        result = self.run_client(plan, responses, callback)
        self.assertEqual([item['command'] for item in calls], ['building.search', 'fee.search', 'bill.batch'])
        self.assertEqual(result['execution_state'], 'PENDING_CONFIRMATION')

    def test_ambiguous_fee_stops_before_any_bill_proposal(self):
        plan = plan_request(
            '给A栋101生成2026-09物业费账单，到期日2026-09-30',
            {'house.search', 'fee.search', 'bill.create'},
        )
        responses = iter([
            completion({'content': '先查房屋'}, 'one'),
            completion({'content': '再查收费项目'}, 'two'),
            completion({'content': '存在多个同名收费项目，请先确认'}, 'three'),
        ])
        calls = []

        def callback(args):
            calls.append(args)
            if args['command'] == 'house.search':
                return {
                    'ok': True, 'code': 'SUCCESS',
                    'data': {'items': [{'id': 11, 'community_id': 3}]},
                    'terminal': False,
                }
            if args['command'] == 'fee.search':
                return {
                    'ok': True, 'code': 'SUCCESS',
                    'data': {'items': [
                        {'id': 21, 'community_id': 3, 'name': '物业费'},
                        {'id': 22, 'community_id': 3, 'name': '物业费'},
                    ]},
                    'terminal': False,
                }
            self.fail('bill proposal must not run after ambiguous fee resolution')

        result = self.run_client(plan, responses, callback)
        self.assertEqual([item['command'] for item in calls], ['house.search', 'fee.search'])
        self.assertNotEqual(result['execution_state'], 'PENDING_CONFIRMATION')


class PaymentServerOwnedParameterTests(unittest.TestCase):
    def test_model_cannot_switch_bill_version_amount_channel_or_reference(self):
        hint = {
            'action': 'CONFIRM',
            'intent': 'payment.record',
            'arguments': {
                'bill_id': 123,
                'amount': 500.0,
                'channel': 'cash',
                'reference': 'CASH-REAL-001',
            },
        }
        token = _PLANNER_HINT.set(hint)
        fake_db = object()
        fake_actor = object()
        fake_bill = SimpleNamespace(id=123, version=7)
        try:
            with patch.object(financial_patch._tools, 'object_session', return_value=fake_db), \
                 patch.object(financial_patch, 'Policy') as policy_cls, \
                 patch.object(financial_patch, '_base_normalize', side_effect=lambda actor, command, params: params):
                policy_cls.return_value.get.return_value = fake_bill
                normalized = financial_patch.normalize(fake_actor, 'payment.record', {
                    'bill_id': 999,
                    'version': 999,
                    'amount': '999999',
                    'channel': 'bank',
                    'reference': 'MODEL-GUESSED',
                })
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual(normalized, {
            'bill_id': 123,
            'version': 7,
            'amount': '500.0',
            'channel': 'cash',
            'reference': 'CASH-REAL-001',
        })
        policy_cls.return_value.get.assert_called_once_with(financial_patch.Bill, 123, True)

    def test_non_payment_or_non_planner_calls_keep_existing_normalize_semantics(self):
        token = _PLANNER_HINT.set({'action': 'TOOL', 'intent': 'order.create', 'arguments': {}})
        original = {'bill_id': 999, 'version': 8}
        try:
            with patch.object(financial_patch, '_base_normalize', side_effect=lambda actor, command, params: params):
                normalized = financial_patch.normalize(object(), 'payment.record', original)
        finally:
            _PLANNER_HINT.reset(token)
        self.assertEqual(normalized, original)


if __name__ == '__main__':
    unittest.main()
