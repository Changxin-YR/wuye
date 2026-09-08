"""Semantic compatibility layer around the stateful deterministic planner.

The state machine and frozen planner remain in ``agent_planner_state`` and
``agent_planner_core``.  This layer only repairs broad Chinese business phrases
and identifier extraction; it never grants permissions or bypasses backend
Policy/DataScope checks.
"""
import re

from agent_planner_state import *
from agent_planner_state import clear_pending_plan, plan_request as _state_plan_request
from agent_planner_core import _candidates


def _repair_notice_content(text, result):
    if result.get('intent') != 'notice.save':
        return result
    arguments = dict(result.get('arguments') or {})
    content = arguments.get('notice_content')
    if isinstance(content, str):
        cleaned = re.sub(
            r'^给\s*[A-Za-z0-9一二三四五六七八九十百]+\s*(?:栋|号楼)\s*发(?:个|一条)?\s*',
            '', content,
        ).strip(' ，,。；;')
        if cleaned:
            arguments['notice_content'] = cleaned
            result = dict(result)
            result['arguments'] = arguments
    return result


def _repair_payment_target(text, result, authorized_commands):
    if result.get('intent') != 'payment.reverse' or result.get('action') != 'CLARIFY':
        return result
    match = re.search(r'(?:冲销|冲正|撤回).*?收款(?:记录)?\s*#?\s*([1-9]\d{0,8})', text)
    if not match:
        match = re.search(r'收款(?:记录)?\s*#?\s*([1-9]\d{0,8})', text)
    if not match:
        return result
    payment_id = int(match.group(1))
    arguments = dict(result.get('arguments') or {})
    arguments['payment_id'] = payment_id
    arguments['id'] = payment_id
    candidates = _candidates('payment.reverse', set(authorized_commands or ()))
    clear_pending_plan()
    return {
        'action': 'CONFIRM',
        'intent': 'payment.reverse',
        'candidates': candidates,
        'missing_fields': [],
        'entity_status': 'RESOLVED',
        'arguments': arguments,
    }


def _repair_device_code(result):
    """Keep device identifiers explicit and compatible with the current gateway.

    ``app.py`` still has a conservative legacy builder that reads ``space_code``
    for ``device.save``.  Preserve the canonical device fields and mirror the
    value only for that builder; backend command normalization still rejects a
    device id that does not resolve inside the actor's DataScope.
    """
    if result.get('intent') not in {'device.save', 'device.search', 'device.archive', 'inspection.create'}:
        return result
    arguments = dict(result.get('arguments') or {})
    code = arguments.get('device_code') or arguments.get('code')
    if code:
        arguments['device_code'] = str(code).upper()
        arguments['code'] = str(code).upper()
        if result.get('intent') == 'device.save':
            arguments.setdefault('space_code', str(code).upper())
        result = dict(result)
        result['arguments'] = arguments
    return result


def plan_request(message, authorized_commands, context=None):
    text = str(message or '').strip()
    result = _state_plan_request(text, authorized_commands, context)

    # “发布一个维修工单” is a create-order phrase, not a public announcement.
    if result.get('intent') == 'unknown' and re.search(r'发布.*(?:维修|报修).*工单|发布.*工单', text):
        result = _state_plan_request(re.sub(r'^发布', '创建', text, count=1), authorized_commands, context)

    result = _repair_notice_content(text, result)
    result = _repair_payment_target(text, result, authorized_commands)
    result = _repair_device_code(result)
    return result
