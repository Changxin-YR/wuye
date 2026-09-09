import json
import unittest
from datetime import datetime, time, timedelta, timezone

import dify_client
from agent_planner import plan_request
from dify_client import _PLANNER_HINT


AUTHORIZED = {'house.search', 'person.search', 'lease.create'}


def request_text():
    today = (datetime.now(timezone.utc) + timedelta(hours=8)).date()
    start = today - timedelta(days=1)
    end = today + timedelta(days=365)
    move_in = datetime.combine(today, time.min).isoformat(timespec='minutes')
    return (
        f'给王五登记租户入住，A栋101室，租期{start.isoformat()}到{end.isoformat()}，'
        f'实际入住时间{move_in}'
    )


class LeaseNewTenantScopeTests(unittest.TestCase):
    def test_person_resolver_uses_house_community_not_current_building_residency(self):
        plan = plan_request(request_text(), AUTHORIZED, {})
        self.assertEqual((plan['intent'], plan['action'], plan['entity_status']), (
            'lease.create', 'TOOL', 'RESOLVE_MULTI',
        ))
        token = _PLANNER_HINT.set(plan)
        try:
            house_call = dify_client._multi_resolver_fallback('house.search', {})
            self.assertIsNotNone(house_call)
            house_params = json.loads(house_call['arguments_json'])
            self.assertEqual(house_params, {'building_name': 'A栋', 'room_no': 101})

            person_call = dify_client._multi_resolver_fallback('person.search', {
                'house.search': {
                    'id': 31,
                    'community_id': 1,
                    'building_id': 7,
                    'building_name': 'A栋',
                    'room_no': 101,
                },
            })
            self.assertIsNotNone(person_call)
            person_params = json.loads(person_call['arguments_json'])
            self.assertEqual(person_params, {'person_name': '王五', 'community_id': 1})
            self.assertNotIn('building_id', person_params)
        finally:
            _PLANNER_HINT.reset(token)


if __name__ == '__main__':
    unittest.main()
