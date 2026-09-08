"""Deterministic financial slot parsing and read-query repair for the property-management Agent.

Financial writes are never allowed to invent fee items, billing dates, payment
channels, receipt references, mutation targets or audit reasons. This module
also repairs common operator-style read phrases after the legacy planner so
queries such as “查看收费项目” cannot be mistaken for mutations. It only
prepares user-supplied business facts for existing scoped resolvers and
PropertyService; it never grants permissions.
"""
from datetime import datetime, timedelta
import re


def _china_today():
    return (datetime.utcnow() + timedelta(hours=8)).date()


def _month_with_offset(offset=0):
    today = _china_today()
    index = today.year * 12 + today.month - 1 + offset
    year, month0 = divmod(index, 12)
    return f"{year:04d}-{month0 + 1:02d}"


def parse_period(text):
    value = str(text or '')
    explicit = re.search(r'(20\d{2})[-/年](0?[1-9]|1[0-2])(?:月)?', value)
    if explicit:
        return f"{int(explicit.group(1)):04d}-{int(explicit.group(2)):02d}"
    if re.search(r'上个?月', value):
        return _month_with_offset(-1)
    if re.search(r'下个?月', value):
        return _month_with_offset(1)
    if re.search(r'本月|这个月|当月', value):
        return _month_with_offset(0)
    return None


def parse_due_date(text):
    value = str(text or '')
    matched = re.search(r'(20\d{2})[-/](\d{1,2})[-/](\d{1,2})', value)
    if not matched:
        return None
    candidate = f"{int(matched.group(1)):04d}-{int(matched.group(2)):02d}-{int(matched.group(3)):02d}"
    try:
        datetime.strptime(candidate, '%Y-%m-%d')
    except ValueError:
        return None
    return candidate


def parse_fee_name(text):
    value = str(text or '').strip()
    explicit = re.search(r'(?:收费项目|费用项目|计费项目)(?:是|为|：|:)?\s*([\u4e00-\u9fffA-Za-z0-9_-]{1,30})', value)
    if explicit:
        return explicit.group(1).strip()
    for name in ('物业费', '停车费', '车位费', '水费', '电费', '垃圾处理费', '管理费'):
        if name in value:
            return name
    return None


def parse_payment_channel(text):
    value = str(text or '')
    if re.search(r'银行|转账|网银', value):
        return 'bank'
    if '现金' in value:
        return 'cash'
    return None


def parse_payment_reference(text):
    value = str(text or '')
    matched = re.search(r'(?:收据号|流水号|银行流水|凭证号|参考号)(?:是|为|：|:)?\s*([A-Za-z0-9._/-]{2,100})', value, re.I)
    return matched.group(1).strip() if matched else None


def parse_financial_reason(text, intent, followup=False):
    """Extract an operator-supplied audit reason; never ask the model to invent it."""
    value = str(text or '').strip()
    explicit = re.search(r'(?:原因|理由|因为)(?:是|为|：|:)?\s*([^，,。；;]{2,300})', value)
    if explicit:
        return explicit.group(1).strip()
    action = r'作废' if intent == 'bill.void' else r'(?:冲销|冲正|撤回)'
    suffix = re.search(action + r'[^，,。；;]{0,50}[，,；;]\s*([^。；;]{2,300})', value)
    if suffix:
        return suffix.group(1).strip()
    if followup and 2 <= len(value) <= 300 and not re.search(r'作废|冲销|冲正|撤回|确认|执行|直接', value):
        return value
    return None


def parse_unpaid_person_name(text):
    """Extract an explicit resident/person name from unpaid-billing questions."""
    value = str(text or '').strip()
    patterns = (
        r'(?:查询|查一下|查下|看看|看下|查)\s*([\u4e00-\u9fff]{2,4})(?=(?:有没有|是否|有无|还有没有).{0,8}(?:欠费|未缴|未交))',
        r'([\u4e00-\u9fff]{2,4})(?=(?:有没有|是否|有无|还有没有).{0,8}(?:欠费|未缴|未交))',
    )
    for pattern in patterns:
        matched = re.search(pattern, value)
        if matched:
            return matched.group(1)
    return None


