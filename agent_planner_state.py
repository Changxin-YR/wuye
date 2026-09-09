"""Stateful wrapper for the deterministic planner.

A pending plan is short-lived server-side state used only to continue a previously
clarified business intent.  It never grants authority: RBAC, DataScope, target
versions and risk checks are still enforced by the backend gateway and services.
"""
import re
import time
from datetime import datetime, timedelta, timezone
from threading import Lock

try:
    from flask import g, has_request_context, request
except Exception:  # pragma: no cover - planner unit tests can run without Flask context
    g = None
    request = None
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
_PENDING_LIMIT = 256
_PENDING = {}
_PENDING_LOCK = Lock()
_CANCEL_RE = re.compile(r'^(?:算了|取消(?:这个|本次)?(?:操作|请求)?|不用了|不做了|先不弄了|结束(?:这个|本次)?操作)[。！!\s]*$')
_CN_HOUR = {'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10,'十一':11,'十二':12}


def visitor_expected_at(text):
    """Parse an explicit operator-supplied arrival time into China-local ISO.

    We only infer relative dates when the user says 今天/明天/后天. A bare
    "下午两点" is intentionally left unresolved because silently choosing a date
    could register a visitor for the wrong day.
    """
    value = str(text or '').strip()
    explicit = re.search(r'(20\d{2})[-/](\d{1,2})[-/](\d{1,2})[ T]([01]?\d|2[0-3])[:：]([0-5]\d)', value)
    if explicit:
        try:
            dt = datetime(int(explicit.group(1)), int(explicit.group(2)), int(explicit.group(3)), int(explicit.group(4)), int(explicit.group(5)))
            return dt.isoformat(timespec='minutes')
        except ValueError:
            return None
    day_offset = 0 if '今天' in value else (1 if '明天' in value else (2 if '后天' in value else None))
    if day_offset is None:
        return None
    matched = re.search(r'(上午|早上|中午|下午|晚上)?\s*([0-9]{1,2}|十二|十一|十|[一二两三四五六七八九])\s*[点时](半|\d{1,2}分?)?', value)
    if not matched:
        clock = re.search(r'([01]?\d|2[0-3])[:：]([0-5]\d)', value)
        if not clock:
            return None
        hour, minute = int(clock.group(1)), int(clock.group(2))
    else:
        token = matched.group(2)
        hour = int(token) if token.isdigit() else _CN_HOUR.get(token)
        if hour is None or hour > 23:
            return None
        period = matched.group(1) or ''
        if period in {'下午','晚上'} and hour < 12:
            hour += 12
        elif period == '中午' and hour < 11:
            hour += 12
        suffix = matched.group(3) or ''
        minute = 30 if suffix == '半' else int(re.sub(r'分$', '', suffix) or 0)
        if minute > 59:
            return None
    local_today = (datetime.now(timezone.utc) + timedelta(hours=8)).date()
    target = local_today + timedelta(days=day_offset)
    return datetime(target.year, target.month, target.day, hour, minute).isoformat(timespec='minutes')


def visitor_purpose(text, host_name=None):
    value = str(text or '').strip()
    explicit = re.search(r'(?:来访目的|目的)(?:是|为|：|:)?\s*([^，,。；;]{1,80})', value)
    if explicit:
        return explicit.group(1).strip()
    keyword = re.search(r'送快递|送货|送餐|看房|维修|保洁|探访|拜访|看望', value)
    if keyword:
        return keyword.group(0)
    if host_name:
        return '拜访' + str(host_name)
    return None


def _identity_key():
    if not has_request_context():
        return None
    user = getattr(g, 'user', None)
    if user is None or getattr(user, 'id', None) is None:
        return None
    return int(user.id), int(getattr(user, 'auth_version', 0) or 0)


def _request_conversation_id():
    if not has_request_context() or request is None:
        return None
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return None
    cid = payload.get('conversation_id')
    return cid if isinstance(cid, str) and cid else None


def _actor_key():
    identity = _identity_key()
    if identity is None:
        return None
    return identity + (_request_conversation_id(),)


def _unbound_key():
    identity = _identity_key()
    return identity + (None,) if identity is not None else None


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
        _cleanup_pending()
        _PENDING.pop(key, None)
        if key[-1] is not None:
            _PENDING.pop(_unbound_key(), None)


def _get_pending():
    key = _actor_key()
    if key is None:
        return None
    now = time.monotonic()
    with _PENDING_LOCK:
        _cleanup_pending(now)
        value = _PENDING.get(key)
        if value:
            return dict(value)
        if key[-1] is not None:
            unbound = _unbound_key()
            value = _PENDING.pop(unbound, None)
            if value:
                _PENDING[key] = value
                return dict(value)
        return None


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
        'building': r'栋|号楼',
        'house': r'栋|号楼|单元|室|房',
        'order': r'WO[-A-Za-z0-9]+|工单|维修单',
        'repairer': r'师傅|维修|工程|[\u4e00-\u9fff]{2,8}',
        'assignee': r'师傅|维修|工程|客服|[\u4e00-\u9fff]{2,8}',
        'person': r'手机号|电话|[\u4e00-\u9fff]{2,4}',
        'host_person': r'手机号|电话|[\u4e00-\u9fff]{2,4}',
        'phone': r'1[3-9]\d{9}',
        'expected_at': r'今天|明天|后天|上午|早上|中午|下午|晚上|\d{1,2}[:：]\d{2}|[点时]',
        'purpose': r'目的|送快递|送货|送餐|看房|维修|保洁|探访|拜访|看望',
        'notice': r'公告|通知|\d+',
        'complaint': r'投诉|\d+',
        'visitor': r'访客|来访|客人|\d+|[\u4e00-\u9fff]{2,4}',
        'vehicle': r'车牌|车辆|[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼][A-Z][A-Z0-9]{5}',
        'space': r'车位|停车位|[A-Za-z]+-\d+',
        'parking_use': r'车位|车牌|[A-Za-z]+-\d+',
        'device': r'设备|[A-Za-z]+-\d+',
        'inspection': r'巡检|\d+',
        'payment': r'收款|付款|\d+',
        'bill': r'账单|\d+',
        'fee': r'收费|物业费|\d+',
        'new_request_details': r'栋|室|房|漏水|坏|故障|维修|报修',
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
        'repairer': {'repairer_name', 'person_name', 'phone', 'repairer_id'},
        'assignee': {'assignee_id', 'repairer_name', 'person_name', 'phone'},
        'person': {'person_name', 'phone', 'person_id', 'id'},
        'host_person': {'person_name', 'host_person_id'},
        'phone': {'phone'},
        'expected_at': {'expected_at'},
        'purpose': {'purpose'},
        'notice': {'notice_id', 'id'},
        'complaint': {'complaint_id', 'id'},
        'visitor': {'visitor_id', 'visitor_name', 'id'},
        'vehicle': {'plate', 'vehicle_id', 'id'},
        'space': {'space_code', 'space_id', 'id'},
        'device': {'device_code', 'code', 'device_id', 'id'},
        'inspection': {'inspection_id', 'id'},
        'payment': {'payment_id', 'id'},
        'bill': {'bill_id', 'id'},
        'fee': {'fee_item_id', 'id'},
        'parking_use': {'space_code', 'plate', 'parking_use_id', 'id'},
        'new_request_details': {'request_details', 'title', 'content', 'building_name', 'unit', 'room_no'},
        'id': {'id'},
        'version': {'version'},
        'disambiguation': {'id', 'phone', 'order_no', 'notice_id', 'bill_id', 'device_code', 'space_code', 'plate'},
    }
    return mapping.get(slot, {slot})


