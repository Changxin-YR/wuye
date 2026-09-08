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
    'staff.search', 'complaint_staff.search', 'inspection_staff.search',
    'order.search', 'order.pending', 'complaint.search', 'complaint.stats', 'visitor.search',
    'vehicle.search', 'parking.search', 'parking_use.search', 'device.search', 'inspection.search', 'fee.search',
    'bill.search', 'payment.search', 'billing.unpaid', 'notice.read', 'whoami',
}

_RESOLVER_ARGUMENTS = {
    'house.search': {'community_id', 'building_id', 'building_name', 'unit', 'room_no', 'house_id', 'id'},
    'building.search': {'community_id', 'building_id', 'building_name', 'building', 'name', 'id'},
    'unit.search': {'building_id', 'unit_id', 'unit', 'unit_name', 'name', 'id'},
    'person.search': {'person_id', 'person_name', 'phone', 'id'},
    'person.properties': {'person_id', 'person_name', 'phone', 'id'},
    'staff.search': {'staff_name', 'username', 'phone', 'community_id', 'building_id', 'id'},
    'complaint_staff.search': {'staff_name', 'username', 'phone', 'community_id', 'building_id', 'id'},
    'inspection_staff.search': {'staff_name', 'username', 'phone', 'community_id', 'building_id', 'id'},
    'order.search': {'order_no', 'status', 'q', 'id'},
    'order.pending': {'order_no', 'status', 'q', 'id'},
    'complaint.search': {'community_id', 'building_id', 'house_id', 'status', 'q', 'id'},
    'visitor.search': {'community_id', 'building_id', 'house_id', 'status', 'phone', 'name', 'id'},
    'vehicle.search': {'community_id', 'building_id', 'house_id', 'person_id', 'status', 'plate', 'id'},
    'parking.search': {'community_id', 'building_id', 'status', 'space_code', 'plate', 'id'},
    'parking_use.search': {'community_id', 'building_id', 'status', 'space_code', 'plate', 'id'},
    'device.search': {'community_id', 'building_id', 'status', 'category', 'code', 'name', 'id'},
    'inspection.search': {'community_id', 'building_id', 'device_id', 'assignee_id', 'status', 'id'},
    'fee.search': {'community_id', 'fee_item_id', 'name', 'id'},
    'bill.search': {'bill_id', 'id', 'community_id', 'building_id', 'house_id', 'fee_item_id', 'status', 'month'},
    'payment.search': {'bill_id', 'status', 'id'},
    'billing.unpaid': {'community_id', 'building_id', 'building_name', 'unit', 'room_no', 'house_id', 'person_id', 'month', 'bill_id', 'id'},
    'notice.read': {'community_id', 'building_id'},
    'whoami': set(),
}

# If a user explicitly states a stable business identifier, the deterministic
# planner owns that target. The provider may present the result but may not
# silently switch to another in-scope object or add filters that hide it.
_PLANNER_OWNED_READ_FIELDS = {
    'order.search': {'order_no': ('order_no',)},
    'complaint.search': {'id': ('id', 'complaint_id')},
    'visitor.search': {'id': ('id',), 'phone': ('phone',), 'name': ('name', 'visitor_name')},
    'vehicle.search': {'plate': ('plate',)},
    'parking.search': {'space_code': ('space_code',), 'plate': ('plate',)},
    'parking_use.search': {'id': ('id',), 'space_code': ('space_code',), 'plate': ('plate',)},
    'device.search': {'code': ('code', 'device_code')},
    'inspection.search': {'id': ('id',)},
    'fee.search': {'id': ('id', 'fee_item_id')},
    'bill.search': {'bill_id': ('bill_id', 'id')},
    'payment.search': {'id': ('id', 'payment_id'), 'bill_id': ('bill_id',)},
    'billing.unpaid': {'bill_id': ('bill_id',)},
}