_READ_WORDS = re.compile(r'查询|查看|看看|查一下|查下|看下|详情|状态|记录|列表|有哪些|有什么|历史')


def _read_result(intent, authorized, values):
    if intent not in authorized:
        return {
            'action': 'DENY', 'intent': intent, 'candidates': [],
            'missing_fields': [], 'entity_status': 'FORBIDDEN', 'arguments': values,
        }
    return {
        'action': 'TOOL', 'intent': intent, 'candidates': [intent],
        'missing_fields': [], 'entity_status': 'RESOLVED', 'arguments': values,
    }


def repair_read_plan(text, result, authorized_commands):
    """Repair common read phrases without changing RBAC/DataScope semantics."""
    value = str(text or '').strip()
    if not value or not _READ_WORDS.search(value):
        return result
    if result.get('intent') == 'security_boundary' or result.get('action') == 'DENY':
        return result
    if re.search(r'新增|新建|创建|登记|修改|更新|作废|冲销|冲正|撤回|发布|删除|归档|分派|派给|分给', value):
        return result

    authorized = set(authorized_commands or ())
    values = dict(result.get('arguments') or {})

    if re.search(r'投诉(?:单|记录|详情|状态)|投诉\s*#?\s*\d+', value):
        matched = re.search(r'投诉(?:单|记录)?\s*#?\s*([1-9]\d{0,8})', value)
        if matched:
            values['id'] = int(matched.group(1))
            values['complaint_id'] = int(matched.group(1))
        return _read_result('complaint.search', authorized, values)

    if re.search(r'访客|来访', value):
        matched = re.search(r'(?:访客|来访)(?:记录)?\s*#?\s*([1-9]\d{0,8})', value)
        if matched:
            values['id'] = int(matched.group(1))
        named = re.search(r'(?:访客|来访人)(?:姓名)?\s*([\u4e00-\u9fff]{2,4})', value)
        if named:
            values['name'] = named.group(1)
        return _read_result('visitor.search', authorized, values)

    if re.search(r'巡检(?:任务|记录|单|详情|状态)', value):
        matched = re.search(r'巡检(?:任务|记录|单)?\s*#?\s*([1-9]\d{0,8})', value)
        if matched:
            values['id'] = int(matched.group(1))
        return _read_result('inspection.search', authorized, values)

    if re.search(r'收款(?:记录|单|详情|状态)|支付记录', value):
        matched = re.search(r'(?:收款(?:记录|单)?|支付记录)\s*#?\s*([1-9]\d{0,8})', value)
        if matched:
            values['id'] = int(matched.group(1))
            values['payment_id'] = int(matched.group(1))
        bill = re.search(r'账单\s*#?\s*([1-9]\d{0,8})', value)
        if bill:
            values['bill_id'] = int(bill.group(1))
        return _read_result('payment.search', authorized, values)

    if re.search(r'收费项目|收费标准|计费项目|费用项目', value):
        matched = re.search(r'(?:收费项目|计费项目|费用项目)\s*#?\s*([1-9]\d{0,8})', value)
        if matched:
            values['id'] = int(matched.group(1))
            values['fee_item_id'] = int(matched.group(1))
        return _read_result('fee.search', authorized, values)

    if re.search(r'车辆|车牌', value) and not re.search(r'停在哪|停车位置|车位', value):
        plate = re.search(r'[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼][A-Z][A-Z0-9]{5}', value, re.I)
        if plate:
            values['plate'] = plate.group(0).upper()
        return _read_result('vehicle.search', authorized, values)

    if re.search(r'车位使用|停车使用|占用关系', value):
        matched = re.search(r'(?:车位使用|停车使用|占用关系)(?:记录)?\s*#?\s*([1-9]\d{0,8})', value)
        if matched:
            values['id'] = int(matched.group(1))
        return _read_result('parking_use.search', authorized, values)

    return result


