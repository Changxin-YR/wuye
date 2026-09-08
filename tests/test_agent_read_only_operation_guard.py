import json
import unittest

from dify_client import (
    _PLANNER_HINT,
    _READ_ONLY_INTENTS,
    _expected_write,
    _normalize_read_call,
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
