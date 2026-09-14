"""把 DeepSeek Harness 的会话事件翻译成浏览器能用的 SSE 事件。

dsh 的通知是"轮次/步骤"粒度的（没有 token 级增量），所以这里做两件事：

* **工具调用 → 人话进度**：``tool/call`` / ``tool/result`` 翻译成
  「正在执行：创建报修工单」这种可展示的进度，前端只需要照抄。
* **最终回答 → 分片**：助手文本整段到达，这里切成小片依次下发，
  前端做打字机效果；``done`` 事件里再给一份权威全文，用于纠正差异。

事件协议（与 docs/REWRITE_CONTRACT.md 第 6 节一致）::

    {"type": "status", "text": "正在思考…"}
    {"type": "tool", "call_id": "…", "name": "create_work_order", "label": "创建报修工单",
     "state": "running" | "done", "summary": "工单 WO2026... 已创建", "ok": true}
    {"type": "delta", "text": "已经为张伟报修"}
    {"type": "title", "title": "1栋1单元101 水管漏水"}
    {"type": "done", "text": "<权威全文>", "message_id": 123}
    {"type": "error", "text": "…"}
"""

from __future__ import annotations

import json
from typing import Any, Callable

from .tools import tool_label

#: MCP 工具在模型侧的名字形如 ``mcp__wuye__create_work_order``
MCP_TOOL_PREFIX = "mcp__wuye__"

#: 分片长度：够平顺，又不会把事件切得太碎
CHUNK_SIZE = 12


def tool_display_name(model_tool_name: str) -> str:
    """把模型侧工具名还原成我们的工具名。"""
    name = model_tool_name or ""
    if name.startswith(MCP_TOOL_PREFIX):
        return name[len(MCP_TOOL_PREFIX) :]
    # 兼容带 serverName 前缀但拼写略有差异的情况
    if "__" in name:
        return name.rsplit("__", 1)[-1]
    return name


