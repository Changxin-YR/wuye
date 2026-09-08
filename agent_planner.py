"""Semantic compatibility layer around the stateful deterministic planner.

The state machine and frozen planner remain in ``agent_planner_state`` and
``agent_planner_core``. This layer repairs broad Chinese business phrases,
turns safe contextual references into scoped resolver workflows, and presents
missing slots as concise operator-style questions. It never grants permissions
or bypasses backend Policy/DataScope.
"""
import re

try:
    from flask import current_app, has_request_context, request
except Exception:  # pragma: no cover
    current_app = None
    request = None
    def has_request_context():
        return False

from agent_planner_state import *
from agent_planner_state import (
    _store_pending,
    clear_pending_plan,
    plan_request as _state_plan_request,
    visitor_expected_at,
    visitor_purpose,
)
from agent_planner_core import CONFIRM_INTENTS, _candidates


_CONTEXT_WORDS = re.compile(r'刚才|刚刚|这个|这个人|这个房|这个工单|这张单|上一笔|最近一笔|上一个|今天的|当前的|已经离开|已经报废|可以进|进去了|处理结果|回访|结案|返修|撤回')
_CONTEXT_RESOLVERS = {
    'order.assign': ('order.search', 'person.search'),
    'order.accept': ('order.search',),
    'order.progress': ('order.search',),
    'order.finish': ('order.search',),
    'order.reopen': ('order.search',),
    'order.close': ('order.search',),
    'order.cancel': ('order.search',),
    'complaint.assign': ('complaint.search', 'person.search'),
    'complaint.resolve': ('complaint.search',),
    'complaint.close': ('complaint.search',),
    'visitor.checkin': ('visitor.search',),
    'visitor.checkout': ('visitor.search',),
    'visitor.cancel': ('visitor.search',),
    'vehicle.archive': ('vehicle.search',),
    'parking.release': ('parking.search', 'vehicle.search'),
    'device.archive': ('device.search',),
    'inspection.complete': ('inspection.search', 'device.search'),
    'payment.reverse': ('payment.search',),
    'bill.void': ('billing.unpaid',),
}


def _repair_notice_content(text, result):
    if result.get('intent') != 'notice.save':
        return result
    arguments = dict(result.get('arguments') or {})
    content = arguments.get('notice_content')
    if isinstance(content, str):
        cleaned = re.sub(
            r'^给\s*[A-Za-z0-9一二三四五六七八九十百]+\s*(?:栋|号楼)\s*发(?:个|一条)?\s*',
            '', content,
        ).strip(' ，,。；;')
        if cleaned:
            arguments['notice_content'] = cleaned
            result = dict(result)
            result['arguments'] = arguments
    return result


def _repair_payment_target(text, result, authorized_commands):
    if result.get('intent') != 'payment.reverse' or result.get('action') != 'CLARIFY':
        return result
    match = re.search(r'(?:冲销|冲正|撤回).*?收款(?:记录)?\s*#?\s*([1-9]\d{0,8})', text)
    if not match:
        match = re.search(r'收款(?:记录)?\s*#?\s*([1-9]\d{0,8})', text)
    if not match:
        return result
    payment_id = int(match.group(1))
    arguments = dict(result.get('arguments') or {})
    arguments['payment_id'] = payment_id
    arguments['id'] = payment_id
    candidates = _candidates('payment.reverse', set(authorized_commands or ()))
    clear_pending_plan()
    return {
        'action': 'CONFIRM',
        'intent': 'payment.reverse',
        'candidates': candidates,
        'missing_fields': [],
        'entity_status': 'RESOLVED',
        'arguments': arguments,
    }


def _repair_device_code(result):
    if result.get('intent') not in {'device.save', 'device.search', 'device.archive', 'inspection.create'}:
        return result
    arguments = dict(result.get('arguments') or {})
    code = arguments.get('device_code') or arguments.get('code')
    if code:
        arguments['device_code'] = str(code).upper()
        arguments['code'] = str(code).upper()
        if result.get('intent') == 'device.save':
            arguments.setdefault('space_code', str(code).upper())
        result = dict(result)
        result['arguments'] = arguments
    return result


