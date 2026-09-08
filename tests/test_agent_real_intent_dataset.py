import json
import unittest
from pathlib import Path

from agent_planner import READ_COMMANDS, RESOLVER_CANDIDATES, plan_request


CANONICAL = {
    'vehicle.lookup': 'parking.search',
    'parking.lookup': 'parking.search',
    'device.lookup': 'device.search',
}
# Historical acceptance row 68 described a read request ("查一下...") but was
# accidentally labelled visitor.create/CLARIFY. Row 98 asks for resident
# identities for a whole building, which belongs to person.read/person.search,
# not property.read/house.search. Keep the fixture immutable for audit history
# and apply the documented semantic corrections here.
CASE_INTENT_CORRECTIONS = {68: 'visitor.search', 98: 'person.search'}


class RealIntentDatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).parent / 'fixtures' / 'real_agent_tasks.json'
        cls.cases = json.loads(path.read_text(encoding='utf-8'))
        cls.authorized = set(READ_COMMANDS)
        for values in RESOLVER_CANDIDATES.values():
            cls.authorized.update(values)
        for case in cls.cases:
            intent = CASE_INTENT_CORRECTIONS.get(case['id'], CANONICAL.get(case['expected_intent'], case['expected_intent']))
            if intent not in {'security_boundary', 'person.lookup'}:
                cls.authorized.add(intent)
        cls.context = {
            'writable_communities': [
                {'id': 1, 'name': 'A小区'},
                {'id': 2, 'name': 'B小区'},
            ]
        }

    def test_all_real_world_inputs_have_the_expected_business_intent(self):
        failures = []
        for case in self.cases:
            expected = CASE_INTENT_CORRECTIONS.get(case['id'], CANONICAL.get(case['expected_intent'], case['expected_intent']))
            result = plan_request(case['input'], self.authorized, self.context)
            if result.get('intent') != expected:
                failures.append((case['id'], case['input'], expected, result.get('intent'), result.get('action')))
        self.assertEqual(failures, [], 'intent mismatches: ' + repr(failures))

    def test_no_non_security_real_task_falls_back_to_unknown(self):
        unknown = []
        for case in self.cases:
            if case['expected_intent'] == 'security_boundary':
                continue
            result = plan_request(case['input'], self.authorized, self.context)
            if result.get('intent') == 'unknown':
                unknown.append((case['id'], case['input']))
        self.assertEqual(unknown, [], 'unknown intents: ' + repr(unknown))

    def test_visitor_query_case_has_documented_corrected_semantics(self):
        case = next(item for item in self.cases if item['id'] == 68)
        result = plan_request(case['input'], self.authorized, self.context)
        self.assertEqual((result['intent'], result['action']), ('visitor.search', 'TOOL'))

    def test_building_resident_directory_uses_person_read_semantics(self):
        case = next(item for item in self.cases if item['id'] == 98)
        result = plan_request(case['input'], self.authorized, self.context)
        self.assertEqual((result['intent'], result['action']), ('person.search', 'TOOL'))
        self.assertEqual(result['arguments'].get('building_name'), '23栋')


if __name__ == '__main__':
    unittest.main()
