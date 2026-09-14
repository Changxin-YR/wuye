"""把物业业务能力以 MCP（Model Context Protocol）工具暴露给 DeepSeek Harness。

职责与边界：

* **动态能力目录**：启动时用短期令牌解析登录用户，**只注册他有权限的工具**。
  没权限的能力不会进入模型上下文；即使模型硬造一个工具名，服务端也调不到。
* **风险闸门**：R0/R1 直接执行；R2 白名单内执行、其余生成待确认动作；R3 一律生成待确认动作。
  这一层不自己做业务判断，真正的校验在 ``PropertyService`` 与 ``Policy``。
* **身份来自登录用户**：令牌只经环境变量传递，绝不进入提示词或模型上下文。
* **执行体与网页共用**：每个工具调用 ``services`` / ``queries`` 的同一个函数，
  智能体没有第二条写数据库的路径。

运行方式（由 dsh 以 stdio 子进程拉起）::

    python -m agent.mcp_server          # 需要环境变量 WUYE_AGENT_TOKEN
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import traceback
from typing import Any, Callable

from werkzeug.exceptions import HTTPException

import db
import risk
from models import User
from permissions import Policy

from . import actions
from .token import TokenError, read_token
from .tools import TOOLS, TOOLS_BY_NAME, ToolSpec, tools_for_permissions

try:  # mcp >= 2：FastMCP 改名为 MCPServer
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover - 兼容 mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]

SERVER_NAME = "wuye"
TOKEN_ENV = "WUYE_AGENT_TOKEN"

mcp = _Server(SERVER_NAME)


def _log(message: str) -> None:
    """日志一律走 stderr，避免污染 stdio 上的 JSON-RPC 帧。"""
    print(f"[wuye-mcp] {message}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# 身份与执行
# --------------------------------------------------------------------------- #
def _resolve_user(session):
    """用环境变量里的令牌解析当前登录用户，并校验账号状态与会话版本。"""
    identity = read_token(os.environ.get(TOKEN_ENV, ""))
    user = session.get(User, identity.user_id)
    if user is None or not user.active:
        raise TokenError("账号不存在或已停用，请重新登录")
    if int(user.auth_version) != identity.auth_version:
        raise TokenError("账号权限已变化，请重新登录后再试")
    return user


def _result(ok: bool, **payload: Any) -> str:
    body = {"ok": ok}
    body.update(payload)
    return json.dumps(body, ensure_ascii=False, default=str)


def _invoke(session, policy, spec: ToolSpec, params: dict[str, Any]) -> str:
    """执行一次工具调用（含风险闸门）。"""
    decision = risk.decision(spec.name)
    if decision == "CONFIRM":
        confirmation = actions.create_action(session, policy, spec, params)
        return _result(
            True,
            requires_confirmation=confirmation.to_dict(),
            message=(
                f"这项操作需要用户确认：{confirmation.preview}。"
                "已经生成确认卡片，请告诉用户点击「确认执行」后才会真正生效；"
                "不要声称已经完成。"
            ),
        )
    data = actions.execute_tool(session, policy, spec, params)
    return _result(True, data=data)


def _call(tool_name: str, **params: Any) -> str:
    """统一的工具执行入口：身份 → 权限 → 风险 → 业务函数 → 结构化结果。"""
    spec = TOOLS_BY_NAME[tool_name]
    cleaned = {key: value for key, value in params.items() if value is not None and value != ""}
    try:
        with db.db_session() as session:
            user = _resolve_user(session)
            policy = Policy(session, user, source="agent")
            policy.require(spec.permission) if spec.permission else None
            return _invoke(session, policy, spec, cleaned)
    except TokenError as exc:
        return _result(False, error=str(exc), need_login=True)
    except HTTPException as exc:
        return _result(False, error=exc.description or "操作被拒绝", code=exc.code)
    except Exception as exc:  # noqa: BLE001 - 不让一次工具异常打断整轮对话
        message = _business_error_message(exc)
        if message is not None:
            return _result(False, error=message)
        _log(f"工具 {tool_name} 异常: {exc}\n{traceback.format_exc()}")
        return _result(False, error=f"系统处理失败：{exc}")


def _business_error_message(exc: Exception) -> str | None:
    """业务错误（ServiceError）说人话；其他异常交给兜底日志。"""
    try:
        from services import ServiceError
    except Exception:  # noqa: BLE001
        return None
    return exc.message if isinstance(exc, ServiceError) else None


# --------------------------------------------------------------------------- #
# 工具注册（按权限动态生成）
# --------------------------------------------------------------------------- #
def _make_function(spec: ToolSpec) -> Callable[..., str]:
    """按工具定义生成带真实签名的可调用对象，供 FastMCP 生成 JSON Schema。"""

    def tool(**kwargs: Any) -> str:
        return _call(spec.name, **kwargs)

    parameters = [
        inspect.Parameter(
            param.name,
            inspect.Parameter.KEYWORD_ONLY,
            default=param.default(),
            annotation=param.annotation(),
        )
        for param in spec.params
    ]
    tool.__signature__ = inspect.Signature(parameters)  # type: ignore[attr-defined]
    tool.__name__ = spec.name
    tool.__doc__ = spec.description
    tool.__annotations__ = {param.name: param.annotation() for param in spec.params}
    return tool


def register_tools(allowed: list[ToolSpec]) -> None:
    """把允许的能力注册成 MCP 工具。"""
    for spec in allowed:
        function = _make_function(spec)
        mcp.tool(name=spec.name, description=spec.description)(function)


def allowed_tools_for(token: str) -> tuple[list[ToolSpec], dict[str, Any]]:
    """解析令牌 → 当前用户可见的能力目录（动态能力目录）。"""
    from .token import read_token as _read

    identity = _read(token)
    with db.db_session() as session:
        user = session.get(User, identity.user_id)
        if user is None or not user.active or int(user.auth_version) != identity.auth_version:
            return [], {"user": None}
        policy = Policy(session, user, source="agent")
        allowed = tools_for_permissions(set(policy.permissions), super_user=bool(policy.super))
        who = {"id": user.id, "name": user.real_name or user.username,
               "roles": sorted(policy.roles), "permissions": len(policy.permissions)}
    return allowed, who


def main() -> None:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        _log(f"缺少环境变量 {TOKEN_ENV}，拒绝启动")
        raise SystemExit(2)
    try:
        allowed, who = allowed_tools_for(token)
    except Exception as exc:  # noqa: BLE001
        _log(f"令牌校验失败：{exc}")
        raise SystemExit(3) from exc
    if not allowed:
        _log("当前账号没有任何可用能力（或令牌已失效），拒绝启动")
        raise SystemExit(4)
    register_tools(allowed)
    skipped = len(TOOLS) - len(allowed)
    _log(f"{who.get('name')} 的能力目录：{len(allowed)} 个工具（因权限隐藏 {skipped} 个）")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
