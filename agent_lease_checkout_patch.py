"""Server-owned lease checkout resolution for the property-management Agent.

Lease checkout is a high-impact relationship/state mutation. The operator only
supplies visible business facts (tenant/house and reason). This patch adds one
scoped read resolver for active leases, forces checkout into CONFIRM mode, and
binds the resolved Lease id/version on the server before a proposal is created.
It never grants authority: business_queries Policy/DataScope and PropertyService
remain authoritative at lookup, proposal and confirmation time.
"""
import json
import re

from sqlalchemy import select

import business_queries as _bq
from models import House, HousePerson, Lease, Person
from permissions import Policy


_QUERY_PATCH_FLAG = '_lease_search_query_patch_installed'
_PROVIDER_PATCH_FLAG = '_lease_checkout_provider_patch_installed'


def _install_lease_search_query():
    """Register a read-only active-lease resolver without widening DataScope."""
    _bq.QUERIES.setdefault('lease.search', 'lease.write')
    if getattr(_bq, _QUERY_PATCH_FLAG, False):
        return
    original_query = _bq.query

    def query(db, actor, command, args):
        if command != 'lease.search':
            return original_query(db, actor, command, args)
        if not isinstance(args, dict):
            from flask import abort
            abort(400, description='无效查询参数')
        from flask import abort

        allowed = {
            'id', 'lease_id', 'house_id', 'status',
            'person_id', 'person_name', 'phone',
            'community_id', 'building_id', 'building_name', 'unit', 'room_no',
        }
        if set(args) - allowed:
            abort(400, description='查询包含未知参数')
        if any(not isinstance(value, (str, int)) or isinstance(value, bool) for value in args.values()):
            abort(400, description='查询参数类型无效')

        policy = Policy(db, actor)
        policy.require('lease.write')
        leases = policy.query(Lease)

        lease_id = args.get('lease_id') or args.get('id')
        if lease_id:
            leases = leases.where(Lease.id == int(lease_id))
        if args.get('house_id'):
            leases = leases.where(Lease.house_id == int(args['house_id']))
        if args.get('status'):
            leases = leases.where(Lease.status == str(args['status']).strip())

        house_targeted = any(args.get(key) not in (None, '') for key in (
            'community_id', 'building_id', 'building_name', 'unit', 'room_no'
        ))
        if house_targeted:
            houses = policy.query(House)
            if args.get('community_id'):
                houses = houses.where(House.community_id == int(args['community_id']))
            if args.get('building_id'):
                houses = houses.where(House.building_id == int(args['building_id']))
            if args.get('building_name'):
                houses = houses.where(House.building_name.in_(_bq._building_aliases(args['building_name'])))
            if args.get('unit'):
                houses = houses.where(House.unit.in_(_bq._unit_aliases(args['unit'])))
            if args.get('room_no'):
                houses = houses.where(House.room_no == int(args['room_no']))
            leases = leases.where(Lease.house_id.in_(houses.with_only_columns(House.id)))

        person_targeted = any(args.get(key) not in (None, '') for key in ('person_id', 'person_name', 'phone'))
        if person_targeted:
            policy.require('person.read')
            people = policy.query(Person)
            if args.get('person_id'):
                people = people.where(Person.id == int(args['person_id']))
            if args.get('person_name'):
                people = people.where(Person.name == str(args['person_name']).strip())
            if args.get('phone'):
                people = people.where(Person.phone == str(args['phone']).strip())
            rows = list(db.scalars(people.order_by(Person.id).limit(2)))
            if not rows:
                abort(404, description='未找到该租户或该租户不在当前账号数据范围内')
            if len(rows) > 1:
                abort(409, description='存在同名租户，请补充联系电话后再办理退租')
            lease_ids = select(HousePerson.lease_id).where(
                HousePerson.person_id == rows[0].id,
                HousePerson.kind == 'tenant',
                HousePerson.lease_id.is_not(None),
            )
            leases = leases.where(Lease.id.in_(lease_ids))

        return _bq._items(db.scalars(leases.order_by(Lease.id.desc()).limit(101)))

    _bq.query = query
    setattr(_bq, _QUERY_PATCH_FLAG, True)