def _question(slot, intent):
    if slot == 'building':
        return '要给哪一栋生成账单？请直接告诉我楼栋，例如“23栋”。'
    if slot == 'house':
        return '要给哪套房生成账单？请告诉我楼栋和房号；如果同栋有多个单元，再补充单元。'
    if slot == 'fee':
        return '要使用哪个收费项目？请说收费项目名称，例如“物业费”。我不会自动选择第一条收费项目。'
    if slot == 'period':
        return '这批账单属于哪个账期？可以说“本月”“下个月”或“2026-09”。'
    if slot == 'due_date':
        return '账单到期日是哪一天？请给出完整日期，例如“2026-09-30”。我不会自动按30天后推算。'
    if slot == 'bill':
        return '要处理哪张账单？请告诉我账单编号，例如“账单123”。'
    if slot == 'payment':
        return '要冲销哪笔收款？可以告诉我收款记录编号；如果确实是最近一笔，也可以说“上一笔”。'
    if slot == 'amount':
        return '实际核验收到多少钱？请给出明确金额，例如“500元”。'
    if slot == 'channel':
        return '这笔已核验收款的渠道是什么？目前请明确说“现金”或“银行转账”。'
    if slot == 'reference':
        return '还缺已核验的收据号或银行流水号，例如“收据号 CASH-001”或“流水号 BANK-001”。'
    if slot == 'reason':
        verb = '作废账单' if intent == 'bill.void' else '冲销收款'
        return f'请补充这次{verb}的业务原因，例如“重复出账”或“重复入账”。原因会原样写入审计记录。'
    return '还缺一项财务业务信息，请补充后我再生成确认单。'


def _result(intent, action, candidates, values, missing=None, status='RESOLVED'):
    result = {
        'action': action,
        'intent': intent,
        'candidates': list(candidates),
        'missing_fields': list(missing or ()),
        'entity_status': status,
        'arguments': values,
    }
    if missing:
        result['clarification_text'] = _question(missing[0], intent)
    return result


