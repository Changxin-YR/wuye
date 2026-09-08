import json
import unittest
from unittest.mock import patch

from dify_client import BailianClient, DeepSeekClient, _PLANNER_HINT


class AgentExecutionGuardTests(unittest.TestCase):
    def test_answer_plan_never_exposes_business_tools(self):
        client = BailianClient("http://agent.invalid", "key", "qwen-plus")
        payloads = []
        calls = []

        def request(method, path, payload=None):
            payloads.append(payload)
            return {"id": "answer-1", "choices": [{"message": {"content": "你好，我可以帮助处理物业业务。"}}]}

        token = _PLANNER_HINT.set({"action": "ANSWER", "intent": "unknown", "candidates": []})
        try:
            with patch.object(client, "_request", side_effect=request):
                result = client.chat("你好", "property:1:v1", tool_callback=lambda args: calls.append(args))
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual(calls, [])
        self.assertNotIn("tools", payloads[0])
        self.assertNotIn("tool_choice", payloads[0])
        self.assertEqual(result["execution_state"], "NOT_EXECUTED")

    def test_answer_plan_cannot_claim_a_write_succeeded(self):
        client = BailianClient("http://agent.invalid", "key", "qwen-plus")
        token = _PLANNER_HINT.set({"action": "ANSWER", "intent": "unknown", "candidates": []})
        try:
            with patch.object(client, "_request", return_value={
                "id": "answer-2",
                "choices": [{"message": {"content": "A栋101的物业费账单已生成完毕。"}}],
            }):
                result = client.chat("生成A栋101的物业费账单", "property:1:v1", tool_callback=lambda _: None)
        finally:
            _PLANNER_HINT.reset(token)

        self.assertIn("没有完成任何业务写入", result["answer"])
        self.assertEqual(result["execution_state"], "NOT_EXECUTED")

    def test_planner_fallback_is_attached_to_assistant_before_tool_result(self):
        client = BailianClient("http://agent.invalid", "key", "qwen-plus")
        payloads = []
        responses = iter([
            {"id": "one", "choices": [{"message": {"content": "我来处理"}}]},
            {"id": "two", "choices": [{"message": {"content": "公告已发布"}}]},
        ])

        def request(method, path, payload=None):
            payloads.append(payload)
            return next(responses)

        token = _PLANNER_HINT.set({
            "action": "TOOL",
            "intent": "notice.save",
            "candidates": ["notice.save"],
            "tool_call": {"operation": "execute", "command": "notice.save", "arguments_json": "{}"},
        })
        try:
            with patch.object(client, "_request", side_effect=request):
                result = client.chat(
                    "发公告",
                    "property:1:v1",
                    tool_callback=lambda _: {"ok": True, "code": "SUCCESS", "terminal": True},
                )
        finally:
            _PLANNER_HINT.reset(token)

        second_messages = payloads[1]["messages"]
        assistant = next(item for item in second_messages if item.get("role") == "assistant" and item.get("tool_calls"))
        tool = next(item for item in second_messages if item.get("role") == "tool")
        self.assertEqual(assistant["tool_calls"][0]["id"], tool["tool_call_id"])
        self.assertEqual(result["execution_state"], "EXECUTED")


class _StreamResponse:
    status_code = 200
    headers = {"Content-Type": "text/event-stream"}
    encoding = "utf-8"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_lines(self, **kwargs):
        events = [
            {
                "id": "deepseek-stream",
                "choices": [{"delta": {"reasoning_content": "先判断权限。"}, "finish_reason": None}],
            },
            {
                "id": "deepseek-stream",
                "choices": [{"delta": {"tool_calls": [{
                    "index": 0,
                    "id": "call-1",
                    "function": {"name": "property_agent_tool", "arguments": "{\\\"operation\\\":\\\"context\\\"}"},
                }]}, "finish_reason": "tool_calls"}],
            },
        ]
        for event in events:
            yield "data: " + json.dumps(event, ensure_ascii=False)
        yield "data: [DONE]"


class DeepSeekProtocolTests(unittest.TestCase):
    def test_stream_preserves_reasoning_content_for_followup_tool_round(self):
        client = DeepSeekClient("https://api.deepseek.com", "deepseek-key", "deepseek-v4-pro")
        with patch("dify_client.requests.request", return_value=_StreamResponse()):
            generator = client._stream_completion({"model": client.model, "messages": [], "stream": True})
            while True:
                try:
                    next(generator)
                except StopIteration as stop:
                    result = stop.value
                    break
        self.assertEqual(result["message"]["reasoning_content"], "先判断权限。")
        self.assertEqual(result["message"]["tool_calls"][0]["id"], "call-1")


if __name__ == "__main__":
    unittest.main()
