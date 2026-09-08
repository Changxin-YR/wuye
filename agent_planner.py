"""Stateful wrapper for the deterministic planner.

It keeps only a short-lived, server-memory pending plan per authenticated actor so
follow-up slot answers (for example a community name after a clarification) can
continue the original business intent. Authorization is still enforced later by
Policy/AgentToolGateway/PropertyService.
"""
import re
import time
from threading import Lock

try:
    from flask import g, has_request_context
except Exception:  # pragma: no cover - planner unit tests can run without Flask context
    g = None
    def has_request_context():
        return False

from agent_planner_core import *  # re-export the stable public planner surface
from agent_planner_core import (
    CONFIRM_INTENTS,
    _candidates,
    _detect_intent,
    _entities,
    _notice_entities,
    plan_request as _core_plan_request,
)

_PENDING_TTL_SECONDS = 600
_PENDING_LIMIT = 128
_PENDING = {}
_PENDING_LOCK = Lock()


def _actor_key():
    if not has_request_context():
        return None
    user = getattr(g, 'user', None)
    if user is None or getattr(user, 'id', None) is None:
        return None
    return int(user.id), int(getattr(user, 'auth_version', 0) or 0)


def _cleanup_pending(now=None):
    now = time.monotonic() if now is None else now
    expired = [key for key, value in _PENDING.items() if value.get('expires_at', 0) <= now]
    for key in expired:
        _PENDING.pop(key, None)
    if len(_PENDING) > _PENDING_LIMIT:
        oldest = sorted(_PENDING.items(), key=lambda item: item[1].get('created_at', 0))
        for key, _ in oldest[:len(_PENDING) - _PENDING_LIMIT]:
            _PENDING.pop(key, None)


def clear_pending_plan():
    key = _actor_key()
    if key is None:
        return
    with _PENDING_LOCK:
        _PENDING.pop(key, None)


def _get_pending():
    key = _actor_key()
    if key is None:
        return None
    now = time.monotonic()
    with _PENDING_LOCK:
        _cleanup_pending(now)
        value = _PENDING.get(key)
        return dict(value) if value else None


def _store_pending(result):
    key = _actor_key()
    if key is None:
        return
    action = result.get('action')
    with _PENDING_LOCK:
        _cleanup_pending()
        if action not in {'CLARIFY', 'DISAMBIGUATE'}:
            _PENDING.pop(key, None)
            return
        missing = list(result.get('missing_fields') or [])
        if action == 'DISAMBIGUATE' and not missing:
            missing = ['disambiguation']
        now = time.monotonic()
        _PENDING[key] = {
            'intent': result.get('intent'),
            'candidates': list(result.get('candidates') or []),
            'missing_fields': missing,
            'entity_status': result.get('entity_status'),
            'arguments': dict(result.get('arguments') or {}),
            'created_at': now,
            'expires_at': now + _PENDING_TTL_SECONDS,
        }


def _looks_like_continuation(text, pending):
    detected = _detect_intent(text)
    if not detected or detected == pending.get('intent'):
        return True
    missing = set(pending.get('missing_fields') or [])
    checks = {
        'community_id': r'小区|社区|花园|园区',
        'house': r'栋|号楼|单元|室|房',
        'order': r'WO[-A-Za-z0-9]+|工单|维修单',
        'repairer': r'师傅|维修|工程|[\u4e00-\u9fff]{2,8}',
        'person': r'手机号|电话|[\u4e00-\u9fff]{2,4}',
        'phone': r'1[3-9]\d{9}',
        'notice': r'公告|通知|\d+',
        'complaint': r'投诉|\d+',
        'visitor': r'访客|来访|\d+',
        'device': r'设备|[A-Za-z]+-\d+',
        'payment': r'收款|付款|\d+',
        'bill': r'账单|\d+',
        'parking_use': r'车位|车牌|[A-Za-z]+-\d+',
        'disambiguation': r'手机号|电话|\d+|[A-Za-z]+-\d+',
    }
    return any(slot in missing and re.search(pattern, text, re.I) for slot, pattern in checks.items())


def _slot_keys(slot):
    mapping = {
        'community_id': {'community_id', 'community_name'},
        'notice_title': {'notice_title'},
        'notice_content': {'notice_content'},
        'building': {'building_name', 'building_id'},
        'house': {'building_name', 'unit', 'room_no', 'house_id'},
        'order': {'order_no', 'id'},
        'repairer': {'repairer_name', 'person_name', 'phone'},
        'person': {'person_name', 'phone', 'person_id', 'id'},
        'phone': {'phone'},
        'notice': {'notice_id', 'id'},
        'complaint': {'complaint_id', 'id'},
        'visitor': {'visitor_id', 'visitor_name', 'id'},
        'device': {'device_code', 'code', 'id'},
        'assignee': {'repairer_name', 'person_name', 'phone', 'assignee_id'},
        'payment': {'payment_id', 'id'},
        'bill': {'bill_id', 'id'},
        'parking_use': {'space_code', 'plate', 'id'},
        'id': {'id'},
        'version': {'version'},
        'disambiguation': {'id', 'phone', 'order_no', 'notice_id', 'bill_id', 'device_code', 'space_code', 'plate'},
    }
    return mapping.get(slot, {slot})


