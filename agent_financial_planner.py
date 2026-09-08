"""Deterministic financial slot parsing for the property-management Agent.

Financial writes are never allowed to invent fee items, billing dates, payment
channels or receipt references. This module only prepares business facts for
existing scoped resolvers and PropertyService; it never grants permissions.
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
        return '要登记哪张账单的收款？请告诉我账单编号，例如“账单123”。'
    if slot == 'amount':
        return '实际核验收到多少钱？请给出明确金额，例如“500元”。'
    if slot == 'channel':
        return '这笔已核验收款的渠道是什么？目前请明确说“现金”或“银行转账”。'
    if slot == 'reference':
        return '还缺已核验的收据号或银行流水号，例如“收据号 CASH-001”或“流水号 BANK-001”。'
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
    intent = result.get('intent')
    if intent not in {'bill.create', 'bill.batch', 'payment.record'}:
        return result
    # Load the direct-provider extensions only on financial turns. Importing the
    # module mutates only the resolver tables/functions used by dify_client.
    import dify_financial_patch  # noqa: F401

    values = dict(result.get('arguments') or {})
    authorized = set(authorized_commands or ())

    period = parse_period(text)
    due_date = parse_due_date(text)
    fee_name = parse_fee_name(text)
    channel = parse_payment_channel(text)
    reference = parse_payment_reference(text)
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

    # A common spoken form is “给A栋101生成物业费账单”. The legacy detector sees
    # “栋 ... 生成” and can classify it as batch billing before it reaches the
    # single-house pattern. If a concrete room is present and the user did not
    # explicitly ask for a batch/whole-building operation, this is one bill.
    batch_words = re.search(r'批量|整栋|全栋|整幢|全楼|整楼|所有房|全部房|这一栋全部|这栋全部', str(text or ''))
    if (
        intent == 'bill.batch'
        and values.get('building_name')
        and values.get('room_no') is not None
        and not batch_words
        and 'bill.create' in authorized
    ):
        intent = 'bill.create'

    # Never accept model/internal identifiers for fee/building/house resolution.
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

    # payment.record: the user-visible bill number is explicit business input.
    # The server still reloads it through Policy and takes its current version.
    required = ('payment.record',)
    missing = []
    if not values.get('bill_id'):
        missing.append('bill')
    if values.get('amount') in (None, ''):
        missing.append('amount')
    if not values.get('channel'):
        missing.append('channel')
    if not values.get('reference'):
        missing.append('reference')
    candidates = [item for item in required if item in authorized]
    if missing:
        return _result(intent, 'CLARIFY', candidates, values, missing, 'MISSING')
    if 'payment.record' not in authorized:
        return _result(intent, 'DENY', candidates, values, status='FORBIDDEN')
    return _result(intent, 'CONFIRM', required, values, status='SERVER_OWNED')
