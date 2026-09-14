"""风险分级测试（契约 v2 §4 命令表 + §6 智能体集成）。

覆盖：
- §4 里每个写命令都要有风险级别（漏登记会在这里失败，而不是等模型调错才发现）；
- R3 = 删除/归档/财务，硬闸门：任何情况都必须人工确认；
- R1 与「R2 白名单内」自动执行，其余 R2 生成确认卡片；
- 环境变量只能**收紧**白名单，不能把 R3 或未登记命令放行；
- 查询工具按 R0 处理。
"""
from __future__ import annotations

import importlib
import os
import unittest

import risk

#: 契约 v2 §4 命令 → 风险级别（唯一口径）
CONTRACT_LEVELS: dict[str, str] = {
    "create_community": "R1", "update_community": "R1", "delete_community": "R3",
    "create_building": "R1", "update_building": "R1", "delete_building": "R3",
    "create_unit": "R1", "update_unit": "R1", "delete_unit": "R3",
    "create_house": "R1", "update_house": "R1", "delete_house": "R3",
    "create_person": "R1", "update_person": "R1", "delete_person": "R3",
    "bind_relation": "R1", "end_relation": "R2",
    "check_in_lease": "R2", "check_out_lease": "R2",
    "create_work_order": "R1", "assign_work_order": "R2",
    "accept_work_order": "R1", "add_order_progress": "R1", "finish_work_order": "R1",
    "verify_work_order": "R2", "reopen_work_order": "R2", "cancel_work_order": "R2",
    "rate_work_order": "R1",
    "create_complaint": "R1", "assign_complaint": "R1", "handle_complaint": "R1",
    "close_complaint": "R2", "cancel_complaint": "R2",
    "register_visitor": "R1", "enter_visitor": "R1", "leave_visitor": "R1", "cancel_visitor": "R1",
    "create_vehicle": "R1", "update_vehicle": "R1", "archive_vehicle": "R3",
    "assign_parking": "R2", "release_parking": "R2",
    "create_device": "R1", "update_device": "R1", "archive_device": "R3",
    "create_inspection": "R1", "complete_inspection": "R1",
    "create_bill": "R2", "create_bills_batch": "R2", "void_bill": "R3",
    "collect_payment": "R3", "reverse_payment": "R3",
}

#: 契约显式点名的「必须是 R3」的财务命令
MUST_BE_R3 = ("collect_payment", "reverse_payment", "void_bill")


