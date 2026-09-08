"""Deterministic intent/entity planner for the property-management Agent.

The planner narrows model choices and identifies missing/ambiguous business
entities. It never grants authority: RBAC, DataScope, state transitions and
risk enforcement remain server-side.
"""
import re

_CN_DIGITS = {'零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}

READ_COMMANDS = {
    'house.search', 'building.search', 'unit.search', 'person.search', 'person.properties',
    'order.search', 'order.pending', 'complaint.search', 'complaint.stats', 'visitor.search',
    'vehicle.search', 'parking.search', 'device.search', 'inspection.search', 'fee.search',
    'payment.search', 'billing.unpaid', 'notice.read', 'whoami'
}

CONFIRM_INTENTS = {
    'property.archive', 'house.ownership', 'order.cancel', 'complaint.close',
    'notice.batch_publish', 'notice.archive', 'parking.release', 'device.archive',
    'fee.save', 'bill.create', 'bill.batch', 'bill.void', 'payment.record', 'payment.reverse'
}

RESOLVER_CANDIDATES = {
    'house.save': ['building.search', 'unit.search', 'house.save'],
    'unit.save': ['building.search', 'unit.save'],
    'house.ownership': ['house.search', 'house.ownership'],
    'property.archive': ['building.search', 'unit.search', 'house.search', 'property.archive'],
    'person.save': ['person.search', 'person.save'],
    'lease.checkout': ['person.search', 'person.properties', 'lease.checkout'],
    'lease.create': ['person.search', 'house.search', 'lease.create'],
    'relation.end': ['person.search', 'person.properties', 'relation.end'],
    'order.assign': ['order.search', 'person.search', 'order.assign'],
    'order.accept': ['order.search', 'order.accept'],
    'order.progress': ['order.search', 'order.progress'],
    'order.finish': ['order.search', 'order.finish'],
    'order.reopen': ['order.search', 'order.reopen'],
    'order.close': ['order.search', 'order.close'],
    'order.cancel': ['order.search', 'order.cancel'],
    'complaint.create': ['house.search', 'complaint.create'],
    'complaint.assign': ['complaint.search', 'person.search', 'complaint.assign'],
    'complaint.resolve': ['complaint.search', 'complaint.resolve'],
    'complaint.close': ['complaint.search', 'complaint.close'],
    'visitor.create': ['person.search', 'house.search', 'visitor.create'],
    'visitor.checkin': ['visitor.search', 'visitor.checkin'],
    'visitor.checkout': ['visitor.search', 'visitor.checkout'],
    'visitor.cancel': ['visitor.search', 'visitor.cancel'],
    'vehicle.save': ['person.search', 'house.search', 'vehicle.search', 'vehicle.save'],
    'vehicle.archive': ['vehicle.search', 'vehicle.archive'],
    'parking.save': ['building.search', 'parking.search', 'parking.save'],
    'parking.assign': ['parking.search', 'vehicle.search', 'parking.assign'],
    'parking.release': ['parking.search', 'vehicle.search', 'parking.release'],
    'device.save': ['building.search', 'device.search', 'device.save'],
    'device.archive': ['device.search', 'device.archive'],
    'inspection.create': ['device.search', 'person.search', 'inspection.create'],
    'inspection.complete': ['inspection.search', 'device.search', 'inspection.complete'],
    'fee.save': ['fee.search', 'fee.save'],
    'bill.create': ['house.search', 'fee.search', 'bill.create'],
    'bill.batch': ['building.search', 'fee.search', 'bill.batch'],
    'bill.void': ['billing.unpaid', 'bill.void'],
    'payment.record': ['billing.unpaid', 'payment.record'],
    'payment.reverse': ['payment.search', 'payment.reverse']
}


def building_name_variants(name):
    """Return common persisted forms for a spoken building name."""
    if not isinstance(name, str) or not name.strip():
        return set()
    value = name.strip()
    variants = {value, value.removesuffix('栋'), value.removesuffix('号楼')}
    core = re.sub(r'(?:栋|号楼)$', '', value)
    if core.isdigit():
        number = int(core)
    elif core and all(char in _CN_DIGITS or char == '十' for char in core):
        if core == '十':
            number = 10
        elif '十' in core:
            left, _, right = core.partition('十')
            number = (_CN_DIGITS.get(left, 1) * 10 if left else 10) + (_CN_DIGITS.get(right, 0) if right else 0)
        else:
            number = _CN_DIGITS.get(core)
    else:
        number = None
    if number is not None:
        variants.update({str(number), f'{number}栋', f'{number}号楼'})
    return {item for item in variants if item}


def _public_notice(text):
    explicit = '公告' in text
    audience = bool(re.search(r'全区|全小区|全园区|所有(?:业主|住户|居民)|全体(?:业主|住户|居民)|大家|全楼|全栋', text))
    public_event = bool(re.search(r'停水|停电|消防演练|电梯(?:检修|维修)|清洗水箱|高空抛物|门岗值守', text))
    generic_notice = bool(re.search(r'(?:发|发布|通知|提醒).*(?:通知|公告)|发个通知|发一条通知', text))
    named_person = bool(re.search(r'(?:通知|提醒)\s*[\u4e00-\u9fff]{2,4}(?:去|来|处理|缴费|交费)', text))
    return (explicit or audience or public_event or generic_notice) and not named_person


def _detect_intent(text):
    if re.search(r'撤掉|撤下|撤回|删除', text) and '公告' in text:
        return 'notice.archive'
    if re.search(r'负责的所有小区|所有负责小区|所有小区', text) and _public_notice(text) and re.search(r'发|发布|通知', text):
        return 'notice.batch_publish'
    if re.search(r'最近|查询|查看|看看|查一下|查下|有哪些|有什么|历史', text) and re.search(r'公告|通知', text):
        return 'notice.read'
    if _public_notice(text) and (re.search(r'发布|发到|发个|发一条|通知|提醒|修改公告|改公告|公告.*改', text) or ('标题' in text and '内容' in text)):
        return 'notice.save'

    if re.search(r'派给|派单|分配维修|分给.*(?:师傅|维修)', text):
        return 'order.assign'
    if re.search(r'我接单|接单了|开始维修', text):
        return 'order.accept'
    if re.search(r'记录.*进度|维修进度|记一下进展|更新进展', text):
        return 'order.progress'
    if re.search(r'提交完工|已经.*(?:修好|试水正常)|完工', text):
        return 'order.finish'
    if re.search(r'返修|还是漏水|仍然漏水', text):
        return 'order.reopen'
    if re.search(r'验收通过|确认验收|关闭工单|关闭这个工单', text):
        return 'order.close'
    if re.search(r'撤销.*(?:报修|工单)|取消.*(?:报修|工单)', text):
        return 'order.cancel'
    if re.search(r'到哪一步|谁负责.*维修单|查看工单|看看工单|我的工单|工单有哪些', text):
        return 'order.search'
    if re.search(r'报修|漏水|坏了|叫师傅|(?:新增|新建|建个|创建|登记).*(?:维修|工单)|公共区域.*(?:坏|故障)', text):
        return 'order.create'

    if re.search(r'产权.*(?:改|变|共有|独有)', text):
        return 'house.ownership'
    if re.search(r'归档.*(?:楼栋|单元|房屋|房子)', text):
        return 'property.archive'
    if re.search(r'(?:建|新增|创建).*[0-9一二三四五六七八九十百]+单元', text):
        return 'unit.save'
    if re.search(r'(?:登记|新增|创建).*(?:栋|楼).*(?:室|房)', text) or re.search(r'(?:房子|房屋).*(?:标成|改成).*(?:空置|自住|出租)', text):
        return 'house.save'
    if (re.search(r'当前绑定情况', text) or re.search(r'(?:查询|查|看看|瞅|瞧|看下|查下|有没有).*(?:栋|楼|房|室|谁住|住户信息|房屋信息)', text)) and not re.search(r'账单|物业费|欠费', text):
        return 'house.search'
    if re.search(r'(?:绑定|登记成).*(?:业主|住户)|(?:栋|楼).*\d{2,4}.*绑定|绑定.*(?:栋|楼).*\d{2,4}|给[\u4e00-\u9fff]{2,4}绑定', text):
        return 'relation.bind_by_name'

    if re.search(r'尾号\d{3,4}', text):
        return 'person.lookup'
    if re.search(r'房产有哪些|名下.*房', text):
        return 'person.properties'
    if re.search(r'已经搬走|搬走了|退租|退房|不住了|租约结束', text):
        return 'lease.checkout'
    if re.search(r'登记租户入住|租户入住|办理入住', text):
        return 'lease.create'
    if re.search(r'撤销.*房屋.*关系|解除.*房屋.*关系|结束.*关系', text):
        return 'relation.end'
    if re.search(r'新增住户|新增人员|登记人员|新建人员|手机号.*(?:改|换)|修改手机号|联系方式改|电话换', text):
        return 'person.save'
    if re.search(r'找一下|查手机号|找人|查找人员|的电话', text):
        return 'person.search'

    if re.search(r'回访.*结案|确认后结案|投诉.*结案', text):
        return 'complaint.close'
    if re.search(r'投诉处理结果|处理结果是|已上门整改', text):
        return 'complaint.resolve'
    if re.search(r'投诉.*分给|把.*投诉分给|分派投诉', text):
        return 'complaint.assign'
    if re.search(r'登记投诉|投诉.*(?:太吵|异响|服务|噪音)|(?:栋|楼).*投诉', text):
        return 'complaint.create'
    if re.search(r'投诉.*按楼栋统计|投诉统计', text):
        return 'complaint.stats'

    if re.search(r'查.*(?:今天|当前).*(?:访客|来访)', text):
        return 'visitor.search'
    if re.search(r'访客.*(?:进入|进去|可以进)|确认.*访客.*进入', text):
        return 'visitor.checkin'
    if re.search(r'访客.*离开|客人.*离开', text):
        return 'visitor.checkout'
    if re.search(r'取消.*(?:来访|访客)', text):
        return 'visitor.cancel'
    if re.search(r'登记访客|登记.*客人|门岗登记.*客人', text):
        return 'visitor.create'

    if re.search(r'车牌.*(?:停在哪|停车位置)', text):
        return 'parking.search'
    if re.search(r'查.*空车位|还有哪些空车位', text):
        return 'parking.search'
    if re.search(r'结束.*车位使用|释放.*车位|解除.*车位', text):
        return 'parking.release'
    if re.search(r'车位.*分给|把.*车位分给|安排停车位', text):
        return 'parking.assign'
    if re.search(r'登记[A-Za-z]+-\d+车位|新增.*车位|登记.*车位', text):
        return 'parking.save'
    if re.search(r'归档.*车|车辆.*归档|车.*归档', text):
        return 'vehicle.archive'
    if re.search(r'登记车牌|登记.*新能源车|登记一辆.*车', text):
        return 'vehicle.save'

    if re.search(r'查.*设备.*状态|设备.*状态.*查', text):
        return 'device.search'
    if re.search(r'归档.*设备|报废.*设备', text):
        return 'device.archive'
    if re.search(r'安排巡检|创建.*巡检任务|新建.*巡检任务', text):
        return 'inspection.create'
    if re.search(r'提交巡检|巡检结果|记为正常|巡检发现故障', text):
        return 'inspection.complete'
    if re.search(r'登记.*设备|新增.*设备|设备.*标记为|设备.*改成', text):
        return 'device.save'

    if re.search(r'撤回上一笔收款|撤回收款|冲销|冲正', text):
        return 'payment.reverse'
    if re.search(r'账单\s*#?\s*\d+.*作废|把账单.*作废', text):
        return 'bill.void'
    if re.search(r'已收款|登记.*收款|收到.*元', text):
        return 'payment.record'
    if re.search(r'新增.*物业费项目|收费项目', text):
        return 'fee.save'
    if re.search(r'(?:楼栋|栋|负责楼栋).*(?:批量出账|生成.*物业费账单|批量算.*物业费)|批量(?:生成|出)账|批量算.*物业费', text):
        return 'bill.batch'
    if re.search(r'生成.*(?:房|室|栋\d{2,4}|[A-Za-z]\s*栋\s*\d{2,4}).*物业费账单', text):
        return 'bill.create'
    if re.search(r'生成.*物业费账单', text):
        return 'bill.batch'
    if re.search(r'欠费|没收|未收|查询.*账单|住户账单|物业费.*还有多少', text):
        return 'billing.unpaid'
    if re.search(r'当前是什么岗位|什么岗位|我的权限|我是谁', text):
        return 'whoami'
    return None


def _notice_entities(message):
    values = {}
    community = re.search(r'(?:发到|给|在|向)?\s*([\u4e00-\u9fffA-Za-z0-9]{1,20}(?:小区|花园|社区|园区))', message)
    building = re.search(r'([A-Za-z0-9一二三四五六七八九十百]+)\s*(?:栋|号楼)', message)
    if community:
        name = community.group(1).strip()
        if not any(token in name for token in ('全小区', '所有小区', '负责的所有小区', '所有负责小区')):
            values['community_name'] = name
    if building:
        values['building_name'] = building.group(1) + '栋'
        values['notice_scope'] = 'building'
    else:
        values['notice_scope'] = 'community'
    title = re.search(r'标题\s*(?:是|为|：|:)\s*([^，,。；;]+)', message)
    content = re.search(r'内容\s*(?:是|为|：|:)\s*(.+)$', message)
    if title:
        values['notice_title'] = title.group(1).strip()
    if content:
        values['notice_content'] = content.group(1).strip(' \t，,。；;')
    if 'notice_content' not in values:
        changed = re.search(r'改成\s*(.+)$', message)
        if changed:
            values['notice_content'] = changed.group(1).strip(' \t，,。；;')
        else:
            colon = re.search(r'[：:]\s*(.+)$', message)
            if colon:
                values['notice_content'] = colon.group(1).strip(' \t，,。；;')
            else:
                spoken = re.search(r'(?:通知|提醒)(?:一下|所有业主|所有住户|大家)?[，,:：\s]*(.+)$', message)
                if spoken:
                    values['notice_content'] = spoken.group(1).strip(' \t，,。；;')
                else:
                    comma = re.search(r'[，,]\s*(.+)$', message)
                    compact = comma.group(1).strip() if comma else message.strip()
                    compact = re.sub(r'^(?:帮我|请|麻烦)?\s*(?:发布|发个|发一条)\s*(?:一个|一条)?\s*(?:全区|全小区|全园区)?\s*', '', compact)
                    compact = re.sub(r'(?:公告|通知)$', '', compact).strip('，,。；; ')
                    if compact and compact not in {'发布', '公告', '通知'}:
                        values['notice_content'] = compact
    if 'notice_title' not in values and values.get('notice_content'):
        content_text = values['notice_content']
        keyword = next((word for word in ('停水', '停电', '电梯检修', '电梯', '检修', '高空抛物') if word in content_text), '物业')
        values['notice_title'] = keyword + ('提醒' if keyword == '高空抛物' else '公告')
    return values


def _entities(message):
    result = {}
    building = re.search(r'([A-Za-z0-9一二三四五六七八九十百]+)\s*(?:栋|号楼)', message)
    unit = re.search(r'([0-9一二三四五六七八九十百]+)\s*单元', message)
    room = re.search(r'(?:单元\s*)?([0-9]{2,4})\s*(?:房|室)', message)
    if not room:
        room = re.search(r'(?:栋|号楼)\s*([0-9]{2,4})(?!\d)', message)
    phone = re.search(r'1[3-9]\d{9}', message)
    plate = re.search(r'[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼][A-Z][A-Z0-9]{5}', message, re.I)
    order_no = re.search(r'WO[-A-Za-z0-9]+', message, re.I)
    space_code = re.search(r'(?<![A-Za-z0-9])([A-Za-z]+-\d{1,8})(?![A-Za-z0-9])', message)
    bill_id = re.search(r'账单\s*#?\s*([1-9][0-9]{0,8})', message)
    notice_id = re.search(r'(?:公告|通知)\s*#?\s*([1-9][0-9]{0,8})', message)
    amount = re.search(r'(?:收款|收到|收了|已收款)\s*(?:人民币|现金)?\s*([0-9]+(?:\.[0-9]{1,2})?)\s*(?:元|块)?', message)
    area = re.search(r'面积\s*([0-9]+(?:\.[0-9]{1,2})?)\s*(?:平|平方米|㎡)', message)
    device_code = re.search(r'(?:编号|设备|给)\s*([A-Za-z]+-\d{1,8})', message)
    if not device_code and re.search(r'设备|巡检', message):
        device_code = re.search(r'(?<![A-Za-z0-9])([A-Za-z]+-\d{1,8})(?![A-Za-z0-9])', message)
    person_patterns = (
        r'新增住户\s*([\u4e00-\u9fff]{2,4})', r'车主\s*([\u4e00-\u9fff]{2,4})',
        r'来找\s*([\u4e00-\u9fff]{2,4})', r'给\s*([\u4e00-\u9fff]{2,4})\s*(?:绑定|登记租户|登记一辆)',
        r'绑定\s*([\u4e00-\u9fff]{2,4})(?:$|[，,。；;\s])',
        r'([\u4e00-\u9fff]{2,4}?)(?=的手机号|手机号|已经搬走|搬走了|的房产)',
        r'找(?:一下)?\s*([\u4e00-\u9fff]{2,4})(?:的电话)?'
    )
    person_name = None
    for pattern in person_patterns:
        matched = re.search(pattern, message)
        if matched:
            person_name = matched.group(1)
            break
    repairer = re.search(r'派给\s*([\u4e00-\u9fff]{2,8})', message)
    visitor = re.search(r'(?:登记访客|访客)\s*([\u4e00-\u9fff]{2,4})', message)
    if building:
        result['building_name'] = building.group(1) + '栋'
    if unit:
        result['unit'] = unit.group(1)
    if room:
        result['room_no'] = int(room.group(1))
    if phone:
        result['phone'] = phone.group(0)
    if plate:
        result['plate'] = plate.group(0).upper()
    if order_no:
        result['order_no'] = order_no.group(0)
    if space_code and re.search(r'车位|停车', message):
        result['space_code'] = space_code.group(1).upper()
    if bill_id:
        result['bill_id'] = int(bill_id.group(1))
    if notice_id:
        result['notice_id'] = int(notice_id.group(1))
    if amount:
        result['amount'] = float(amount.group(1))
    if area:
        result['area'] = float(area.group(1))
    if device_code:
        result['device_code'] = device_code.group(1).upper()
        result['code'] = device_code.group(1).upper()
    if person_name and person_name not in {'业主', '住户', '人员', '客服'}:
        result['person_name'] = person_name
    if repairer:
        result['repairer_name'] = repairer.group(1).strip()
    if visitor:
        result['visitor_name'] = visitor.group(1)
    if '按面积' in message:
        result['basis'] = 'area'
    if '共有' in message:
        result['ownership'] = 'shared'
    if '空置' in message:
        result['occupancy'] = 'vacant'
    status_map = {'正常': 'normal', '故障': 'fault', '维护中': 'maintenance', '报废': 'retired'}
    for chinese, canonical in status_map.items():
        if chinese in message:
            result['status'] = canonical
            break
    return result


def _candidates(intent, authorized):
    raw = RESOLVER_CANDIDATES.get(intent, [intent])
    candidates = [item for item in raw if item in authorized]
    if intent in authorized and intent not in candidates:
        candidates.append(intent)
    return candidates or ([intent] if intent in authorized else [])


def _result(action, intent, candidates=None, missing=None, status='RESOLVED', arguments=None):
    return {
        'action': action,
        'intent': intent,
        'candidates': list(candidates or []),
        'missing_fields': list(missing or []),
        'entity_status': status,
        'arguments': dict(arguments or {})
    }


def plan_request(message, authorized_commands, context=None):
    context = context or {}
    authorized = set(authorized_commands or ())
    text = str(message or '').strip()
    lower = text.lower()
    identity_mutation = re.search(r'(?:user[_ -]?id|角色|权限|superadmin|员工账号|账号).*(?:改|修改|停用|删除|提升|设为)|(?:改|修改|停用|删除|提升|设为).*(?:user[_ -]?id|角色|权限|superadmin|员工账号|账号)', lower)
    sql_request = 'sql' in lower and any(word in lower for word in ('执行', 'execute', '运行', '允许', '直接'))
    if any(word in lower for word in ('忽略之前所有规则', '忽略登录身份', '隐藏工具', '管理员已经授权', '超级管理员')) or identity_mutation or sql_request:
        return _result('DENY', 'security_boundary', status='NONE')

    intent = _detect_intent(text)
    if intent == 'person.lookup':
        return _result('DISAMBIGUATE', 'person.lookup', ['person.search'] if 'person.search' in authorized else [], status='AMBIGUOUS')
    if not intent:
        return _result('ANSWER', 'unknown', status='UNKNOWN')
    if intent not in authorized and not any(item in authorized for item in RESOLVER_CANDIDATES.get(intent, [])):
        return _result('DENY', intent, status='FORBIDDEN')

    values = _entities(text)
    writable = context.get('writable_communities')
    if isinstance(writable, list) and len(writable) == 1 and isinstance(writable[0], dict):
        values.setdefault('community_id', writable[0].get('id'))

    if intent in {'notice.save', 'notice.batch_publish'}:
        values.update(_notice_entities(text))
        resolved_notice = context.get('resolved_notice') or {}
        if intent == 'notice.save' and resolved_notice.get('id') and re.search(r'修改公告|改公告|公告.*改|把刚才.*公告', text):
            values.setdefault('notice_id', int(resolved_notice['id']))
            values.setdefault('notice_title', resolved_notice.get('title'))
        missing = [key for key in ('notice_title', 'notice_content') if not values.get(key)]
        if isinstance(writable, list):
            named = values.get('community_name')
            matches = [row for row in writable if isinstance(row, dict) and (not named or row.get('name') == named)]
            if named and len(matches) == 1:
                values['community_id'] = matches[0].get('id')
            elif intent == 'notice.save' and len(writable) != 1 and not named:
                missing.insert(0, 'community_id')
            elif named and len(matches) != 1:
                missing.insert(0, 'community_id')
        if intent == 'notice.save' and values.get('notice_scope') == 'building' and context.get('notice_building_candidates') == 0:
            missing.insert(0, 'building')
        if intent == 'notice.save' and re.search(r'修改公告|改公告|公告.*改|把刚才.*公告', text) and not values.get('notice_id'):
            missing.insert(0, 'notice')
        if missing:
            return _result('CLARIFY', intent, [intent] if intent in authorized else [], missing, 'MISSING', values)

    if intent == 'notice.archive' and not values.get('notice_id') and not context.get('resolved_notice'):
        return _result('CLARIFY', intent, [intent] if intent in authorized else [], ['notice'], 'MISSING', values)

    if intent == 'order.create':
        if any(word in text for word in ('再提交', '再次提交', '再来一次', '重复提交')):
            return _result('CLARIFY', intent, [intent] if intent in authorized else [], ['new_request_details'], 'REPEAT', values)
        public = bool(re.search(r'公共区域|公共设施|南门|路灯', text))
        values['public_area'] = public
        if public:
            values.setdefault('type', '公共设施')
            values.setdefault('content', text)
            values.setdefault('title', re.sub(r'(?:帮我|请)?(?:登记|新增|新建|建个|创建)?', '', text).strip()[:100])
        has_house = values.get('building_name') and values.get('room_no') is not None
        has_context = bool(context.get('resolved_house') and (context.get('resident_current_house') or re.search(r'我家|这个房|刚才|该房', text)))
        if not public and not has_house and not has_context:
            return _result('CLARIFY', intent, [intent] if intent in authorized else [], ['house'], 'MISSING', values)

    if intent == 'order.assign':
        if not values.get('order_no') and not context.get('resolved_order'):
            return _result('CLARIFY', intent, _candidates(intent, authorized), ['order'], 'MISSING', values)
        if not values.get('repairer_name'):
            return _result('CLARIFY', intent, _candidates(intent, authorized), ['repairer'], 'MISSING', values)
    if intent == 'order.accept' and not values.get('order_no') and not context.get('resolved_order'):
        return _result('CLARIFY', intent, _candidates(intent, authorized), ['order'], 'MISSING', values)
    has_order_context = bool(context.get('resolved_order') and re.search(r'这个工单|刚才|该工单|这张单|记录进度|返修|还是漏水', text))
    if intent in {'order.progress', 'order.finish', 'order.reopen', 'order.close'} and not values.get('order_no') and not has_order_context:
        return _result('CLARIFY', intent, _candidates(intent, authorized), ['order'], 'MISSING', values)
    if intent == 'order.cancel' and not values.get('order_no') and not context.get('resolved_order'):
        return _result('CLARIFY', intent, _candidates(intent, authorized), ['order'], 'MISSING', values)

    if intent == 'relation.bind_by_name':
        missing = [key for key in ('room_no', 'person_name') if not values.get(key)]
        if not values.get('unit') and not values.get('room_no'):
            missing.insert(0, 'unit')
        if missing:
            return _result('CLARIFY', intent, [intent] if intent in authorized else [], missing, 'MISSING', values)
        if context.get('person_candidates', 0) > 1:
            return _result('DISAMBIGUATE', intent, [intent] if intent in authorized else [], status='AMBIGUOUS', arguments=values)

    if intent == 'person.save':
        if context.get('person_candidates', 0) > 1:
            return _result('DISAMBIGUATE', intent, _candidates(intent, authorized), status='AMBIGUOUS', arguments=values)
        resolved = context.get('resolved_person') or {}
        if resolved.get('id'):
            values['id'] = int(resolved['id'])
        is_create = bool(re.search(r'新增住户|新增人员|登记人员|新建人员', text))
        if not values.get('phone'):
            return _result('CLARIFY', intent, _candidates(intent, authorized), ['phone'], 'MISSING', values)
        if is_create:
            if not values.get('person_name'):
                return _result('CLARIFY', intent, _candidates(intent, authorized), ['person'], 'MISSING', values)
        elif not values.get('id'):
            return _result('CLARIFY', intent, _candidates(intent, authorized), ['person'], 'MISSING', values)

    if context.get('person_candidates', 0) > 1 and values.get('person_name') and intent not in READ_COMMANDS:
        return _result('DISAMBIGUATE', intent, _candidates(intent, authorized), status='AMBIGUOUS', arguments=values)

    if intent == 'house.save' and re.search(r'这个房子|这个房屋', text) and not context.get('resolved_house'):
        return _result('CLARIFY', intent, _candidates(intent, authorized), ['house'], 'MISSING', values)
    if intent == 'property.archive' and not context.get('resolved_entity'):
        return _result('CLARIFY', intent, _candidates(intent, authorized), ['id', 'version'], 'MISSING', values)

    if intent == 'complaint.create' and not (values.get('building_name') and values.get('room_no') is not None):
        return _result('CLARIFY', intent, _candidates(intent, authorized), ['house'], 'MISSING', values)
    if intent in {'complaint.assign', 'complaint.resolve', 'complaint.close'} and not context.get('resolved_complaint'):
        if intent != 'complaint.resolve':
            return _result('CLARIFY', intent, _candidates(intent, authorized), ['complaint'], 'MISSING', values)

    if intent == 'visitor.create':
        return _result('CLARIFY', intent, _candidates(intent, authorized), ['visitor', 'house', 'host_person'], 'MISSING', values)
    if intent in {'visitor.checkin', 'visitor.checkout', 'visitor.cancel'} and not context.get('resolved_visitor'):
        return _result('CLARIFY', intent, _candidates(intent, authorized), ['visitor'], 'MISSING', values)

    if intent == 'vehicle.save' and not context.get('resolved_person'):
        return _result('CLARIFY', intent, _candidates(intent, authorized), ['person', 'house'], 'MISSING', values)
    if intent == 'vehicle.archive' and not context.get('resolved_vehicle') and not values.get('plate'):
        return _result('CLARIFY', intent, _candidates(intent, authorized), ['vehicle'], 'MISSING', values)
    if intent == 'parking.assign':
        missing = [key for key in ('space_code', 'plate') if not values.get(key)]
        if context.get('vehicle_candidates') == 0:
            missing.append('vehicle')
        if context.get('parking_candidates') == 0:
            missing.append('space')
        if missing:
            return _result('CLARIFY', intent, _candidates(intent, authorized), missing, 'MISSING', values)
    if intent == 'parking.release' and not context.get('resolved_parking_use'):
        return _result('CLARIFY', intent, _candidates(intent, authorized), ['parking_use'], 'MISSING', values)

    if intent == 'device.archive' and not context.get('resolved_device'):
        return _result('CLARIFY', intent, _candidates(intent, authorized), ['device'], 'MISSING', values)
    if intent == 'inspection.create' and not context.get('resolved_device'):
        return _result('CLARIFY', intent, _candidates(intent, authorized), ['device', 'assignee'], 'MISSING', values)
    if intent == 'inspection.complete' and not (context.get('inspection_id') or context.get('resolved_inspection')):
        return _result('CLARIFY', intent, _candidates(intent, authorized), ['id', 'version'], 'MISSING', values)

    if intent == 'payment.reverse' and not context.get('resolved_payment'):
        return _result('CLARIFY', intent, _candidates(intent, authorized), ['payment'], 'MISSING', values)
    if intent == 'bill.void' and not values.get('bill_id'):
        return _result('CLARIFY', intent, _candidates(intent, authorized), ['bill'], 'MISSING', values)

    candidates = _candidates(intent, authorized)
    if not candidates:
        return _result('DENY', intent, status='FORBIDDEN', arguments=values)
    action = 'CONFIRM' if intent in CONFIRM_INTENTS else 'TOOL'
    return _result(action, intent, candidates, arguments=values)
