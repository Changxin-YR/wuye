import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from dify_client import BailianClient


def main():
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    client = BailianClient(
        os.getenv("BAILIAN_BASE_URL"),
        os.getenv("BAILIAN_API_KEY") or os.getenv("DASHSCOPE_API_KEY", ""),
        os.getenv("BAILIAN_MODEL", "qwen-plus"),
        20,
    )
    check = client.check(infer=True)
    streamed = list(client.chat_stream(
        "请只回复：流式连接成功。",
        "property-live-stream-check",
        system_prompt="你是测试助手。",
    ))
    calls = []
    result = client.chat(
        "请调用 property_agent_tool，operation 使用 context；不要执行任何写操作。",
        "property-live-tool-check",
        tool_callback=lambda args: (calls.append(args) or {"status": "ok", "queries": []}),
        system_prompt="必须优先调用 property_agent_tool 获取 context；不要执行写操作。",
    )
    output = {
        "configured": client.configured,
        "check_status": check.get("status"),
        "inference_checked": check.get("inference_checked"),
        "stream_event_count": len(streamed),
        "stream_done": any(item.get("type") == "done" for item in streamed),
        "tool_callback_calls": len(calls),
        "tool_args_keys": sorted(calls[0]) if calls else [],
        "answer_received": bool(result.get("answer")),
    }
    Path(__file__).with_name("bailian_acceptance_result.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
