"""Fail closed when a single-tenant Agent lease request contains multiple people.

The backend supports multiple person_ids, but the current Agent resolver binds one
server-resolved tenant. Until a true N-person resolver exists, a multi-person
utterance must never be silently reduced to whichever name a regex happened to
capture. The operator chooses one tenant, then the existing safe flow continues.
"""
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


def repair_multi_tenant_lease_plan(text, result, authorized_commands):
    """Require one explicit tenant instead of guessing from a multi-person phrase."""
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