def _repair_order_cancel(text, result, authorized_commands):
    if not re.search(r'(?:撤销|取消).*(?:报修|工单)|(?:报修|工单).*(?:撤销|取消)', text):
        return result
    authorized = set(authorized_commands or ())
    if 'order.cancel' not in authorized:
        return result
    resolver = 'order.search' if 'order.search' in authorized else None
    arguments = dict(result.get('arguments') or {})
    if not arguments.get('order_no') and not resolver:
        return {
            'action': 'CLARIFY', 'intent': 'order.cancel', 'candidates': ['order.cancel'],
            'missing_fields': ['order'], 'entity_status': 'MISSING', 'arguments': arguments,
        }
    candidates = ([resolver] if resolver else []) + ['order.cancel']
    return {
        'action': 'CONFIRM',
        'intent': 'order.cancel',
        'candidates': candidates,
        'missing_fields': [],
        'entity_status': 'RESOLVED' if arguments.get('order_no') else 'RESOLVE_FIRST',
        'arguments': arguments,
    }


def _smooth_context_resolution(text, result, authorized_commands):
    if result.get('action') != 'CLARIFY':
        return result
    intent = result.get('intent')
    resolvers = _CONTEXT_RESOLVERS.get(intent)
    if not resolvers or not _CONTEXT_WORDS.search(text):
        return result
    authorized = set(authorized_commands or ())
    resolver_candidates = [command for command in resolvers if command in authorized]
    if not resolver_candidates or intent not in authorized:
        return result
    candidates = resolver_candidates + [intent]
    arguments = dict(result.get('arguments') or {})
    if intent == 'visitor.checkin':
        arguments.setdefault('status', 'registered')
    elif intent == 'visitor.checkout':
        arguments.setdefault('status', 'inside')
    elif intent == 'visitor.cancel':
        arguments.setdefault('status', 'registered')
    elif intent == 'inspection.complete':
        arguments.setdefault('status', 'pending')
    elif intent == 'complaint.resolve':
        arguments.setdefault('status', 'open')
    elif intent == 'complaint.close':
        arguments.setdefault('status', 'resolved')
    elif intent == 'device.archive' and '报废' in text:
        arguments.setdefault('status', 'retired')
    if intent == 'payment.reverse' and re.search(r'上一笔|最近一笔', text):
        arguments['_resolve_strategy'] = 'latest'
    action = 'CONFIRM' if intent in CONFIRM_INTENTS else 'TOOL'
    clear_pending_plan()
    return {
        'action': action,
        'intent': intent,
        'candidates': list(dict.fromkeys(candidates)),
        'missing_fields': [],
        'entity_status': 'RESOLVE_FIRST',
        'arguments': arguments,
    }


def _repair_exact_community_followup(text, result, context, authorized_commands):
    if result.get('action') != 'CLARIFY' or result.get('intent') not in {'notice.save', 'notice.batch_publish'}:
        return result
    missing = list(result.get('missing_fields') or [])
    if 'community_id' not in missing:
        return result
    writable = (context or {}).get('writable_communities')
    if not isinstance(writable, list):
        return result
    answer = str(text or '').strip()
    matches = [row for row in writable if isinstance(row, dict) and str(row.get('name') or '').strip() == answer]
    if len(matches) != 1 or not matches[0].get('id'):
        return result
    arguments = dict(result.get('arguments') or {})
    arguments['community_name'] = matches[0]['name']
    arguments['community_id'] = matches[0]['id']
    remaining = [slot for slot in missing if slot != 'community_id']
    if remaining:
        repaired = dict(result)
        repaired['missing_fields'] = remaining
        repaired['arguments'] = arguments
        return repaired
    intent = result.get('intent')
    clear_pending_plan()
    return {
        'action': 'CONFIRM' if intent in CONFIRM_INTENTS else 'TOOL',
        'intent': intent,
        'candidates': _candidates(intent, set(authorized_commands or ())),
        'missing_fields': [],
        'entity_status': 'RESOLVED',
        'arguments': arguments,
    }