def repair_financial_plan(text, result, authorized_commands):
    """Turn financial writes into explicit-slot, server-resolved plans."""
    result = repair_read_plan(text, result, authorized_commands)
    authorized = set(authorized_commands or ())
    intent = result.get('intent')

    unpaid_question = bool(re.search(
        r'(?:有没有|是否|有无|还有没有).{0,12}(?:欠费|未缴|未交)|'
        r'(?:欠费|未缴|未交).{0,8}(?:多少|吗|情况)|'
        r'(?:本月|这个月|当月).{0,12}物业费.{0,8}(?:没收|未收)',
        str(text or ''),
    ))
    if intent == 'unknown' and unpaid_question and 'billing.unpaid' in authorized:
        result = _read_result('billing.unpaid', authorized, dict(result.get('arguments') or {}))
        intent = 'billing.unpaid'

    if intent == 'billing.unpaid':
        values = dict(result.get('arguments') or {})
        person_name = parse_unpaid_person_name(text)
        period = parse_period(text)
        if person_name:
            values['person_name'] = person_name
        if period:
            values['month'] = period
        repaired = dict(result)
        repaired['arguments'] = values
        return repaired

    financial_intents = {'bill.create', 'bill.batch', 'bill.void', 'payment.record', 'payment.reverse'}
    if intent not in financial_intents:
        return result
    import dify_financial_patch  # noqa: F401

    values = dict(result.get('arguments') or {})

    period = parse_period(text)
    due_date = parse_due_date(text)
    fee_name = parse_fee_name(text)
    channel = parse_payment_channel(text)
    reference = parse_payment_reference(text)
    reason = parse_financial_reason(text, intent, 'reason' in set(result.get('missing_fields') or ()))
    if period:
        values['period'] = period
    if due_date:
        values['due_date'] = due_date
    if fee_name:
        values['fee_name'] = fee_name
    if channel:
        values['channel'] = channel
    if reference:
        values['reference'] = reference
    if reason:
        values['reason'] = reason

    batch_words = re.search(r'批量|整栋|全栋|整幢|全楼|整楼|所有房|全部房|这一栋全部|这栋全部', str(text or ''))
    if (
        intent == 'bill.batch'
        and values.get('building_name')
        and values.get('room_no') is not None
        and not batch_words
        and 'bill.create' in authorized
    ):
        intent = 'bill.create'

    for key in ('fee_item_id', 'building_id', 'house_id', 'version'):
        values.pop(key, None)

    if intent == 'bill.create':
        required = ('house.search', 'fee.search', 'bill.create')
        missing = []
        if not (values.get('building_name') and values.get('room_no') is not None):
            missing.append('house')
        if not values.get('fee_name'):
            missing.append('fee')
        if not values.get('period'):
            missing.append('period')
        if not values.get('due_date'):
            missing.append('due_date')
        candidates = [item for item in required if item in authorized]
        if missing:
            return _result(intent, 'CLARIFY', candidates, values, missing, 'MISSING')
        if not all(item in authorized for item in required):
            return _result(intent, 'DENY', candidates, values, status='FORBIDDEN')
        return _result(intent, 'CONFIRM', required, values, status='RESOLVE_MULTI')

    if intent == 'bill.batch':
        required = ('building.search', 'fee.search', 'bill.batch')
        missing = []
        if not values.get('building_name'):
            missing.append('building')
        if not values.get('fee_name'):
            missing.append('fee')
        if not values.get('period'):
            missing.append('period')
        if not values.get('due_date'):
            missing.append('due_date')
        candidates = [item for item in required if item in authorized]
        if missing:
            return _result(intent, 'CLARIFY', candidates, values, missing, 'MISSING')
        if not all(item in authorized for item in required):
            return _result(intent, 'DENY', candidates, values, status='FORBIDDEN')
        return _result(intent, 'CONFIRM', required, values, status='RESOLVE_MULTI')

    if intent == 'bill.void':
        missing = []
        if not values.get('bill_id'):
            missing.append('bill')
        if not values.get('reason'):
            missing.append('reason')
        candidates = ['bill.void'] if 'bill.void' in authorized else []
        if missing:
            return _result(intent, 'CLARIFY', candidates, values, missing, 'MISSING')
        if 'bill.void' not in authorized:
            return _result(intent, 'DENY', candidates, values, status='FORBIDDEN')
        return _result(intent, 'CONFIRM', candidates, values, status='SERVER_OWNED')

    if intent == 'payment.record':
        missing = []
        if not values.get('bill_id'):
            missing.append('bill')
        if values.get('amount') in (None, ''):
            missing.append('amount')
        if not values.get('channel'):
            missing.append('channel')
        if not values.get('reference'):
            missing.append('reference')
        candidates = ['payment.record'] if 'payment.record' in authorized else []
        if missing:
            return _result(intent, 'CLARIFY', candidates, values, missing, 'MISSING')
        if 'payment.record' not in authorized:
            return _result(intent, 'DENY', candidates, values, status='FORBIDDEN')
        return _result(intent, 'CONFIRM', candidates, values, status='SERVER_OWNED')

    explicit_payment = values.get('payment_id') or values.get('id')
    latest = values.get('_resolve_strategy') == 'latest'
    missing = []
    if not explicit_payment and not latest:
        missing.append('payment')
    if not values.get('reason'):
        missing.append('reason')
    if missing:
        candidates = [item for item in ('payment.search', 'payment.reverse') if item in authorized]
        return _result(intent, 'CLARIFY', candidates, values, missing, 'MISSING')
    if 'payment.reverse' not in authorized:
        return _result(intent, 'DENY', [], values, status='FORBIDDEN')
    if explicit_payment:
        values['payment_id'] = int(explicit_payment)
        values.pop('id', None)
        return _result(intent, 'CONFIRM', ['payment.reverse'], values, status='SERVER_OWNED')
    required = ('payment.search', 'payment.reverse')
    if not all(item in authorized for item in required):
        return _result(intent, 'DENY', [item for item in required if item in authorized], values, status='FORBIDDEN')
    return _result(intent, 'CONFIRM', required, values, status='RESOLVE_FIRST')