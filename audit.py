"""审计写入（web 与智能体共用一张表，用 source 区分）。"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from models import AuditLog, User
from permissions import Policy


def _jsonable(value: Any) -> Any:
    """把 detail 里的值清洗成可 JSON 序列化的形式。"""
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def actor_of(actor: Policy | User | int | None) -> tuple[int | None, str]:
    """从 Policy / User / user_id 里取出 (user_id, source)。"""
    if isinstance(actor, Policy):
        return actor.user_id, actor.source
    if isinstance(actor, User):
        return actor.id, "web"
    if actor is None:
        return None, "web"
    try:
        return int(actor), "web"
    except (TypeError, ValueError):
        return None, "web"


def write_audit(
    session: Session,
    policy: Policy | User | int | None = None,
    action: str | None = None,
    target_type: str = "",
    target_id: Any = "",
    detail: dict | None = None,
    source: str | None = None,
    ok: bool = True,
) -> AuditLog:
    """写一条审计记录（与业务写操作同一事务）。

    参数名与 ``agent/`` 侧钉死的接口一致：``session`` + ``policy``（``permissions.Policy``），
    其余走关键字：``action`` / ``target_type`` / ``target_id`` / ``detail`` / ``source`` / ``ok``。
    位置参数调用同样兼容（``write_audit(db, actor, "order.create", "work_order", 12, {...})``）。

    ``source`` 为 ``web``（网页操作）或 ``agent``（AI 助手）；用 Policy 时自动取它的 source，
    避免智能体写的操作被记成网页操作。``ok=False`` 用于记录失败的尝试（detail 里会带 ok 标记）。
    """
    if action is None:
        raise ValueError("write_audit 需要 action")
    user_id, actor_source = actor_of(policy)
    if isinstance(policy, Policy):
        actor_source = policy.source
    # 显式传入的 source 优先（agent/ 侧统一传 "agent"）；没传就跟随 Policy 的来源
    if source not in ("web", "agent"):
        source = actor_source
    detail_payload = _jsonable(detail) if detail else {}
    if not isinstance(detail_payload, dict):
        detail_payload = {"detail": detail_payload}
    detail_payload.setdefault("ok", bool(ok))  # 失败也要留痕：ok=False
    row = AuditLog(
        user_id=user_id,
        action=action,
        target_type=target_type or "",
        target_id="" if target_id is None else str(target_id),
        detail=detail_payload,
        source=source if source in ("web", "agent") else "web",
    )
    session.add(row)
    return row


def list_audit_actions() -> list[str]:
    """审计动作清单（页面过滤器用）。"""
    return [
        "community.create",
        "community.update",
        "community.delete",
        "building.create",
        "building.update",
        "building.delete",
        "house.create",
        "house.update",
        "house.delete",
        "person.create",
        "person.update",
        "person.delete",
        "relation.bind",
        "relation.end",
        "order.create",
        "order.assign",
        "order.accept",
        "order.progress",
        "order.finish",
        "order.verify",
        "order.cancel",
        "order.rate",
    ]
