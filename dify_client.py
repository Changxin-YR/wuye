"""AI provider adapters for Dify and OpenAI-compatible direct model providers.

The LLM is a planner/presenter, never an authorization authority. Direct model
providers only receive business tools when the deterministic planner has
resolved a TOOL/CONFIRM action. Final answers are guarded so a model cannot
claim a write succeeded unless the server-side tool pipeline actually executed
or produced a pending confirmation.
"""
import json
import os
import re
import time
import uuid
from copy import deepcopy
from contextvars import ContextVar
from urllib.parse import urlsplit

import requests


class DifyUnavailable(RuntimeError):
    def __init__(self, message, code="unavailable"):
        super().__init__(message)
        self.code = code


_TOOL_COMMANDS = ContextVar("bailian_tool_commands", default=None)
_PLANNER_HINT = ContextVar("bailian_planner_hint", default=None)

_WRITE_SUCCESS_RE = re.compile(
    r"(?:已|成功)(?:创建|生成|发布|保存|修改|更新|删除|归档|登记|分配|派单|绑定|解绑|收款|冲销|关闭|取消|完成|执行)"
    r"|(?:创建|生成|发布|保存|修改|更新|删除|归档|登记|分配|派单|绑定|解绑|收款|冲销|关闭|取消|完成)(?:成功|完毕)"
)


def _planner_action():
    return str((_PLANNER_HINT.get() or {}).get("action") or "ANSWER").upper()


def _tools_allowed():
    return _planner_action() in {"TOOL", "CONFIRM"}


def _planner_calls(message, calls, force_final, allow_fallback=True):
    """Keep provider calls inside the deterministic planner candidate set."""
    if force_final or not _tools_allowed():
        return []
    hint = _PLANNER_HINT.get() or {}
    candidates = {item for item in hint.get("candidates", ()) if isinstance(item, str)}
    fallback = hint.get("tool_call")
    valid = []
    for call in calls or ():
        try:
            fn = call.get("function") or {}
            args = json.loads(fn.get("arguments", "{}"))
            command = args.get("command") if isinstance(args, dict) else None
        except (TypeError, ValueError, json.JSONDecodeError):
            command = None
        if not candidates or command in candidates:
            valid.append(call)
    if valid or not allow_fallback or not isinstance(fallback, dict):
        return valid
    return [{
        "id": "planner-fallback",
        "type": "function",
        "function": {
            "name": "property_agent_tool",
            "arguments": json.dumps(fallback, ensure_ascii=False),
        },
    }]


def _expected_write():
    hint = _PLANNER_HINT.get() or {}
    fallback = hint.get("tool_call")
    if not isinstance(fallback, dict):
        return False
    return fallback.get("operation") in {"execute", "propose"}


def _safe_final_answer(answer, executed=False, pending=False):
    """Prevent false-success language when no server-side mutation occurred."""
    text = answer.strip() if isinstance(answer, str) else ""
    if not text:
        raise DifyUnavailable("AI未返回有效文本。", "bad_response")
    if pending:
        return text
    if executed:
        return text
    if _expected_write() or (_planner_action() == "ANSWER" and _WRITE_SUCCESS_RE.search(text)):
        return "本轮没有完成任何业务写入，系统也没有把该操作标记为已执行。请补充必要信息后重新发起，我会在真实执行并通过数据库校验后再告知成功。"
    return text


