import unittest

from agent_planner import plan_request


class AgentReadQuerySemanticsTests(unittest.TestCase):
    def plan(self, text, commands):
        return plan_request(text, set(commands))

    def assert_read(self, text, command, commands=None, **expected):
        allowed = set(commands or ()) | {command}
        result = self.plan(text, allowed)
        self.assertEqual(result['action'], 'TOOL', result)
        self.assertEqual(result['intent'], command, result)
        self.assertEqual(result['candidates'], [command], result)
        for key, value in expected.items():
            self.assertEqual(result['arguments'].get(key), value, result)

    def test_fee_read_does_not_become_fee_save(self):
        self.assert_read('查看收费项目', 'fee.search', {'fee.save'})
        self.assert_read('看看收费标准', 'fee.search', {'fee.save'})

    def test_explicit_complaint_query(self):
        self.assert_read('查看投诉单123的状态', 'complaint.search', id=123)

    def test_explicit_visitor_query(self):
        self.assert_read('查看访客记录23', 'visitor.search', id=23)

    def test_explicit_inspection_query(self):
        self.assert_read('查看巡检任务78的状态', 'inspection.search', id=78)

    def test_explicit_payment_query(self):
        self.assert_read('查看收款记录456', 'payment.search', id=456)

    def test_vehicle_query_by_plate(self):
        self.assert_read('查看车辆粤A12345', 'vehicle.search', plate='粤A12345')

    def test_parking_use_query(self):
        self.assert_read('查看车位使用记录12', 'parking_use.search', id=12)

    def test_explicit_mutation_is_not_downgraded_to_read(self):
        result = self.plan('新增收费项目物业费', {'fee.search', 'fee.save'})
        self.assertEqual(result['intent'], 'fee.save', result)
        self.assertNotEqual(result['action'], 'TOOL' if result['candidates'] == ['fee.search'] else 'NEVER')

    def test_read_permission_is_still_required(self):
        result = self.plan('查看收费项目', {'fee.save'})
        self.assertEqual(result['action'], 'DENY', result)
        self.assertEqual(result['intent'], 'fee.search', result)


if __name__ == '__main__':
    unittest.main()
