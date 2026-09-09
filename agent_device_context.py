"""Short-lived server-owned current-device context for Agent follow-ups.

Only a user-visible device code is remembered. No ORM id/version or model-selected
identifier is stored. The next mutation still resolves the code through the normal
read Tool and backend Policy/DataScope before any write can happen.
"""
import re
import time
from threading import Lock

try:
    from flask import current_app, g, has_request_context, request
except Exception:  # pragma: no cover
    current_app = None
    g = None
    request = None

    def has_request_context():
        return False

from agent_business_context import repair_business_current_plan


_TTL_SECONDS = 600
_LIMIT = 256
_CURRENT = {}
_LOCK = Lock()
_PRONOUN_RE = re.compile(r'给它|让它|这个设备|该设备|刚才(?:查|看|说)?的?设备|刚才那个设备')


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
    for key in [key for key, value in _CURRENT.items() if value.get('expires_at', 0) <= now]:
        _CURRENT.pop(key, None)
    if len(_CURRENT) > _LIMIT:
        oldest = sorted(_CURRENT.items(), key=lambda item: item[1].get('created_at', 0))
        for key, _ in oldest[:len(_CURRENT) - _LIMIT]:
            _CURRENT.pop(key, None)


def _store_code(code):
    key = _key()
    if key is None:
        return
    normalized = str(code or '').strip().upper()
    if not normalized or len(normalized) > 80:
        return
    now = time.monotonic()
    with _LOCK:
        _cleanup(now)
        _CURRENT[key] = {
            'code': normalized,
            'created_at': now,
            'expires_at': now + _TTL_SECONDS,
        }


def _get_code():
    key = _key()
    if key is None:
        return None
    now = time.monotonic()
    with _LOCK:
        _cleanup(now)
        value = _CURRENT.get(key)
        if value:
            return value.get('code')
        if key[-1] is not None:
            unbound = _unbound_key()
            value = _CURRENT.pop(unbound, None)
            if value:
                _CURRENT[key] = value
                return value.get('code')
    return None


def repair_device_current_plan(text, result, authorized_commands):
    """Remember visible business targets and safely resume contextual requests."""
    result = repair_business_current_plan(text, result, authorized_commands)
    values = dict(result.get('arguments') or {})
    intent = result.get('intent')

    if intent == 'device.search' and result.get('action') == 'TOOL':
        code = values.get('device_code') or values.get('code')
        if code:
            _store_code(code)
        return result

    if intent != 'inspection.create' or not _PRONOUN_RE.search(str(text or '')):
        return result
    if values.get('device_code') or values.get('code'):
        return result

    code = _get_code()
    if not code:
        return result

    values.pop('device_id', None)
    values['device_code'] = code
    values['code'] = code
    missing = [item for item in (result.get('missing_fields') or []) if item != 'device']
    required = ('device.search', 'inspection_staff.search', 'inspection.create')
    authorized = set(authorized_commands or ())
    candidates = [command for command in required if command in authorized]

    repaired = dict(result)
    repaired['arguments'] = values
    repaired['missing_fields'] = missing
    repaired['candidates'] = candidates
    if missing:
        repaired['action'] = 'CLARIFY'
        repaired['entity_status'] = 'MISSING'
        return repaired
    if not all(command in authorized for command in required):
        repaired['action'] = 'DENY'
        repaired['entity_status'] = 'FORBIDDEN'
        return repaired
    repaired['action'] = 'TOOL'
    repaired['entity_status'] = 'RESOLVE_MULTI'
    repaired['candidates'] = list(required)
    repaired.pop('clarification_text', None)
    return repaired
