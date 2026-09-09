"""Visitor-create safety for same-name host disambiguation.

`phone` remains the visitor's own contact number. A separately and explicitly
supplied `host_phone` may only narrow `person.search` for the visited resident.
Neither value can select an internal database id directly; the final host id is
still bound from the server resolver result and revalidated by PropertyService.
"""
import json
import re

try:
    from flask import g, has_request_context
except Exception:  # pragma: no cover - planner tests can run without Flask
    g = None

    def has_request_context():
        return False

from agent_order_staff_patch import repair_order_staff_disambiguation_plan


_REQUEST_CACHE_ATTR = '_visitor_completed_plan_for_request'
_HOST_PHONE_RE = re.compile(
    r'(?:被访住户|被访人|住户|业主)(?:的)?(?:联系电话|手机号|电话)'
    r'(?:是|为|：|:)?\s*(1[3-9]\d{9})'
)


def host_phone(text):
    matched = _HOST_PHONE_RE.search(str(text or '').strip())
    return matched.group(1) if matched else None


def _copy_plan(result):
    copied = dict(result)
    copied['candidates'] = list(result.get('candidates') or [])
    copied['missing_fields'] = list(result.get('missing_fields') or [])
    copied['arguments'] = dict(result.get('arguments') or {})
    return copied


def _install_state_patch():
    """Teach pending-plan continuation about the dedicated host-phone slot."""
    import agent_planner_state as state

    if getattr(state, '_visitor_host_phone_patch_installed', False):
        return
    original_continuation = state._looks_like_continuation
    original_extract = state._extract_followup

    def visitor_continuation(text, pending):
        if (
            pending.get('intent') == 'visitor.create'
            and 'host_phone' in set(pending.get('missing_fields') or ())
            and host_phone(text)
        ):
            return True
        return original_continuation(text, pending)

    def visitor_extract(intent, text, missing=None):
        values = original_extract(intent, text, missing)
        if intent == 'visitor.create' and 'host_phone' in set(missing or ()):
            value = host_phone(text)
            if value:
                values['host_phone'] = value
        return values

    state._looks_like_continuation = visitor_continuation
    state._extract_followup = visitor_extract
    state._visitor_host_phone_patch_installed = True


def _install_provider_patch():
    """Bind host_phone only to the host-person resolver, never visitor.create.phone."""
    import dify_client

    if getattr(dify_client, '_visitor_host_phone_resolver_patch_installed', False):
        return
    original_resolver = dify_client._multi_resolver_fallback

    def visitor_resolver(command, resolved=None):
        call = original_resolver(command, resolved)
        hint = dify_client._PLANNER_HINT.get() or {}
        if (
            hint.get('intent') != 'visitor.create'
            or hint.get('entity_status') != 'RESOLVE_MULTI'
            or command != 'person.search'
            or not isinstance(call, dict)
        ):
            return call
        value = (hint.get('arguments') or {}).get('host_phone')
        if not value:
            return call
        try:
            params = json.loads(call.get('arguments_json') or '{}')
        except (TypeError, ValueError, json.JSONDecodeError):
            return call
        if not isinstance(params, dict):
            return call
        params['phone'] = str(value)
        safe = dict(call)
        safe['arguments_json'] = json.dumps(params, ensure_ascii=False)
        return safe

    dify_client._multi_resolver_fallback = visitor_resolver
    dify_client._visitor_host_phone_resolver_patch_installed = True


def _cache_completed_plan(text, result):
    if not has_request_context():
        return
    setattr(g, _REQUEST_CACHE_ATTR, {
        'text': str(text or '').strip(),
        'plan': _copy_plan(result),
    })


