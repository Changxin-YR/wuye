"""AI 助手页面与接口（Flask 蓝图）。

提供给前端的东西：

* ``GET  /ai``                     聊天页（会话列表 + 当前会话）
* ``POST /ai/sessions``            新建会话
* ``GET  /ai/sessions/<id>``       历史消息（JSON）
* ``POST /ai/chat``                **SSE** 流式回答（带 CSRF）
* ``GET  /ai/actions``             当前待确认动作（JSON，刷新后找回卡片）
* ``POST /ai/actions/<id>/confirm`` 确认执行（服务端重新校验后执行）
* ``POST /ai/actions/<id>/cancel``  取消
* ``GET  /ai/status``              AI 通道与运行时状态（诊断用）

一轮对话的执行过程：请求线程先落库用户消息 → 起一个后台线程调用 dsh → dsh 的每个
会话事件由 :class:`agent.bridge.TurnTranslator` 翻译后推进队列 → 请求线程边收边以
SSE 帧下发 → 结束后把助手回复落库、并回填确认卡片所属会话。
"""

from __future__ import annotations

import json
import queue
import threading
from typing import Any, Iterator

from flask import Blueprint, Response, current_app, jsonify, redirect, render_template, request, session, url_for

import db
from models import User
from permissions import Policy

from . import actions, session_store
from .bridge import TurnTranslator, sse_frame
from .runtime import AgentError, AgentRuntimeManager

ai_bp = Blueprint("ai", __name__)

_manager: AgentRuntimeManager | None = None
_manager_lock = threading.Lock()

_DONE = object()


# --------------------------------------------------------------------------- #
# 运行时管理
# --------------------------------------------------------------------------- #
def get_manager() -> AgentRuntimeManager:
    """全局唯一的运行时管理器（懒创建）。"""
    global _manager
    settings = current_app.config.get("AGENT_SETTINGS")
    if settings is None:
        raise AgentError(
            "智能体运行时未就绪：请确认已安装 deepseek-harness-sdk，并配置了 DEEPSEEK_API_KEY"
        )
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = AgentRuntimeManager(settings)
    return _manager


def try_get_manager() -> AgentRuntimeManager | None:
    """诊断接口用：拿不到运行时返回 None，而不是抛错。"""
    try:
        return get_manager()
    except AgentError:
        return None


def shutdown_agent_runtimes() -> None:
    """进程退出时回收所有 dsh 子进程。"""
    global _manager
    if _manager is not None:
        _manager.shutdown_all()
        _manager = None


# --------------------------------------------------------------------------- #
# 登录态与 CSRF（与页面层保持同一套 session 口径）
# --------------------------------------------------------------------------- #
def _current_user(session_db):
    """取当前登录用户；session 里的 auth_version 与库里不一致视为已登出。"""
    user_id = session.get("user_id")
    if not user_id:
        return None
    user = session_db.get(User, int(user_id))
    if user is None or not user.active:
        return None
    if int(session.get("auth_version", -1)) != int(user.auth_version):
        return None
    return user


def _csrf_ok() -> bool:
    """CSRF 校验：接受 ``X-CSRF-Token`` 头或表单里的 ``csrf_token``。"""
    sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
    for key in ("csrf_token", "_csrf_token"):
        expected = session.get(key)
        if expected and sent and str(expected) == str(sent):
            return True
    return False


def _login_required():
    return jsonify({"ok": False, "error": "请先登录"}), 401


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #
@ai_bp.get("/ai")
def ai_page():
    with db.db_session() as session_db:
        user = _current_user(session_db)
        if user is None:
            return redirect(url_for("login"))
        sessions = session_store.list_sessions(session_db, user.id)
        current_id = request.args.get("session", type=int)
        current = None
        if current_id:
            row = session_store.get_session(session_db, user.id, current_id)
            if row is not None:
                current = {"id": row.id, "title": row.title}
        if current is None and sessions:
            current = {"id": sessions[0]["id"], "title": sessions[0]["title"]}
        messages = (
            session_store.list_messages(session_db, user.id, current["id"]) if current else []
        )
        pending = actions.list_pending(session_db, user.id)
        policy = Policy(session_db, user, source="agent")
        identity = policy.identity()
    settings = current_app.config.get("AGENT_SETTINGS")
    # 把 Policy.identity() 的结果同时作为 current_user 传入：
    # ai.html / base.html 都要读 current_user.role_names / scope_text，
    # 而 app 上下文处理器注入的是 User 模型对象（没有 role_names）。
    user_view = dict(identity)
    user_view.setdefault("username", identity.get("username", ""))
    return render_template(
        "ai.html",
        sessions=sessions,
        current_user=user_view,
        current_session=current,
        active_session=current,
        messages=messages,
        pending_actions=pending,
        agent_identity=identity,
        identity_name=identity.get("real_name") or identity.get("username"),
        scope_text=identity.get("data_scope_text") or identity.get("dataScopeText"),
        agent_ready=bool(settings and getattr(settings, "api_key", None)),
        stream_url=url_for("ai.ai_chat"),
        new_session_url=url_for("ai.ai_create_session"),
    )


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #
@ai_bp.post("/ai/sessions")
def ai_create_session():
    if not _csrf_ok():
        return jsonify({"ok": False, "error": "页面校验已过期，请刷新后重试"}), 400
    with db.db_session() as session_db:
        user = _current_user(session_db)
        if user is None:
            return _login_required()
        created = session_store.create_session(session_db, user.id)
    if request.form.get("ajax") == "1":
        return jsonify({"ok": True, "session": created})
    return redirect(url_for("ai.ai_page", session=created["id"]))