class BailianClient:
    """Alibaba Bailian/Qwen OpenAI-compatible chat client."""

    def __init__(self, base_url, api_key, model="qwen-plus", timeout=60):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = (api_key or "").strip()
        self.model = (model or "qwen-plus").strip()
        self.timeout = max(1, min(int(timeout), 120))
        self.provider = "bailian"
        self._histories = {}

    @property
    def service_name(self):
        return "DeepSeek" if self.provider == "deepseek" else "百炼"

    @property
    def configured(self):
        return bool(
            self.api_key
            and not self.api_key.startswith(("sk-your", "your-", "replace-"))
            and self.base_url
            and self.model
        )

    def _conversation_messages(self, query, user, conversation_id, system_prompt):
        key = (user, conversation_id) if conversation_id else None
        messages = deepcopy(self._histories.get(key, [])) if key else []
        if messages:
            if isinstance(system_prompt, str) and system_prompt.strip():
                if messages[0].get("role") == "system":
                    messages[0] = {"role": "system", "content": system_prompt}
                else:
                    messages.insert(0, {"role": "system", "content": system_prompt})
        else:
            messages = (
                [{"role": "system", "content": system_prompt}]
                if isinstance(system_prompt, str) and system_prompt.strip()
                else []
            ) + [{"role": "user", "content": query}]
            return messages
        messages.append({"role": "user", "content": query})
        return messages

    def _save_conversation(self, user, conversation_id, messages):
        if conversation_id:
            self._histories[(user, conversation_id)] = deepcopy(messages[-24:])

    def _validate_url(self):
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise DifyUnavailable(f"{self.service_name}地址配置不正确，请管理员检查。", "configuration")

    def _request(self, method, path, payload=None):
        if not self.configured:
            raise DifyUnavailable(f"AI尚未配置{self.service_name} API Key，请联系管理员。", "not_configured")
        self._validate_url()
        try:
            with requests.request(
                method,
                self.base_url + path,
                headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"},
                json=payload,
                timeout=(5, self.timeout),
                allow_redirects=False,
            ) as response:
                if response.status_code in {401, 403}:
                    raise DifyUnavailable(f"{self.service_name} API Key 无效或权限不足，请联系管理员。", "authentication")
                if response.status_code == 429:
                    raise DifyUnavailable(f"{self.service_name}服务繁忙，请稍后重试。", "rate_limit")
                if not 200 <= response.status_code < 300:
                    raise DifyUnavailable(f"{self.service_name}服务返回异常，请管理员检查模型配置。", "upstream")
                obj = response.json()
                if not isinstance(obj, dict):
                    raise DifyUnavailable(f"{self.service_name}返回格式异常。", "bad_response")
                return obj
        except requests.Timeout as exc:
            raise DifyUnavailable(f"{self.service_name}响应超时，请稍后重试。", "timeout") from exc
        except requests.RequestException as exc:
            raise DifyUnavailable(f"无法连接{self.service_name}服务，请管理员检查网络和 API 地址。", "connection") from exc
        except (ValueError, UnicodeError) as exc:
            raise DifyUnavailable(f"{self.service_name}返回了无法解析的内容。", "bad_response") from exc

    def _stream_completion(self, payload):
        if not self.configured:
            raise DifyUnavailable(f"AI尚未配置{self.service_name} API Key，请联系管理员。", "not_configured")
        self._validate_url()
        try:
            with requests.request(
                "POST",
                self.base_url + "/chat/completions",
                headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"},
                json=payload,
                timeout=(5, self.timeout),
                allow_redirects=False,
                stream=True,
            ) as response:
                if response.status_code in {401, 403}:
                    raise DifyUnavailable(f"{self.service_name} API Key 无效或权限不足，请联系管理员。", "authentication")
                if response.status_code == 429:
                    raise DifyUnavailable(f"{self.service_name}服务繁忙，请稍后重试。", "rate_limit")
                if not 200 <= response.status_code < 300:
                    raise DifyUnavailable(f"{self.service_name}服务返回异常，请管理员检查模型配置。", "upstream")
                if "text/event-stream" not in response.headers.get("Content-Type", ""):
                    raise DifyUnavailable(f"{self.service_name}未返回SSE流，请检查模型配置。", "bad_response")
                response.encoding = "utf-8"
                started = time.monotonic()
                size = 0
                parts = []
                reasoning_parts = []
                calls = {}
                response_id = ""
                done = False
                for line in response.iter_lines(chunk_size=512, decode_unicode=True):
                    size += len(line or "")
                    if size > 2_000_000:
                        raise DifyUnavailable("AI响应过长，请缩小问题范围。", "bad_response")
                    if time.monotonic() - started > self.timeout:
                        raise DifyUnavailable(f"{self.service_name}响应超时，请稍后重试。", "timeout")
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[5:].lstrip()
                    if raw == "[DONE]":
                        done = True
                        break
                    obj = json.loads(raw)
                    choices = obj.get("choices") if isinstance(obj, dict) else None
                    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                        raise ValueError()
                    response_id = response_id or obj.get("id", "")
                    delta = choices[0].get("delta") or {}
                    if not isinstance(delta, dict):
                        raise ValueError()
                    reasoning = delta.get("reasoning_content")
                    if isinstance(reasoning, str) and reasoning:
                        reasoning_parts.append(reasoning)
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        parts.append(content)
                        yield {"type": "delta", "content": content}
                    fragments = delta.get("tool_calls") or []
                    if not isinstance(fragments, list):
                        raise ValueError()
                    for fragment in fragments:
                        if not isinstance(fragment, dict):
                            raise ValueError()
                        index = fragment.get("index", 0)
                        if not isinstance(index, int) or index < 0 or index > 4:
                            raise ValueError()
                        call = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        if isinstance(fragment.get("id"), str) and not call["id"]:
                            call["id"] = fragment["id"]
                        fn = fragment.get("function") or {}
                        if not isinstance(fn, dict):
                            raise ValueError()
                        if isinstance(fn.get("name"), str):
                            call["function"]["name"] += fn["name"]
                        if isinstance(fn.get("arguments"), str):
                            call["function"]["arguments"] += fn["arguments"]
                if not done:
                    raise DifyUnavailable(f"{self.service_name}流式回答不完整，请重试。", "bad_response")
                message = {"role": "assistant", "content": "".join(parts) or None}
                if reasoning_parts:
                    message["reasoning_content"] = "".join(reasoning_parts)
                if calls:
                    message["tool_calls"] = [calls[i] for i in sorted(calls)]
                return {"id": response_id, "message": message}
        except requests.Timeout as exc:
            raise DifyUnavailable(f"{self.service_name}响应超时，请稍后重试。", "timeout") from exc
        except requests.RequestException as exc:
            raise DifyUnavailable(f"无法连接{self.service_name}服务，请管理员检查网络和 API 地址。", "connection") from exc
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise DifyUnavailable(f"{self.service_name}返回了无法解析的内容。", "bad_response") from exc

    def _tool_definition(self):
        command_schema = {"type": "string", "description": "仅使用授权目录中的命令"}
        planner_candidates = (_PLANNER_HINT.get() or {}).get("candidates")
        if planner_candidates:
            command_schema["enum"] = sorted(set(planner_candidates))
        elif _TOOL_COMMANDS.get():
            command_schema["enum"] = sorted(_TOOL_COMMANDS.get())
        return [{
            "type": "function",
            "function": {
                "name": "property_agent_tool",
                "description": "查询授权物业数据或办理业务。最终身份、权限、DataScope、风险和成功状态均以后端为准。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "request_token": {"type": "string"},
                        "operation": {"type": "string", "enum": ["context", "lookup", "execute", "propose"]},
                        "command": command_schema,
                        "arguments_json": {"type": "string", "description": "JSON 对象；写操作只使用服务端解析出的真实 id/version"},
                    },
                    "required": ["operation"],
                },
            },
        }]

    def _run_tool_call(self, call, tool_callback, seen_tool_calls, completed_commands):
        fn = call["function"]
        args = json.loads(fn["arguments"])
        if fn.get("name") != "property_agent_tool" or not isinstance(args, dict):
            raise ValueError()
        if not _tools_allowed():
            return args, {"ok": False, "code": "PLANNER_BLOCKED", "message": "当前规划不允许调用业务工具。", "terminal": True}
        if _planner_action() == "CONFIRM" and args.get("operation") == "execute":
            args["operation"] = "propose"
        canonical = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        signature = json.dumps([fn.get("name"), canonical], ensure_ascii=False)
        command = args.get("command")
        if signature in seen_tool_calls or command in completed_commands:
            return args, {"ok": True, "code": "ALREADY_EXECUTED", "message": "本轮已处理该业务命令，请直接给出结果。", "terminal": True}
        seen_tool_calls.add(signature)
        result = tool_callback(args)
        if isinstance(result, dict) and result.get("terminal") and args.get("operation") == "execute" and result.get("ok") is not False:
            completed_commands.add(command)
        return args, result

    def _chat_common(self, query, user, conversation_id, tool_callback, system_prompt, stream=False):
        messages = self._conversation_messages(query, user, conversation_id, system_prompt)
        seen_tool_calls = set()
        completed_commands = set()
        previous_progress = None
        no_progress = 0
        force_final = False
        planner_fallback_used = False
        pending = False
        executed = False
        tools = self._tool_definition()
        for _ in range(6):
            payload = {"model": self.model, "messages": messages, "stream": stream}
            allow_tools = bool(tool_callback and not force_final and _tools_allowed())
            if allow_tools:
                payload["tools"] = tools
                payload["tool_choice"] = "required"
            if stream:
                completion = yield from self._stream_completion(payload)
                message = completion.get("message") or {}
                response_id = completion.get("id") or ""
            else:
                obj = self._request("POST", "/chat/completions", payload)
                choices = obj.get("choices")
                if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                    raise DifyUnavailable(f"{self.service_name}返回了无法识别的回答。", "bad_response")
                message = choices[0].get("message") or {}
                response_id = obj.get("id") or ""
            if not isinstance(message, dict):
                raise DifyUnavailable(f"{self.service_name}返回了无法识别的回答。", "bad_response")
            provider_calls = message.get("tool_calls") or []
            calls = _planner_calls(message, provider_calls, force_final, not planner_fallback_used) if allow_tools else []
            if not provider_calls and calls:
                planner_fallback_used = True
                message = dict(message)
                message["tool_calls"] = calls
                message["content"] = message.get("content") or None
            if calls and tool_callback:
                if not isinstance(calls, list) or len(calls) > 4:
                    raise DifyUnavailable(f"{self.service_name}返回的工具调用过多。", "bad_response")
                messages.append(message)
                for call in calls:
                    try:
                        args, result = self._run_tool_call(call, tool_callback, seen_tool_calls, completed_commands)
                        if isinstance(result, dict):
                            if args.get("operation") == "execute" and result.get("ok") is not False and result.get("terminal"):
                                executed = True
                            if args.get("operation") == "propose" and result.get("code") == "CONFIRMATION_REQUIRED":
                                pending = True
                            progress = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
                            if progress == previous_progress:
                                no_progress += 1
                            else:
                                previous_progress = progress
                                no_progress = 0
                            if no_progress >= 1:
                                result = {"ok": False, "code": "NO_PROGRESS", "message": "连续工具结果没有进展，请澄清后再试。", "terminal": True}
                                force_final = True
                            if result.get("terminal") and args.get("operation") in {"execute", "propose"}:
                                force_final = True
                            if result.get("error") or result.get("code") in {
                                "MISSING_PARAMETER", "AMBIGUOUS_ENTITY", "PERMISSION_DENIED", "DATA_SCOPE_DENIED",
                                "RESOURCE_NOT_FOUND", "BUSINESS_CONFLICT", "VALIDATION_ERROR", "SYSTEM_ERROR", "PLANNER_BLOCKED",
                            }:
                                force_final = True
                    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
                        result = {"ok": False, "code": "VALIDATION_ERROR", "message": "工具调用参数无效", "terminal": True}
                        force_final = True
                    messages.append({
                        "role": "tool",
                        "tool_call_id": str(call.get("id", "")),
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    })
                continue
            answer = _safe_final_answer(message.get("content"), executed=executed, pending=pending)
            cid = conversation_id or response_id or str(uuid.uuid4())
            final_message = dict(message)
            final_message["role"] = "assistant"
            final_message["content"] = answer
            messages.append(final_message)
            self._save_conversation(user, cid, messages)
            state = "EXECUTED" if executed else ("PENDING_CONFIRMATION" if pending else ("LOOKUP_ONLY" if _planner_action() == "TOOL" else "NOT_EXECUTED"))
            if stream:
                yield {"type": "done", "answer": answer, "conversation_id": cid, "execution_state": state}
                return
            return {"answer": answer, "conversation_id": cid, "execution_state": state}
        raise DifyUnavailable(f"{self.service_name}工具调用次数超出限制，请重试。", "bad_response")

    def chat_stream(self, query, user, conversation_id="", tool_callback=None, system_prompt=""):
        yield from self._chat_common(query, user, conversation_id, tool_callback, system_prompt, stream=True)

    def chat(self, query, user, conversation_id="", tool_callback=None, system_prompt=""):
        runner = self._chat_common(query, user, conversation_id, tool_callback, system_prompt, stream=False)
        try:
            next(runner)
        except StopIteration as stop:
            return stop.value
        raise DifyUnavailable("AI内部状态异常。", "bad_response")

    def _native_tool_check(self):
        tools = [{
            "type": "function",
            "function": {
                "name": "healthcheck_tool",
                "description": "连接测试工具",
                "parameters": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
            },
        }]
        obj = self._request("POST", "/chat/completions", {
            "model": self.model,
            "messages": [{"role": "user", "content": "请调用 healthcheck_tool，并将 ok 设为 true。"}],
            "tools": tools,
            "tool_choice": "required",
            "stream": False,
        })
        choices = obj.get("choices")
        message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
        calls = message.get("tool_calls") if isinstance(message, dict) else None
        if not isinstance(calls, list) or not calls:
            raise DifyUnavailable(f"{self.service_name}未返回原生 Tool Call。", "bad_response")

    def check(self, infer=False):
        obj = self._request("GET", "/models")
        if not isinstance(obj.get("data"), list):
            raise DifyUnavailable(f"{self.service_name}模型列表返回格式异常。", "bad_response")
        tool_call = "not_checked"
        if infer:
            self.chat("这是连接测试，请只回答：连接成功。", "property-healthcheck")
            self._native_tool_check()
            tool_call = "ok"
        return {
            "status": "ok",
            "app_mode": "chat",
            "provider": self.provider,
            "model": self.model,
            "configured": self.configured,
            "inference": "ok" if infer else "not_checked",
            "tool_call": tool_call,
            "inference_checked": infer,
            "agent_tools_verified": bool(infer and tool_call == "ok"),
            "message": "AI API、模型推理与原生 Tool Call 正常" if infer else "AI API Key 和模型配置有效",
        }


