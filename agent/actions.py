"""待确认动作（AiAction）：高风险操作的"人工闸门"。

链路：智能体调用工具 → ``risk.decision`` 判定需要确认 → **不执行**，落一条待确认动作
（服务端保存参数与哈希、生成人话预览、10 分钟有效期）→ 浏览器弹出确认卡片 →
用户点击确认 → 服务端**重新校验权限、数据范围、会话版本、目标版本**后执行 → 写审计。

浏览器只提交 ``action_id``，不能替换参数：参数以服务端落库的 payload 为准，
``payload_hash`` 用于防止落库后被篡改。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import Any

from sqlalchemy import select

import audit
import risk
from models import AiAction, utcnow

from .tools import ToolSpec, tool_label

log = logging.getLogger(__name__)

#: 确认卡片有效期（分钟）
ACTION_TTL_MINUTES = 10

#: 参数名 → 预览里要解析的实体（模型类名, 显示字段）
_ENTITY_HINTS: dict[str, tuple[str, str]] = {
    "community_id": ("Community", "name"),
    "building_id": ("Building", "name"),
    "unit_id": ("Unit", "name"),
    "house_id": ("House", "full_name"),
    "person_id": ("Person", "name"),
    "lease_id": ("Lease", "display"),
    "relation_id": ("HousePerson", "display"),
    "order_id": ("WorkOrder", "no"),
    "complaint_id": ("Complaint", "no"),
    "visitor_id": ("Visitor", "name"),
    "vehicle_id": ("Vehicle", "plate"),
    "parking_id": ("ParkingSpace", "code"),
    "device_id": ("Device", "name"),
    "inspection_id": ("Inspection", "display"),
    "bill_id": ("Bill", "no"),
    "payment_id": ("Payment", "no"),
}

_ACTION_LABELS = {
    "confirm": "已确认执行",
    "cancel": "已取消",
    "expire": "已过期",
    "fail": "执行失败",
}


@dataclass(frozen=True)
class Confirmation:
    """一条待确认动作对外的最小信息。"""

    action_id: int
    tool: str
    risk: str
    preview: str
    expires_at: str  # 带 Z 的 UTC ISO8601，例：2026-09-12T04:33:11Z
    expires_in: int = 0  # 剩余有效秒数

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "tool": self.tool,
            "risk": self.risk,
            "preview": self.preview,
            "expires_at": self.expires_at,
            # 前端可以直接用秒数倒计时，避免时区/解析差异导致"卡片一出现就被判过期"
            "expires_in": self.expires_in,
        }


def payload_hash(tool_name: str, params: dict[str, Any]) -> str:
    canonical = json.dumps({"tool": tool_name, "params": params}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _resolve_display(session, policy, key: str, value: Any) -> str | None:
    """把内部编号翻译成人话，例：house_id=12 → “1栋1单元101”。"""
    hint = _ENTITY_HINTS.get(key)
    if hint is None:
        return None
    model_name, field = hint
    try:
        import models

        model = getattr(models, model_name)
        obj = policy.get(model, int(value))
    except Exception:  # noqa: BLE001 - 预览失败不能阻塞确认流程
        return None
    for attr in ("house_text", "full_name_text", "display", field, "name", "no", "plate", "code"):
        text = getattr(obj, attr, None)
        if isinstance(text, str) and text:
            return text
    return None


def build_preview(session, policy, spec: ToolSpec, params: dict[str, Any]) -> str:
    """生成人话预览，例：「删除房屋：云邻花园 1栋1单元101」。"""
    details: list[str] = []
    for key, value in params.items():
        display = _resolve_display(session, policy, key, value)
        if display:
            details.append(display)
        elif key in {"reason", "note", "result", "description", "content", "fee_type", "period", "amount",
                     "contact_name", "contact_phone", "urgency", "category", "relation", "rent", "plate",
                     "name", "phone", "purpose", "visit_at", "start_at", "end_at", "due_at", "rating", "result"}:
            details.append(f"{value}")
    tail = "，".join(details[:4])
    return f"{spec.label}：{tail}" if tail else spec.label


def find_pending(session, user_id: int, tool_name: str, digest: str) -> AiAction | None:
    """同一用户、同一工具、同一参数的未过期待确认动作（避免模型重试时刷屏）。"""
    return session.scalars(
        select(AiAction)
        .where(
            AiAction.user_id == user_id,
            AiAction.tool_name == tool_name,
            AiAction.payload_hash == digest,
            AiAction.status == 0,
        )
        .order_by(AiAction.id.desc())
        .limit(1)
    ).first()


def create_action(
    session,
    policy,
    spec: ToolSpec,
    params: dict[str, Any],
    *,
    session_id: int | None = None,
    risk_level: str | None = None,
) -> Confirmation:
    """落一条待确认动作（不执行）。"""
    digest = payload_hash(spec.name, params)
    existing = find_pending(session, policy.user.id, spec.name, digest)
    if existing is not None and existing.expires_at > utcnow():
        return _to_confirmation(existing)

    level = risk_level or risk.level(spec.name)
    now = utcnow()
    row = AiAction(
        user_id=policy.user.id,
        auth_version=int(getattr(policy.user, "auth_version", 0) or 0),
        session_id=session_id,
        tool_name=spec.name,
        payload=json.dumps(params, ensure_ascii=False, default=str),
        payload_hash=digest,
        preview=build_preview(session, policy, spec, params),
        risk_level=level,
        status=0,
        expires_at=now + timedelta(minutes=ACTION_TTL_MINUTES),
    )
    session.add(row)
    session.flush()
    audit.write_audit(
        session, policy, action=f"ai_propose:{spec.name}", target_type="ai_action", target_id=row.id,
        detail={"risk": level, "preview": row.preview, "params": params}, source="agent",
    )
    return _to_confirmation(row)


def confirm_action(session, policy, action_id: int, *, request_key: str | None = None) -> dict[str, Any]:
    """执行一条待确认动作（重新做全套校验）。"""
    row = _load_owned(session, policy, action_id)
    if row.status != 0:
        return _replay(row)
    if row.expires_at <= utcnow():
        row.status = 3
        return {"ok": False, "action_id": row.id, "status": "expired", "message": "这张确认卡片已过期，请让智能体重新发起。"}
    if int(row.auth_version) != int(getattr(policy.user, "auth_version", 0) or 0):
        return {"ok": False, "action_id": row.id, "status": "stale", "message": "账号权限已变化，请重新登录后再确认。"}

    from .tools import TOOLS_BY_NAME

    spec = TOOLS_BY_NAME.get(row.tool_name)
    if spec is None:
        row.status = 4
        return {"ok": False, "action_id": row.id, "status": "unknown_tool", "message": "这条动作对应的能力已不存在。"}

    params = json.loads(row.payload or "{}")
    try:
        result = execute_tool(session, policy, spec, params, request_key=request_key or f"ai-action-{row.id}")
    except Exception as exc:  # noqa: BLE001 - 失败要落库并告诉用户
        from services import ServiceError
        from werkzeug.exceptions import HTTPException

        if isinstance(exc, ServiceError):
            message = exc.message
        elif isinstance(exc, HTTPException):
            message = exc.description or "操作被拒绝"
        else:
            message = f"执行失败：{exc}"
        row.status = 4
        row.result = json.dumps({"ok": False, "error": message}, ensure_ascii=False)
        audit.write_audit(
            session, policy, action=f"ai_execute:{row.tool_name}", target_type="ai_action", target_id=row.id,
            detail={"ok": False, "error": message}, source="agent", ok=False,
        )
        return {"ok": False, "action_id": row.id, "status": "failed", "message": message}

    row.status = 1
    row.result = json.dumps(result, ensure_ascii=False, default=str)
    audit.write_audit(
        session, policy, action=f"ai_execute:{row.tool_name}", target_type="ai_action", target_id=row.id,
        detail={"ok": True, "result": result}, source="agent",
    )
    return {"ok": True, "action_id": row.id, "status": "executed", "result": result,
            "message": result.get("message") or f"{spec.label}已完成"}


def cancel_action(session, policy, action_id: int) -> dict[str, Any]:
    row = _load_owned(session, policy, action_id)
    if row.status != 0:
        return _replay(row)
    row.status = 2
    audit.write_audit(
        session, policy, action="ai_cancel:" + row.tool_name, target_type="ai_action", target_id=row.id,
        detail={"preview": row.preview}, source="agent",
    )
    return {"ok": True, "action_id": row.id, "status": "cancelled", "message": "已取消，不会执行。"}


def attach_session(session, action_id: int, agent_session_id: int | None) -> None:
    """把待确认动作挂到当前 AI 会话上（页面刷新后还能找回卡片）。"""
    if not agent_session_id:
        return
    row = session.get(AiAction, int(action_id))
    if row is not None and row.session_id is None:
        row.session_id = int(agent_session_id)


def list_pending(session, user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    """列出该用户未过期的待确认动作（页面刷新后仍能找回卡片）。"""
    rows = session.scalars(
        select(AiAction)
        .where(AiAction.user_id == user_id, AiAction.status == 0, AiAction.expires_at > utcnow())
        .order_by(AiAction.id.desc())
        .limit(limit)
    )
    # label 是人话名称（派单 / 删除房屋…）：刷新后补渲染的卡片要靠它，
    # 否则标题只能显示 assign_work_order 这种内部工具名。
    return [
        dict(_to_confirmation(row).to_dict(), label=tool_label(row.tool_name), session_id=row.session_id)
        for row in rows
    ]


def execute_tool(
    session,
    policy,
    spec: ToolSpec,
    params: dict[str, Any],
    *,
    request_key: str | None = None,
) -> dict[str, Any]:
    """真正调用业务服务（自动执行路径与人工确认路径共用）。"""
    import inspect

    handler = spec.resolve()
    kwargs = dict(params)
    signature = inspect.signature(handler)
    if "source" in signature.parameters:
        kwargs["source"] = "agent"
    if request_key and "request_key" in signature.parameters:
        kwargs.setdefault("request_key", request_key)
    return handler(policy, **kwargs)


def _load_owned(session, policy, action_id: int) -> AiAction:
    """取出**属于当前账号**的待确认动作。

    「查不到」和「不是你的」分开报：以前两种情况都说"不属于当前账号"，
    前端把地址模板渲染成 /ai/actions/0/confirm 时，用户看到的是权限错误，
    排查方向被带偏（实际是动作号没传上来）。
    """
    row = session.get(AiAction, int(action_id))
    if row is None:
        log.warning("待确认动作不存在：action_id=%s user_id=%s", action_id, policy.user.id)
        raise PermissionError("这条待确认动作已失效，请刷新页面后让助手重新发起一次。")
    if row.user_id != policy.user.id:
        log.warning(
            "待确认动作归属不符：action_id=%s 属于 user_id=%s，当前 user_id=%s",
            action_id, row.user_id, policy.user.id,
        )
        raise PermissionError("这条待确认动作不属于当前账号")
    return row


def _replay(row: AiAction) -> dict[str, Any]:
    result = json.loads(row.result) if row.result else {}
    status_map = {1: "executed", 2: "cancelled", 3: "expired", 4: "failed"}
    return {
        "ok": row.status == 1,
        "action_id": row.id,
        "status": status_map.get(row.status, "unknown"),
        "message": _ACTION_LABELS.get(status_map.get(row.status, ""), "该动作已处理过"),
        "result": result,
    }


def _iso_utc(value) -> str:
    """把数据库里的 naive UTC 时间输出成带 Z 的 ISO8601（前端 Date 才能正确解析）。"""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _to_confirmation(row: AiAction) -> Confirmation:
    expires_in = 0
    if row.expires_at is not None:
        expires_in = max(0, int((row.expires_at - utcnow()).total_seconds()))
    return Confirmation(
        action_id=row.id,
        tool=row.tool_name,
        risk=row.risk_level,
        preview=row.preview or "",
        expires_at=_iso_utc(row.expires_at),
        expires_in=expires_in,
    )
