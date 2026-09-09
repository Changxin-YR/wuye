"""Server-owned entity resolution for Agent vehicle registration.

The operator supplies only visible business facts: plate, resident name and
house address. Database identifiers are selected by scoped read tools and are
never accepted from the model for the final vehicle.save call.
"""
import json


def _install_provider_patch():
    import dify_client

    if getattr(dify_client, '_vehicle_save_resolution_patch_installed', False):
        return

    specs = getattr(dify_client, '_MULTI_RESOLVE_SPECS', None)
    if isinstance(specs, dict):
        specs['vehicle.save'] = ('house.search', 'person.search')

    original_resolver = dify_client._multi_resolver_fallback
    original_write = dify_client._multi_resolved_write_fallback

    def vehicle_resolver(command, resolved=None):
        hint = dify_client._PLANNER_HINT.get() or {}
        if hint.get('intent') != 'vehicle.save' or hint.get('entity_status') != 'RESOLVE_MULTI':
            return original_resolver(command, resolved)

        values = dict(hint.get('arguments') or {})
        resolved = resolved or {}
        params = {}
        if command == 'house.search':
            for key in ('community_id', 'building_name', 'unit', 'room_no'):
                value = values.get(key)
                if value not in (None, ''):
                    params[key] = value
            if not params.get('building_name') or params.get('room_no') is None:
                return None
        elif command == 'person.search':
            name = values.get('person_name')
            if not name:
                return None
            params['person_name'] = name
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

    def vehicle_write(resolved):
        hint = dify_client._PLANNER_HINT.get() or {}
        if hint.get('intent') != 'vehicle.save':
            return original_write(resolved)

        values = dict(hint.get('arguments') or {})
        house = (resolved or {}).get('house.search') or {}
        person = (resolved or {}).get('person.search') or {}
        plate = str(values.get('plate') or '').strip().upper().replace(' ', '')
        if house.get('id') is None or person.get('id') is None or not plate:
            return None
        params = {
            'house_id': house['id'],
            'person_id': person['id'],
            'plate': plate,
        }
        model = values.get('model')
        if isinstance(model, str) and model.strip():
            params['model'] = model.strip()[:50]
        return {
            'operation': 'execute',
            'command': 'vehicle.save',
            'arguments_json': json.dumps(params, ensure_ascii=False),
        }

    dify_client._multi_resolver_fallback = vehicle_resolver
    dify_client._multi_resolved_write_fallback = vehicle_write
    dify_client._vehicle_save_resolution_patch_installed = True


def _question(slot):
    if slot == 'person':
        return '这辆车属于哪位住户？请直接告诉我住户姓名；同名时我再请你补充联系电话。'
    if slot == 'house':
        return '这辆车登记在哪套房名下？请告诉我楼栋和房号；如果同栋有多个单元，再补充单元。'
    return '还缺车牌号，请直接告诉我完整车牌号。'


def repair_vehicle_save_plan(text, result, authorized_commands):
    """Turn vehicle registration into two scoped lookups followed by one write."""
    if result.get('intent') != 'vehicle.save' or result.get('action') == 'DENY':
        return result

    values = dict(result.get('arguments') or {})
    # IDs and versions are server-owned for this Agent flow even if a model or
    # stale upstream plan tried to place them in the argument map.
    for key in ('house_id', 'person_id', 'id', 'version'):
        values.pop(key, None)
    if values.get('plate'):
        values['plate'] = str(values['plate']).strip().upper().replace(' ', '')

    missing = []
    if not values.get('person_name'):
        missing.append('person')
    if not (values.get('building_name') and values.get('room_no') is not None):
        missing.append('house')
    if not values.get('plate'):
        missing.append('vehicle')

    required = ('house.search', 'person.search', 'vehicle.save')
    authorized = set(authorized_commands or ())
    candidates = [command for command in required if command in authorized]
    if missing:
        return {
            'action': 'CLARIFY',
            'intent': 'vehicle.save',
            'candidates': candidates,
            'missing_fields': missing,
            'entity_status': 'MISSING',
            'arguments': values,
            'clarification_text': _question(missing[0]),
        }
    if not all(command in authorized for command in required):
        return {
            'action': 'DENY',
            'intent': 'vehicle.save',
            'candidates': candidates,
            'missing_fields': [],
            'entity_status': 'FORBIDDEN',
            'arguments': values,
        }

    _install_provider_patch()
    return {
        'action': 'TOOL',
        'intent': 'vehicle.save',
        'candidates': list(required),
        'missing_fields': [],
        'entity_status': 'RESOLVE_MULTI',
        'arguments': values,
    }
