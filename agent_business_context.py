"""Short-lived server-owned business-target memory for Agent follow-ups.

The cache stores only operator-visible selectors (business numbers, names,
plates, device codes and address facts). It never stores model-selected write
IDs or versions. A follow-up still goes through the normal read resolver and
Policy/DataScope before any mutation can happen.
"""
import re
import time
from threading import Lock

try:
    from flask import current_app, g, has_request_context, request
except Exception:  # pragma: no cover - planner unit tests can run without Flask
    current_app = None
    g = None
    request = None

    def has_request_context():
        return False

from agent_planner_core import CONFIRM_INTENTS


_TTL_SECONDS = 600
_LIMIT = 256
_CURRENT = {}
_LOCK = Lock()
_CONTEXT_RE = re.compile(
    r'刚才|刚刚|这个|这条|这位|这辆|这台|这张|这笔|该(?:投诉|访客|车辆|车位|租约|账单|收款|工单|设备|巡检)|'
    r'上一(?:条|张|笔|个)|刚登记|刚创建|刚办理|刚查(?:到|的)?'
)


def _identity_key():
    if not has_request_context():
        return None
    user = getattr(g, 'user', None)
    if user is None or getattr(user, 'id', None) is None:
        return None
    app_obj = current_app._get_current_object() if current_app is not None else None
    return id(app_obj), int(user.id), int(getattr(user, 'auth_version', 0) or 0)


def _conversation_id():
    if not has_request_context() or request is None:
        return None
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return None
    cid = payload.get('conversation_id')
    return cid if isinstance(cid, str) and cid else None


def _key():
    identity = _identity_key()
    return identity + (_conversation_id(),) if identity is not None else None


def _unbound_key():
    identity = _identity_key()
    return identity + (None,) if identity is not None else None


def _cleanup(now=None):
    now = time.monotonic() if now is None else now
    expired = [key for key, value in _CURRENT.items() if value.get('expires_at', 0) <= now]
    for key in expired:
        _CURRENT.pop(key, None)
    if len(_CURRENT) > _LIMIT:
        oldest = sorted(_CURRENT.items(), key=lambda item: item[1].get('created_at', 0))
        for key, _ in oldest[:len(_CURRENT) - _LIMIT]:
            _CURRENT.pop(key, None)


def _state(create=False):
    key = _key()
    if key is None:
        return None, None
    now = time.monotonic()
    with _LOCK:
        _cleanup(now)
        value = _CURRENT.get(key)
        if value is None and key[-1] is not None:
            unbound = _unbound_key()
            value = _CURRENT.pop(unbound, None)
            if value is not None:
                _CURRENT[key] = value
        if value is None and create:
            value = {'domains': {}, 'created_at': now, 'expires_at': now + _TTL_SECONDS}
            _CURRENT[key] = value
        elif value is not None:
            value['expires_at'] = now + _TTL_SECONDS
        return key, value


def _store(domain, selectors):
    clean = {
        key: value
        for key, value in (selectors or {}).items()
        if value not in (None, '') and not isinstance(value, bool)
    }
    if not clean:
        return
    key, value = _state(create=True)
    if key is None or value is None:
        return
    with _LOCK:
        value['domains'][domain] = clean


def _get(domain):
    _, value = _state(create=False)
    if not value:
        return {}
    selectors = value.get('domains', {}).get(domain)
    return dict(selectors) if isinstance(selectors, dict) else {}


def _domain_for_intent(intent):
    value = str(intent or '')
    if value == 'payment.record':
        return 'bill'
    if value.startswith('payment.'):
        return 'payment'
    if value.startswith('bill.') or value == 'billing.unpaid':
        return 'bill'
    for domain in (
        'complaint', 'visitor', 'vehicle', 'parking', 'lease', 'order',
        'house', 'person', 'device', 'inspection', 'notice',
    ):
        if value.startswith(domain + '.'):
            return domain
    return None


def _selectors(domain, values):
    values = dict(values or {})
    if domain == 'complaint':
        return {'complaint_id': values.get('complaint_id') or values.get('id')}
    if domain == 'visitor':
        return {
            'visitor_id': values.get('visitor_id') or values.get('id'),
            'visitor_name': values.get('visitor_name') or values.get('name'),
            'phone': values.get('phone'),
        }
    if domain == 'vehicle':
        return {'plate': values.get('plate')}
    if domain == 'parking':
        return {'space_code': values.get('space_code'), 'plate': values.get('plate')}
    if domain == 'lease':
        return {
            'person_name': values.get('person_name'),
            'phone': values.get('phone'),
            'building_name': values.get('building_name'),
            'unit': values.get('unit'),
            'room_no': values.get('room_no'),
        }
    if domain == 'bill':
        return {
            'bill_id': values.get('bill_id') or values.get('id'),
            'period': values.get('period') or values.get('month'),
        }
    if domain == 'payment':
        return {
            'payment_id': values.get('payment_id') or values.get('id'),
            'bill_id': values.get('bill_id'),
        }
    if domain == 'order':
        return {'order_no': values.get('order_no')}
    if domain == 'house':
        return {
            'building_name': values.get('building_name'),
            'unit': values.get('unit'),
            'room_no': values.get('room_no'),
        }
    if domain == 'person':
        return {'person_name': values.get('person_name'), 'phone': values.get('phone')}
    if domain == 'device':
        return {'device_code': values.get('device_code') or values.get('code')}
    if domain == 'inspection':
        return {
            'inspection_id': values.get('inspection_id') or values.get('id'),
            'device_code': values.get('device_code') or values.get('code'),
        }
    if domain == 'notice':
        return {'notice_id': values.get('notice_id') or values.get('id')}
    return {}


