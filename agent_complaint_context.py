"""Server-owned complaint context and lifecycle guards for conversational Agent turns.

A remembered complaint number is only a visible selector. Every mutation still
re-reads the complaint through complaint.search under Policy/DataScope, obtains
the current version from the server, and then lets PropertyService enforce the
current state transition. The provider cannot replace the target, version or
operator-supplied resolution text.
"""
import json
import re

from agent_business_context import repair_business_current_plan


_WRITE_INTENTS = {'complaint.resolve', 'complaint.close'}
_COMPLAINT_ID_RE = re.compile(r'投诉(?:单|记录)?\s*#?\s*([1-9]\d{0,8})')
_RESULT_RE = re.compile(r'(?:投诉)?处理结果(?:是|为|：|:)\s*([^，,。；;]{1,1000})')
_REVISIT_RE = re.compile(r'回访(?:结果)?(?:是|为|：|:)?\s*([^，,。；;]{1,500})')


def _resolution(text, intent):
    value = str(text or '').strip()
    if intent == 'complaint.resolve':
        matched = _RESULT_RE.search(value)
        if matched:
            return matched.group(1).strip()
        matched = re.search(r'(已(?:经)?(?:上门|电话|现场)?(?:整改|处理|修复)[^，,。；;]{0,80})', value)
        if matched:
            return matched.group(1).strip()
        return None

    matched = _REVISIT_RE.search(value)
    if matched:
        note = matched.group(1).strip()
        note = re.sub(r'(?:后)?(?:并)?结案$', '', note).strip()
        if note == '确认':
            return '住户回访确认'
        if note:
            return note
    if '回访' in value and '确认' in value:
        return '住户回访确认'
    return None


def _install_provider_patch():
    import dify_client

    if getattr(dify_client, '_complaint_context_provider_patch_installed', False):
        return

    # For contextual close, the planner-owned state filter is part of the
    # target contract. The model may not remove "resolved" and select another
    # complaint state instead.
    owned = getattr(dify_client, '_PLANNER_OWNED_READ_FIELDS', {})
    if isinstance(owned, dict):
        spec = owned.setdefault('complaint.search', {})
        if isinstance(spec, dict):
            spec.setdefault('status', ('status',))

    original_write = dify_client._resolved_write_fallback
    original_match = dify_client._call_matches_resolved_target

    def complaint_write(query, item):
        hint = dify_client._PLANNER_HINT.get() or {}
        intent = hint.get('intent')
        if intent not in _WRITE_INTENTS:
            return original_write(query, item)
        if not isinstance(item, dict) or item.get('id') is None or item.get('version') is None:
            return None
        resolution = str((hint.get('arguments') or {}).get('resolution') or '').strip()
        if not resolution:
            return None
        operation = 'propose' if str(hint.get('action') or '').upper() == 'CONFIRM' else 'execute'
        return {
            'operation': operation,
            'command': intent,
            'arguments_json': json.dumps(
                {'id': item['id'], 'version': item['version'], 'resolution': resolution},
                ensure_ascii=False,
            ),
        }

    def complaint_match(call, item):
        if not original_match(call, item):
            return False
        hint = dify_client._PLANNER_HINT.get() or {}
        intent = hint.get('intent')
        if intent not in _WRITE_INTENTS:
            return True
        outer = dify_client._parse_call_args(call)
        try:
            params = json.loads(outer.get('arguments_json') or '{}')
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        expected = str((hint.get('arguments') or {}).get('resolution') or '').strip()
        return (
            isinstance(params, dict)
            and str(params.get('version')) == str((item or {}).get('version'))
            and str(params.get('resolution') or '').strip() == expected
        )

    dify_client._resolved_write_fallback = complaint_write
    dify_client._call_matches_resolved_target = complaint_match
    dify_client._complaint_context_provider_patch_installed = True


def repair_complaint_current_plan(text, result, authorized_commands):
    """Force complaint mutations through a fresh server read of the current row."""
    result = repair_business_current_plan(text, result, authorized_commands)
    if not isinstance(result, dict) or result.get('intent') not in _WRITE_INTENTS:
        return result

    intent = result['intent']
    values = dict(result.get('arguments') or {})
    explicit = _COMPLAINT_ID_RE.search(str(text or ''))
    if explicit:
        complaint_id = int(explicit.group(1))
        values['complaint_id'] = complaint_id
        values['id'] = complaint_id
    for key in ('version', 'assignee_id'):
        values.pop(key, None)

    resolution = _resolution(text, intent)
    if resolution:
        values['resolution'] = resolution

    target = values.get('complaint_id') or values.get('id')
    missing = []
    if not target:
        missing.append('complaint')
    if not values.get('resolution'):
        missing.append('resolution')

    required = ('complaint.search', intent)
    authorized = set(authorized_commands or ())
    candidates = [command for command in required if command in authorized]
    if missing:
        question = (
            '你指哪条投诉？可以直接说投诉编号；如果就是刚才那条，也可以说“刚才这条”。'
            if missing[0] == 'complaint'
            else ('请告诉我这条投诉的实际处理结果，我会原样写入处理记录。'
                  if intent == 'complaint.resolve'
                  else '请告诉我回访结果，例如“住户确认已解决”，我再生成结案确认。')
        )
        shown = dict(result)
        shown.update({
            'action': 'CLARIFY',
            'candidates': candidates,
            'missing_fields': missing,
            'entity_status': 'MISSING',
            'arguments': values,
            'clarification_text': question,
        })
        return shown

    if not all(command in authorized for command in required):
        return {
            'action': 'DENY', 'intent': intent, 'candidates': candidates,
            'missing_fields': [], 'entity_status': 'FORBIDDEN', 'arguments': values,
        }

    if intent == 'complaint.close':
        # Only a currently resolved complaint is eligible for close. Keeping the
        # filter server-owned means a cached target becomes unusable immediately
        # after it is closed instead of behaving like stale mutable state.
        values['status'] = 'resolved'

    _install_provider_patch()
    return {
        'action': 'CONFIRM' if intent == 'complaint.close' else 'TOOL',
        'intent': intent,
        'candidates': list(required),
        'missing_fields': [],
        'entity_status': 'RESOLVE_FIRST',
        'arguments': values,
    }
