"""SSE 事件协议测试：用假通知驱动 ``TurnTranslator``，不花模型配额。

覆盖：工具进度、工具结果（成功 / 失败 / 需确认）、最终回答分片、标题、结束帧。
"""

from __future__ import annotations

import json
import unittest
from dataclasses import dataclass, field
from typing import Any

from agent import bridge


@dataclass
class FakeNotification:
    """模拟 dsh 的 ``session.event`` 通知。"""

    event_type: str
    data: dict[str, Any] = field(default_factory=dict)
    method: str = "session.event"

    @property
    def payload(self) -> dict[str, Any]:
        return {"sessionId": "s1", "event": {"type": self.event_type, "data": self.data}}


def tool_result_text(payload: dict[str, Any]) -> dict[str, Any]:
    """构造 dsh 的工具结果消息结构。"""
    return {
        "message": {
            "source": {"kind": "tool", "callId": payload.get("callId", "c1")},
            "content": [
                {
                    "type": "tool-result",
                    "toolCallId": payload.get("callId", "c1"),
                    "content": [{"type": "text", "text": json.dumps(payload.get("body"), ensure_ascii=False)}],
                }
            ],
        }
    }


class TranslatorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.translator = bridge.TurnTranslator(self.events.append)

    def feed(self, notification: Any) -> None:
        self.translator.handle(notification)

    # ------------------------------------------------------------------ 工具
    def test_tool_call_becomes_human_progress(self) -> None:
        self.feed(FakeNotification("tool/call", {
            "turn": 1, "step": 1, "callId": "c1",
            "name": "mcp__wuye__create_work_order",
            "arguments": json.dumps({"house_id": 12, "category": "水电"}),
        }))
        self.assertEqual(len(self.events), 1)
        event = self.events[0]
        self.assertEqual(event["type"], "tool")
        self.assertEqual(event["state"], "running")
        self.assertEqual(event["name"], "create_work_order")
        self.assertEqual(event["label"], "创建报修工单")
        self.assertEqual(event["args"]["house_id"], 12)

    def test_tool_result_success_summary(self) -> None:
        self.feed(FakeNotification("tool/call", {"callId": "c1", "name": "mcp__wuye__create_work_order", "arguments": "{}"}))
        self.events.clear()
        self.feed(FakeNotification("tool/result", tool_result_text({
            "callId": "c1",
            "body": {"ok": True, "data": {"id": 5, "no": "WO20260912001", "message": "工单 WO20260912001 已创建"}},
        })))
        event = self.events[0]
        self.assertEqual(event["state"], "done")
        self.assertTrue(event["ok"])
        self.assertEqual(event["summary"], "工单 WO20260912001 已创建")
        self.assertEqual(event["label"], "创建报修工单")

    def test_tool_result_failure_is_reported_honestly(self) -> None:
        self.feed(FakeNotification("tool/call", {"callId": "c2", "name": "mcp__wuye__assign_work_order", "arguments": "{}"}))
        self.events.clear()
        self.feed(FakeNotification("tool/result", tool_result_text({
            "callId": "c2",
            "body": {"ok": False, "error": "当前账号没有派单权限"},
        })))
        event = self.events[0]
        self.assertFalse(event["ok"])
        self.assertIn("没有派单权限", event["summary"])

    def test_confirmation_emits_action_card(self) -> None:
        self.feed(FakeNotification("tool/call", {"callId": "c3", "name": "mcp__wuye__delete_house", "arguments": "{}"}))
        self.events.clear()
        self.feed(FakeNotification("tool/result", tool_result_text({
            "callId": "c3",
            "body": {
                "ok": True,
                "requires_confirmation": {
                    "action_id": 12, "tool": "delete_house", "risk": "R3",
                    "preview": "删除房屋：美家花园 1栋1单元101",
                    "expires_at": "2026-09-12T10:00:00",
                },
                "message": "这项操作需要用户确认。",
            },
        })))
        kinds = [event["type"] for event in self.events]
        self.assertEqual(kinds, ["tool", "action"])
        action = self.events[1]
        self.assertEqual(action["action_id"], 12)
        self.assertEqual(action["risk"], "R3")
        self.assertEqual(action["label"], "删除房屋")
        self.assertIn("1栋1单元101", action["preview"])
        self.assertEqual(self.translator.action_ids, [12])

    def test_query_result_summary_counts_rows(self) -> None:
        self.feed(FakeNotification("tool/call", {"callId": "c4", "name": "mcp__wuye__list_houses", "arguments": "{}"}))
        self.events.clear()
        self.feed(FakeNotification("tool/result", tool_result_text({
            "callId": "c4", "body": {"ok": True, "data": {"items": [{}, {}], "total": 2}},
        })))
        self.assertEqual(self.events[0]["summary"], "查到 2 条记录")

    # ------------------------------------------------------------------ 回答
    def test_final_answer_is_chunked_into_deltas(self) -> None:
        text = "已经为张伟创建报修工单，维修工会尽快上门处理。"
        self.feed(FakeNotification("assistant/message", {"message": {"content": [{"type": "text", "text": text}]}}))
        deltas = [event for event in self.events if event["type"] == "delta"]
        self.assertGreater(len(deltas), 1)
        self.assertEqual("".join(event["text"] for event in deltas), text)

    def test_tool_step_text_is_not_shown_as_answer(self) -> None:
        """带工具调用的中间 assistant 文本不应作为最终回答下发。"""
        self.feed(FakeNotification("assistant/message", {"message": {"content": [
            {"type": "text", "text": "我先查一下这套房子。"},
            {"type": "tool-call", "id": "c9", "name": "list_houses", "arguments": "{}"},
        ]}}))
        self.assertEqual([event for event in self.events if event["type"] == "delta"], [])

    def test_reasoning_content_is_ignored(self) -> None:
        self.feed(FakeNotification("assistant/message", {"message": {"content": [
            {"type": "reasoning", "text": "（内部推理，不该给用户看）"},
        ]}}))
        self.assertEqual(self.events, [])

    def test_title_event(self) -> None:
        self.feed(FakeNotification("session/title", {"title": "1栋1单元101 水管漏水"}))
        self.assertEqual(self.events[0], {"type": "title", "title": "1栋1单元101 水管漏水"})
        self.assertEqual(self.translator.title, "1栋1单元101 水管漏水")

    def test_status_events(self) -> None:
        self.feed(FakeNotification("turn/start", {}))
        self.feed(FakeNotification("step/start", {"step": 2}))
        texts = [event["text"] for event in self.events if event["type"] == "status"]
        self.assertEqual(len(texts), 2)

    def test_finish_emits_authoritative_text(self) -> None:
        self.feed(FakeNotification("assistant/message", {"message": {"content": [{"type": "text", "text": "片段"}]}}))
        self.events.clear()
        self.translator.finish("权威全文", message_id=9)
        self.assertEqual(self.events[-1], {"type": "done", "text": "权威全文", "message_id": 9})

    def test_failed_turn_emits_error(self) -> None:
        self.translator.failed("智能体执行失败：x")
        self.assertEqual(self.events[-1]["type"], "error")

    # ------------------------------------------------------------------ 协议
    def test_sse_frame_is_valid_sse(self) -> None:
        frame = bridge.sse_frame({"type": "delta", "text": "你好"})
        self.assertTrue(frame.startswith("data: "))
        self.assertTrue(frame.endswith("\n\n"))
        payload = json.loads(frame[len("data: "):].strip())
        self.assertEqual(payload["text"], "你好")

    def test_tool_name_without_prefix_still_maps(self) -> None:
        self.assertEqual(bridge.tool_display_name("create_work_order"), "create_work_order")
        self.assertEqual(bridge.tool_display_name("mcp__wuye__verify_work_order"), "verify_work_order")


if __name__ == "__main__":
    unittest.main()
