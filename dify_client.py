"""Compatibility and safety wrapper for direct model providers.

The stable provider implementation lives in :mod:`dify_client_core`.  This
wrapper tightens final-answer semantics and repeated-tool termination without
copying authorization logic out of the backend gateway.
"""
import json
import uuid

import dify_client_core as _core
from dify_client_core import *

_TOOL_COMMANDS = _core._TOOL_COMMANDS
_PLANNER_HINT = _core._PLANNER_HINT
_planner_calls = _core._planner_calls
_safe_final_answer = _core._safe_final_answer

_READ_ONLY_INTENTS = {
    'house.search', 'building.search', 'unit.search', 'person.search', 'person.properties',
    'order.search', 'order.pending', 'complaint.search', 'complaint.stats', 'visitor.search',
    'vehicle.search', 'parking.search', 'device.search', 'inspection.search', 'fee.search',
    'payment.search', 'billing.unpaid', 'notice.read', 'whoami',
}


def _planner_action():
    hint = _PLANNER_HINT.get() or {}
    action = str(hint.get('action') or 'ANSWER').upper()
    # A repeated-write request is intentionally routed through the existing
    # idempotency check in app.py. It is not permission to create a second row.
    if action == 'CLARIFY' and hint.get('entity_status') == 'REPEAT' and isinstance(hint.get('tool_call'), dict):
        return 'TOOL'
    return action


def _expected_write():
    hint = _PLANNER_HINT.get() or {}
    action = str(hint.get('action') or '').upper()
    intent = str(hint.get('intent') or '')
    if action == 'CONFIRM':
        return True
    if action == 'TOOL' and intent and intent not in _READ_ONLY_INTENTS and intent not in {'unknown', 'security_boundary'}:
        return True
    fallback = hint.get('tool_call')
    return isinstance(fallback, dict) and fallback.get('operation') in {'execute', 'propose'}


def _chat_common(self, query, user, conversation_id, tool_callback, system_prompt, stream=False):
    """Run a bounded tool loop and turn repeated post-terminal calls into feedback.

    Once a write succeeds, tools are removed from subsequent provider payloads.
    If a provider nevertheless repeats a Tool Call, it is never executed again;
    a synthetic terminal tool result is appended and one final text round is
    requested.  Lookup no-progress behaves the same way after two identical
    results.
    """
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
        payload = {'model': self.model, 'messages': messages, 'stream': stream}
        allow_tools = bool(tool_callback and not force_final and _core._tools_allowed())
        if allow_tools:
            payload['tools'] = tools
            payload['tool_choice'] = 'required'
        if stream:
            completion = yield from self._stream_completion(payload)
            message = completion.get('message') or {}
            response_id = completion.get('id') or ''
        else:
            obj = self._request('POST', '/chat/completions', payload)
            choices = obj.get('choices')
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise _core.DifyUnavailable(f'{self.service_name}返回了无法识别的回答。', 'bad_response')
            message = choices[0].get('message') or {}
            response_id = obj.get('id') or ''
        if not isinstance(message, dict):
            raise _core.DifyUnavailable(f'{self.service_name}返回了无法识别的回答。', 'bad_response')

        provider_calls = message.get('tool_calls') or []
        if provider_calls and not allow_tools:
            # No callback is invoked here. This is either a provider ignoring the
            # final-text round or a hallucinated tool call in an ANSWER plan.
            if force_final:
                if not isinstance(provider_calls, list) or len(provider_calls) > 4:
                    raise _core.DifyUnavailable(f'{self.service_name}返回的工具调用过多。', 'bad_response')
                messages.append(message)
                code = 'ALREADY_EXECUTED' if executed or completed_commands else 'NO_PROGRESS'
                text = '业务操作已经处理，不会重复执行，请直接给出最终结果。' if code == 'ALREADY_EXECUTED' else '工具调用没有新进展，请直接给出最终结果。'
                for call in provider_calls:
                    messages.append({
                        'role': 'tool',
                        'tool_call_id': str(call.get('id', '')),
                        'content': json.dumps({'ok': True, 'code': code, 'message': text, 'terminal': True}, ensure_ascii=False),
                    })
                continue
            message = dict(message)
            message.pop('tool_calls', None)
            message['content'] = message.get('content') or '当前请求没有执行任何业务操作。'
            provider_calls = []

        calls = _core._planner_calls(message, provider_calls, force_final, not planner_fallback_used) if allow_tools else []
        if not provider_calls and calls:
            planner_fallback_used = True
            message = dict(message)
            message['tool_calls'] = calls
            message['content'] = message.get('content') or None
        if calls and tool_callback:
            if not isinstance(calls, list) or len(calls) > 4:
                raise _core.DifyUnavailable(f'{self.service_name}返回的工具调用过多。', 'bad_response')
            messages.append(message)
            for call in calls:
                try:
                    args, result = self._run_tool_call(call, tool_callback, seen_tool_calls, completed_commands)
                    if isinstance(result, dict):
                        command = args.get('command')
                        if args.get('operation') == 'execute' and result.get('ok') is not False and result.get('terminal'):
                            executed = True
                        if args.get('operation') == 'propose' and result.get('code') == 'CONFIRMATION_REQUIRED':
                            pending = True
                        progress = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
                        if progress == previous_progress:
                            no_progress += 1
                        else:
                            previous_progress = progress
                            no_progress = 0
                        if no_progress >= 1:
                            result = {'ok': False, 'code': 'NO_PROGRESS', 'message': '连续工具结果没有进展，请澄清后再试。', 'terminal': True}
                            if command:
                                completed_commands.add(command)
                            force_final = True
                        elif result.get('code') == 'ALREADY_EXECUTED':
                            force_final = True
                        elif result.get('terminal') and args.get('operation') in {'execute', 'propose'}:
                            force_final = True
                        if result.get('error') or result.get('code') in {
                            'MISSING_PARAMETER', 'AMBIGUOUS_ENTITY', 'PERMISSION_DENIED', 'DATA_SCOPE_DENIED',
                            'RESOURCE_NOT_FOUND', 'BUSINESS_CONFLICT', 'VALIDATION_ERROR', 'SYSTEM_ERROR', 'PLANNER_BLOCKED',
                        }:
                            force_final = True
                except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
                    result = {'ok': False, 'code': 'VALIDATION_ERROR', 'message': '工具调用参数无效', 'terminal': True}
                    force_final = True
                messages.append({
                    'role': 'tool',
                    'tool_call_id': str(call.get('id', '')),
                    'content': json.dumps(result, ensure_ascii=False, default=str),
                })
            continue

        answer = _core._safe_final_answer(message.get('content'), executed=executed, pending=pending)
        cid = conversation_id or response_id or str(uuid.uuid4())
        final_message = dict(message)
        final_message['role'] = 'assistant'
        final_message['content'] = answer
        messages.append(final_message)
        self._save_conversation(user, cid, messages)
        state = 'EXECUTED' if executed else ('PENDING_CONFIRMATION' if pending else ('LOOKUP_ONLY' if _planner_action() == 'TOOL' else 'NOT_EXECUTED'))
        if stream:
            yield {'type': 'done', 'answer': answer, 'conversation_id': cid, 'execution_state': state}
            return
        return {'answer': answer, 'conversation_id': cid, 'execution_state': state}
    raise _core.DifyUnavailable(f'{self.service_name}工具调用次数超出限制，请重试。', 'bad_response')


_core._planner_action = _planner_action
_core._expected_write = _expected_write
_core.BailianClient._chat_common = _chat_common