@ai_bp.get("/ai/sessions/<int:session_id>")
def ai_session_messages(session_id: int):
    with db.db_session() as session_db:
        user = _current_user(session_db)
        if user is None:
            return _login_required()
        row = session_store.get_session(session_db, user.id, session_id)
        if row is None:
            return jsonify({"ok": False, "error": "会话不存在"}), 404
        messages = session_store.list_messages(session_db, user.id, session_id)
    return jsonify({"ok": True, "session": {"id": row.id, "title": row.title}, "messages": messages})


# --------------------------------------------------------------------------- #
# 待确认动作
# --------------------------------------------------------------------------- #
@ai_bp.get("/ai/actions")
def ai_list_actions():
    with db.db_session() as session_db:
        user = _current_user(session_db)
        if user is None:
            return _login_required()
        pending = actions.list_pending(session_db, user.id)
    return jsonify({"ok": True, "actions": pending})


#: 模板里用 ``ai.ai_actions``，这里补一个别名端点（与 ``ai_list_actions`` 同一视图）
ai_bp.add_url_rule("/ai/actions", "ai_actions", ai_list_actions, methods=["GET"])


@ai_bp.post("/ai/actions/<int:action_id>/confirm")
def ai_confirm_action(action_id: int):
    if not _csrf_ok():
        return jsonify({"ok": False, "error": "页面校验已过期，请刷新后重试"}), 400
    with db.db_session() as session_db:
        user = _current_user(session_db)
        if user is None:
            return _login_required()
        policy = Policy(session_db, user, source="agent")
        try:
            result = actions.confirm_action(session_db, policy, action_id)
        except PermissionError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 403
        if result.get("ok"):
            session_db.commit()
            summary = result.get("message") or "操作已完成"
            if result.get("result") and isinstance(result["result"], dict):
                summary = result["result"].get("message") or summary
            ai_session_id = _action_session_id(session_db, action_id)
            if ai_session_id:
                session_store.append_message(session_db, ai_session_id, "system", f"你已确认执行：{summary}")
        else:
            session_db.commit()
            ai_session_id = _action_session_id(session_db, action_id)
            if ai_session_id:
                session_store.append_message(
                    session_db, ai_session_id, "system", f"确认执行失败：{result.get('message') or '未知原因'}"
                )
    return jsonify(result)


@ai_bp.post("/ai/actions/<int:action_id>/cancel")
def ai_cancel_action(action_id: int):
    if not _csrf_ok():
        return jsonify({"ok": False, "error": "页面校验已过期，请刷新后重试"}), 400
    with db.db_session() as session_db:
        user = _current_user(session_db)
        if user is None:
            return _login_required()
        policy = Policy(session_db, user, source="agent")
        try:
            result = actions.cancel_action(session_db, policy, action_id)
        except PermissionError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 403
        ai_session_id = _action_session_id(session_db, action_id)
        if ai_session_id:
            session_store.append_message(session_db, ai_session_id, "system", "你取消了这次操作。")
    return jsonify(result)


def _action_session_id(session_db, action_id: int) -> int | None:
    from models import AiAction

    row = session_db.get(AiAction, int(action_id))
    return row.session_id if row else None


