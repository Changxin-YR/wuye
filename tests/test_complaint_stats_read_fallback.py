import json
import unittest
from datetime import datetime, timedelta, timezone

from agent_planner import plan_request
from dify_client import _PLANNER_HINT, _normalize_read_call, _planner_read_fallback, _synthetic_call


def current_china_month():
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime('%Y-%m')


class ComplaintStatsReadFallbackTests(unittest.TestCase):
    def plan(self):
        return plan_request('这个月投诉按楼栋统计一下', {'complaint.stats'}, {})

    def test_current_month_is_explicit_server_owned_business_semantic(self):
        plan = self.plan()
        self.assertEqual((plan['intent'], plan['action']), ('complaint.stats', 'TOOL'))
        self.assertEqual(plan['arguments'].get('month'), current_china_month())

    def test_provider_cannot_switch_complaint_stats_month(self):
        plan = self.plan()
        token = _PLANNER_HINT.set(plan)
        try:
            malicious = _synthetic_call({
                'operation': 'lookup',
                'command': 'complaint.stats',
                'arguments_json': json.dumps({'month': '2000-01'}, ensure_ascii=False),
            }, 'complaint-stats-switch')
            normalized = _normalize_read_call(malicious)
        finally:
            _PLANNER_HINT.reset(token)
        outer = json.loads(normalized['function']['arguments'])
        self.assertEqual(outer['operation'], 'lookup')
        self.assertEqual(json.loads(outer['arguments_json']), {'month': current_china_month()})

    def test_model_omission_uses_deterministic_complaint_stats_fallback(self):
        plan = self.plan()
        token = _PLANNER_HINT.set(plan)
        try:
            fallback = _planner_read_fallback()
        finally:
            _PLANNER_HINT.reset(token)
        self.assertIsNotNone(fallback)
        self.assertEqual(fallback['operation'], 'lookup')
        self.assertEqual(fallback['command'], 'complaint.stats')
        self.assertEqual(json.loads(fallback['arguments_json']), {'month': current_china_month()})


if __name__ == '__main__':
    unittest.main()
