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

    def test_explicit_bill_lookup_target_is_owned_by_planner(self):
        token = _PLANNER_HINT.set({
            'action': 'TOOL',
            'intent': 'bill.search',
            'candidates': ['bill.search'],
            'arguments': {'bill_id': 123},
        })
        try:
            malicious = _synthetic_call({
                'operation': 'execute',
                'command': 'bill.search',
                'arguments_json': json.dumps({
                    'bill_id': 999,
                    'id': 998,
                    'status': 'unpaid',
                    'house_id': 777,
                }),
            }, 'bill-switch')
            normalized = _normalize_read_call(malicious)
        finally:
            _PLANNER_HINT.reset(token)
        args = self._args(normalized)
        self.assertEqual(args['operation'], 'lookup')
        self.assertEqual(args['command'], 'bill.search')
        self.assertEqual(json.loads(args['arguments_json']), {'bill_id': 123})

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
