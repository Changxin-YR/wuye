"""Order-assignment runtime disambiguation for same-name repair workers.

The model never owns work-order ids, versions, or repairer ids. If scoped
`staff.search` returns multiple eligible workers, this patch stores only the
operator-visible order number/name as short-lived planner pending state. A
follow-up phone number narrows the staff resolver and the final id remains the
server-selected row.
"""
import json

try:
    from flask import has_request_context
except Exception:  # pragma: no cover
    def has_request_context():
        return False


def _store_runtime_staff_disambiguation(hint):
    if not has_request_context():
        return
    try:
        from agent_planner_state import _store_pending
    except Exception:  # pragma: no cover
        return
    values = dict((hint or {}).get('arguments') or {})
    for key in ('repairer_id', 'order_id', 'id', 'version'):
        values.pop(key, None)
    _store_pending({
        'action': 'DISAMBIGUATE',
        'intent': 'order.assign',
        'candidates': ['order.search', 'staff.search', 'order.assign'],
        'missing_fields': ['disambiguation'],
        'entity_status': 'AMBIGUOUS',
        'arguments': values,
    })


def _install_runtime_pending_patch():
    import dify_client

    if getattr(dify_client, '_order_staff_runtime_pending_patch_installed', False):
        return
    client_cls = getattr(dify_client, 'BailianClient', None)
    if client_cls is None or not hasattr(client_cls, '_run_tool_call'):
        return
    original_run_tool_call = client_cls._run_tool_call

    def order_staff_run_tool_call(self, call, tool_callback, seen_tool_calls, completed_commands):
        args, result = original_run_tool_call(
            self, call, tool_callback, seen_tool_calls, completed_commands
        )
        hint = dify_client._PLANNER_HINT.get() or {}
        if (
            hint.get('intent') == 'order.assign'
            and hint.get('entity_status') == 'RESOLVE_MULTI'
            and isinstance(args, dict)
            and args.get('operation') == 'lookup'
            and args.get('command') == 'staff.search'
            and isinstance(result, dict)
            and result.get('ok') is not False
        ):
            data = result.get('data')
            items = data.get('items') if isinstance(data, dict) else None
            if isinstance(items, list) and len(items) > 1:
                _store_runtime_staff_disambiguation(hint)
        return args, result

    client_cls._run_tool_call = order_staff_run_tool_call
    dify_client._order_staff_runtime_pending_patch_installed = True


def _install_staff_phone_resolver_patch():
    import dify_client

    if getattr(dify_client, '_order_staff_phone_resolver_patch_installed', False):
        return
    original_resolver = dify_client._multi_resolver_fallback

    def order_staff_resolver(command, resolved=None):
        call = original_resolver(command, resolved)
        hint = dify_client._PLANNER_HINT.get() or {}
        if (
            hint.get('intent') != 'order.assign'
            or hint.get('entity_status') != 'RESOLVE_MULTI'
            or command != 'staff.search'
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

    dify_client._multi_resolver_fallback = order_staff_resolver
    dify_client._order_staff_phone_resolver_patch_installed = True


def repair_order_staff_disambiguation_plan(text, result, authorized_commands):
    """Install runtime guards only for a fully authorized server-resolved dispatch."""
    if (
        result.get('intent') == 'order.assign'
        and result.get('action') == 'TOOL'
        and result.get('entity_status') == 'RESOLVE_MULTI'
    ):
        required = {'order.search', 'staff.search', 'order.assign'}
        if required.issubset(set(authorized_commands or ())):
            _install_runtime_pending_patch()
            _install_staff_phone_resolver_patch()
    return result
