"""AI 会话与消息落库（页面刷新后仍能看到历史对话）。

数据库只保存"用户看到的东西"：会话标题、每条消息的文本、以及 dsh 的会话 id。
真正的智能体上下文由 dsh 自己持久化在 ``instances/dsh-home/u<id>/sessions`` 里。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from models import AgentMessage, AgentSession


def new_dsh_session_id(user_id: int) -> str:
    """给 dsh 用的会话 id：加用户前缀，避免不同用户之间串话。"""
    return f"wuye-u{user_id}-{uuid.uuid4().hex[:12]}"


def create_session(session, user_id: int, title: str = "新会话") -> dict[str, Any]:
    """新建一个会话。"""
    row = AgentSession(user_id=user_id, title=(title or "新会话")[:80], dsh_session_id=new_dsh_session_id(user_id))
    session.add(row)
    session.flush()
    return _session_dict(row)


def list_sessions(session, user_id: int, limit: int = 30) -> list[dict[str, Any]]:
    """列出某用户的会话（最近在前），并带上消息条数（页面侧栏要显示）。"""
    rows = list(session.scalars(
        select(AgentSession)
        .where(AgentSession.user_id == user_id, AgentSession.deleted.is_(False))
        .order_by(AgentSession.id.desc())
        .limit(limit)
    ))
    counts: dict[int, int] = {}
    if rows:
        from sqlalchemy import func

        ids = [row.id for row in rows]
        for session_id, count in session.execute(
            select(AgentMessage.session_id, func.count(AgentMessage.id))
            .where(AgentMessage.session_id.in_(ids))
            .group_by(AgentMessage.session_id)
        ):
            counts[int(session_id)] = int(count)
    return [dict(_session_dict(row), message_count=counts.get(row.id, 0)) for row in rows]


def get_session(session, user_id: int, session_id: int) -> AgentSession | None:
    """取会话（校验归属）。"""
    row = session.get(AgentSession, int(session_id))
    if row is None or row.deleted or row.user_id != user_id:
        return None
    return row


def ensure_session(session, user_id: int, session_id: int | None) -> AgentSession:
    """没有会话就建一个，保证每次对话都有归属。"""
    if session_id:
        row = get_session(session, user_id, session_id)
        if row is not None:
            return row
    created = create_session(session, user_id)
    row = get_session(session, user_id, created["id"])
    assert row is not None
    return row


def append_message(session, session_id: int, role: str, content: str) -> dict[str, Any]:
    """追加一条消息（role: user / assistant / system）。"""
    row = AgentMessage(session_id=int(session_id), role=role, content=content or "")
    session.add(row)
    session.flush()
    return {"id": row.id, "role": row.role, "content": row.content, "created_at": row.created_at.isoformat()}


def list_messages(session, user_id: int, session_id: int, limit: int = 200) -> list[dict[str, Any]]:
    """列出会话消息；会话不属于当前用户时返回空列表。"""
    if get_session(session, user_id, session_id) is None:
        return []
    rows = session.scalars(
        select(AgentMessage)
        .where(AgentMessage.session_id == int(session_id))
        .order_by(AgentMessage.id.asc())
        .limit(limit)
    )
    role_text = {"system": "提示", "user": "我", "assistant": "助手"}
    return [
        {
            "id": row.id,
            "role": row.role,
            "role_text": role_text.get(row.role, row.role),
            "content": row.content,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


def rename_session(session, session_id: int, title: str) -> None:
    """用模型给出的标题更新会话名（只更新还没改过名的会话）。"""
    row = session.get(AgentSession, int(session_id))
    if row is None or not title:
        return
    if row.title and row.title != "新会话":
        return
    row.title = title.strip()[:80]


def rotate_dsh_session_id(session, user_id: int, agent_session_id: int) -> str | None:
    """给一个会话换新的 dsh 会话 id（返回新 id）。

    背景：dsh 的会话上下文是"进程 + 磁盘"共同管理的——新起的运行时进程遇到磁盘上
    已存在的会话 id 会直接报 ``session "..." already exists``。所以运行时被重建
    （服务重启 / 空闲回收）后，必须把该用户已落库的会话 id 换掉，否则对话会报错。
    """
    row = get_session(session, user_id, int(agent_session_id))
    if row is None:
        return None
    row.dsh_session_id = new_dsh_session_id(user_id)
    return row.dsh_session_id


def list_dsh_session_ids(session, user_id: int) -> list[tuple[int, str]]:
    """列出某用户所有会话的 ``(会话id, dsh_session_id)``，供运行时代理检查冲突。"""
    rows = session.scalars(
        select(AgentSession).where(AgentSession.user_id == user_id, AgentSession.deleted.is_(False))
    )
    return [(row.id, row.dsh_session_id) for row in rows]


def _session_dict(row: AgentSession) -> dict[str, Any]:
    return {
        "id": row.id,
        "title": row.title,
        "dsh_session_id": row.dsh_session_id,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
