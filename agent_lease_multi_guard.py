"""Lease safety guards layered after the single-tenant server resolver.

The backend supports multiple person_ids, but the current Agent resolver binds one
server-resolved tenant. Until a true N-person resolver exists, a multi-person
utterance must never be silently reduced to whichever name a regex happened to
capture. Also, a new tenant is not yet a current resident of the target building,
so lease-specific person resolution is constrained to the resolved house's
community rather than the resident-directory building filter.
"""
import json
import re

try:
    from flask import g, has_request_context
except Exception:  # pragma: no cover - planner tests can run without Flask
    g = None

    def has_request_context():
        return False

from agent_complaint_create_patch import repair_complaint_create_plan
from agent_device_context import repair_device_current_plan
from agent_lease_patch import is_lease_create_text, parse_lease_business_facts
from agent_visitor_patch import repair_visitor_host_plan


_MULTI_TENANT_RE = re.compile(
    r'(?:给\s*|租户(?:是|为|：|:)?\s*)?'
    r'[\u4e00-\u9fff]{2,4}\s*(?:、|和|与|及|,|，)\s*'
    r'[\u4e00-\u9fff]{2,4}(?=.{0,12}(?:办理入住|租户入住|登记租户入住|入住))'
)
_REQUEST_CACHE_ATTR = '_lease_completed_plan_for_request'


def has_multiple_tenants(text):
    value = str(text or '').strip()
    return bool(is_lease_create_text(value) and _MULTI_TENANT_RE.search(value))


def _install_new_tenant_scope_patch():
    """Remove only the inappropriate current-building-resident filter for leases."""
    import dify_client

    if getattr(dify_client, '_lease_new_tenant_scope_patch_installed', False):
        return
    original_resolver = dify_client._multi_resolver_fallback

    def scoped_resolver(command, resolved=None):
        call = original_resolver(command, resolved)
        hint = dify_client._PLANNER_HINT.get() or {}
        if (
            hint.get('intent') != 'lease.create'
            or hint.get('entity_status') != 'RESOLVE_MULTI'
            or command != 'person.search'
            or not isinstance(call, dict)
        ):
            return call
        try:
            params = json.loads(call.get('arguments_json') or '{}')
        except (TypeError, ValueError, json.JSONDecodeError):
            return call
        if not isinstance(params, dict):
            return call
        house = (resolved or {}).get('house.search') or {}
        community_id = house.get('community_id')
        if community_id is None:
            return call
        params.pop('building_id', None)
        params['community_id'] = community_id
        safe = dict(call)
        safe['arguments_json'] = json.dumps(params, ensure_ascii=False)
        return safe

    dify_client._multi_resolver_fallback = scoped_resolver
    dify_client._lease_new_tenant_scope_patch_installed = True


def _store_runtime_person_disambiguation(hint):
    """Persist only user-visible lease facts after a runtime same-name collision.

    A completed lease plan normally clears planner pending state. If person.search
    later returns multiple rows, the next turn still needs the original house and
    lease dates, but it must not remember any model-selected database identifier.
    The unbound pending entry is attached to the conversation by the existing
    state machine on the next request.
    """
    if not has_request_context():
        return
    try:
        from agent_planner_state import _store_pending
    except Exception:  # pragma: no cover - provider-only unit tests
        return
    values = dict((hint or {}).get('arguments') or {})
    for key in ('person_id', 'person_ids', 'house_id', 'id', 'version'):
        values.pop(key, None)
    _store_pending({
        'action': 'DISAMBIGUATE',
        'intent': 'lease.create',
        'candidates': ['house.search', 'person.search', 'lease.create'],
        'missing_fields': ['disambiguation'],
        'entity_status': 'AMBIGUOUS',
        'arguments': values,
    })


