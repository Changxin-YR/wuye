"""Narrow financial extensions for the direct-provider tool loop.

Imported only for financial planner turns. It extends the already-tested
resolver loop without replacing it: bill creation resolves user-visible
business objects first and only then builds a confirmation proposal. Payment
recording is normalized again at the backend gateway so provider-supplied
bill/version/amount/channel/reference values can never retarget the proposal.
"""
import json

import agent_tools as _tools
import dify_client as _client
from models import Bill
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

    def _server_owned_payment_params(actor, command, params):
        """Replace all provider payment fields with the deterministic planner facts.

        The user-visible bill number comes from the natural-language request.
        The current row version is loaded from the actor's scoped Policy at the
        gateway immediately before proposal validation. This protects both
        provider-generated calls and the older app.py fallback builder.
        """
        if command != 'payment.record':
            return params
        hint = _client._PLANNER_HINT.get() or {}
        if str(hint.get('action') or '').upper() != 'CONFIRM' or hint.get('intent') != 'payment.record':
            return params
        values = dict(hint.get('arguments') or {})
        required = ('bill_id', 'amount', 'channel', 'reference')
        if any(values.get(key) in (None, '') for key in required):
            return params
        db = _tools.object_session(actor)
        if db is None:
            return params
        bill = Policy(db, actor).get(Bill, int(values['bill_id']), True)
        return {
            'bill_id': bill.id,
            'version': bill.version,
            # Agent normalize accepts scalar strings/ints; a canonical string also
            # avoids binary-float surprises before PropertyService Decimal parsing.
            'amount': str(values['amount']),
            'channel': str(values['channel']),
            'reference': str(values['reference']),
        }

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
        params = _server_owned_payment_params(actor, command, params)
        return _base_normalize(actor, command, params)

    _client._multi_resolver_fallback = multi_resolver_fallback
    _client._multi_resolved_write_fallback = multi_resolved_write_fallback
    _tools.normalize = normalize