# --------------------------------------------------------------------------- #
# SSE 对话
# --------------------------------------------------------------------------- #
@ai_bp.route("/ai/chat", methods=["GET", "POST"])
def ai_chat():
    """SSE 对话。

    同时接受 GET 与 POST：**两种情况都要求 CSRF 令牌**
    （header ``X-CSRF-Token``，或 POST 表单里的 ``csrf_token``）。
    前端用 fetch + ReadableStream 读流，因此 GET 也能带自定义头，
    第三方页面无法伪造该头，CSRF 防护不打折。
    """
    if not _csrf_ok():
        return jsonify({"ok": False, "error": "页面校验已过期，请刷新后重试"}), 400
    question = (request.form.get("q") or request.args.get("q") or "").strip()
    if not question:
        return jsonify({"ok": False, "error": "请输入内容"}), 400

    with db.db_session() as session_db:
        user = _current_user(session_db)
        if user is None:
            return _login_required()
        session_param = request.form.get("session") or request.args.get("session")
        try:
            session_id_value = int(session_param) if session_param else None
        except (TypeError, ValueError):
            session_id_value = None
        agent_session = session_store.ensure_session(session_db, user.id, session_id_value)
        session_store.append_message(session_db, agent_session.id, "user", question)
        payload = {
            "user_id": user.id,
            "auth_version": int(user.auth_version),
            "agent_session_id": agent_session.id,
            "dsh_session_id": agent_session.dsh_session_id,
        }

    # 运行时必须在视图里解析：生成器迭代时已经没有应用上下文了
    try:
        manager = get_manager()
    except AgentError as exc:
        message = str(exc)

        def error_stream():
            yield sse_frame({"type": "error", "text": message})
            yield sse_frame({"type": "done", "text": ""})

        return _sse_response(error_stream())

    def stream():
        events: queue.Queue[Any] = queue.Queue()
        translator = TurnTranslator(events.put)

        def run_once() -> None:
            result = manager.run_turn(
                user_id=payload["user_id"],
                auth_version=payload["auth_version"],
                session_id=payload["dsh_session_id"],
                text=question,
                on_notification=translator.handle,
            )
            translator.finish(getattr(result, "final_response", "") or "")

        def worker() -> None:
            try:
                run_once()
            except Exception as exc:  # noqa: BLE001
                # 会话 id 被磁盘上的旧会话占用（多实例/极端竞态）：换新 id 再试一次
                if "already exists" in str(exc) and _rotate_session(payload):
                    try:
                        run_once()
                        return
                    except Exception as second:  # noqa: BLE001
                        translator.failed(f"智能体执行失败：{second}")
                        return
                translator.failed(str(exc) if isinstance(exc, AgentError) else f"智能体执行失败：{exc}")
            finally:
                _persist_turn(payload, translator)
                events.put(_DONE)

        threading.Thread(target=worker, name="agent-turn", daemon=True).start()

        while True:
            item = events.get()
            if item is _DONE:
                break
            yield sse_frame(item)

    return _sse_response(stream())


def _sse_response(iterator) -> Response:
    return Response(
        iterator,
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )



def _rotate_session(payload: dict[str, Any]) -> bool:
    """给当前会话换一个 dsh 会话 id（换成功后就地更新 payload）。"""
    try:
        with db.db_session() as session:
            new_id = session_store.rotate_dsh_session_id(
                session, payload["user_id"], payload["agent_session_id"]
            )
    except Exception:  # noqa: BLE001
        return False
    if not new_id:
        return False
    payload["dsh_session_id"] = new_id
    return True


def _persist_turn(payload: dict[str, Any], translator: TurnTranslator) -> None:
    """把助手回复、会话标题、卡片归属写回数据库（独立会话，线程安全）。"""
    text = "".join(translator.final_text_parts).strip()
    try:
        with db.db_session() as session_db:
            if translator.action_ids:
                for action_id in translator.action_ids:
                    actions.attach_session(session_db, action_id, payload["agent_session_id"])
            if translator.title:
                session_store.rename_session(session_db, payload["agent_session_id"], translator.title)
            if text:
                session_store.append_message(session_db, payload["agent_session_id"], "assistant", text)
    except Exception:  # noqa: BLE001 - 落库失败不能影响已经看到的回答
        pass


# --------------------------------------------------------------------------- #
# 诊断
# --------------------------------------------------------------------------- #
@ai_bp.get("/ai/status")
def ai_status():
    from .runtime import describe_settings, dsh_runtime_path

    manager = try_get_manager()
    settings = current_app.config["AGENT_SETTINGS"]
    info: dict[str, Any] = {
        "runtime": dsh_runtime_path(),
        "settings": describe_settings(settings),
        "model": settings.model,
        "provider": settings.provider,
        "api_key_configured": bool(settings.api_key),
        "runtimes": manager.status() if manager else {"runtimes": 0},
        "tools": len(__import__("agent.tools", fromlist=["TOOLS"]).TOOLS),
    }
    with db.db_session() as session_db:
        user = _current_user(session_db)
        if user is not None:
            policy = Policy(session_db, user, source="agent")
            allowed = __import__("agent.tools", fromlist=["tools_for_permissions"]).tools_for_permissions(
                set(policy.permissions), super_user=bool(policy.super)
            )
            info["allowed_tools"] = len(allowed)
            info["identity"] = policy.identity()
    return jsonify(info)


@ai_bp.get("/ai/ping")
def ai_ping():
    """一键检查智能体通道（页面上的"检测"按钮用）。"""
    settings = current_app.config["AGENT_SETTINGS"]
    ok = bool(settings.api_key)
    return jsonify({
        "ok": ok,
        "message": "智能体通道就绪" if ok else "缺少 DEEPSEEK_API_KEY，智能体无法调用模型",
        "model": settings.model,
        "runtime": __import__("agent.runtime", fromlist=["dsh_runtime_path"]).dsh_runtime_path(),
    })


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)