OpenAICompatibleAgentClient = BailianClient


class DeepSeekClient(BailianClient):
    def __init__(self, base_url, api_key, model="deepseek-v4-pro", timeout=60):
        super().__init__(base_url, api_key, model, timeout)
        self.provider = "deepseek"


class DifyClient:
    def __init__(self, base_url, api_key, timeout=60):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = (api_key or "").strip()
        self.timeout = max(1, min(int(timeout), 120))

    @property
    def configured(self):
        return bool(self.api_key and not self.api_key.startswith("app-your") and self.base_url)

    def _request(self, method, path, payload=None, stream=False):
        if not self.configured:
            raise DifyUnavailable("AI尚未配置，请联系管理员。", "not_configured")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise DifyUnavailable("AI地址配置不正确，请管理员检查。", "configuration")
        try:
            with requests.request(
                method,
                self.base_url + path,
                headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"},
                json=payload,
                timeout=(5, self.timeout),
                allow_redirects=False,
                stream=stream,
            ) as response:
                if response.status_code in {401, 403}:
                    raise DifyUnavailable("AI应用密钥无效或权限不足，请联系管理员。", "authentication")
                if response.status_code == 429:
                    raise DifyUnavailable("AI服务繁忙，请稍后重试。", "rate_limit")
                if not 200 <= response.status_code < 300:
                    raise DifyUnavailable("AI服务返回异常，请管理员检查已发布的应用。", "upstream")
                if stream:
                    if "text/event-stream" not in response.headers.get("Content-Type", ""):
                        raise DifyUnavailable("Agent未返回SSE流，请检查应用接口。", "bad_response")
                    return self._stream(response)
                obj = response.json()
                if not isinstance(obj, dict):
                    raise DifyUnavailable("AI返回格式异常。", "bad_response")
                return obj
        except requests.Timeout as exc:
            raise DifyUnavailable("AI响应超时，请稍后重试。", "timeout") from exc
        except requests.RequestException as exc:
            raise DifyUnavailable("无法连接AI服务，请管理员检查Dify及模型服务。", "connection") from exc
        except (ValueError, UnicodeError) as exc:
            raise DifyUnavailable("AI返回了无法解析的内容。", "bad_response") from exc

    def _stream(self, response):
        started = time.monotonic()
        chunks = []
        cid = ""
        ended = False
        size = 0
        event_lines = []
        response.encoding = "utf-8"
        for line in response.iter_lines(chunk_size=512, decode_unicode=True):
            size += len(line)
            if size > 2_000_000:
                raise DifyUnavailable("AI响应过长，请缩小问题范围。", "bad_response")
            if time.monotonic() - started > self.timeout:
                raise DifyUnavailable("AI响应超时，请稍后重试。", "timeout")
            if line.startswith("data:"):
                event_lines.append(line[5:].lstrip())
            elif not line and event_lines:
                raw = "\n".join(event_lines)
                event_lines = []
                if raw == "[DONE]":
                    break
                obj = json.loads(raw)
                if not isinstance(obj, dict):
                    raise ValueError()
                event = obj.get("event")
                if event == "error":
                    raise DifyUnavailable("Agent执行失败，请管理员查看Dify运行记录。", "upstream")
                if event in {"message", "agent_message", "message_replace", "message_end"}:
                    newcid = obj.get("conversation_id")
                    if newcid:
                        if not isinstance(newcid, str) or len(newcid) > 128 or (cid and cid != newcid):
                            raise ValueError()
                        cid = newcid
                    if event in {"message", "agent_message", "message_replace"}:
                        answer = obj.get("answer")
                        if not isinstance(answer, str):
                            raise ValueError()
                        if event == "message_replace":
                            chunks = [answer]
                        else:
                            chunks.append(answer)
                    if event == "message_end":
                        ended = True
                        break
        answer = "".join(chunks)
        if not ended or not answer.strip() or not cid:
            raise DifyUnavailable("AI流式回答不完整，请重试。", "bad_response")
        return {"answer": answer, "conversation_id": cid}

    def check(self, infer=False):
        obj = self._request("GET", "/info")
        mode = obj.get("mode")
        if mode not in {"chat", "agent-chat", "advanced-chat"}:
            raise DifyUnavailable("该应用不使用受支持的chat-messages接口，请核对Dify API访问页。", "app_mode")
        if infer:
            self.chat("这是连接测试，请不要使用任何工具，只回答：连接成功。", "property-healthcheck")
        return {
            "status": "ok",
            "app_mode": mode,
            "inference_checked": infer,
            "agent_tools_verified": False,
            "message": "模型已返回有效文本；业务工具须另做联调" if infer else "Dify接口与密钥有效，尚未验证模型推理",
        }

    def chat(self, query, user, conversation_id=""):
        payload = {"inputs": {}, "query": query, "response_mode": "streaming", "user": user}
        if conversation_id:
            payload["conversation_id"] = conversation_id
        return self._request("POST", "/chat-messages", payload, stream=True)


def chat(message, user_id, role):
    return DifyClient(
        os.getenv("DIFY_BASE_URL", "http://127.0.0.1/v1"),
        os.getenv("DIFY_API_KEY", ""),
    ).chat(message, f"property:{user_id}:{role}")["answer"]


def client_from_env():
    provider = os.getenv("AI_PROVIDER", "bailian").strip().lower()
    timeout = os.getenv("DIFY_TIMEOUT", "60")
    if provider == "dify":
        return DifyClient(os.getenv("DIFY_BASE_URL", "http://127.0.0.1/v1"), os.getenv("DIFY_API_KEY", ""), timeout)
    if provider == "deepseek":
        return DeepSeekClient(
            os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            os.getenv("DEEPSEEK_API_KEY") or os.getenv("DEEPSEEK_KEY", ""),
            os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro"),
            timeout,
        )
    return BailianClient(
        os.getenv("BAILIAN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        os.getenv("BAILIAN_API_KEY") or os.getenv("DASHSCOPE_API_KEY", ""),
        os.getenv("BAILIAN_MODEL", "qwen-plus"),
        timeout,
    )