_RESOLVED_SINGLE_TARGET_INTENTS = {
    'order.accept', 'order.progress', 'order.finish', 'order.reopen', 'order.close', 'order.cancel',
    'complaint.resolve', 'complaint.close',
    'visitor.checkin', 'visitor.checkout', 'visitor.cancel',
    'vehicle.archive', 'parking.release', 'device.archive', 'inspection.complete',
    'payment.reverse', 'bill.void',
}

_MULTI_RESOLVE_SPECS = {
    'visitor.create': ('person.search', 'house.search'),
    'order.assign': ('order.search', 'staff.search'),
    'complaint.assign': ('complaint.search', 'complaint_staff.search'),
    'inspection.create': ('device.search', 'inspection_staff.search'),
    'parking.assign': ('parking.search', 'vehicle.search'),
}

_RESOLVER_LABELS = {
    'person.search': '住户',
    'house.search': '房屋',
    'order.search': '工单',
    'staff.search': '维修人员',
    'complaint.search': '投诉记录',
    'complaint_staff.search': '投诉处理人员',
    'device.search': '设备',
    'inspection_staff.search': '巡检人员',
    'parking.search': '车位',
    'vehicle.search': '车辆',
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
    if not isinstance(fallback, dict):
        return False
    command = str(fallback.get('command') or '')
    if command in _READ_ONLY_INTENTS:
        return False
    return fallback.get('operation') in {'execute', 'propose'}


def _parse_call_args(call):
    try:
        function = call.get('function') or {}
        arguments = json.loads(function.get('arguments') or '{}')
        return arguments if isinstance(arguments, dict) else {}
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return {}


def _parse_call_command(call):
    return _parse_call_args(call).get('command')


def _planner_owned_read_params(command):
    hint = _PLANNER_HINT.get() or {}
    if hint.get('intent') != command:
        return None
    values = dict(hint.get('arguments') or {})
    spec = _PLANNER_OWNED_READ_FIELDS.get(command)
    if not spec:
        return None
    params = {}
    for output_key, source_keys in spec.items():
        for source_key in source_keys:
            value = values.get(source_key)
            if value not in (None, ''):
                params[output_key] = value
                break
    return params or None


def _normalize_read_call(call):
    outer = _parse_call_args(call)
    command = outer.get('command')
    if command not in _READ_ONLY_INTENTS:
        return call
    outer = dict(outer)
    outer['operation'] = 'lookup'
    owned = _planner_owned_read_params(command)
    if owned is not None:
        outer['arguments_json'] = json.dumps(owned, ensure_ascii=False)
    if command == 'parking_use.search':
        try:
            params = json.loads(outer.get('arguments_json') or '{}')
        except (TypeError, ValueError, json.JSONDecodeError):
            return call
        if not isinstance(params, dict):
            return call
        allowed = _RESOLVER_ARGUMENTS['parking_use.search']
        params = {key: value for key, value in params.items() if key in allowed and value not in (None, '')}
        outer['arguments_json'] = json.dumps(params, ensure_ascii=False)
    safe = dict(call) if isinstance(call, dict) else call
    if not isinstance(safe, dict):
        return call
    function = dict(safe.get('function') or {})
    function['arguments'] = json.dumps(outer, ensure_ascii=False)
    safe['function'] = function
    return safe


def _context_resolver_fallback():
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
    if command == 'device.search' and 'code' not in params and values.get('device_code'):
        params['code'] = values['device_code']
    if command == 'visitor.search' and 'name' not in params and values.get('visitor_name'):
        params['name'] = values['visitor_name']
    if command == 'parking.search' and 'space_code' not in params and values.get('space_code'):
        params['space_code'] = values['space_code']
    return {'operation': 'lookup', 'command': command, 'arguments_json': json.dumps(params, ensure_ascii=False)}


def _multi_resolver_fallback(command, resolved=None):
    hint = _PLANNER_HINT.get() or {}
    if hint.get('entity_status') != 'RESOLVE_MULTI':
        return None
    intent = hint.get('intent')
    values = dict(hint.get('arguments') or {})
    resolved = resolved or {}
    params = {}
    if intent == 'visitor.create':
        if command == 'person.search':
            if values.get('host_person_id'):
                params['id'] = values['host_person_id']
            elif values.get('person_name'):
                params['person_name'] = values['person_name']
            else:
                return None
        elif command == 'house.search':
            if values.get('house_id'):
                params['id'] = values['house_id']
            else:
                for key in ('building_name', 'unit', 'room_no'):
                    if values.get(key) not in (None, ''):
                        params[key] = values[key]
                if not params.get('building_name') or params.get('room_no') is None:
                    return None
        else:
            return None
    elif intent == 'order.assign':
        if command == 'order.search':
            if values.get('order_id'):
                params['id'] = values['order_id']
            elif values.get('order_no'):
                params['order_no'] = values['order_no']
            else:
                return None
        elif command == 'staff.search':
            repairer_name = values.get('repairer_name')
            if not repairer_name:
                return None
            params['staff_name'] = repairer_name
            order = resolved.get('order.search') or {}
            if order.get('community_id') is not None:
                params['community_id'] = order['community_id']
            if order.get('building_id') is not None:
                params['building_id'] = order['building_id']
        else:
            return None
    elif intent == 'complaint.assign':
        if command == 'complaint.search':
            complaint_id = values.get('complaint_id') or values.get('id')
            if complaint_id:
                params['id'] = complaint_id
        elif command == 'complaint_staff.search':
            assignee_name = values.get('assignee_name')
            if not assignee_name:
                return None
            params['staff_name'] = assignee_name
            complaint = resolved.get('complaint.search') or {}
            if complaint.get('community_id') is not None:
                params['community_id'] = complaint['community_id']
            if complaint.get('building_id') is not None:
                params['building_id'] = complaint['building_id']
        else:
            return None
    elif intent == 'inspection.create':
        if command == 'device.search':
            code = values.get('device_code') or values.get('code')
            if code:
                params['code'] = code
            else:
                return None
        elif command == 'inspection_staff.search':
            assignee_name = values.get('assignee_name')
            if not assignee_name:
                return None
            params['staff_name'] = assignee_name
            device = resolved.get('device.search') or {}
            if device.get('community_id') is not None:
                params['community_id'] = device['community_id']
            if device.get('building_id') is not None:
                params['building_id'] = device['building_id']
        else:
            return None
    elif intent == 'parking.assign':
        if command == 'parking.search':
            space_code = values.get('space_code')
            if not space_code:
                return None
            params['space_code'] = str(space_code).strip().upper()
        elif command == 'vehicle.search':
            plate = values.get('plate')
            if not plate:
                return None
            params['plate'] = str(plate).strip().upper().replace(' ', '')
        else:
            return None
    else:
        return None
    return {'operation': 'lookup', 'command': command, 'arguments_json': json.dumps(params, ensure_ascii=False)}


def _synthetic_call(args, call_id='planner-fallback'):
    return {
        'id': call_id,
        'type': 'function',
        'function': {'name': 'property_agent_tool', 'arguments': json.dumps(args, ensure_ascii=False)},
    }


def _resolver_items(result):
    if not isinstance(result, dict):
        return None
    data = result.get('data')
    if isinstance(data, dict) and isinstance(data.get('items'), list):
        return data['items']
    if isinstance(result.get('items'), list):
        return result['items']
    if isinstance(data, dict):
        nested = data.get('data')
        if isinstance(nested, dict) and isinstance(nested.get('items'), list):
            return nested['items']
    return None


def _narrow_resolver_result(result, item):
    envelope = dict(result) if isinstance(result, dict) else {}
    if isinstance(envelope.get('items'), list):
        envelope['items'] = [item]
        return envelope
    data = envelope.get('data')
    if isinstance(data, dict):
        data = dict(data)
        if isinstance(data.get('items'), list):
            data['items'] = [item]
            envelope['data'] = data
            return envelope
        nested = data.get('data')
        if isinstance(nested, dict) and isinstance(nested.get('items'), list):
            nested = dict(nested)
            nested['items'] = [item]
            data['data'] = nested
            envelope['data'] = data
    return envelope


def _resolver_terminal_result(result, code, message):
    envelope = dict(result) if isinstance(result, dict) else {}
    envelope.update({'ok': False, 'code': code, 'message': message, 'terminal': True})
    return envelope


def _resolved_write_fallback(query, item):
    hint = _PLANNER_HINT.get() or {}
    intent = hint.get('intent')
    if intent not in _RESOLVED_SINGLE_TARGET_INTENTS or not isinstance(item, dict):
        return None
    rid = item.get('id')
    version = item.get('version')
    if rid is None or version is None:
        return None
    params = {'id': rid, 'version': version}
    text = str(query or '').strip()
    if intent in {'order.progress', 'order.finish', 'order.reopen', 'order.close', 'order.cancel'}:
        params['remark'] = text
    elif intent in {'complaint.resolve', 'complaint.close'}:
        params['resolution'] = text
    elif intent == 'parking.release':
        reason = str((hint.get('arguments') or {}).get('reason') or '').strip()
        if not reason:
            return None
        params['reason'] = reason
    elif intent in {'vehicle.archive', 'device.archive', 'payment.reverse', 'bill.void'}:
        params['reason'] = text
    elif intent == 'inspection.complete':
        params['findings'] = text
        params['fault'] = '故障' in text or '异常' in text
    operation = 'propose' if str(hint.get('action') or '').upper() == 'CONFIRM' else 'execute'
    return {'operation': operation, 'command': intent, 'arguments_json': json.dumps(params, ensure_ascii=False)}


def _multi_resolved_write_fallback(resolved):
    hint = _PLANNER_HINT.get() or {}
    intent = hint.get('intent')
    values = dict(hint.get('arguments') or {})
    if intent == 'visitor.create':
        person = resolved.get('person.search') or {}
        house = resolved.get('house.search') or {}
        if person.get('id') is None or house.get('id') is None:
            return None
        required = ('visitor_name', 'phone', 'purpose', 'expected_at')
        if any(values.get(key) in (None, '') for key in required):
            return None
        params = {
            'house_id': house['id'],
            'host_person_id': person['id'],
            'name': values['visitor_name'],
            'phone': values['phone'],
            'purpose': values['purpose'],
            'expected_at': values['expected_at'],
        }
        return {'operation': 'execute', 'command': 'visitor.create', 'arguments_json': json.dumps(params, ensure_ascii=False)}
    if intent == 'order.assign':
        order = resolved.get('order.search') or {}
        repairer = resolved.get('staff.search') or {}
        if order.get('id') is None or order.get('version') is None or repairer.get('id') is None:
            return None
        params = {'id': order['id'], 'version': order['version'], 'repairer_id': repairer['id']}
        return {'operation': 'execute', 'command': 'order.assign', 'arguments_json': json.dumps(params, ensure_ascii=False)}
    if intent == 'complaint.assign':
        complaint = resolved.get('complaint.search') or {}
        handler = resolved.get('complaint_staff.search') or {}
        if complaint.get('id') is None or complaint.get('version') is None or handler.get('id') is None:
            return None
        params = {'id': complaint['id'], 'version': complaint['version'], 'assignee_id': handler['id']}
        return {'operation': 'execute', 'command': 'complaint.assign', 'arguments_json': json.dumps(params, ensure_ascii=False)}
    if intent == 'inspection.create':
        device = resolved.get('device.search') or {}
        inspector = resolved.get('inspection_staff.search') or {}
        if device.get('id') is None or inspector.get('id') is None:
            return None
        if not values.get('due_at') or not values.get('checklist'):
            return None
        params = {
            'device_id': device['id'],
            'assignee_id': inspector['id'],
            'due_at': values['due_at'],
            'checklist': values['checklist'],
        }
        return {'operation': 'execute', 'command': 'inspection.create', 'arguments_json': json.dumps(params, ensure_ascii=False)}
    if intent == 'parking.assign':
        space = resolved.get('parking.search') or {}
        vehicle = resolved.get('vehicle.search') or {}
        if space.get('id') is None or vehicle.get('id') is None:
            return None
        params = {'space_id': space['id'], 'vehicle_id': vehicle['id']}
        return {'operation': 'execute', 'command': 'parking.assign', 'arguments_json': json.dumps(params, ensure_ascii=False)}
    return None


def _call_matches_resolved_target(call, item):
    if not isinstance(item, dict) or item.get('id') is None:
        return False
    outer = _parse_call_args(call)
    try:
        params = json.loads(outer.get('arguments_json') or '{}')
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(params, dict):
        return False
    target = params.get('id')
    return target is not None and str(target) == str(item['id'])


def _chat_common(self, query, user, conversation_id, tool_callback, system_prompt, stream=False):
    messages = self._conversation_messages(query, user, conversation_id, system_prompt)
    hint = _PLANNER_HINT.get() or {}
    clarification = hint.get('clarification_text')
    if isinstance(clarification, str) and clarification.strip():
        answer = clarification.strip()
        cid = conversation_id or str(uuid.uuid4())
        messages.append({'role': 'assistant', 'content': answer})
        self._save_conversation(user, cid, messages)
        if stream:
            yield {'type': 'delta', 'content': answer}
            yield {'type': 'done', 'answer': answer, 'conversation_id': cid, 'execution_state': 'NOT_EXECUTED'}
            return
        return {'answer': answer, 'conversation_id': cid, 'execution_state': 'NOT_EXECUTED'}

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
    resolver_ready = False
    resolved_item = None
    tools = self._tool_definition()
    resolve_first = hint.get('entity_status') == 'RESOLVE_FIRST'
    resolve_multi = hint.get('entity_status') == 'RESOLVE_MULTI'
    resolve_strategy = (hint.get('arguments') or {}).get('_resolve_strategy')
    final_intent = hint.get('intent')
    multi_commands = list(_MULTI_RESOLVE_SPECS.get(final_intent, ())) if resolve_multi else []
    multi_index = 0
    multi_resolved = {}
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
        if allow_tools and provider_calls:
            provider_calls = [_normalize_read_call(call) for call in provider_calls]
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
                    messages.append({'role': 'tool', 'tool_call_id': str(call.get('id', '')), 'content': json.dumps({'ok': True, 'code': code, 'message': text, 'terminal': True}, ensure_ascii=False)})
                continue
            message = dict(message)
            message.pop('tool_calls', None)
            message['content'] = message.get('content') or '当前请求没有执行任何业务操作。'
            provider_calls = []

        if allow_tools and resolve_multi:
            provider_calls = []
        elif allow_tools and resolve_first and not resolver_ready and provider_calls:
            if final_intent == 'parking.release':
                provider_calls = []
            else:
                allowed_resolvers = {item for item in hint.get('candidates', ()) if item in _READ_ONLY_INTENTS}
                provider_calls = [call for call in provider_calls if _parse_call_command(call) in allowed_resolvers]
        elif allow_tools and resolve_first and resolver_ready and resolved_item and provider_calls:
            filtered = []
            for call in provider_calls:
                command = _parse_call_command(call)
                if command in _READ_ONLY_INTENTS:
                    continue
                if final_intent == 'parking.release':
                    continue
                if command == final_intent and _call_matches_resolved_target(call, resolved_item):
                    filtered.append(call)
            provider_calls = filtered

        calls = _core._planner_calls(message, provider_calls, force_final, False) if allow_tools else []
        if provider_calls and not calls:
            provider_calls = []
        if allow_tools and not provider_calls and not calls:
            if resolve_multi:
                if multi_index < len(multi_commands):
                    resolver = _multi_resolver_fallback(multi_commands[multi_index], multi_resolved)
                    if isinstance(resolver, dict):
                        calls = [_synthetic_call(resolver, f'planner-multi-resolver-{multi_index}')]
                elif multi_commands and len(multi_resolved) == len(multi_commands) and not planner_fallback_used:
                    resolved_fallback = _multi_resolved_write_fallback(multi_resolved)
                    if isinstance(resolved_fallback, dict):
                        calls = [_synthetic_call(resolved_fallback, 'planner-multi-write')]
                        planner_fallback_used = True
            elif resolve_first and resolver_ready and resolved_item and not planner_fallback_used:
                resolved_fallback = _resolved_write_fallback(query, resolved_item)
                if isinstance(resolved_fallback, dict):
                    calls = [_synthetic_call(resolved_fallback, 'planner-resolved-write')]
                    planner_fallback_used = True
            if not calls and not resolve_multi:
                fallback = hint.get('tool_call')
                if isinstance(fallback, dict) and not planner_fallback_used and (not resolve_first or resolver_ready):
                    calls = [_synthetic_call(fallback)]
                    planner_fallback_used = True
                elif resolve_first and not resolver_ready and not resolver_fallback_used:
                    resolver = _context_resolver_fallback()
                    if isinstance(resolver, dict):
                        calls = [_synthetic_call(resolver, 'planner-resolver')]
                        resolver_fallback_used = True
        if calls:
            calls = [_normalize_read_call(call) for call in calls]
        if not provider_calls and calls:
            message = dict(message)
            message['role'] = 'assistant'
            message['tool_calls'] = calls
            message['content'] = message.get('content') or None
        elif provider_calls:
            message = dict(message)
            message['tool_calls'] = provider_calls
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
                            if resolve_multi and command in multi_commands:
                                items = _resolver_items(result)
                                if items is not None:
                                    if len(items) == 1:
                                        multi_resolved[command] = items[0]
                                        result = _narrow_resolver_result(result, items[0])
                                        if multi_index < len(multi_commands) and command == multi_commands[multi_index]:
                                            multi_index += 1
                                    elif len(items) == 0:
                                        label = _RESOLVER_LABELS.get(command, '业务对象')
                                        result = _resolver_terminal_result(result, 'RESOURCE_NOT_FOUND', f'在当前权限范围内没有找到对应{label}，请核对业务信息后再继续。')
                                        force_final = True
                                    else:
                                        label = _RESOLVER_LABELS.get(command, '业务对象')
                                        result = _resolver_terminal_result(result, 'AMBIGUOUS_ENTITY', f'找到多个可能的{label}，请补充更多信息确认具体对象后再继续。')
                                        force_final = True
                            elif resolve_first and command in _READ_ONLY_INTENTS:
                                items = _resolver_items(result)
                                if items is not None:
                                    if resolve_strategy == 'latest' and command == 'payment.search' and items:
                                        resolved_item = items[0]
                                        result = _narrow_resolver_result(result, resolved_item)
                                        resolver_ready = True
                                    elif len(items) == 1:
                                        resolved_item = items[0]
                                        resolver_ready = True
                                    elif len(items) == 0:
                                        result = _resolver_terminal_result(result, 'RESOURCE_NOT_FOUND', '在当前权限范围内没有找到可继续操作的对象，请确认业务对象后再试。')
                                        force_final = True
                                    else:
                                        result = _resolver_terminal_result(result, 'AMBIGUOUS_ENTITY', '找到多个可能的业务对象，请根据返回的候选信息确认具体对象后再继续。')
                                        force_final = True
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
                        if result.get('error') or result.get('code') in {'MISSING_PARAMETER', 'AMBIGUOUS_ENTITY', 'PERMISSION_DENIED', 'DATA_SCOPE_DENIED', 'RESOURCE_NOT_FOUND', 'BUSINESS_CONFLICT', 'VALIDATION_ERROR', 'SYSTEM_ERROR', 'PLANNER_BLOCKED'}:
                            force_final = True
                except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
                    result = {'ok': False, 'code': 'VALIDATION_ERROR', 'message': '工具调用参数无效', 'terminal': True}
                    force_final = True
                messages.append({'role': 'tool', 'tool_call_id': str(call.get('id', '')), 'content': json.dumps(result, ensure_ascii=False, default=str)})
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
