"""High-risk archive safety for conversational Agent workflows.

Vehicle/device archive must use an explicit operator-supplied audit reason and a
server-resolved current target. The model never owns the final id/version and
cannot substitute its own reason after resolution.
"""
import json
import re


_ARCHIVE_SPECS = {
    'vehicle.archive': {
        'resolver': 'vehicle.search',
        'target_slot': 'vehicle',
        'visible': ('plate',),
        'label': '车辆',
    },
    'device.archive': {
        'resolver': 'device.search',
        'target_slot': 'device',
        'visible': ('device_code', 'code'),
        'label': '设备',
    },
}
_REASON_RE = re.compile(r'(?:原因|理由|因为|因)(?:是|为|：|:)?\s*([^，,。；;]{1,300})')


def explicit_reason(text, allow_plain=False):
    value = str(text or '').strip()
    matched = _REASON_RE.search(value)
    if matched:
        return matched.group(1).strip()
    if (
        allow_plain
        and value
        and len(value) <= 300
        and not re.search(r'归档|报废|注销|删除|作废|冲销|退租|释放|关闭', value)
    ):
        return value.strip(' ，,。；;')
    return None


def _install_state_patch():
    import agent_planner_state as state

    if getattr(state, '_archive_reason_followup_patch_installed', False):
        return
    original_extract = state._extract_followup

    def archive_extract(intent, text, missing=None):
        values = original_extract(intent, text, missing)
        if intent in _ARCHIVE_SPECS and 'reason' in set(missing or ()):
            reason = explicit_reason(text, allow_plain=True)
            if reason:
                values['reason'] = reason
        return values

    state._extract_followup = archive_extract
    state._archive_reason_followup_patch_installed = True


def _install_provider_patch():
    import dify_client

    if getattr(dify_client, '_archive_reason_provider_patch_installed', False):
        return
    original_write = dify_client._resolved_write_fallback

    def archive_write(query, item):
        hint = dify_client._PLANNER_HINT.get() or {}
        intent = hint.get('intent')
        if intent not in _ARCHIVE_SPECS:
            return original_write(query, item)
        if not isinstance(item, dict) or item.get('id') is None or item.get('version') is None:
            return None
        reason = str((hint.get('arguments') or {}).get('reason') or '').strip()
        if not reason:
            return None
        return {
            'operation': 'propose',
            'command': intent,
            'arguments_json': json.dumps(
                {'id': item['id'], 'version': item['version'], 'reason': reason},
                ensure_ascii=False,
            ),
        }

    dify_client._resolved_write_fallback = archive_write
    dify_client._archive_reason_provider_patch_installed = True


def repair_archive_reason_plan(text, result, authorized_commands):
    """Require target + explicit audit reason before any archive proposal."""
    if not isinstance(result, dict):
        return result
    intent = result.get('intent')
    spec = _ARCHIVE_SPECS.get(intent)
    if not spec:
        return result

    _install_state_patch()
    values = dict(result.get('arguments') or {})
    for key in ('id', 'version', 'vehicle_id', 'device_id'):
        values.pop(key, None)

    reason = explicit_reason(text)
    if reason:
        values['reason'] = reason

    target_present = any(values.get(key) not in (None, '') for key in spec['visible'])
    missing = []
    if not target_present:
        missing.append(spec['target_slot'])
    if not values.get('reason'):
        missing.append('reason')

    required = (spec['resolver'], intent)
    authorized = set(authorized_commands or ())
    candidates = [command for command in required if command in authorized]
    if missing:
        question = (
            f'请先告诉我要归档的{spec["label"]}，我会用业务编号重新核对。'
            if missing[0] == spec['target_slot']
            else f'请补充这次{spec["label"]}归档的原因，原因会原样写入审计记录。'
        )
        return {
            'action': 'CLARIFY',
            'intent': intent,
            'candidates': candidates,
            'missing_fields': missing,
            'entity_status': 'MISSING',
            'arguments': values,
            'clarification_text': question,
        }

    if not all(command in authorized for command in required):
        return {
            'action': 'DENY',
            'intent': intent,
            'candidates': candidates,
            'missing_fields': [],
            'entity_status': 'FORBIDDEN',
            'arguments': values,
        }

    _install_provider_patch()
    return {
        'action': 'CONFIRM',
        'intent': intent,
        'candidates': list(required),
        'missing_fields': [],
        'entity_status': 'RESOLVE_FIRST',
        'arguments': values,
    }