def _plain_name(text, max_len=8):
    matched = re.fullmatch(r'\s*([\u4e00-\u9fff]{2,%d})\s*' % max_len, text)
    return matched.group(1) if matched else None


def _extract_followup(intent, text, missing=None):
    values = _entities(text)
    missing = set(missing or ())
    if intent in {'notice.save', 'notice.batch_publish'}:
        values.update(_notice_entities(text))
    if intent == 'visitor.create':
        expected = visitor_expected_at(text)
        if expected:
            values['expected_at'] = expected
        purpose = visitor_purpose(text, values.get('person_name'))
        if purpose:
            values['purpose'] = purpose
    for label, key in (
        ('投诉', 'complaint_id'), ('访客', 'visitor_id'), ('巡检', 'inspection_id'),
        ('收款', 'payment_id'), ('账单', 'bill_id'),
    ):
        matched = re.search(label + r'(?:单|记录|任务)?\s*#?\s*([1-9]\d{0,8})', text)
        if matched:
            values[key] = int(matched.group(1))
            values.setdefault('id', int(matched.group(1)))
    generic = re.fullmatch(r'\s*(?:ID\s*)?#?([1-9]\d{0,8})\s*', text, re.I)
    if generic:
        values['id'] = int(generic.group(1))
    name = _plain_name(text)
    if name:
        if 'repairer' in missing:
            values['repairer_name'] = name
        elif 'assignee' in missing:
            values['repairer_name'] = name
        elif 'visitor' in missing:
            values['visitor_name'] = name
        elif 'person' in missing or 'host_person' in missing:
            values['person_name'] = name
    if 'community_id' in missing and re.fullmatch(r'\s*[\u4e00-\u9fffA-Za-z0-9]{1,20}(?:小区|花园|社区|园区)\s*', text):
        values['community_name'] = text.strip()
    if 'purpose' in missing and not values.get('purpose'):
        purpose = visitor_purpose(text)
        if purpose:
            values['purpose'] = purpose
    if 'expected_at' in missing and not values.get('expected_at'):
        expected = visitor_expected_at(text)
        if expected:
            values['expected_at'] = expected
    if 'new_request_details' in missing and text.strip():
        values['request_details'] = text.strip()
        values.setdefault('content', text.strip())
        values.setdefault('title', text.strip()[:100])
    return values