def _extract_followup(intent, text):
    values = _entities(text)
    if intent in {'notice.save', 'notice.batch_publish'}:
        values.update(_notice_entities(text))
    for label, key in (
        ('投诉', 'complaint_id'), ('访客', 'visitor_id'), ('收款', 'payment_id'),
    ):
        matched = re.search(label + r'(?:单|记录)?\s*#?\s*([1-9]\d{0,8})', text)
        if matched:
            values[key] = int(matched.group(1))
            values.setdefault('id', int(matched.group(1)))
    generic = re.fullmatch(r'\s*(?:ID\s*)?#?([1-9]\d{0,8})\s*', text, re.I)
    if generic:
        values['id'] = int(generic.group(1))
    return values


def _merge_pending(pending, text, context):
    intent = pending.get('intent')
    merged = dict(pending.get('arguments') or {})
    extracted = _extract_followup(intent, text)
    missing = list(pending.get('missing_fields') or [])
    allowed = set()
    for slot in missing:
        allowed.update(_slot_keys(slot))
    # Identifiers are safe to carry even when the original missing label was broad.
    allowed.update({'community_name', 'building_name', 'unit', 'room_no', 'order_no', 'phone', 'plate', 'space_code', 'device_code', 'code'})
    for key, value in extracted.items():
        if key in allowed and value not in (None, ''):
            merged[key] = value

    writable = context.get('writable_communities')
    if 'community_id' in missing and not merged.get('community_id') and merged.get('community_name') and isinstance(writable, list):
        matches = [row for row in writable if isinstance(row, dict) and row.get('name') == merged['community_name']]
        if len(matches) == 1:
            merged['community_id'] = matches[0].get('id')

    def satisfied(slot):
        if slot == 'community_id':
            return bool(merged.get('community_id'))
        if slot in {'notice_title', 'notice_content', 'phone', 'version', 'id'}:
            return merged.get(slot) not in (None, '')
        if slot == 'building':
            return bool(merged.get('building_id') or merged.get('building_name'))
        if slot == 'house':
            return bool(context.get('resolved_house') or merged.get('house_id') or (merged.get('building_name') and merged.get('room_no') is not None))
        if slot == 'order':
            return bool(context.get('resolved_order') or merged.get('order_no') or merged.get('id'))
        if slot == 'repairer':
            return bool(merged.get('repairer_name') or merged.get('person_name') or merged.get('phone'))
        if slot == 'person':
            return bool(context.get('resolved_person') or merged.get('person_name') or merged.get('phone') or merged.get('person_id') or merged.get('id'))
        if slot == 'notice':
            return bool(context.get('resolved_notice') or merged.get('notice_id') or merged.get('id'))
        if slot == 'complaint':
            return bool(context.get('resolved_complaint') or merged.get('complaint_id') or merged.get('id'))
        if slot == 'visitor':
            return bool(context.get('resolved_visitor') or merged.get('visitor_id') or merged.get('visitor_name') or merged.get('id'))
        if slot == 'device':
            return bool(context.get('resolved_device') or merged.get('device_code') or merged.get('code') or merged.get('id'))
        if slot == 'assignee':
            return bool(merged.get('assignee_id') or merged.get('repairer_name') or merged.get('person_name') or merged.get('phone'))
        if slot == 'payment':
            return bool(context.get('resolved_payment') or merged.get('payment_id') or merged.get('id'))
        if slot == 'bill':
            return bool(merged.get('bill_id') or merged.get('id'))
        if slot == 'parking_use':
            return bool(context.get('resolved_parking_use') or merged.get('id') or merged.get('space_code') or merged.get('plate'))
        if slot == 'disambiguation':
            return any(merged.get(key) not in (None, '') for key in _slot_keys(slot))
        return merged.get(slot) not in (None, '')

    remaining = [slot for slot in missing if not satisfied(slot)]
    if remaining:
        return {
            'action': 'DISAMBIGUATE' if pending.get('entity_status') == 'AMBIGUOUS' else 'CLARIFY',
            'intent': intent,
            'candidates': list(pending.get('candidates') or _candidates(intent, set())),
            'missing_fields': remaining,
            'entity_status': pending.get('entity_status') or 'MISSING',
            'arguments': merged,
        }
    return {
        'action': 'CONFIRM' if intent in CONFIRM_INTENTS else 'TOOL',
        'intent': intent,
        'candidates': list(pending.get('candidates') or []),
        'missing_fields': [],
        'entity_status': 'RESOLVED',
        'arguments': merged,
    }


def plan_request(message, authorized_commands, context=None):
    context = context or {}
    text = str(message or '').strip()
    pending = _get_pending()
    detected = _detect_intent(text)
    if pending and _looks_like_continuation(text, pending):
        result = _merge_pending(pending, text, context)
        # Never revive an intent that is no longer represented by an authorized
        # candidate. Backend authorization remains authoritative as a second gate.
        authorized = set(authorized_commands or ())
        candidates = [item for item in result.get('candidates', []) if item in authorized]
        if result.get('intent') in authorized and result.get('intent') not in candidates:
            candidates.append(result['intent'])
        result['candidates'] = candidates
        if not candidates:
            result = {'action': 'DENY', 'intent': result.get('intent'), 'candidates': [], 'missing_fields': [], 'entity_status': 'FORBIDDEN', 'arguments': result.get('arguments', {})}
        _store_pending(result)
        return result
    if pending and detected and detected != pending.get('intent'):
        clear_pending_plan()
    result = _core_plan_request(text, authorized_commands, context)
    _store_pending(result)
    return result
