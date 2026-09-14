"""能力目录一致性测试：工具 ↔ 服务函数 ↔ 权限 ↔ 风险级别 ↔ MCP 注册。

这类测试的价值在于"新加了一个工具却忘了配权限/风险/服务函数"能立刻暴露，
而不是等到演示现场模型调用时才发现。
"""

from __future__ import annotations

import asyncio
import inspect
import unittest

import permissions
import risk
from agent import tools as tool_module
from agent.tools import TOOLS, TOOLS_BY_NAME, tools_for_permissions, validate_catalog



def role_permissions(role_code: str) -> set[str]:
    """取角色权限集合（兼容 ROLES 的 dict / tuple 两种写法）。"""
    entry = permissions.ROLES[role_code]
    if isinstance(entry, dict):
        return set(entry.get("permissions") or ())
    for item in entry:
        if isinstance(item, (set, frozenset, list, tuple)) and not isinstance(item, str):
            return set(item)
    return set()


class CatalogShapeTestCase(unittest.TestCase):
    def test_catalog_is_valid(self) -> None:
        problems = validate_catalog()
        self.assertEqual(problems, [], f"能力目录存在问题：{problems}")

    def test_tool_names_unique_and_lowercase(self) -> None:
        names = [spec.name for spec in TOOLS]
        self.assertEqual(len(names), len(set(names)), "工具名重复")
        for name in names:
            self.assertTrue(name.islower(), f"工具名应为小写：{name}")
            self.assertLessEqual(len(name), 64)

    def test_every_tool_exposes_a_callable(self) -> None:
        for spec in TOOLS:
            handler = spec.resolve()
            self.assertTrue(callable(handler), f"{spec.name} 的目标不可调用")

    def test_tool_params_match_service_signature(self) -> None:
        """工具声明的参数必须在服务函数签名里真实存在，避免"模型传了但函数不认"。"""
        for spec in TOOLS:
            handler = spec.resolve()
            signature = inspect.signature(handler)
            accepted = set(signature.parameters)
            declared = set(spec.param_names())
            missing = declared - accepted
            self.assertEqual(missing, set(), f"{spec.name} 的参数 {missing} 在 {spec.target} 签名里不存在")
            self.assertIn("actor", signature.parameters, f"{spec.target} 第一个参数应为 actor（Policy）")


class PermissionAlignmentTestCase(unittest.TestCase):
    def test_declared_permissions_all_exist(self) -> None:
        known = set(permissions.ALL_PERMISSIONS)
        unknown = {spec.permission for spec in TOOLS if spec.permission} - known
        self.assertEqual(unknown, set(), f"工具声明了未定义的权限点：{unknown}")

    def test_write_commands_declare_permission(self) -> None:
        for spec in TOOLS:
            if not spec.is_query:
                self.assertIsNotNone(spec.permission, f"写命令 {spec.name} 必须声明权限")

    def test_owner_cannot_see_delete_house(self) -> None:
        owner_perms = role_permissions("owner")
        visible = {spec.name for spec in tools_for_permissions(owner_perms)}
        self.assertNotIn("delete_house", visible)
        self.assertNotIn("assign_work_order", visible)
        self.assertIn("create_work_order", visible)

    def test_manager_sees_more_than_owner(self) -> None:
        owner_perms = role_permissions("owner")
        manager_perms = role_permissions("manager")
        owner_tools = tools_for_permissions(owner_perms)
        manager_tools = tools_for_permissions(manager_perms)
        self.assertGreater(len(manager_tools), len(owner_tools))

    def test_super_user_sees_everything(self) -> None:
        self.assertEqual(len(tools_for_permissions(set(), super_user=True)), len(TOOLS))

    def test_no_permission_hides_commands(self) -> None:
        visible = tools_for_permissions(set())
        names = {spec.name for spec in visible}
        self.assertEqual(names, {"whoami"}, "拿不到权限时只应看到无需权限的能力")


class RiskAlignmentTestCase(unittest.TestCase):
    def test_every_command_has_risk_level(self) -> None:
        missing = risk.missing_levels([spec.name for spec in TOOLS])
        self.assertEqual(missing, [], f"这些工具没有登记风险级别：{missing}")

    def test_queries_are_read_only(self) -> None:
        for spec in TOOLS:
            if spec.is_query:
                self.assertEqual(risk.level(spec.name), risk.R0, f"查询 {spec.name} 应为 R0")
                self.assertEqual(risk.decision(spec.name), risk.AUTO)

    def test_read_only_queries_never_confirm(self) -> None:
        for spec in TOOLS:
            if spec.is_query:
                self.assertNotEqual(risk.decision(spec.name), risk.CONFIRM)

    def test_r3_always_requires_confirmation(self) -> None:
        r3 = [spec.name for spec in TOOLS if risk.level(spec.name) == risk.R3]
        self.assertTrue(r3, "至少要有一个 R3 工具，否则风险分级形同虚设")
        for name in r3:
            self.assertEqual(risk.decision(name), risk.CONFIRM, f"R3 工具 {name} 必须人工确认")

    def test_financial_operations_are_r3(self) -> None:
        for name in ("collect_payment", "reverse_payment", "void_bill", "create_bills_batch"):
            self.assertEqual(risk.level(name), risk.R3, f"{name} 属于财务/批量操作，必须是 R3")

    def test_r2_whitelist_controls_mode(self) -> None:
        r2_tools = [spec.name for spec in TOOLS if risk.level(spec.name) == risk.R2]
        self.assertTrue(r2_tools)
        for name in r2_tools:
            expected = risk.AUTO if name in risk.R2_AUTO_COMMANDS else risk.CONFIRM
            self.assertEqual(risk.decision(name), expected)

    def test_unknown_tool_raises(self) -> None:
        with self.assertRaises(KeyError):
            risk.level("this_tool_does_not_exist")


class McpRegistrationTestCase(unittest.TestCase):
    def test_all_tools_register_with_json_schema(self) -> None:
        from agent import mcp_server

        for spec in TOOLS:
            function = mcp_server._make_function(spec)
            signature = inspect.signature(function)
            # 生成器用的是 spec.params（含 expected_version 等公共控制参数）
            self.assertEqual(set(signature.parameters), {param.name for param in spec.params})
        mcp_server.register_tools(list(TOOLS))
        listed = asyncio.run(mcp_server.mcp.list_tools())
        self.assertEqual(len(listed), len(TOOLS))
        names = {tool.name for tool in listed}
        self.assertEqual(names, set(TOOLS_BY_NAME))
        sample = next(tool for tool in listed if tool.name == "create_work_order")
        schema = sample.inputSchema if hasattr(sample, "inputSchema") else sample.input_schema
        self.assertIn("house_id", schema.get("properties", {}))
        self.assertIn("house_id", schema.get("required", []))


if __name__ == "__main__":
    unittest.main()
