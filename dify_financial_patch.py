"""Narrow financial extensions for the direct-provider tool loop.

Imported only for financial planner turns. Bill generation resolves visible
business objects before proposal. Collection, bill voiding and payment reversal
are normalized again at the backend gateway so provider-supplied target ids,
versions, amounts, channels, references or audit reasons cannot retarget a
financial confirmation.
"""
import json

import agent_tools as _tools
import dify_client as _client
from models import Bill, Payment
from permissions import Policy

if not getattr(_client, '_FINANCIAL_RESOLVER_PATCHED', False):
    _client._FINANCIAL_RESOLVER_PATCHED = True
    _client._MULTI_RESOLVE_SPECS.update({
        'bill.create': ('house.search', 'fee.search'),
        'bill.batch': ('building.search', 'fee.search'),
    })
    _client._RESOLVER_LABELS.update({
        'building.search': '楼栋',
        'fee.search': '收费项目',
    })

    _base_multi_resolver = _client._multi_resolver_fallback
    _base_multi_write = _client._multi_resolved_write_fallback
    _base_resolved_write = _client._resolved_write_fallback
    _base_normalize_read = _client._normalize_read_call
    _base_call_matches = _client._call_matches_resolved_target
    _base_normalize = _tools.normalize

    def _financial_multi_resolver(command, resolved=None):
        hint = _client._PLANNER_HINT.get() or {}
        if hint.get('entity_status') != 'RESOLVE_MULTI':
            return None
        intent = hint.get('intent')
        values = dict(hint.get('arguments') or {})
        resolved = resolved or {}
        params = {}
        if intent == 'bill.create':
            if command == 'house.search':
                for key in ('building_name', 'unit', 'room_no'):
                    if values.get(key) not in (None, ''):
                        params[key] = values[key]
                if not params.get('building_name') or params.get('room_no') is None:
                    return None
            elif command == 'fee.search':
                house = resolved.get('house.search') or {}
                if house.get('community_id') is None or not values.get('fee_name'):
                    return None
                params = {'community_id': house['community_id'], 'name': values['fee_name']}
            else:
                return None
        elif intent == 'bill.batch':
            if command == 'building.search':
                if not values.get('building_name'):
                    return None
                params['building_name'] = values['building_name']
            elif command == 'fee.search':
                building = resolved.get('building.search') or {}
                if building.get('community_id') is None or not values.get('fee_name'):
                    return None
                params = {'community_id': building['community_id'], 'name': values['fee_name']}
            else:
                return None
        else:
            return None
        return {'operation': 'lookup', 'command': command, 'arguments_json': json.dumps(params, ensure_ascii=False)}

    def _financial_multi_write(resolved):
        hint = _client._PLANNER_HINT.get() or {}
        intent = hint.get('intent')
        values = dict(hint.get('arguments') or {})
        if intent == 'bill.create':
            house = resolved.get('house.search') or {}
            fee = resolved.get('fee.search') or {}
            if house.get('id') is None or fee.get('id') is None:
                return None
            if not values.get('period') or not values.get('due_date'):
                return None
            params = {
                'house_id': house['id'],
                'fee_item_id': fee['id'],
                'period': values['period'],
                'due_date': values['due_date'],
            }
            return {'operation': 'propose', 'command': 'bill.create', 'arguments_json': json.dumps(params, ensure_ascii=False)}
        if intent == 'bill.batch':
            building = resolved.get('building.search') or {}
            fee = resolved.get('fee.search') or {}
            if building.get('id') is None or fee.get('id') is None:
                return None
            if not values.get('period') or not values.get('due_date'):
                return None
            params = {
                'building_id': building['id'],
                'fee_item_id': fee['id'],
                'period': values['period'],
                'due_date': values['due_date'],
            }
            return {'operation': 'propose', 'command': 'bill.batch', 'arguments_json': json.dumps(params, ensure_ascii=False)}
        return None

    def _policy_target(actor, command, model, target_id):
        db = _tools.object_session(actor)
        if db is None:
            return None
        policy = Policy(db, actor)
        permission = _tools.DOMAIN_COMMANDS[command][0]
        policy.require(permission)
        return policy.get(model, int(target_id), True)

    def _server_owned_financial_params(actor, command, params):
        """Replace provider financial fields with deterministic planner facts."""
        hint = _client._PLANNER_HINT.get() or {}
        if str(hint.get('action') or '').upper() != 'CONFIRM' or hint.get('intent') != command:
            return params
        values = dict(hint.get('arguments') or {})

        if command == 'payment.record':
            required = ('bill_id', 'amount', 'channel', 'reference')
            if any(values.get(key) in (None, '') for key in required):
                return params
            bill = _policy_target(actor, command, Bill, values['bill_id'])
            if bill is None:
                return params
            return {
                'bill_id': bill.id,
                'version': bill.version,
                'amount': str(values['amount']),
                'channel': str(values['channel']),
                'reference': str(values['reference']),
            }

        if command == 'bill.void':
            if not values.get('bill_id') or not values.get('reason'):
                return params
            bill = _policy_target(actor, command, Bill, values['bill_id'])
            if bill is None:
                return params
            return {'id': bill.id, 'version': bill.version, 'reason': str(values['reason'])}

        if command == 'payment.reverse':
            reason = values.get('reason')
            if not reason:
                return params
            payment_id = values.get('payment_id') or values.get('id')
            if payment_id:
                payment = _policy_target(actor, command, Payment, payment_id)
                if payment is None:
                    return params
                return {'id': payment.id, 'version': payment.version, 'reason': str(reason)}
            # “上一笔” is first resolved by the server read tool. Keep its resolved
            # id/version but still replace any provider-invented audit reason.
            safe = dict(params) if isinstance(params, dict) else {}
            safe['reason'] = str(reason)
            return safe

        return params

    def _financial_read_call(call):
        safe = _base_normalize_read(call)
        hint = _client._PLANNER_HINT.get() or {}
        if hint.get('intent') != 'payment.reverse' or hint.get('entity_status') != 'RESOLVE_FIRST':
            return safe
        outer = _client._parse_call_args(safe)
        if outer.get('command') != 'payment.search':
            return safe
        values = dict(hint.get('arguments') or {})
        # The provider cannot choose which “latest” payment is considered. An
        # explicit target/bill may be forwarded only from deterministic planner facts.
        if values.get('_resolve_strategy') == 'latest':
            resolver_params = {}
        elif values.get('payment_id') or values.get('id'):
            resolver_params = {'id': values.get('payment_id') or values.get('id')}
        elif values.get('bill_id'):
            resolver_params = {'bill_id': values['bill_id']}
        else:
            resolver_params = {}
        outer = dict(outer)
        outer['operation'] = 'lookup'
        outer['command'] = 'payment.search'
        outer['arguments_json'] = json.dumps(resolver_params, ensure_ascii=False)
        rewritten = dict(safe)
        function = dict(rewritten.get('function') or {})
        function['arguments'] = json.dumps(outer, ensure_ascii=False)
        rewritten['function'] = function
        return rewritten

    def _financial_call_matches(call, item):
        if not _base_call_matches(call, item):
            return False
        hint = _client._PLANNER_HINT.get() or {}
        if hint.get('intent') != 'payment.reverse':
            return True
        outer = _client._parse_call_args(call)
        try:
            params = json.loads(outer.get('arguments_json') or '{}')
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        values = dict(hint.get('arguments') or {})
        return (
            isinstance(params, dict)
            and str(params.get('version')) == str(item.get('version'))
            and str(params.get('reason') or '') == str(values.get('reason') or '')
        )

    def _financial_resolved_write(query, item):
        fallback = _base_resolved_write(query, item)
        hint = _client._PLANNER_HINT.get() or {}
        if hint.get('intent') != 'payment.reverse' or not isinstance(fallback, dict):
            return fallback
        try:
            params = json.loads(fallback.get('arguments_json') or '{}')
        except (TypeError, ValueError, json.JSONDecodeError):
            return fallback
        reason = (hint.get('arguments') or {}).get('reason')
        if not reason:
            return None
        params['reason'] = str(reason)
        safe = dict(fallback)
        safe['arguments_json'] = json.dumps(params, ensure_ascii=False)
        return safe

    def multi_resolver_fallback(command, resolved=None):
        financial = _financial_multi_resolver(command, resolved)
        if financial is not None:
            return financial
        return _base_multi_resolver(command, resolved)

    def multi_resolved_write_fallback(resolved):
        financial = _financial_multi_write(resolved)
        if financial is not None:
            return financial
        return _base_multi_write(resolved)

    def normalize(actor, command, params):
        params = _server_owned_financial_params(actor, command, params)
        return _base_normalize(actor, command, params)

    _client._multi_resolver_fallback = multi_resolver_fallback
    _client._multi_resolved_write_fallback = multi_resolved_write_fallback
    _client._resolved_write_fallback = _financial_resolved_write
    _client._normalize_read_call = _financial_read_call
    _client._call_matches_resolved_target = _financial_call_matches
    _tools.normalize = normalize
