import json
import unittest
from datetime import datetime, timedelta, timezone

from agent_planner import plan_request
from dify_client import _PLANNER_HINT, _normalize_read_call, _synthetic_call


def china_today():
    return (datetime.now(timezone.utc) + timedelta(hours=8)).date().isoformat()


class SemanticReadFilterPlannerTests(unittest.TestCase):
    def test_today_visitor_query_keeps_local_business_date(self):
        plan = plan_request('查一下今天登记的访客', {'visitor.search'}, {})
        self.assertEqual((plan['intent'], plan['action']), ('visitor.search', 'TOOL'))
        self.assertEqual(plan['arguments'].get('created_date'), china_today())

    def test_provider_cannot_change_today_visitor_date(self):
        plan = plan_request('查一下今天登记的访客', {'visitor.search'}, {})
        token = _PLANNER_HINT.set(plan)
        try:
            malicious = _synthetic_call({
                'operation': 'lookup',
                'command': 'visitor.search',
                'arguments_json': json.dumps({'created_date': '2000-01-01', 'status': 'inside'}, ensure_ascii=False),
            }, 'visitor-date-switch')
            normalized = _normalize_read_call(malicious)
        finally:
            _PLANNER_HINT.reset(token)
        outer = json.loads(normalized['function']['arguments'])
        self.assertEqual(outer['operation'], 'lookup')
        self.assertEqual(json.loads(outer['arguments_json']), {'created_date': china_today()})

    def test_available_parking_query_binds_available_status(self):
        plan = plan_request('查一下还有哪些空车位', {'parking.search'}, {})
        self.assertEqual((plan['intent'], plan['action']), ('parking.search', 'TOOL'))
        self.assertEqual(plan['arguments'].get('status'), 'available')

    def test_provider_cannot_change_available_parking_to_occupied(self):
        plan = plan_request('查一下还有哪些空车位', {'parking.search'}, {})
        token = _PLANNER_HINT.set(plan)
        try:
            malicious = _synthetic_call({
                'operation': 'lookup',
                'command': 'parking.search',
                'arguments_json': json.dumps({'status': 'occupied'}, ensure_ascii=False),
            }, 'parking-status-switch')
            normalized = _normalize_read_call(malicious)
        finally:
            _PLANNER_HINT.reset(token)
        outer = json.loads(normalized['function']['arguments'])
        self.assertEqual(outer['operation'], 'lookup')
        self.assertEqual(json.loads(outer['arguments_json']), {'status': 'available'})


if __name__ == '__main__':
    unittest.main()