def _merge_pending(pending, text, context):
    intent = pending.get('intent')
    merged = dict(pending.get('arguments') or {})
    missing = list(pending.get('missing_fields') or [])
    extracted = _extract_followup(intent, text, missing)
    allowed = set()
    for slot in missing:
        allowed.update(_slot_keys(slot))
    allowed.update({
        'community_name', 'building_name', 'unit', 'room_no', 'order_no', 'phone',
        'plate', 'space_code', 'device_code', 'code', 'request_details', 'expected_at', 'purpose',
    })
    for key, value in extracted.items():
        if key in allowed and value not in (None, ''):
            merged[key] = value
    if intent == 'visitor.create' and not merged.get('purpose') and merged.get('person_name'):
        merged['purpose'] = visitor_purpose('', merged.get('person_name'))

    writable = context.get('writable_communities')
    if 'community_id' in missing and not merged.get('community_id') and merged.get('community_name') and isinstance(writable, list):
        matches = [row for row in writable if isinstance(row, dict) and row.get('name') == merged['community_name']]
        if len(matches) == 1:
            merged['community_id'] = matches[0].get('id')

    def satisfied(slot):
        if slot == 'community_id':
            return bool(merged.get('community_id'))
        if slot in {'notice_title', 'notice_content', 'phone', 'expected_at', 'purpose', 'version', 'id'}:
            return merged.get(slot) not in (None, '')
        if slot == 'building':
            return bool(merged.get('building_id') or merged.get('building_name'))
        if slot == 'house':
            return bool(context.get('resolved_house') or merged.get('house_id') or (merged.get('building_name') and merged.get('room_no') is not None))
        if slot == 'order':
            return bool(context.get('resolved_order') or merged.get('order_no') or merged.get('id'))
        if slot == 'repairer':
            return bool(merged.get('repairer_id') or merged.get('repairer_name') or merged.get('person_name') or merged.get('phone'))
        if slot == 'assignee':
            return bool(merged.get('assignee_id') or merged.get('repairer_name') or merged.get('person_name') or merged.get('phone'))
        if slot == 'person':
            return bool(context.get('resolved_person') or merged.get('person_name') or merged.get('phone') or merged.get('person_id') or merged.get('id'))
        if slot == 'host_person':
            return bool(context.get('resolved_person') or merged.get('host_person_id') or merged.get('person_name'))
        if slot == 'notice':
            return bool(context.get('resolved_notice') or merged.get('notice_id') or merged.get('id'))
        if slot == 'complaint':
            return bool(context.get('resolved_complaint') or merged.get('complaint_id') or merged.get('id'))
        if slot == 'visitor':
            return bool(context.get('resolved_visitor') or merged.get('visitor_id') or merged.get('visitor_name') or merged.get('id'))
        if slot == 'vehicle':
            return bool(context.get('resolved_vehicle') or merged.get('vehicle_id') or merged.get('plate') or merged.get('id'))
        if slot == 'space':
            return bool(context.get('resolved_parking_space') or merged.get('space_id') or merged.get('space_code') or merged.get('id'))
        if slot == 'parking_use':
            return bool(context.get('resolved_parking_use') or merged.get('parking_use_id') or merged.get('id') or (merged.get('space_code') and merged.get('plate')))
        if slot == 'device':
            return bool(context.get('resolved_device') or merged.get('device_id') or merged.get('device_code') or merged.get('code') or merged.get('id'))
        if slot == 'inspection':
            return bool(context.get('resolved_inspection') or context.get('inspection_id') or merged.get('inspection_id') or merged.get('id'))
        if slot == 'payment':
            return bool(context.get('resolved_payment') or merged.get('payment_id') or merged.get('id'))
        if slot == 'bill':
            return bool(context.get('resolved_bill') or merged.get('bill_id') or merged.get('id'))
        if slot == 'fee':
            return bool(context.get('resolved_fee') or merged.get('fee_item_id') or merged.get('id'))
        if slot == 'new_request_details':
            return bool(merged.get('request_details') or merged.get('content'))
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
    if _CANCEL_RE.fullmatch(text):
        clear_pending_plan()
        return {
            'action': 'ANSWER', 'intent': 'cancelled', 'candidates': [],
            'missing_fields': [], 'entity_status': 'NONE', 'arguments': {},
        }
    pending = _get_pending()
    if pending is None and isinstance(context.get('pending_plan'), dict):
        pending = dict(context['pending_plan'])
    detected = _detect_intent(text)
    if pending and _looks_like_continuation(text, pending):
        result = _merge_pending(pending, text, context)
        authorized = set(authorized_commands or ())
        candidates = [item for item in result.get('candidates', []) if item in authorized]
        if result.get('intent') in authorized and result.get('intent') not in candidates:
            candidates.append(result['intent'])
        result['candidates'] = candidates
        if not candidates:
            result = {
                'action': 'DENY', 'intent': result.get('intent'), 'candidates': [],
                'missing_fields': [], 'entity_status': 'FORBIDDEN',
                'arguments': result.get('arguments', {}),
            }
        _store_pending(result)
        return result
    if pending and detected and detected != pending.get('intent'):
        clear_pending_plan()
    result = _core_plan_request(text, authorized_commands, context)
    _store_pending(result)
    return result