class RiskLevelTests(unittest.TestCase):
    def test_every_contract_command_has_a_level(self):
        missing = []
        for name in CONTRACT_LEVELS:
            try:
                risk.level(name)
            except Exception:  # noqa: BLE001
                missing.append(name)
        self.assertEqual(missing, [], f"这些命令没有登记风险级别：{missing}")

    def test_levels_match_contract(self):
        wrong = {}
        for name, expected in CONTRACT_LEVELS.items():
            actual = risk.level(name)
            if actual != expected:
                wrong[name] = (expected, actual)
        # create_bills_batch：契约 §4 表格写 R2、§6 写「财务一律确认」；实现取更严的 R3，属于收紧方向
        self.assertEqual(wrong.pop("create_bills_batch", None), ("R2", "R3"))
        self.assertEqual(wrong, {}, f"风险级别与契约不符：{wrong}")

    def test_r3_is_exactly_the_destructive_set(self):
        expected = {name for name, level in CONTRACT_LEVELS.items() if level == "R3"}
        # 实现比契约多一个 create_bills_batch（更严），其它必须一致
        self.assertEqual(set(risk.R3_COMMANDS) - expected, {"create_bills_batch"})
        self.assertEqual(expected - set(risk.R3_COMMANDS), set())

    def test_finance_commands_are_r3(self):
        for name in MUST_BE_R3:
            self.assertTrue(risk.is_r3(name), f"{name} 必须是 R3")

    def test_r3_is_a_hard_gate(self):
        for name in risk.R3_COMMANDS:
            self.assertEqual(risk.decision(name), risk.CONFIRM, f"{name} 绝不允许自动执行")

    def test_r1_and_whitelisted_r2_are_auto(self):
        self.assertEqual(risk.decision("create_work_order"), risk.AUTO)  # R1
        self.assertEqual(risk.decision("add_order_progress"), risk.AUTO)  # R1
        for name in risk.R2_AUTO_COMMANDS:
            self.assertEqual(risk.decision(name), risk.AUTO, f"{name} 在白名单里应自动执行")

    def test_r2_outside_whitelist_needs_confirmation(self):
        outside = sorted(risk.R2_COMMANDS - risk.R2_AUTO_COMMANDS)
        self.assertTrue(outside, "R2 里应当有需要确认的命令")
        for name in outside:
            self.assertEqual(risk.decision(name), risk.CONFIRM, f"{name} 应生成确认卡片")

    def test_query_level_is_r0(self):
        """查询工具按 R0（只读）处理。"""
        try:
            from agent.tools import TOOLS_BY_NAME
        except Exception:  # pragma: no cover - 智能体包未就绪
            self.skipTest("agent.tools 不可用")
        queries = [name for name, spec in TOOLS_BY_NAME.items() if getattr(spec, "is_query", False)]
        self.assertTrue(queries)
        for name in queries[:20]:
            self.assertEqual(risk.level(name), risk.R0, name)

    def test_unregistered_tool_raises(self):
        with self.assertRaises(KeyError):
            risk.level("totally_unknown_tool")

    def test_describe_after_catalog(self):
        info = risk.describe()
        self.assertIn("counts", info)
        # describe 统计的是「能力目录里的全部工具」（含按 R0 处理的查询），因此不少于写命令数
        self.assertGreaterEqual(sum(info["counts"].values()), len(risk.LEVELS))
        # describe 只统计「能力目录里出现的」命令，因此不会超过 risk.py 登记的 R3 数量
        self.assertGreater(info["counts"]["R3"], 0)
        self.assertLessEqual(info["counts"]["R3"], len(risk.R3_COMMANDS))


class RiskEnvWhitelistTests(unittest.TestCase):
    """环境变量只能收紧 R2 白名单，不能放宽 R3。"""

    def setUp(self):
        self.original = os.environ.get("AGENT_R2_AUTO_COMMANDS")

    def tearDown(self):
        if self.original is None:
            os.environ.pop("AGENT_R2_AUTO_COMMANDS", None)
        else:
            os.environ["AGENT_R2_AUTO_COMMANDS"] = self.original
        importlib.reload(risk)

    def test_env_can_tighten(self):
        os.environ["AGENT_R2_AUTO_COMMANDS"] = "assign_parking"
        reloaded = importlib.reload(risk)
        self.assertEqual(set(reloaded.R2_AUTO_COMMANDS), {"assign_parking"})
        self.assertEqual(reloaded.decision("assign_parking"), reloaded.AUTO)
        # check_in_lease 也是 R2 白名单成员，被收紧后必须回到「需要确认」；
        # bind_relation 是 R1（本身自动执行），不受白名单影响
        self.assertEqual(reloaded.decision("check_in_lease"), reloaded.CONFIRM)
        self.assertEqual(reloaded.decision("bind_relation"), reloaded.AUTO)

    def test_env_none_disables_all_r2_auto(self):
        os.environ["AGENT_R2_AUTO_COMMANDS"] = "none"
        reloaded = importlib.reload(risk)
        self.assertEqual(set(reloaded.R2_AUTO_COMMANDS), set())
        for name in reloaded.R2_COMMANDS:
            self.assertEqual(reloaded.decision(name), reloaded.CONFIRM, name)

    def test_env_cannot_enable_r3_or_unknown(self):
        os.environ["AGENT_R2_AUTO_COMMANDS"] = "delete_house,collect_payment,not_a_tool"
        reloaded = importlib.reload(risk)
        self.assertEqual(set(reloaded.R2_AUTO_COMMANDS), set())
        for name in ("delete_house", "collect_payment"):
            self.assertEqual(reloaded.decision(name), reloaded.CONFIRM, f"{name} 必须仍然需要确认")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
