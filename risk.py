"""R0–R3 风险分级：决定智能体的能力是"直接执行"还是"必须人工确认"。

分级口径（动态能力目录 + R0-R3 风险分级 + 人工确认）：

* **R0 只读**：任何查询，直接执行。
* **R1 普通写**：可恢复、影响面小的写操作（新建报修、记录进展、登记访客……），直接执行。
* **R2 重要变更**：关系、状态、分配类操作。**白名单内**自动执行，其余生成确认卡片。
* **R3 高风险**：删除、归档、财务、批量。**永远生成确认卡片**，不接受自动执行。

白名单可以用环境变量 ``AGENT_R2_AUTO_COMMANDS``（逗号分隔）**收紧**；
把 R3 命令写进白名单也不会生效——R3 是硬闸门。

本文件的级别表由 ``agent/tools.py`` 的能力目录校验：漏登记会在
``tests/test_mcp_tools.py`` 里失败，避免"新加了工具却忘了定风险"。
"""

from __future__ import annotations

import os

R0, R1, R2, R3 = "R0", "R1", "R2", "R3"

AUTO = "AUTO"
CONFIRM = "CONFIRM"

#: R2 默认自动执行白名单（可用环境变量收紧，不能放宽 R3）
DEFAULT_R2_AUTO: frozenset[str] = frozenset({"bind_relation", "check_in_lease", "assign_parking"})

#: 删除 / 归档 / 财务 / 批量：永远需要人工确认
R3_COMMANDS: frozenset[str] = frozenset({
    "delete_community", "delete_building", "delete_unit", "delete_house", "delete_person",
    "archive_vehicle", "archive_device",
    "void_bill", "collect_payment", "reverse_payment", "create_bills_batch",
})

#: 重要但不至于"永远确认"的写操作：白名单内自动，其余确认
R2_COMMANDS: frozenset[str] = frozenset({
    "end_relation", "check_in_lease", "check_out_lease",
    "assign_work_order", "verify_work_order", "reopen_work_order", "cancel_work_order",
    "close_complaint", "cancel_complaint",
    "assign_parking", "release_parking",
    "create_bill",
})

#: 其余写命令按 R1 处理
R1_COMMANDS: frozenset[str] = frozenset({
    "create_community", "update_community",
    "create_building", "update_building",
    "create_unit", "update_unit",
    "create_house", "update_house",
    "create_person", "update_person", "bind_relation",
    "create_work_order", "accept_work_order", "add_order_progress", "finish_work_order",
    "rate_work_order",
    "create_complaint", "assign_complaint", "handle_complaint",
    "register_visitor", "enter_visitor", "leave_visitor", "cancel_visitor",
    "create_vehicle", "update_vehicle",
    "create_device", "update_device",
    "create_inspection", "complete_inspection",
})


def _env_r2_auto() -> frozenset[str]:
    """环境变量只允许在白名单基础上**收紧**（去掉某些命令），不会新增。"""
    raw = os.environ.get("AGENT_R2_AUTO_COMMANDS", "").strip()
    if not raw:
        return DEFAULT_R2_AUTO
    requested = {item.strip() for item in raw.replace(";", ",").split(",") if item.strip()}
    if requested == {"none"}:
        return frozenset()
    # 只保留本来就允许自动的 R2 命令；写 R3/未知名一律忽略
    return frozenset(requested & R2_COMMANDS)


def _build_levels() -> dict[str, str]:
    levels: dict[str, str] = {}
    for name in R3_COMMANDS:
        levels[name] = R3
    for name in R2_COMMANDS:
        levels[name] = R2
    for name in R1_COMMANDS:
        levels[name] = R1
    return levels


LEVELS: dict[str, str] = _build_levels()

#: 「能力 → 风险级别」的对外快照（自检与文档用）
R2_AUTO_COMMANDS: frozenset[str] = _env_r2_auto()


def level(tool_name: str) -> str:
    """取工具的风险级别；未登记的工具抛 ``KeyError``（宁可启动失败也不放过）。"""
    try:
        return LEVELS[tool_name]
    except KeyError as exc:  # 查询类工具默认 R0
        from agent.tools import TOOLS_BY_NAME

        spec = TOOLS_BY_NAME.get(tool_name)
        if spec is not None and spec.is_query:
            return R0
        raise KeyError(f"工具 {tool_name} 没有登记风险级别，请在 risk.py 中补充") from exc


def decision(tool_name: str) -> str:
    """返回 ``AUTO`` 或 ``CONFIRM``。"""
    risk = level(tool_name)
    if risk in (R0, R1):
        return AUTO
    if risk == R2:
        return AUTO if tool_name in R2_AUTO_COMMANDS else CONFIRM
    return CONFIRM  # R3：硬闸门


def is_r3(tool_name: str) -> bool:
    return level(tool_name) == R3


def describe() -> dict[str, object]:
    """给诊断接口/文档用的摘要。"""
    from agent.tools import TOOLS

    counts: dict[str, int] = {R0: 0, R1: 0, R2: 0, R3: 0}
    confirm: list[str] = []
    for spec in TOOLS:
        risk = level(spec.name)
        counts[risk] = counts.get(risk, 0) + 1
        if decision(spec.name) == CONFIRM:
            confirm.append(spec.name)
    return {
        "counts": counts,
        "r2_auto": sorted(R2_AUTO_COMMANDS),
        "confirm_required": sorted(confirm),
    }


def missing_levels(tool_names: list[str]) -> list[str]:
    """返回没有登记风险级别的工具名（自检用）。"""
    missing: list[str] = []
    from agent.tools import TOOLS_BY_NAME

    for name in tool_names:
        spec = TOOLS_BY_NAME.get(name)
        if spec is not None and spec.is_query:
            continue
        if name not in LEVELS:
            missing.append(name)
    return missing
