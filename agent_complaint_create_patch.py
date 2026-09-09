"""Server-owned complaint creation from visible house/address facts.

The model never chooses complaint.house_id and never rewrites the operator's
complaint content. A scoped house lookup resolves the target first; only one
resolved house can be used for the final write.
"""
import json
import re


def _category(text):
    value = str(text or '')
    if re.search(r'噪音|太吵|吵闹|施工|异响', value):
        return '噪音'
    if re.search(r'服务|态度|客服|工作人员', value):
        return '服务'
    if re.search(r'卫生|垃圾|保洁|环境|异味', value):
        return '环境'
    if re.search(r'电梯|水泵|门禁|路灯|设施|设备|故障', value):
        return '设施'
    return '其他'


def parse_complaint_business_facts(text):
    value = str(text or '').strip()
    if '投诉' not in value:
        return None
    facts = {}
    building = re.search(r'([A-Za-z0-9一二三四五六七八九十百]+)\s*(?:栋|号楼)', value)
    unit = re.search(r'([0-9一二三四五六七八九十百]+)\s*单元', value)
    room = re.search(r'(?:单元\s*)?([0-9]{2,4})\s*(?:房|室)', value)
    if not room:
        room = re.search(r'(?:栋|号楼)\s*([0-9]{2,4})(?=\s*(?:投诉|，|,|。|；|;|$))', value)
    if building:
        facts['building_name'] = building.group(1) + '栋'
    if unit:
        facts['unit'] = unit.group(1)
    if room:
        facts['room_no'] = int(room.group(1))

    explicit_title = re.search(r'标题(?:是|为|：|:)?\s*([^，,。；;]{1,100})', value)
    explicit_content = re.search(r'内容(?:是|为|：|:)?\s*([^。；;]{1,5000})', value)
    if explicit_content:
        content = explicit_content.group(1).strip()
    else:
        _, _, suffix = value.partition('投诉')
        content = suffix.strip(' ：:，,。；;')
        content = re.sub(r'^(?:一下|一个|一条|：|:)\s*', '', content).strip()
    if content:
        facts['complaint_content'] = content[:5000]
        category = _category(content)
        facts['complaint_category'] = category
        facts['complaint_title'] = (
            explicit_title.group(1).strip()[:100]
            if explicit_title else (category + '投诉')
        )
    return facts


def _install_provider_patch():
    import dify_client

    if getattr(dify_client, '_complaint_create_resolution_patch_installed', False):
        return
    specs = getattr(dify_client, '_MULTI_RESOLVE_SPECS', None)
    if isinstance(specs, dict):
        specs['complaint.create'] = ('house.search',)

    original_resolver = dify_client._multi_resolver_fallback
    original_write = dify_client._multi_resolved_write_fallback

    def complaint_resolver(command, resolved=None):
        hint = dify_client._PLANNER_HINT.get() or {}
        if hint.get('intent') != 'complaint.create' or hint.get('entity_status') != 'RESOLVE_MULTI':
            return original_resolver(command, resolved)
        if command != 'house.search':
            return None
        values = dict(hint.get('arguments') or {})
        params = {}
        for key in ('community_id', 'building_name', 'unit', 'room_no'):
            if values.get(key) not in (None, ''):
                params[key] = values[key]
        if not params.get('building_name') or params.get('room_no') is None:
            return None
        return {
            'operation': 'lookup',
            'command': 'house.search',
            'arguments_json': json.dumps(params, ensure_ascii=False),
        }

    def complaint_write(resolved):
        hint = dify_client._PLANNER_HINT.get() or {}
        if hint.get('intent') != 'complaint.create':
            return original_write(resolved)
        house = (resolved or {}).get('house.search') or {}
        values = dict(hint.get('arguments') or {})
        if house.get('id') is None:
            return None
        required = ('complaint_title', 'complaint_content', 'complaint_category')
        if any(not values.get(key) for key in required):
            return None
        params = {
            'house_id': house['id'],
            'title': values['complaint_title'],
            'content': values['complaint_content'],
            'category': values['complaint_category'],
        }
        return {
            'operation': 'execute',
            'command': 'complaint.create',
            'arguments_json': json.dumps(params, ensure_ascii=False),
        }

    dify_client._multi_resolver_fallback = complaint_resolver
    dify_client._multi_resolved_write_fallback = complaint_write
    dify_client._complaint_create_resolution_patch_installed = True


def repair_complaint_create_plan(text, result, authorized_commands):
    facts = parse_complaint_business_facts(text)
    if result.get('intent') != 'complaint.create':
        if facts is None or 'complaint.create' not in set(authorized_commands or ()):
            return result
    if result.get('action') == 'DENY':
        return result

    values = dict(result.get('arguments') or {})
    if facts:
        values.update({key: value for key, value in facts.items() if value not in (None, '')})
    for key in ('house_id', 'id', 'version'):
        values.pop(key, None)

    missing = []
    if not (values.get('building_name') and values.get('room_no') is not None):
        missing.append('house')
    if not values.get('complaint_content'):
        missing.append('content')

    required = ('house.search', 'complaint.create')
    authorized = set(authorized_commands or ())
    candidates = [command for command in required if command in authorized]
    if missing:
        return {
            'action': 'CLARIFY',
            'intent': 'complaint.create',
            'candidates': candidates,
            'missing_fields': missing,
            'entity_status': 'MISSING',
            'arguments': values,
            'clarification_text': (
                '请补充投诉对应的楼栋和房号。' if missing[0] == 'house'
                else '请告诉我具体投诉内容，我不会替你编写投诉事实。'
            ),
        }
    if not all(command in authorized for command in required):
        return {
            'action': 'DENY',
            'intent': 'complaint.create',
            'candidates': candidates,
            'missing_fields': [],
            'entity_status': 'FORBIDDEN',
            'arguments': values,
        }

    _install_provider_patch()
    return {
        'action': 'TOOL',
        'intent': 'complaint.create',
        'candidates': list(required),
        'missing_fields': [],
        'entity_status': 'RESOLVE_MULTI',
        'arguments': values,
    }
