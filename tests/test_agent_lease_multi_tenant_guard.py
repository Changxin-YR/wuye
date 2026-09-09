import unittest
from datetime import datetime, time, timedelta, timezone

from agent_planner import plan_request


AUTHORIZED = {'house.search', 'person.search', 'lease.create'}


def lease_suffix():
    today = (datetime.now(timezone.utc) + timedelta(hours=8)).date()
    start = today - timedelta(days=1)
    end = today + timedelta(days=365)
    move_in = datetime.combine(today, time.min).isoformat(timespec='minutes')
    return f'A栋101室，租期{start.isoformat()}到{end.isoformat()}，实际入住时间{move_in}'


class LeaseMultiTenantGuardTests(unittest.TestCase):
    def assert_multi_tenant_is_not_reduced(self, text):
        plan = plan_request(text, AUTHORIZED, {})
        self.assertEqual(plan['intent'], 'lease.create')
        self.assertEqual(plan['action'], 'CLARIFY')
        self.assertEqual(plan['missing_fields'], ['person'])
        self.assertNotIn('person_name', plan['arguments'])
        self.assertNotIn('person_id', plan['arguments'])
        self.assertNotIn('person_ids', plan['arguments'])
        self.assertIn('一次只支持一位租户', plan['clarification_text'])
        self.assertEqual(plan['arguments']['building_name'], 'A栋')
        self.assertEqual(plan['arguments']['room_no'], 101)
        self.assertTrue(plan['arguments'].get('start_date'))
        self.assertTrue(plan['arguments'].get('end_date'))
        self.assertTrue(plan['arguments'].get('move_in'))

    def test_comma_separated_tenants_are_never_silently_reduced(self):
        self.assert_multi_tenant_is_not_reduced('王五、李四办理入住，' + lease_suffix())

    def test_and_joined_tenants_are_never_silently_reduced(self):
        self.assert_multi_tenant_is_not_reduced('给王五和李四办理入住，' + lease_suffix())

    def test_single_tenant_ban_does_not_break_normal_variant(self):
        plan = plan_request('王五办理入住，' + lease_suffix(), AUTHORIZED, {})
        self.assertEqual((plan['intent'], plan['action']), ('lease.create', 'TOOL'))
        self.assertEqual(plan['entity_status'], 'RESOLVE_MULTI')
        self.assertEqual(plan['arguments']['person_name'], '王五')


if __name__ == '__main__':
    unittest.main()
