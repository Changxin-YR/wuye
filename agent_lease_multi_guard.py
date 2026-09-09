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

from agent_lease_patch import is_lease_create_text, parse_lease_business_facts


_MULTI_TENANT_RE = re.compile(
    r'(?:给\s*|租户(?:是|为|：|:)?\s*)?'
    r'[\u4e00-\u9fff]{2,4}\s*(?:、|和|与|及|,|，)\s*'
    r'[\u4e00-\u9fff]{2,4}(?=.{0,12}(?:办理入住|租户入住|登记租户入住|入住))'
)


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


def repair_multi_tenant_lease_plan(text, result, authorized_commands):
    """Keep lease tenant resolution server-owned and fail closed on person lists."""
    if result.get('intent') == 'lease.create' and result.get('entity_status') == 'RESOLVE_MULTI':
        _install_new_tenant_scope_patch()

    if not has_multiple_tenants(text):
        return result

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