def _restore_completed_plan(text, result, authorized_commands):
    """Restore a just-consumed continuation during `/ai/chat` same-request replanning."""
    if not has_request_context():
        return result
    cached = getattr(g, _REQUEST_CACHE_ATTR, None)
    if not isinstance(cached, dict) or cached.get('text') != str(text or '').strip():
        return result
    plan = cached.get('plan')
    if not isinstance(plan, dict):
        return result
    required = {'person.search', 'house.search', 'visitor.create'}
    if not required.issubset(set(authorized_commands or ())):
        return result
    if plan.get('intent') != 'visitor.create' or plan.get('action') != 'TOOL' or plan.get('entity_status') != 'RESOLVE_MULTI':
        return result
    # A fresh local same-name decision without an explicit host phone is safer
    # than the earlier cached plan and must not be overridden.
    if (
        result.get('intent') == 'visitor.create'
        and result.get('action') == 'DISAMBIGUATE'
        and not (plan.get('arguments') or {}).get('host_phone')
    ):
        return result
    if result.get('intent') == 'security_boundary':
        return result
    return _copy_plan(plan)


def _complete_visitor_plan(values, authorized_commands):
    required = ('person.search', 'house.search', 'visitor.create')
    authorized = set(authorized_commands or ())
    candidates = [item for item in required if item in authorized]
    missing = []
    if not values.get('visitor_name'):
        missing.append('visitor')
    if not values.get('person_name'):
        missing.append('host_person')
    if not (values.get('house_id') or (values.get('building_name') and values.get('room_no') is not None)):
        missing.append('house')
    if not values.get('phone'):
        missing.append('phone')
    if not values.get('expected_at'):
        missing.append('expected_at')
    if not values.get('purpose'):
        missing.append('purpose')
    if missing:
        return {
            'action': 'CLARIFY', 'intent': 'visitor.create',
            'candidates': candidates, 'missing_fields': missing,
            'entity_status': 'MISSING', 'arguments': values,
        }
    if not all(item in authorized for item in required):
        return {
            'action': 'DENY', 'intent': 'visitor.create',
            'candidates': candidates, 'missing_fields': [],
            'entity_status': 'FORBIDDEN', 'arguments': values,
        }
    return {
        'action': 'TOOL', 'intent': 'visitor.create',
        'candidates': list(required), 'missing_fields': [],
        'entity_status': 'RESOLVE_MULTI', 'arguments': values,
    }


def repair_visitor_host_plan(text, result, authorized_commands):
    """Preserve visitor phone while a same-name host is disambiguated by host phone."""
    result = repair_order_staff_disambiguation_plan(text, result, authorized_commands)
    if result.get('intent') == 'order.assign':
        return result

    _install_state_patch()
    result = _restore_completed_plan(text, result, authorized_commands)
    if result.get('intent') != 'visitor.create':
        return result

    values = dict(result.get('arguments') or {})
    if values.get('phone') and not values.get('visitor_phone'):
        values['visitor_phone'] = values['phone']
    explicit_host_phone = host_phone(text)
    if explicit_host_phone:
        values['host_phone'] = explicit_host_phone
    # Never let a host-phone follow-up replace the visitor contact already saved
    # in the original request.
    if values.get('visitor_phone'):
        values['phone'] = values['visitor_phone']

    if result.get('action') == 'DISAMBIGUATE':
        if values.get('host_phone'):
            repaired = _complete_visitor_plan(values, authorized_commands)
            if repaired.get('action') == 'TOOL':
                _install_provider_patch()
                _cache_completed_plan(text, repaired)
            return repaired
        shown = dict(result)
        shown['arguments'] = values
        shown['missing_fields'] = ['host_phone']
        shown['entity_status'] = 'AMBIGUOUS'
        shown['clarification_text'] = (
            '我找到了多位同名住户。请补充被访住户的联系电话，例如“住户联系电话13800000123”。'
            '这不会覆盖访客自己的联系电话。'
        )
        return shown

    shown = dict(result)
    shown['arguments'] = values
    if shown.get('action') == 'TOOL' and shown.get('entity_status') in {'RESOLVED', 'RESOLVE_MULTI'}:
        shown['entity_status'] = 'RESOLVE_MULTI'
        _install_provider_patch()
        _cache_completed_plan(text, shown)
    return shown