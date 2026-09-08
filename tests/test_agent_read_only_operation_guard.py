import json
import unittest

from dify_client import _READ_ONLY_INTENTS, _normalize_read_call, _synthetic_call


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

    def test_mutation_operation_is_not_silently_downgraded(self):
        call = _synthetic_call({
            'operation': 'execute',
            'command': 'visitor.create',
            'arguments_json': '{"house_id":1}',
        }, 'visitor-write')
        normalized = _normalize_read_call(call)
        self.assertEqual(self._args(normalized)['operation'], 'execute')

    def test_malformed_call_is_left_for_existing_validator(self):
        malformed = {'id': 'bad', 'type': 'function', 'function': {'name': 'property_agent_tool', 'arguments': '{'}}
        self.assertIs(_normalize_read_call(malformed), malformed)


if __name__ == '__main__':
    unittest.main()
