import unittest

import agent_lease_checkout_patch  # noqa: F401 - installs the safety repair
from agent_planner import plan_request


class LeaseCheckoutNaturalReasonTests(unittest.TestCase):
    def test_move_out_statement_is_user_reason_and_requires_confirmation(self):
        plan = plan_request(
            '王五已经搬走了',
            {'lease.search', 'lease.checkout'},
        )
        self.assertEqual(plan['intent'], 'lease.checkout')
        self.assertEqual(plan['action'], 'CONFIRM')
        self.assertEqual(plan['entity_status'], 'RESOLVE_FIRST')
        self.assertEqual(plan['candidates'], ['lease.search', 'lease.checkout'])
        self.assertEqual(plan['arguments']['person_name'], '王五')
        self.assertIn('搬走', plan['arguments']['reason'])
        self.assertEqual(plan['arguments']['status'], 'active')

    def test_missing_safe_resolver_never_upgrades_to_execution(self):
        plan = plan_request('王五已经搬走了', {'lease.checkout'})
        self.assertEqual(plan['intent'], 'lease.checkout')
        self.assertEqual(plan['action'], 'CLARIFY')
        self.assertEqual(plan['candidates'], ['lease.checkout'])
        self.assertIn('lease', plan['missing_fields'])


if __name__ == '__main__':
    unittest.main()
