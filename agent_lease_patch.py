"""Server-owned single-tenant lease creation for the property Agent.

A lease is an R2 relationship/state mutation. The model may explain the plan,
but it never chooses house/person IDs or invents lease dates. The operator must
supply the tenant name, house address, lease start/end dates and an already
occurred move-in time. Scoped lookup tools resolve database identifiers.
"""
import json
import re
from datetime import date, datetime, timedelta, timezone


def is_lease_create_text(text):
    value = str(text or '').strip()
    return bool(re.search(r'登记租户入住|租户入住|办理入住|登记.*租户.*入住', value))


def _valid_date(value):
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        return None


def _valid_local_moment(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
    return parsed.isoformat(timespec='minutes')


def parse_lease_business_facts(text, require_intent=True):
    """Extract only explicit visible lease facts; never synthesize a lease term."""
    value = str(text or '').strip()
    if require_intent and not is_lease_create_text(value):
        return None
    facts = {}

    tenant_patterns = (
        r'给\s*([\u4e00-\u9fff]{2,4})\s*登记租户入住',
        r'租户(?:是|为|：|:)?\s*([\u4e00-\u9fff]{2,4})(?=入住|，|,|。|；|;|\s|$)',
        r'([\u4e00-\u9fff]{2,4})(?=\s*(?:办理入住|租户入住))',
    )
    for pattern in tenant_patterns:
        matched = re.search(pattern, value)
        if matched:
            facts['person_name'] = matched.group(1)
            break

    building = re.search(r'([A-Za-z0-9一二三四五六七八九十百]+)\s*(?:栋|号楼)', value)
    unit = re.search(r'([0-9一二三四五六七八九十百]+)\s*单元', value)
    room = re.search(r'(?:单元\s*)?([0-9]{2,4})\s*(?:房|室)', value)
    phone = re.search(r'1[3-9]\d{9}', value)
    if building:
        facts['building_name'] = building.group(1) + '栋'
    if unit:
        facts['unit'] = unit.group(1)
    if room:
        facts['room_no'] = int(room.group(1))
    if phone:
        facts['phone'] = phone.group(0)

    term = re.search(
        r'租期(?:是|为|：|:)?\s*(20\d{2}-\d{1,2}-\d{1,2})\s*(?:到|至)\s*(20\d{2}-\d{1,2}-\d{1,2})',
        value,
    )
    if term:
        start = _valid_date(term.group(1))
        end = _valid_date(term.group(2))
        if start:
            facts['start_date'] = start
        if end:
            facts['end_date'] = end
    else:
        start = re.search(r'(?:起租日|开始日期|租期开始)(?:是|为|：|:)?\s*(20\d{2}-\d{1,2}-\d{1,2})', value)
        end = re.search(r'(?:到期日|结束日期|租期结束)(?:是|为|：|:)?\s*(20\d{2}-\d{1,2}-\d{1,2})', value)
        if start:
            normalized = _valid_date(start.group(1))
            if normalized:
                facts['start_date'] = normalized
        if end:
            normalized = _valid_date(end.group(1))
            if normalized:
                facts['end_date'] = normalized

    move = re.search(
        r'(?:实际入住时间|入住时间|实际入住|已于)(?:是|为|：|:)?\s*'
        r'(20\d{2}-\d{1,2}-\d{1,2}[ T]\d{1,2}:\d{2})',
        value,
    )
    if move:
        normalized = _valid_local_moment(move.group(1))
        if normalized:
            facts['move_in'] = normalized

    note = re.search(r'(?:备注|说明)(?:是|为|：|:)?\s*([^，,。；;]{1,500})', value)
    if note:
        facts['note'] = note.group(1).strip()
    return facts


def _install_state_patch():
    import agent_planner_state as state

    if getattr(state, '_lease_pending_patch_installed', False):
        return
    original_continuation = state._looks_like_continuation
    original_extract = state._extract_followup

    def lease_continuation(text, pending):
        if pending.get('intent') == 'lease.create':
            missing = set(pending.get('missing_fields') or ())
            facts = parse_lease_business_facts(text, require_intent=False) or {}
            if any(key in facts for key in missing):
                return True
            if missing & {'start_date', 'end_date', 'move_in'} and re.search(
                r'租期|起租|到期|结束日期|入住时间|实际入住|20\d{2}-\d{1,2}-\d{1,2}',
                str(text or ''),
            ):
                return True
        return original_continuation(text, pending)

    def lease_extract(intent, text, missing=None):
        values = original_extract(intent, text, missing)
        if intent != 'lease.create':
            return values
        facts = parse_lease_business_facts(text, require_intent=False) or {}
        for key in ('person_name', 'phone', 'building_name', 'unit', 'room_no', 'start_date', 'end_date', 'move_in', 'note'):
            if facts.get(key) not in (None, ''):
                values[key] = facts[key]
        return values

    state._looks_like_continuation = lease_continuation
    state._extract_followup = lease_extract
    state._lease_pending_patch_installed = True


def _install_provider_patch():
    import dify_client

    if getattr(dify_client, '_lease_create_resolution_patch_installed', False):
        return

    specs = getattr(dify_client, '_MULTI_RESOLVE_SPECS', None)
    if isinstance(specs, dict):
        specs['lease.create'] = ('house.search', 'person.search')

    original_resolver = dify_client._multi_resolver_fallback
    original_write = dify_client._multi_resolved_write_fallback

    def lease_resolver(command, resolved=None):
        hint = dify_client._PLANNER_HINT.get() or {}
        if hint.get('intent') != 'lease.create' or hint.get('entity_status') != 'RESOLVE_MULTI':
            return original_resolver(command, resolved)
        values = dict(hint.get('arguments') or {})
        resolved = resolved or {}
        params = {}
        if command == 'house.search':
            for key in ('community_id', 'building_name', 'unit', 'room_no'):
                if values.get(key) not in (None, ''):
                    params[key] = values[key]
            if not params.get('building_name') or params.get('room_no') is None:
                return None
        elif command == 'person.search':
            if not values.get('person_name'):
                return None
            params['person_name'] = values['person_name']
            if values.get('phone'):
                params['phone'] = values['phone']
            house = resolved.get('house.search') or {}
            if house.get('community_id') is not None:
                params['community_id'] = house['community_id']
            if house.get('building_id') is not None:
                params['building_id'] = house['building_id']
        else:
            return None
        return {
            'operation': 'lookup',
            'command': command,
            'arguments_json': json.dumps(params, ensure_ascii=False),
        }

    def lease_write(resolved):
        hint = dify_client._PLANNER_HINT.get() or {}
        if hint.get('intent') != 'lease.create':
            return original_write(resolved)
        values = dict(hint.get('arguments') or {})
        house = (resolved or {}).get('house.search') or {}
        person = (resolved or {}).get('person.search') or {}
        if house.get('id') is None or person.get('id') is None:
            return None
        for key in ('start_date', 'end_date', 'move_in'):
            if not values.get(key):
                return None
        params = {
            'house_id': house['id'],
            'person_ids': [person['id']],
            'start_date': values['start_date'],
            'end_date': values['end_date'],
            'move_in': values['move_in'],
        }
        if isinstance(values.get('note'), str) and values['note'].strip():
            params['note'] = values['note'].strip()[:500]
        return {
            'operation': 'execute',
            'command': 'lease.create',
            'arguments_json': json.dumps(params, ensure_ascii=False),
        }

    dify_client._multi_resolver_fallback = lease_resolver
    dify_client._multi_resolved_write_fallback = lease_write
    dify_client._lease_create_resolution_patch_installed = True


def _question(missing):
    missing = set(missing or ())
    if 'person' in missing:
        return '要给哪位租户办理入住？请直接告诉我租户姓名；同名时再补联系电话。'
    if 'house' in missing:
        return '租户要入住哪套房？请告诉我楼栋和房号；同栋有多个单元时再补单元。'
    if {'start_date', 'end_date'} & missing:
        return '请补充明确租期，例如“租期2026-09-01到2027-08-31”；我不会自动猜一年租期。'
    if 'move_in' in missing:
        return '请补充已经实际发生的入住时间，例如“实际入住时间2026-09-09 08:00”。'
    return '还缺租户入住所需的业务信息，请补充后我再继续办理。'


def repair_lease_create_plan(text, result, authorized_commands):
    authorized = set(authorized_commands or ())
    _install_state_patch()

    if result.get('intent') == 'lease.create' and result.get('action') == 'DISAMBIGUATE':
        return result

    facts = parse_lease_business_facts(text, require_intent=True)
    if result.get('intent') == 'lease.create':
        if result.get('action') == 'DENY':
            return result
        values = dict(result.get('arguments') or {})
        if facts:
            for key, value in facts.items():
                if value not in (None, ''):
                    values[key] = value
    elif facts is not None and 'lease.create' in authorized:
        # Repair generic house-create collisions using only facts from user text.
        values = dict(facts)
    else:
        return result

    for key in ('house_id', 'person_id', 'person_ids', 'id', 'version'):
        values.pop(key, None)

    missing = []
    if not values.get('person_name'):
        missing.append('person')
    if not (values.get('building_name') and values.get('room_no') is not None):
        missing.append('house')
    for key in ('start_date', 'end_date', 'move_in'):
        if not values.get(key):
            missing.append(key)

    if not missing:
        start = date.fromisoformat(values['start_date'])
        end = date.fromisoformat(values['end_date'])
        move_local = datetime.fromisoformat(values['move_in'])
        now_local = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)
        if end < start or not start <= move_local.date() <= end:
            missing.extend(['start_date', 'end_date'])
        elif move_local > now_local + timedelta(minutes=5):
            missing.append('move_in')

    required = ('house.search', 'person.search', 'lease.create')
    candidates = [command for command in required if command in authorized]
    if missing:
        return {
            'action': 'CLARIFY',
            'intent': 'lease.create',
            'candidates': candidates,
            'missing_fields': list(dict.fromkeys(missing)),
            'entity_status': 'MISSING',
            'arguments': values,
            'clarification_text': _question(missing),
        }
    if not all(command in authorized for command in required):
        return {
            'action': 'DENY',
            'intent': 'lease.create',
            'candidates': candidates,
            'missing_fields': [],
            'entity_status': 'FORBIDDEN',
            'arguments': values,
        }

    _install_provider_patch()
    return {
        'action': 'TOOL',
        'intent': 'lease.create',
        'candidates': list(required),
        'missing_fields': [],
        'entity_status': 'RESOLVE_MULTI',
        'arguments': values,
    }