_install_lease_search_query()


def _parse_reason(text):
    value = str(text or '').strip()
    matched = re.search(r'(?:原因|理由|因为)(?:是|为|：|:)?\s*([^，,。；;]{1,300})', value)
    return matched.group(1).strip() if matched else None


def _install_provider_patch():
    """Bind the exact scoped Lease row into a confirmation proposal."""
    import dify_client

    if getattr(dify_client, _PROVIDER_PATCH_FLAG, False):
        return

    dify_client._READ_ONLY_INTENTS.add('lease.search')
    dify_client._RESOLVER_ARGUMENTS['lease.search'] = {
        'id', 'lease_id', 'house_id', 'status', 'person_id', 'person_name', 'phone',
        'community_id', 'building_id', 'building_name', 'unit', 'room_no',
    }
    dify_client._PLANNER_OWNED_READ_FIELDS['lease.search'] = {
        'id': ('lease_id', 'id'),
        'house_id': ('house_id',),
        'status': ('status',),
        'person_id': ('person_id',),
        'person_name': ('person_name',),
        'phone': ('phone',),
        'community_id': ('community_id',),
        'building_id': ('building_id',),
        'building_name': ('building_name',),
        'unit': ('unit',),
        'room_no': ('room_no',),
    }
    dify_client._RESOLVED_SINGLE_TARGET_INTENTS.add('lease.checkout')

    original_write = dify_client._resolved_write_fallback

    def resolved_write(query, item):
        hint = dify_client._PLANNER_HINT.get() or {}
        if hint.get('intent') != 'lease.checkout':
            return original_write(query, item)
        if not isinstance(item, dict) or item.get('id') is None or item.get('version') is None:
            return None
        reason = str((hint.get('arguments') or {}).get('reason') or '').strip()
        if not reason:
            return None
        params = {'id': item['id'], 'version': item['version'], 'reason': reason}
        return {
            'operation': 'propose',
            'command': 'lease.checkout',
            'arguments_json': json.dumps(params, ensure_ascii=False),
        }

    dify_client._resolved_write_fallback = resolved_write
    setattr(dify_client, _PROVIDER_PATCH_FLAG, True)


def repair_lease_checkout_plan(text, result, authorized_commands):
    """Force checkout to resolve one active lease and require browser confirmation."""
    if result.get('intent') != 'lease.checkout':
        return result

    values = dict(result.get('arguments') or {})
    for key in ('id', 'version', 'lease_id'):
        values.pop(key, None)
    reason = _parse_reason(text)
    if reason:
        values['reason'] = reason
    values['status'] = 'active'

    target_present = bool(
        values.get('person_name')
        or values.get('phone')
        or values.get('person_id')
        or values.get('house_id')
        or (values.get('building_name') and values.get('room_no') is not None)
    )
    missing = []
    if not target_present:
        missing.append('lease')
    if not values.get('reason'):
        missing.append('reason')

    required = ('lease.search', 'lease.checkout')
    authorized = set(authorized_commands or ())
    candidates = [command for command in required if command in authorized]
    if missing:
        shown = {
            'action': 'CLARIFY',
            'intent': 'lease.checkout',
            'candidates': candidates,
            'missing_fields': missing,
            'entity_status': 'MISSING',
            'arguments': values,
        }
        shown['clarification_text'] = (
            '请告诉我要办理退租的租户姓名（同名时再补联系电话）以及退租原因。'
            if missing[0] == 'lease'
            else '请补充这次退租的原因，原因会原样写入审计记录。'
        )
        return shown

    if not all(command in authorized for command in required):
        return {
            'action': 'DENY',
            'intent': 'lease.checkout',
            'candidates': candidates,
            'missing_fields': [],
            'entity_status': 'FORBIDDEN',
            'arguments': values,
        }

    _install_provider_patch()
    return {
        'action': 'CONFIRM',
        'intent': 'lease.checkout',
        'candidates': list(required),
        'missing_fields': [],
        'entity_status': 'RESOLVE_FIRST',
        'arguments': values,
    }