def _merge_cached(domain, values):
    cached = _get(domain)
    if not cached:
        return values, False
    merged = dict(values or {})
    changed = False
    for key, value in cached.items():
        if merged.get(key) in (None, '') and value not in (None, ''):
            merged[key] = value
            changed = True
    if domain == 'complaint' and merged.get('complaint_id') and not merged.get('id'):
        merged['id'] = merged['complaint_id']
        changed = True
    if domain == 'visitor' and merged.get('visitor_id') and not merged.get('id'):
        merged['id'] = merged['visitor_id']
        changed = True
    if domain == 'payment' and merged.get('payment_id') and not merged.get('id'):
        merged['id'] = merged['payment_id']
        changed = True
    if domain == 'inspection' and merged.get('inspection_id') and not merged.get('id'):
        merged['id'] = merged['inspection_id']
        changed = True
    return merged, changed


_TARGET_SLOT = {
    'complaint': 'complaint', 'visitor': 'visitor', 'vehicle': 'vehicle',
    'parking': 'parking_use', 'lease': 'lease', 'bill': 'bill',
    'payment': 'payment', 'order': 'order', 'house': 'house', 'person': 'person',
    'device': 'device', 'inspection': 'inspection', 'notice': 'notice',
}

_RESOLVER = {
    'complaint.resolve': 'complaint.search',
    'complaint.close': 'complaint.search',
    'visitor.checkin': 'visitor.search',
    'visitor.checkout': 'visitor.search',
    'visitor.cancel': 'visitor.search',
    'vehicle.archive': 'vehicle.search',
    'parking.release': 'parking_use.search',
    'device.archive': 'device.search',
    'inspection.complete': 'inspection.search',
    'order.assign': 'order.search',
    'order.accept': 'order.search',
    'order.progress': 'order.search',
    'order.finish': 'order.search',
    'order.reopen': 'order.search',
    'order.close': 'order.search',
    'order.cancel': 'order.search',
    'notice.archive': 'notice.read',
}


def _promote_context_target(result, domain, authorized_commands):
    missing = list(result.get('missing_fields') or [])
    slot = _TARGET_SLOT.get(domain)
    if slot not in missing:
        return result
    remaining = [item for item in missing if item != slot]
    repaired = dict(result)
    repaired['missing_fields'] = remaining
    if remaining:
        return repaired
    if result.get('action') not in {'CLARIFY', 'DISAMBIGUATE'}:
        return repaired
    intent = result.get('intent')
    resolver = _RESOLVER.get(intent)
    authorized = set(authorized_commands or ())
    if not resolver or resolver not in authorized or intent not in authorized:
        return repaired
    repaired['action'] = 'CONFIRM' if intent in CONFIRM_INTENTS else 'TOOL'
    repaired['entity_status'] = 'RESOLVE_FIRST'
    repaired['candidates'] = [resolver, intent]
    repaired.pop('clarification_text', None)
    return repaired


def repair_business_current_plan(text, result, authorized_commands):
    """Reuse a visible target only inside the same domain and conversation.

    Explicit facts in the new message always win. Cached facts only fill missing
    selectors for contextual phrases such as ``刚才那辆车`` or ``这条投诉``.
    Final object ids/versions are still resolved and bound by the backend.
    """
    if not isinstance(result, dict):
        return result
    intent = result.get('intent')
    if intent in {'unknown', 'security_boundary'} or result.get('action') == 'DENY':
        return result
    domain = _domain_for_intent(intent)
    if not domain:
        return result

    repaired = result
    if _CONTEXT_RE.search(str(text or '')):
        merged, changed = _merge_cached(domain, result.get('arguments') or {})
        if changed:
            repaired = dict(result)
            repaired['arguments'] = merged
            repaired = _promote_context_target(repaired, domain, authorized_commands)

    selectors = _selectors(domain, repaired.get('arguments') or {})
    if any(value not in (None, '') for value in selectors.values()):
        _store(domain, selectors)
    return repaired
