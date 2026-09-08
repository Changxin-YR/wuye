"""Semantic compatibility layer around the stateful deterministic planner.

The state machine and frozen planner remain in ``agent_planner_state`` and
``agent_planner_core``. This layer repairs broad Chinese business phrases and
turns safe contextual references ("刚才那个", "上一笔") into scoped resolver
workflows. It never grants permissions or bypasses backend Policy/DataScope.
"""
import re

from agent_planner_state import *
from agent_planner_state import clear_pending_plan, plan_request as _state_plan_request
from agent_planner_core import CONFIRM_INTENTS, _candidates


_CONTEXT_WORDS = re.compile(r'刚才|刚刚|这个|这个人|这个房|这个工单|这张单|上一笔|上一个|今天的|当前的|已经离开|可以进|进去了|处理结果|结案|返修|撤回')
_CONTEXT_RESOLVERS = {
    'order.assign': ('order.search', 'person.search'),
    'order.accept': ('order.search',),
    'order.progress': ('order.search',),
    'order.finish': ('order.search',),
    'order.reopen': ('order.search',),
    'order.close': ('order.search',),
    'order.cancel': ('order.search',),
    'complaint.assign': ('complaint.search', 'person.search'),
    'complaint.resolve': ('complaint.search',),
    'complaint.close': ('complaint.search',),
    'visitor.checkin': ('visitor.search',),
    'visitor.checkout': ('visitor.search',),
    'visitor.cancel': ('visitor.search',),
    'vehicle.archive': ('vehicle.search',),
    'parking.release': ('parking.search', 'vehicle.search'),
    'device.archive': ('device.search',),
    'inspection.complete': ('inspection.search', 'device.search'),
    'payment.reverse': ('payment.search',),
    'bill.void': ('billing.unpaid',),
}


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


def _repair_order_cancel(text, result, authorized_commands):
    if not re.search(r'(?:撤销|取消).*(?:报修|工单)|(?:报修|工单).*(?:撤销|取消)', text):
        return result
    authorized = set(authorized_commands or ())
    if 'order.cancel' not in authorized:
        return result
    resolver = 'order.search' if 'order.search' in authorized else None
    arguments = dict(result.get('arguments') or {})
    if not arguments.get('order_no') and not resolver:
        return {
            'action': 'CLARIFY', 'intent': 'order.cancel', 'candidates': ['order.cancel'],
            'missing_fields': ['order'], 'entity_status': 'MISSING', 'arguments': arguments,
        }
    candidates = ([resolver] if resolver else []) + ['order.cancel']
    return {
        'action': 'CONFIRM',
        'intent': 'order.cancel',
        'candidates': candidates,
        'missing_fields': [],
        'entity_status': 'RESOLVED' if arguments.get('order_no') else 'RESOLVE_FIRST',
        'arguments': arguments,
    }


def _smooth_context_resolution(text, result, authorized_commands):
    if result.get('action') != 'CLARIFY':
        return result
    intent = result.get('intent')
    resolvers = _CONTEXT_RESOLVERS.get(intent)
    if not resolvers or not _CONTEXT_WORDS.search(text):
        return result
    authorized = set(authorized_commands or ())
    resolver_candidates = [command for command in resolvers if command in authorized]
    # Smooth auto-resolution is allowed only when the actor can actually perform
    # at least one scoped read resolver and the final intent itself is authorized.
    if not resolver_candidates or intent not in authorized:
        return result
    candidates = resolver_candidates + [intent]
    arguments = dict(result.get('arguments') or {})
    if intent == 'visitor.checkin':
        arguments.setdefault('status', 'registered')
    elif intent == 'visitor.checkout':
        arguments.setdefault('status', 'inside')
    elif intent == 'visitor.cancel':
        arguments.setdefault('status', 'registered')
    elif intent == 'inspection.complete':
        arguments.setdefault('status', 'pending')
    elif intent == 'complaint.resolve':
        arguments.setdefault('status', 'open')
    elif intent == 'complaint.close':
        arguments.setdefault('status', 'resolved')
    action = 'CONFIRM' if intent in CONFIRM_INTENTS else 'TOOL'
    clear_pending_plan()
    return {
        'action': action,
        'intent': intent,
        'candidates': list(dict.fromkeys(candidates)),
        'missing_fields': [],
        'entity_status': 'RESOLVE_FIRST',
        'arguments': arguments,
    }


def plan_request(message, authorized_commands, context=None):
    text = str(message or '').strip()
    result = _state_plan_request(text, authorized_commands, context)

    if result.get('intent') == 'unknown' and re.search(r'发布.*(?:维修|报修).*工单|发布.*工单', text):
        result = _state_plan_request(re.sub(r'^发布', '创建', text, count=1), authorized_commands, context)

    result = _repair_notice_content(text, result)
    result = _repair_payment_target(text, result, authorized_commands)
    result = _repair_device_code(result)
    result = _repair_order_cancel(text, result, authorized_commands)
    result = _smooth_context_resolution(text, result, authorized_commands)
    return result
