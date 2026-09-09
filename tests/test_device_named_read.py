import json
import unittest

from agent_planner import plan_request
from dify_client import _PLANNER_HINT, _normalize_read_call, _synthetic_call


class DeviceNamedReadTests(unittest.TestCase):
    def test_planner_extracts_device_name_without_falling_back_to_all_devices(self):
        plan = plan_request('查一下水泵设备状态', {'device.search'}, {})
        self.assertEqual((plan['intent'], plan['action']), ('device.search', 'TOOL'))
        self.assertEqual(plan['arguments'].get('name'), '水泵')
        self.assertNotIn('code', plan['arguments'])

    def test_explicit_device_code_wins_over_natural_name(self):
        plan = plan_request('查一下P-01设备状态', {'device.search'}, {})
        self.assertEqual((plan['intent'], plan['action']), ('device.search', 'TOOL'))
        self.assertEqual(plan['arguments'].get('code'), 'P-01')
        self.assertNotIn('name', plan['arguments'])

    def test_provider_cannot_switch_planner_owned_device_name(self):
        plan = plan_request('查一下水泵设备状态', {'device.search'}, {})
        token = _PLANNER_HINT.set(plan)
        try:
            malicious = _synthetic_call({
                'operation': 'execute',
                'command': 'device.search',
                'arguments_json': json.dumps({'name': '电梯', 'status': 'normal'}, ensure_ascii=False),
            }, 'device-name-switch')
            normalized = _normalize_read_call(malicious)
        finally:
            _PLANNER_HINT.reset(token)
        outer = json.loads(normalized['function']['arguments'])
        self.assertEqual(outer['operation'], 'lookup')
        self.assertEqual(outer['command'], 'device.search')
        self.assertEqual(json.loads(outer['arguments_json']), {'name': '水泵'})


if __name__ == '__main__':
    unittest.main()
