"""Compatibility and safety wrapper for direct model providers.

The stable provider implementation lives in :mod:`dify_client_core`. This
wrapper tightens final-answer semantics, repeated-tool termination and safe
contextual resolution without copying authorization logic out of the backend.
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

_RESOLVER_ARGUMENTS = {
    'house.search': {'community_id', 'building_id', 'building_name', 'unit', 'room_no', 'house_id', 'id'},
    'building.search': {'community_id', 'building_id', 'building_name', 'building', 'name', 'id'},
    'unit.search': {'building_id', 'unit_id', 'unit', 'unit_name', 'name', 'id'},
    'person.search': {'person_id', 'person_name', 'phone', 'id'},
    'person.properties': {'person_id', 'person_name', 'phone', 'id'},
    'order.search': {'order_no', 'status', 'q', 'id'},
    'order.pending': {'order_no', 'status', 'q', 'id'},
    'complaint.search': {'community_id', 'building_id', 'house_id', 'status', 'q', 'id'},
    'visitor.search': {'community_id', 'building_id', 'house_id', 'status', 'phone', 'name', 'id'},
    'vehicle.search': {'community_id', 'building_id', 'house_id', 'person_id', 'status', 'plate', 'id'},
    'parking.search': {'community_id', 'building_id', 'status', 'space_code', 'plate', 'id'},
    'device.search': {'community_id', 'building_id', 'status', 'category', 'code', 'name', 'id'},
    'inspection.search': {'community_id', 'building_id', 'device_id', 'assignee_id', 'status', 'id'},
    'fee.search': {'community_id', 'fee_item_id', 'name', 'id'},
    'payment.search': {'bill_id', 'status', 'id'},
    'billing.unpaid': {'community_id', 'building_id', 'building_name', 'unit', 'room_no', 'house_id', 'person_id', 'month', 'id'},
    'notice.read': {'community_id', 'building_id'},
    'whoami': set(),
}


def _planner_action():
    hint = _PLANNER_HINT.get() or {}
    action = str(hint.get('action') or 'ANSWER').upper()
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


def _context_resolver_fallback():
    """Build one read-only resolver call for a RESOLVE_FIRST plan.

    This never invents ids and never upgrades authority. The candidate list has
    already been intersected with the logged-in user's server-side capabilities;
    the backend query performs Policy/DataScope checks again.
    """
    hint = _PLANNER_HINT.get() or {}
    if hint.get('entity_status') != 'RESOLVE_FIRST':
        return None
    candidates = [item for item in hint.get('candidates', ()) if item in _READ_ONLY_INTENTS]
    if not candidates:
        return None
    command = candidates[0]
    values = dict(hint.get('arguments') or {})
    allowed = _RESOLVER_ARGUMENTS.get(command, set())
    params = {key: value for key, value in values.items() if key in allowed and value not in (None, '')}
    # Normalize planner-side business aliases to resolver field names.
    if command == 'device.search' and 'code' not in params and values.get('device_code'):
        params['code'] = values['device_code']
    if command == 'visitor.search' and 'name' not in params and values.get('visitor_name'):
        params['name'] = values['visitor_name']
    if command == 'parking.search' and 'space_code' not in params and values.get('space_code'):
        params['space_code'] = values['space_code']
    return {
        'operation': 'lookup',
        'command': command,
        'arguments_json': json.dumps(params, ensure_ascii=False),
    }


def _synthetic_call(args, call_id='planner-fallback'):
    return {
        'id': call_id,
        'type': 'function',
        'function': {
            'name': 'property_agent_tool',
            'arguments': json.dumps(args, ensure_ascii=False),
        },
    }


def _chat_common(self, query, user, conversation_id, tool_callback, system_prompt, stream=False):
    """Run a bounded tool loop with server-verifiable execution state."""
    messages = self._conversation_messages(query, user, conversation_id, system_prompt)
    seen_tool_calls = set()
    completed_commands = set()
    previous_progress = None
    no_progress = 0
    force_final = False
    planner_fallback_used = False
    resolver_fallback_used = False
    pending = False
    executed = False
    lookup_performed = False
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
            if force_final:
                if not isinstance(provider_calls, list) or len(provider_calls) > 4:
                    raise _core.DifyUnavailable(f'{self.service_name}返回的工具调用过多。', 'bad_response')
                message = dict(message)
                message.setdefault('role', 'assistant')
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
        if allow_tools and not provider_calls and not calls:
            fallback = (_PLANNER_HINT.get() or {}).get('tool_call')
            if isinstance(fallback, dict) and not planner_fallback_used:
                calls = [_synthetic_call(fallback)]
                planner_fallback_used = True
            elif not resolver_fallback_used:
                resolver = _context_resolver_fallback()
                if isinstance(resolver, dict):
                    calls = [_synthetic_call(resolver, 'planner-resolver')]
                    resolver_fallback_used = True
        if not provider_calls and calls:
            message = dict(message)
            message['role'] = 'assistant'
            message['tool_calls'] = calls
            message['content'] = message.get('content') or None
        elif provider_calls:
            message = dict(message)
            message.setdefault('role', 'assistant')
        if calls and tool_callback:
            if not isinstance(calls, list) or len(calls) > 4:
                raise _core.DifyUnavailable(f'{self.service_name}返回的工具调用过多。', 'bad_response')
            messages.append(message)
            for call in calls:
                try:
                    args, result = self._run_tool_call(call, tool_callback, seen_tool_calls, completed_commands)
                    if isinstance(result, dict):
                        command = args.get('command')
                        operation = args.get('operation')
                        if operation == 'lookup' and result.get('ok') is not False:
                            lookup_performed = True
                        if operation == 'execute' and result.get('ok') is not False and result.get('terminal'):
                            executed = True
                        if operation == 'propose' and result.get('code') == 'CONFIRMATION_REQUIRED':
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
                        elif result.get('terminal') and operation in {'execute', 'propose'}:
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
        state = 'EXECUTED' if executed else ('PENDING_CONFIRMATION' if pending else ('LOOKUP_ONLY' if lookup_performed else 'NOT_EXECUTED'))
        if stream:
            yield {'type': 'done', 'answer': answer, 'conversation_id': cid, 'execution_state': state}
            return
        return {'answer': answer, 'conversation_id': cid, 'execution_state': state}
    raise _core.DifyUnavailable(f'{self.service_name}工具调用次数超出限制，请重试。', 'bad_response')


_core._planner_action = _planner_action
_core._expected_write = _expected_write
_core.BailianClient._chat_common = _chat_common