def _install_runtime_pending_patch():
    """Observe resolver results without changing Provider execution semantics."""
    import dify_client

    if getattr(dify_client, '_lease_runtime_pending_patch_installed', False):
        return
    client_cls = getattr(dify_client, 'BailianClient', None)
    if client_cls is None or not hasattr(client_cls, '_run_tool_call'):
        return
    original_run_tool_call = client_cls._run_tool_call

    def lease_run_tool_call(self, call, tool_callback, seen_tool_calls, completed_commands):
        args, result = original_run_tool_call(
            self, call, tool_callback, seen_tool_calls, completed_commands
        )
        hint = dify_client._PLANNER_HINT.get() or {}
        if (
            hint.get('intent') == 'lease.create'
            and hint.get('entity_status') == 'RESOLVE_MULTI'
            and isinstance(args, dict)
            and args.get('operation') == 'lookup'
            and args.get('command') == 'person.search'
            and isinstance(result, dict)
            and result.get('ok') is not False
        ):
            data = result.get('data')
            items = data.get('items') if isinstance(data, dict) else None
            if isinstance(items, list) and len(items) > 1:
                _store_runtime_person_disambiguation(hint)
        return args, result

    client_cls._run_tool_call = lease_run_tool_call
    dify_client._lease_runtime_pending_patch_installed = True


def _copy_plan(result):
    copied = dict(result)
    copied['candidates'] = list(result.get('candidates') or [])
    copied['missing_fields'] = list(result.get('missing_fields') or [])
    copied['arguments'] = dict(result.get('arguments') or {})
    return copied


def _cache_completed_plan(text, result):
    """Keep one safe completed lease plan only for the current Flask request.

    /ai/chat may deliberately re-run the planner after adding DB-derived context.
    A pending-plan continuation is consumed by the first run, so the second run
    can otherwise see only a short follow-up such as lease dates and degrade to
    ``unknown``. Flask ``g`` is request-local, so this never becomes cross-turn
    authority or long-lived state.
    """
    if not has_request_context():
        return
    setattr(g, _REQUEST_CACHE_ATTR, {
        'text': str(text or '').strip(),
        'plan': _copy_plan(result),
    })


def _restore_completed_plan(text, result, authorized_commands):
    if not has_request_context():
        return result
    if result.get('intent') != 'unknown' or result.get('action') != 'ANSWER':
        return result
    cached = getattr(g, _REQUEST_CACHE_ATTR, None)
    if not isinstance(cached, dict) or cached.get('text') != str(text or '').strip():
        return result
    plan = cached.get('plan')
    if not isinstance(plan, dict):
        return result
    required = {'house.search', 'person.search', 'lease.create'}
    if not required.issubset(set(authorized_commands or ())):
        return result
    if plan.get('intent') != 'lease.create' or plan.get('entity_status') != 'RESOLVE_MULTI' or plan.get('action') != 'TOOL':
        return result
    return _copy_plan(plan)


def repair_multi_tenant_lease_plan(text, result, authorized_commands):
    """Keep lease tenant resolution server-owned and fail closed on person lists."""
    result = repair_device_current_plan(text, result, authorized_commands)
    result = repair_complaint_create_plan(text, result, authorized_commands)
    result = repair_visitor_host_plan(text, result, authorized_commands)
    if result.get('intent') == 'visitor.create':
        return result

    if has_multiple_tenants(text):
        authorized = set(authorized_commands or ())
        required = ('house.search', 'person.search', 'lease.create')
        candidates = [command for command in required if command in authorized]
        if result.get('action') == 'DENY' or not all(command in authorized for command in required):
            return {
                'action': 'DENY',
                'intent': 'lease.create',
                'candidates': candidates,
                'missing_fields': [],
                'entity_status': 'FORBIDDEN',
                'arguments': {},
            }

        # Rebuild only from explicit visible lease facts. Any name/phone that the
        # single-person parser happened to pick from the list is intentionally
        # discarded; IDs and versions are never accepted here either.
        values = dict(parse_lease_business_facts(text, require_intent=False) or {})
        for key in ('person_name', 'phone', 'person_id', 'person_ids', 'house_id', 'id', 'version'):
            values.pop(key, None)

        return {
            'action': 'CLARIFY',
            'intent': 'lease.create',
            'candidates': list(required),
            'missing_fields': ['person'],
            'entity_status': 'MISSING',
            'arguments': values,
            'clarification_text': (
                '当前一次只支持一位租户办理入住。请明确本次先办理哪一位租户；'
                '其他租户请分别办理，我不会擅自只选择其中一人。'
            ),
        }

    if result.get('intent') == 'lease.create' and result.get('entity_status') == 'RESOLVE_MULTI' and result.get('action') == 'TOOL':
        _install_new_tenant_scope_patch()
        _install_runtime_pending_patch()
        _cache_completed_plan(text, result)
        return result

    restored = _restore_completed_plan(text, result, authorized_commands)
    if restored is not result:
        _install_new_tenant_scope_patch()
        _install_runtime_pending_patch()
    return restored