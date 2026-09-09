import json
import unittest
from unittest.mock import patch

from dify_client import (
    BailianClient,
    _PLANNER_HINT,
    _READ_ONLY_INTENTS,
    _expected_write,
    _normalize_read_call,
    _planner_read_fallback,
    _synthetic_call,
)


class ReadOnlyOperationGuardTests(unittest.TestCase):
    def _args(self, call):
        return json.loads(call['function']['arguments'])

    def test_every_read_capability_is_forced_to_lookup(self):
        for command in sorted(_READ_ONLY_INTENTS):
            for unsafe_operation in ('execute', 'propose'):
                with self.subTest(command=command, operation=unsafe_operation):
                    call = _synthetic_call({
                        'operation': unsafe_operation,
                        'command': command,
                        'arguments_json': '{}',
                    }, f'{command}-{unsafe_operation}')
                    normalized = _normalize_read_call(call)
                    args = self._args(normalized)
                    self.assertEqual(args['command'], command)
                    self.assertEqual(args['operation'], 'lookup')

    def test_explicit_read_targets_are_owned_by_planner(self):
        cases = [
            ('house.search', {'building_name': '23栋', 'unit': '3', 'room_no': 311}, {'building_name': '24栋', 'unit': '9', 'room_no': 999, 'id': 999}, {'building_name': '23栋', 'unit': '3', 'room_no': 311}),
            ('building.search', {'community_id': 1, 'building_name': '3栋'}, {'community_id': 2, 'building_name': '8栋', 'id': 999}, {'community_id': 1, 'building_name': '3栋'}),
            ('unit.search', {'building_id': 7, 'unit': '3'}, {'building_id': 8, 'unit': '9', 'id': 999}, {'building_id': 7, 'unit': '3'}),
            ('person.search', {'person_name': '王五', 'phone': '13800000123'}, {'person_name': '赵六', 'phone': '13900000999', 'id': 999}, {'person_name': '王五', 'phone': '13800000123'}),
            ('person.properties', {'person_name': '王五', 'phone': '13800000123'}, {'person_name': '赵六', 'phone': '13900000999', 'id': 999}, {'person_name': '王五', 'phone': '13800000123'}),
            ('order.search', {'order_no': 'WO-2026-001'}, {'order_no': 'WO-EVIL'}, {'order_no': 'WO-2026-001'}),
            ('complaint.search', {'id': 123}, {'id': 999, 'status': 'open'}, {'id': 123}),
            ('visitor.search', {'id': 23}, {'id': 999, 'phone': '13800000000'}, {'id': 23}),
            ('vehicle.search', {'plate': '粤A12345'}, {'plate': '粤B99999', 'status': 'active'}, {'plate': '粤A12345'}),
            ('parking.search', {'space_code': 'A-001'}, {'space_code': 'B-999', 'status': 'free'}, {'space_code': 'A-001'}),
            ('parking_use.search', {'id': 12}, {'id': 999, 'status': 'active'}, {'id': 12}),
            ('device.search', {'code': 'P-01'}, {'code': 'P-99', 'status': 'normal'}, {'code': 'P-01'}),
            ('inspection.search', {'id': 78}, {'id': 999, 'status': 'pending'}, {'id': 78}),
            ('fee.search', {'id': 7}, {'id': 999, 'name': '其他费用'}, {'id': 7}),
            ('bill.search', {'bill_id': 123}, {'bill_id': 999, 'status': 'unpaid'}, {'bill_id': 123}),
            ('payment.search', {'id': 456}, {'id': 999, 'bill_id': 888}, {'id': 456}),
            ('billing.unpaid', {'bill_id': 321}, {'bill_id': 999, 'month': '2026-01'}, {'bill_id': 321}),
            ('notice.read', {'community_id': 1, 'building_name': '3栋'}, {'community_id': 2, 'building_name': '8栋'}, {'community_id': 1, 'building_name': '3栋'}),
        ]
        for command, planner_arguments, malicious_arguments, expected in cases:
            with self.subTest(command=command):
                token = _PLANNER_HINT.set({
                    'action': 'TOOL',
                    'intent': command,
                    'candidates': [command],
                    'arguments': planner_arguments,
                })
                try:
                    malicious = _synthetic_call({
                        'operation': 'execute',
                        'command': command,
                        'arguments_json': json.dumps(malicious_arguments, ensure_ascii=False),
                    }, command + '-switch')
                    normalized = _normalize_read_call(malicious)
                finally:
                    _PLANNER_HINT.reset(token)
                args = self._args(normalized)
                self.assertEqual(args['operation'], 'lookup')
                self.assertEqual(args['command'], command)
                self.assertEqual(json.loads(args['arguments_json']), expected)

    def test_read_without_explicit_planner_target_keeps_safe_provider_filters(self):
        token = _PLANNER_HINT.set({
            'action': 'TOOL',
            'intent': 'visitor.search',
            'candidates': ['visitor.search'],
            'arguments': {},
        })
        try:
            call = _synthetic_call({
                'operation': 'lookup',
                'command': 'visitor.search',
                'arguments_json': json.dumps({'status': 'inside'}, ensure_ascii=False),
            })
            normalized = _normalize_read_call(call)
        finally:
            _PLANNER_HINT.reset(token)
        args = self._args(normalized)
        self.assertEqual(json.loads(args['arguments_json']), {'status': 'inside'})

    def test_missing_provider_tool_call_uses_planner_read_fallback_once(self):
        token = _PLANNER_HINT.set({
            'action': 'TOOL',
            'intent': 'complaint.search',
            'candidates': ['complaint.search'],
            'arguments': {'id': 123, 'complaint_id': 123},
        })
        seen = []
        client = BailianClient('http://agent.invalid', 'fixture-key', 'qwen-plus')
        responses = iter([
            {'id': 'one', 'choices': [{'message': {'content': '我来查一下'}}]},
            {'id': 'two', 'choices': [{'message': {'content': '投诉单123当前状态已查询。'}}]},
        ])

        def tool(args):
            seen.append(dict(args))
            return {
                'ok': True,
                'code': 'SUCCESS',
                'data': {'items': [{'id': 123, 'status': 'open'}]},
                'terminal': True,
            }

        try:
            with patch.object(client, '_request', side_effect=lambda *args, **kwargs: next(responses)):
                result = client.chat('查看投诉单123的状态', 'admin', None, tool)
        finally:
            _PLANNER_HINT.reset(token)
        self.assertEqual(result['execution_state'], 'LOOKUP_ONLY')
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]['operation'], 'lookup')
        self.assertEqual(seen[0]['command'], 'complaint.search')
        self.assertEqual(json.loads(seen[0]['arguments_json']), {'id': 123})

    def test_read_fallback_never_runs_for_non_tool_or_write_plan(self):
        cases = [
            {'action': 'CLARIFY', 'intent': 'complaint.search', 'candidates': ['complaint.search'], 'arguments': {'id': 123}},
            {'action': 'DENY', 'intent': 'complaint.search', 'candidates': [], 'arguments': {'id': 123}},
            {'action': 'TOOL', 'intent': 'visitor.create', 'candidates': ['visitor.create'], 'arguments': {}},
        ]
        for hint in cases:
            with self.subTest(hint=hint):
                token = _PLANNER_HINT.set(hint)
                try:
                    self.assertIsNone(_planner_read_fallback())
                finally:
                    _PLANNER_HINT.reset(token)

    def test_read_fallback_never_marks_turn_as_expected_write(self):
        for command in sorted(_READ_ONLY_INTENTS):
            with self.subTest(command=command):
                token = _PLANNER_HINT.set({
                    'action': 'TOOL',
                    'intent': command,
                    'tool_call': {
                        'operation': 'execute',
                        'command': command,
                        'arguments_json': '{}',
                    },
                })
                try:
                    self.assertFalse(_expected_write())
                finally:
                    _PLANNER_HINT.reset(token)

    def test_mutation_operation_is_not_silently_downgraded(self):
        call = _synthetic_call({
            'operation': 'execute',
            'command': 'visitor.create',
            'arguments_json': '{"house_id":1}',
        }, 'visitor-write')
        normalized = _normalize_read_call(call)
        self.assertEqual(self._args(normalized)['operation'], 'execute')

    def test_mutation_fallback_still_counts_as_expected_write(self):
        token = _PLANNER_HINT.set({
            'action': 'TOOL',
            'intent': 'visitor.create',
            'tool_call': {
                'operation': 'execute',
                'command': 'visitor.create',
                'arguments_json': '{}',
            },
        })
        try:
            self.assertTrue(_expected_write())
        finally:
            _PLANNER_HINT.reset(token)

    def test_malformed_call_is_left_for_existing_validator(self):
        malformed = {'id': 'bad', 'type': 'function', 'function': {'name': 'property_agent_tool', 'arguments': '{'}}
        self.assertIs(_normalize_read_call(malformed), malformed)


if __name__ == '__main__':
    unittest.main()