def _repair_visitor_create(text, result, context, authorized_commands):
    """Separate human business facts from internal visitor-create identifiers.

    The user supplies names/address/contact/time. Person and house database IDs
    are resolved later through scoped read tools, never requested from the user.
    """
    if result.get('intent') != 'visitor.create':
        return result
    values = dict(result.get('arguments') or {})
    expected = visitor_expected_at(text)
    if expected:
        values['expected_at'] = expected
    if not values.get('purpose'):
        purpose = visitor_purpose(text, values.get('person_name'))
        if purpose:
            values['purpose'] = purpose
    resolved_person = (context or {}).get('resolved_person') or {}
    resolved_house = (context or {}).get('resolved_house') or {}
    if resolved_person.get('id') and not values.get('person_name'):
        values['host_person_id'] = resolved_person['id']
    if resolved_house.get('id') and not (values.get('building_name') and values.get('room_no') is not None):
        values['house_id'] = resolved_house['id']
    missing = []
    if not values.get('visitor_name'):
        missing.append('visitor')
    if not (values.get('person_name') or values.get('host_person_id')):
        missing.append('host_person')
    if not (values.get('house_id') or (values.get('building_name') and values.get('room_no') is not None)):
        missing.append('house')
    if not values.get('phone'):
        missing.append('phone')
    if not values.get('expected_at'):
        missing.append('expected_at')
    if not values.get('purpose'):
        missing.append('purpose')
    candidates = _candidates('visitor.create', set(authorized_commands or ()))
    if (context or {}).get('person_candidates', 0) > 1 and values.get('person_name'):
        return {
            'action': 'DISAMBIGUATE', 'intent': 'visitor.create', 'candidates': candidates,
            'missing_fields': ['host_person'], 'entity_status': 'AMBIGUOUS', 'arguments': values,
        }
    if missing:
        return {
            'action': 'CLARIFY', 'intent': 'visitor.create', 'candidates': candidates,
            'missing_fields': missing, 'entity_status': 'MISSING', 'arguments': values,
        }
    return {
        'action': 'TOOL', 'intent': 'visitor.create', 'candidates': candidates,
        'missing_fields': [], 'entity_status': 'RESOLVE_MULTI', 'arguments': values,
    }