def sse_frame(event: dict[str, Any]) -> str:
    """把事件编码成一帧 SSE。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _extract_text(message: dict[str, Any] | None) -> str:
    """从助手消息里取纯文本（忽略推理内容）。"""
    if not isinstance(message, dict):
        return ""
    parts: list[str] = []
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "".join(parts)


def _has_tool_call(message: dict[str, Any] | None) -> bool:
    if not isinstance(message, dict):
        return False
    return any(
        isinstance(block, dict) and block.get("type") == "tool-call"
        for block in message.get("content") or []
    )


def _tool_result_text(message: dict[str, Any] | None) -> str:
    """从工具结果消息里取出文本内容。"""
    if not isinstance(message, dict):
        return ""
    chunks: list[str] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        inner = block.get("content")
        if isinstance(inner, list):
            for item in inner:
                if isinstance(item, dict) and item.get("type") == "text":
                    chunks.append(str(item.get("text") or ""))
        elif isinstance(inner, str):
            chunks.append(inner)
        elif block.get("type") == "text":
            chunks.append(str(block.get("text") or ""))
    return "".join(chunks)


def summarize_tool_result(raw_text: str) -> tuple[bool, str]:
    """把工具返回的 JSON 变成一句人话。返回 ``(是否成功, 摘要)``。"""
    ok, summary, _confirmation = parse_tool_result(raw_text)
    return ok, summary


def parse_tool_result(raw_text: str) -> tuple[bool, str, dict[str, Any] | None]:
    """解析工具返回，额外取出"待用户确认"信息。"""
    text = (raw_text or "").strip()
    if not text:
        return True, "已完成", None
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return True, text[:160], None
    if not isinstance(payload, dict):
        return True, text[:160], None
    if payload.get("ok") is False:
        return False, str(payload.get("error") or "操作没有成功"), None
    confirmation = payload.get("requires_confirmation")
    if isinstance(confirmation, dict):
        summary = str(payload.get("message") or "已生成待确认卡片")
        return True, summary, confirmation
    data = payload.get("data")
    if isinstance(data, dict):
        message = data.get("message")
        if isinstance(message, str) and message:
            return True, message, None
        if "items" in data:
            total = data.get("total", len(data.get("items") or []))
            return True, f"查到 {total} 条记录", None
        if "no" in data:
            return True, f"工单 {data['no']} 处理完成", None
    if isinstance(data, list):
        return True, f"查到 {len(data)} 条记录", None
    if isinstance(data, str) and data:
        return True, data[:160], None
    return True, "已完成", None


class TurnTranslator:
    """一轮对话的事件翻译器：把 dsh 通知转成 SSE 事件，逐个交给 ``emit``。"""

    def __init__(self, emit: Callable[[dict[str, Any]], None]) -> None:
        self._emit = emit
        self._tool_names: dict[str, str] = {}  # callId → 我们的工具名
        self.final_text_parts: list[str] = []
        self.final_response: str = ""
        self.title: str | None = None
        self.action_ids: list[int] = []  # 本轮生成的待确认动作

    # ---------------------------------------------------------------- 对外
    def handle(self, notification: Any) -> None:
        method = getattr(notification, "method", None)
        payload = getattr(notification, "payload", None) or {}
        if not isinstance(payload, dict):
            return
        if method != "session.event":
            return
        event = payload.get("event")
        if not isinstance(event, dict):
            return
        self._handle_event(str(event.get("type") or ""), event.get("data") or {})

    def finish(self, final_response: str, message_id: int | None = None) -> None:
        """一轮结束：下发权威全文与结束标记。"""
        text = (final_response or "").strip() or "".join(self.final_text_parts).strip()
        self.final_response = text
        self._emit({"type": "done", "text": text, "message_id": message_id})

    def failed(self, message: str) -> None:
        self._emit({"type": "error", "text": message})

    # ------------------------------------------------------------ 内部实现
    def _handle_event(self, event_type: str, data: Any) -> None:
        if not isinstance(data, dict):
            return
        if event_type == "tool/call":
            self._on_tool_call(data)
        elif event_type == "tool/result":
            self._on_tool_result(data)
        elif event_type == "assistant/message":
            self._on_assistant_message(data)
        elif event_type == "session/title":
            self._on_title(data)
        elif event_type == "turn/start":
            self._emit({"type": "status", "text": "正在理解你的需求…"})
        elif event_type == "step/start":
            step = data.get("step") or 1
            if step > 1:
                self._emit({"type": "status", "text": "正在思考下一步…"})
        elif event_type == "turn/end":
            reason = data.get("reason") or {}
            kind = reason.get("kind") if isinstance(reason, dict) else None
            if kind and kind not in {"completed"}:
                self._emit({"type": "status", "text": f"本轮结束（{kind}）"})

    def _on_tool_call(self, data: dict[str, Any]) -> None:
        call_id = str(data.get("callId") or "")
        name = tool_display_name(str(data.get("name") or ""))
        self._tool_names[call_id] = name
        event: dict[str, Any] = {
            "type": "tool",
            "call_id": call_id,
            "name": name,
            "label": tool_label(name),
            "state": "running",
        }
        raw_args = data.get("arguments")
        if isinstance(raw_args, str) and raw_args.strip():
            try:
                parsed = json.loads(raw_args)
                if isinstance(parsed, dict):
                    event["args"] = parsed
            except (ValueError, TypeError):
                event["args"] = {"raw": raw_args[:200]}
        self._emit(event)

    def _on_tool_result(self, data: dict[str, Any]) -> None:
        message = data.get("message")
        call_id = ""
        if isinstance(message, dict):
            source = message.get("source")
            if isinstance(source, dict):
                call_id = str(source.get("callId") or "")
        name = self._tool_names.get(call_id, "")
        ok, summary, confirmation = parse_tool_result(_tool_result_text(message))
        self._emit(
            {
                "type": "tool",
                "call_id": call_id,
                "name": name,
                "label": tool_label(name),
                "state": "done",
                "ok": ok,
                "summary": summary,
            }
        )
        if confirmation:
            # 高风险动作：把确认卡片推给浏览器，并记下 action_id 以便回填会话
            action_id = confirmation.get("action_id")
            if isinstance(action_id, int):
                self.action_ids.append(action_id)
            self._emit(
                {
                    "type": "action",
                    "action_id": action_id,
                    "tool": confirmation.get("tool") or name,
                    "label": tool_label(str(confirmation.get("tool") or name)),
                    "risk": confirmation.get("risk"),
                    "preview": confirmation.get("preview"),
                    "expires_at": confirmation.get("expires_at"),
                    "expires_in": confirmation.get("expires_in"),
                }
            )

    def _on_assistant_message(self, data: dict[str, Any]) -> None:
        message = data.get("message")
        text = _extract_text(message)
        if not text or _has_tool_call(message):
            return
        self.final_text_parts.append(text)
        for index in range(0, len(text), CHUNK_SIZE):
            self._emit({"type": "delta", "text": text[index : index + CHUNK_SIZE]})

    def _on_title(self, data: dict[str, Any]) -> None:
        title = data.get("title")
        if isinstance(title, str) and title.strip():
            self.title = title.strip()
            self._emit({"type": "title", "title": self.title})


def describe_notification(notification: Any) -> str:
    """调试用：把通知压成一行。"""
    payload = getattr(notification, "payload", None) or {}
    event = payload.get("event") if isinstance(payload, dict) else None
    if isinstance(event, dict):
        data = event.get("data")
        head = _stringify(data)[:120]
        return f"{event.get('type')} {head}"
    return _stringify(payload)[:120]
