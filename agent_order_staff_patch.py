"""Runtime staff disambiguation for server-resolved Agent workflows.

The model never owns worker ids. When a scoped staff resolver returns multiple
eligible same-name workers, keep only operator-visible business facts as
short-lived pending state. A follow-up phone number narrows the same scoped
resolver; the final worker id still comes exclusively from the backend result.
"""
import json

try:
    from flask import has_request_context
except Exception:  # pragma: no cover
    def has_request_context():
        return False

from agent_planner_core import _detect_intent


_LOCKED_FACTS_KEY = '_staff_disambiguation_locked_facts'
_STAFF_DOMAINS = {
    'order.assign': {
        'resolver': 'staff.search',
        'required': ('order.search', 'staff.search', 'order.assign'),
        'drop': ('repairer_id', 'order_id', 'id', 'version'),
        'lock': ('order_no', 'repairer_name'),
    },
    'complaint.assign': {
        'resolver': 'complaint_staff.search',
        'required': ('complaint.search', 'complaint_staff.search', 'complaint.assign'),
        'drop': ('assignee_id', 'id', 'version'),
        'lock': ('complaint_id', 'assignee_name'),
    },
    'inspection.create': {
        'resolver': 'inspection_staff.search',
        'required': ('device.search', 'inspection_staff.search', 'inspection.create'),
        'drop': ('assignee_id', 'device_id', 'id', 'version'),
        'lock': ('device_code', 'code', 'assignee_name', 'due_at', 'checklist'),
    },
}


def _store_runtime_staff_disambiguation(hint, spec):
    if not has_request_context():
        return
    try:
        from agent_planner_state import _store_pending
    except Exception:  # pragma: no cover
        return
    values = dict((hint or {}).get('arguments') or {})
    for key in spec['drop']:
        values.pop(key, None)
    locked = {
        key: values[key]
        for key in spec.get('lock', ())
        if values.get(key) not in (None, '')
    }
    if locked:
        # This is server-created state from the original operator request. It is
        # removed again before the next planner hint reaches any provider/tool.
        values[_LOCKED_FACTS_KEY] = locked
    _store_pending({
        'action': 'DISAMBIGUATE',
        'intent': hint.get('intent'),
        'candidates': list(spec['required']),
        'missing_fields': ['disambiguation'],
        'entity_status': 'AMBIGUOUS',
        'arguments': values,
    })


def _restore_locked_business_facts(result, spec):
    """Let a disambiguation turn choose staff only, not mutate the original task."""
    values = dict((result or {}).get('arguments') or {})
    locked = values.pop(_LOCKED_FACTS_KEY, None)
    if not isinstance(locked, dict):
        return result
    allowed = set(spec.get('lock', ()))
    for key, value in locked.items():
        if key in allowed and value not in (None, ''):
            values[key] = value
    repaired = dict(result)
    repaired['arguments'] = values
    return repaired


def _install_runtime_pending_patch():
    import dify_client

    if getattr(dify_client, '_staff_runtime_pending_patch_installed', False):
        return
    client_cls = getattr(dify_client, 'BailianClient', None)
    if client_cls is None or not hasattr(client_cls, '_run_tool_call'):
        return
    original_run_tool_call = client_cls._run_tool_call

    def staff_run_tool_call(self, call, tool_callback, seen_tool_calls, completed_commands):
        args, result = original_run_tool_call(
            self, call, tool_callback, seen_tool_calls, completed_commands
        )
        hint = dify_client._PLANNER_HINT.get() or {}
        spec = _STAFF_DOMAINS.get(hint.get('intent'))
        if (
            spec
            and hint.get('entity_status') == 'RESOLVE_MULTI'
            and isinstance(args, dict)
            and args.get('operation') == 'lookup'
            and args.get('command') == spec['resolver']
            and isinstance(result, dict)
            and result.get('ok') is not False
        ):
            data = result.get('data')
            items = data.get('items') if isinstance(data, dict) else None
            if isinstance(items, list) and len(items) > 1:
                _store_runtime_staff_disambiguation(hint, spec)
        return args, result

    client_cls._run_tool_call = staff_run_tool_call
    dify_client._staff_runtime_pending_patch_installed = True
    # Keep the old marker for compatibility with code/tests that may inspect it.
    dify_client._order_staff_runtime_pending_patch_installed = True


def _install_staff_phone_resolver_patch():
    import dify_client

    if getattr(dify_client, '_staff_phone_resolver_patch_installed', False):
        return
    original_resolver = dify_client._multi_resolver_fallback

    def staff_resolver(command, resolved=None):
        call = original_resolver(command, resolved)
        hint = dify_client._PLANNER_HINT.get() or {}
        spec = _STAFF_DOMAINS.get(hint.get('intent'))
        if (
            not spec
            or hint.get('entity_status') != 'RESOLVE_MULTI'
            or command != spec['resolver']
            or not isinstance(call, dict)
        ):
            return call
        phone = (hint.get('arguments') or {}).get('phone')
        if not phone:
            return call
        try:
            params = json.loads(call.get('arguments_json') or '{}')
        except (TypeError, ValueError, json.JSONDecodeError):
            return call
        if not isinstance(params, dict):
            return call
        params['phone'] = str(phone)
        safe = dict(call)
        safe['arguments_json'] = json.dumps(params, ensure_ascii=False)
        return safe

    dify_client._multi_resolver_fallback = staff_resolver
    dify_client._staff_phone_resolver_patch_installed = True
    dify_client._order_staff_phone_resolver_patch_installed = True


def _install_staff_pending_intent_boundary_patch():
    """An explicit different business intent must replace staff disambiguation.

    Generic disambiguation accepts digits/device codes as possible follow-up
    clues. That is useful for ambiguous entities but unsafe for runtime staff
    pending state: a new request such as ``给P-01安排巡检`` must not be consumed
    as a clue for an older complaint assignment merely because it contains
    ``P-01``. Only staff-runtime pending receives this stricter boundary.
    """
    import agent_planner_state as state

    if getattr(state, '_staff_pending_intent_boundary_patch_installed', False):
        return
    original = state._looks_like_continuation

    def staff_safe_continuation(text, pending):
        pending_intent = getattr(pending, 'intent', None)
        if pending_intent in _STAFF_DOMAINS:
            detected = _detect_intent(str(text or '').strip())
            if detected and detected != pending_intent:
                return False
        return original(text, pending)

    state._looks_like_continuation = staff_safe_continuation
    state._staff_pending_intent_boundary_patch_installed = True


def repair_order_staff_disambiguation_plan(text, result, authorized_commands):
    """Install runtime guards for authorized staff-resolved workflows.

    The historical function name is preserved because visitor/lease safety
    layers already call it. It now covers order dispatch, complaint assignment,
    and inspection creation with the same fail-closed semantics.
    """
    spec = _STAFF_DOMAINS.get(result.get('intent'))
    if spec:
        result = _restore_locked_business_facts(result, spec)
    if (
        spec
        and result.get('action') == 'TOOL'
        and result.get('entity_status') == 'RESOLVE_MULTI'
        and set(spec['required']).issubset(set(authorized_commands or ()))
    ):
        _install_runtime_pending_patch()
        _install_staff_phone_resolver_patch()
        _install_staff_pending_intent_boundary_patch()
    return result