def _clarification_text(result):
    action = result.get('action')
    intent = result.get('intent') or ''
    missing = list(result.get('missing_fields') or [])
    if action == 'DISAMBIGUATE':
        if intent == 'visitor.create' and 'host_person' in missing:
            return '我找到了多位同名住户。请补充住户的联系电话或更具体的房屋信息，我再继续登记访客。'
        if intent.startswith('person.') or 'person' in missing:
            return '我找到了多位可能的人员。请补一个能区分的信息，例如联系电话。'
        if intent.startswith('order.'):
            return '我找到了多张可能的工单。请告诉我工单号，或补充房号/报修内容来确认是哪一张。'
        return '我找到了多个匹配对象。请补充一个能区分它们的信息，例如姓名、联系电话、业务编号或房号。'
    slot = missing[0] if missing else None
    if slot == 'community_id':
        return '这项操作要在哪个小区办理？直接告诉我小区名称即可。'
    if slot == 'building':
        return '具体是哪一栋？直接说“3栋”或“A栋”即可。'
    if slot in {'house', 'unit'}:
        return '具体是哪套房？请告诉我楼栋和房号；如果同栋有多个单元，再补充单元。'
    if slot == 'order':
        return '你指哪张工单？可以直接说工单号；如果就是刚才那张，也可以说“刚才那张”。'
    if slot in {'repairer', 'assignee'}:
        return '要交给哪位工作人员处理？直接说姓名即可；同名时我再请你补充区分信息。'
    if slot == 'person':
        return '你指哪位人员？直接说姓名即可；同名时可以再补联系电话。'
    if slot == 'host_person':
        return '这位访客要找哪位住户？直接告诉我住户姓名即可。'
    if slot == 'phone':
        return '还缺访客的联系电话，请直接把手机号或联系电话发给我。' if intent == 'visitor.create' else '还缺一个联系电话，请直接把手机号或联系电话发给我。'
    if slot == 'expected_at':
        return '访客预计什么时候到？请带上日期范围，例如“明天下午两点”或“2026-09-09 14:00”。'
    if slot == 'purpose':
        return '这次来访的目的是什么？例如拜访、送货、维修或看房。'
    if slot == 'notice_title':
        return '这条公告的标题写什么？'
    if slot == 'notice_content':
        return '这条公告具体要通知什么内容？'
    if slot == 'notice':
        return '你要操作哪条公告？可以说公告标题，或说“刚才那条”。'
    if slot == 'complaint':
        return '你指哪条投诉？可以说投诉内容/住户，或说“刚才那条”。'
    if slot == 'visitor':
        return '访客叫什么名字？' if intent == 'visitor.create' else '你指哪位访客？可以说访客姓名，或说“刚才登记的那位”。'
    if slot == 'vehicle':
        return '你指哪辆车？直接告诉我车牌号即可。'
    if slot in {'space', 'parking_use'}:
        return '你指哪个车位或哪条停车关系？直接说车位编号或车牌即可。'
    if slot == 'device':
        return '你指哪台设备？直接告诉我设备编号或名称即可。'
    if slot == 'inspection':
        return '你指哪条巡检任务？可以说设备编号或巡检任务编号。'
    if slot == 'payment':
        return '要处理哪笔收款？可以说“上一笔”，也可以告诉我对应账单或收款记录。'
    if slot == 'bill':
        return '你指哪张账单？可以告诉我账单编号、房号或账期。'
    if slot == 'fee':
        return '要使用哪个收费项目？直接告诉我收费项目名称即可。'
    if slot == 'new_request_details':
        return '这次新的报修具体是什么问题？告诉我位置和故障情况即可，我不会重复提交上一条。'
    if slot in {'id', 'version', 'disambiguation'}:
        return '请再说明一下你要操作的具体业务对象，例如名称、编号、房号或“刚才那条”；内部记录 ID 和版本号不用你提供。'
    return '我还缺一项办理所需的信息。请补充你要操作的具体对象或业务条件，我会接着刚才的任务继续办理。'


def _use_local_clarification(result):
    if result.get('action') not in {'CLARIFY', 'DISAMBIGUATE'}:
        return result
    if result.get('entity_status') == 'REPEAT':
        return result
    shown = dict(result)
    shown['clarification_text'] = _clarification_text(result)
    return shown


def plan_request(message, authorized_commands, context=None):
    text = str(message or '').strip()
    context = context or {}
    result = _state_plan_request(text, authorized_commands, context)

    if result.get('intent') == 'unknown' and re.search(r'发布.*(?:维修|报修).*工单|发布.*工单', text):
        result = _state_plan_request(re.sub(r'^发布', '创建', text, count=1), authorized_commands, context)

    result = _repair_notice_content(text, result)
    result = _repair_payment_target(text, result, authorized_commands)
    result = _repair_device_code(result)
    result = _repair_order_cancel(text, result, authorized_commands)
    result = _smooth_context_resolution(text, result, authorized_commands)
    result = _repair_exact_community_followup(text, result, context, authorized_commands)
    result = _repair_visitor_create(text, result, context, authorized_commands)
    result = _use_local_clarification(result)
    # The state layer stored the core plan before semantic repairs. Persist the
    # repaired slots as the canonical pending plan so the next short answer can
    # continue exactly where this turn stopped.
    _store_pending(result)
    return result
