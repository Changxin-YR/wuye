"""Small deterministic planner that narrows model choices without authorizing them."""

import re

from agent_security import risk_for


ALIASES = (
    # Specific actions must precede generic words such as "工单" and "投诉".
    ("relation.bind_by_name", ("绑定", "业主")),
    ("property.archive", ("归档",)),
    ("visitor.checkin", ("访客", "进入")),
    ("order.finish", ("提交完工", "完工")),
    ("complaint.resolve", ("投诉", "修好")),
    ("order.assign", ("派给", "派单", "分配维修")),
    ("order.accept", ("接单", "开始维修")),
    ("order.progress", ("记录进度", "维修进度", "记一下进展", "更新进展")),
    ("order.reopen", ("返修", "还是漏水")),
    ("order.close", ("验收", "关闭这个工单")),
    ("lease.checkout", ("搬走", "退租", "退房", "不住了", "租约结束")),
    ("parking.assign", ("车位", "停车位")),
    ("inspection.complete", ("巡检", "发现故障", "巡查")),
    ("complaint.create", ("登记投诉", "投诉")),
    ("visitor.create", ("登记访客", "访客")),
    ("vehicle.save", ("登记车牌", "车牌")),
    ("device.save", ("登记设备", "设备")),
    ("payment.reverse", ("冲销", "冲正", "撤回上一笔收款", "撤回收款")),
    ("payment.record", ("收款", "已收款")),
    ("bill.batch", ("生成物业费账单", "批量生成账单", "批量算", "批量计算", "批量出账")),
    ("bill.lookup", ("物业费账单", "账单")),
    ("notice.save", ("发布公告",)),
    ("notice.lookup", ("公告", "通知")),
    ("person.save", ("手机号改", "修改手机号", "联系方式改", "电话换")),
    ("person.lookup", ("找人", "查找人员", "找")),
    ("order.create", ("报修", "维修", "漏水", "坏了", "叫师傅", "工单")),
    ("order.lookup", ("工单", "到哪一步")),
    ("house.lookup", ("查询房", "查房", "房屋信息", "住户信息", "这个房子", "空置")),
)
CANONICAL = {
    "house.lookup": "house.search",
    "person.lookup": "person.search",
    "order.lookup": "order.search",
    "notice.lookup": "notice.read",
    "bill.lookup": "billing.unpaid",
}


def _first_command(message, authorized):
    for command, words in ALIASES:
        actual = CANONICAL.get(command, command)
        if command not in authorized and actual not in authorized:
            continue
        if command == "relation.bind_by_name":
            matched = ("绑定" in message or "登记成" in message or bool(re.search(r"手机号\s*1[3-9]\d{9}\s*的", message))) and not ("不要" in message and "只" in message)
        elif command == "visitor.checkin":
            matched = "进入" in message or "签到" in message or ("确认" in message and "访客" in message)
        elif command == "complaint.resolve":
            matched = "修好" in message or "处理结果" in message
        elif command == "order.lookup":
            matched = "到哪一步" in message or "谁负责" in message or "查看工单" in message or "我的工单" in message
            if "工单" in message and not any(word in message for word in ("报修", "提交", "派给", "派单", "完工", "返修", "验收", "进展")):
                matched = True
        elif command == "house.lookup":
            matched = bool(re.search(r"(?:查询|查|看看|瞅|瞧|看下|查下).*(?:栋|楼|房|室|谁住)", message))
            matched = matched or any(word in message for word in words if word in {"这个房子", "空置"})
            matched = matched or ("当前绑定情况" in message)
        elif command == "notice.lookup":
            matched = any(word in message for word in words) and "发布" not in message
        elif command == "order.create":
            matched = any(word in message for word in words)
            if "工单" in message and not any(
                word in message for word in ("报修", "维修", "漏水", "坏了", "叫师傅", "新增", "新建", "提交")
            ):
                matched = False
        else:
            matched = any(word in message for word in words)
        if matched:
            return actual
    return None


def _entities(message):
    building = re.search(r"([A-Za-z0-9一二三四五六七八九十百]+)\s*(?:栋|号楼)", message)
    unit = re.search(r"([0-9一二三四五六七八九十百]+)\s*单元", message)
    room = re.search(r"单元\s*([0-9]{2,4})\s*(?:房|室)?", message)
    if not room:
        room = re.search(r"(?:栋|号楼)\s*([0-9]{2,4})\s*(?:房|室)?", message)
    phone = re.search(r"1[3-9]\d{9}", message)
    plate = re.search(r"[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼][A-Z][A-Z0-9]{5}", message, re.I)
    order_no = re.search(r"WO[-一字母0-9]+", message, re.I)
    space_code = re.search(r"(?<![A-Za-z0-9])[A-Za-z]+-\d{1,8}(?![A-Za-z0-9])", message)
    bill_id = re.search(r"(?:账单|收款)\s*#?\s*([1-9][0-9]{0,8})", message)
    amount = re.search(r"(?:收款|收到|收了)\s*(?:人民币|现金)?\s*([0-9]+(?:\.[0-9]{1,2})?)\s*(?:元|块)", message)
    person = re.search(r"给\s*([\u4e00-\u9fff]{2,4})\s*绑定", message)
    person = person or re.search(r"绑定(?:给|到)?\s*([\u4e00-\u9fff]{2,4})", message)
    person = person or re.search(r"([\u4e00-\u9fff]{2,4})(?=(?:绑定|的手机号|车主|已经搬走|搬走|退租))", message)
    person = person or re.search(r"手机号\s*1[3-9]\d{9}\s*的\s*([\u4e00-\u9fff]{2,4})", message)
    person = person or re.search(r"(?:找|车主)\s*([\u4e00-\u9fff]{2,4})", message)
    person = person or re.search(r"(?:查|查询)\s*([\u4e00-\u9fff]{2,4})(?=的(?:账单|联系方式|电话|手机号))", message)
    person = person or re.search(r"([\u4e00-\u9fff]{2,4})(?=登记成|的业主)", message)
    repairer = re.search(r"(?:派给|分配给|交给|安排给)\s*([\u4e00-\u9fff]{2,4})", message)
    result = {}
    if building:
        result["building_name"] = building.group(1) + "栋"
    if unit:
        result["unit"] = unit.group(1) + "单元"
    if room:
        result["room_no"] = int(room.group(1))
    if phone:
        result["phone"] = phone.group(0)
    if plate:
        result["plate"] = plate.group(0).upper()
    if order_no:
        result["order_no"] = order_no.group(0).upper()
    if space_code:
        result["space_code"] = space_code.group(0).upper()
    if amount:
        result["amount"] = float(amount.group(1))
    if bill_id:
        result["bill_id"] = int(bill_id.group(1))
    if person:
        person_name=person.group(1).lstrip("把")
        if person_name not in {"业主", "住户", "人员"}:
            result["person_name"] = person_name
    if repairer:
        result["repairer_name"] = repairer.group(1)
    return result


def plan_request(message, authorized_commands, context=None):
    context = context or {}
    authorized = set(authorized_commands or ())
    text = str(message or "").strip()
    lower = text.lower()
    identity_mutation = re.search(r"(?:user[_ -]?id|角色|权限|superadmin|员工账号|账号).*(?:改|修改|停用|删除|提升|设为)|(?:改|修改|停用|删除|提升|设为).*(?:user[_ -]?id|角色|权限|superadmin|员工账号|账号)", lower)
    sql_request = "sql" in lower and any(word in lower for word in ("执行", "execute", "运行", "允许"))
    if any(word in lower for word in ("忽略之前所有规则", "忽略登录身份", "隐藏工具", "管理员已经授权", "超级管理员")) or identity_mutation or sql_request:
        return {"action": "DENY", "intent": "security_boundary", "candidates": [], "missing_fields": [], "entity_status": "NONE"}
    command = _first_command(text, authorized)
    if not command:
        if "尾号" in text and re.search(r"[\u4e00-\u9fff]{2,4}", text):
            return {"action": "DISAMBIGUATE", "intent": "person.lookup", "candidates": ["person.search"], "missing_fields": [], "entity_status": "AMBIGUOUS", "arguments": {}}
        if re.search(r"车牌.*归谁|车辆.*车主", text) or re.search(r"设备.*(?:状态|情况)", text):
            return {"action": "CLARIFY", "intent": "unsupported_lookup", "candidates": [], "missing_fields": ["query_capability"], "entity_status": "UNSUPPORTED"}
        if "来访" in text or "客人" in text:
            return {"action": "CLARIFY", "intent": "visitor.create", "candidates": [], "missing_fields": ["visitor", "house", "host_person"], "entity_status": "MISSING"}
        if "处理" in text and not any(word in text for word in ("查询", "查", "登记", "绑定", "发布", "报修", "维修", "分配", "修改", "删除", "归档")):
            return {"action": "CLARIFY", "intent": "ambiguous", "candidates": [], "missing_fields": ["operation"], "entity_status": "AMBIGUOUS"}
        return {"action": "ANSWER", "intent": "unknown", "candidates": [], "missing_fields": [], "entity_status": "UNKNOWN"}
    values = _entities(text)
    if command == "order.create" and any(word in text for word in ("再提交", "再次提交", "再来一次", "重复提交")):
        return {"action": "CLARIFY", "intent": command, "candidates": [command], "missing_fields": ["new_request_details"], "entity_status": "REPEAT", "arguments": values}
    if command == "person.save":
        if context.get("person_candidates", 0) > 1:
            return {"action": "DISAMBIGUATE", "intent": command, "candidates": [command], "missing_fields": [], "entity_status": "AMBIGUOUS", "arguments": values}
        person = context.get("resolved_person") or {}
        if person.get("id"):
            values["id"] = int(person["id"])
        if "phone" not in values:
            return {"action": "CLARIFY", "intent": command, "candidates": [command], "missing_fields": ["phone"], "entity_status": "MISSING", "arguments": values}
        if "id" not in values and not values.get("person_name"):
            return {"action": "CLARIFY", "intent": command, "candidates": [command], "missing_fields": ["person"], "entity_status": "MISSING", "arguments": values}
    if command == "relation.bind_by_name":
        missing = [key for key in ("room_no", "person_name") if key not in values]
        if "room_no" not in values and "unit" not in values:
            missing.insert(0, "unit")
        if missing:
            return {"action": "CLARIFY", "intent": command, "candidates": [command], "missing_fields": missing, "entity_status": "MISSING"}
        if context.get("person_candidates", 0) > 1:
            return {"action": "DISAMBIGUATE", "intent": command, "candidates": [command], "missing_fields": [], "entity_status": "AMBIGUOUS", "arguments": values}
    if context.get("person_candidates", 0) > 1 and values.get("person_name") and command not in {"person.search", "house.search", "order.search", "notice.read"}:
        return {"action": "DISAMBIGUATE", "intent": command, "candidates": [command], "missing_fields": [], "entity_status": "AMBIGUOUS", "arguments": values}
    if command == "order.create" and "我家" in text and "building_name" not in values and "room_no" not in values:
        return {"action": "CLARIFY", "intent": command, "candidates": [command], "missing_fields": ["house"], "entity_status": "MISSING", "arguments": values}
    if command == "inspection.complete" and not context.get("inspection_id"):
        return {"action": "CLARIFY", "intent": command, "candidates": [command], "missing_fields": ["id", "version"], "entity_status": "MISSING", "arguments": values}
    if command in {"property.archive", "visitor.checkin"} and not context.get("resolved_entity"):
        return {"action": "CLARIFY", "intent": command, "candidates": [command], "missing_fields": ["id", "version"], "entity_status": "MISSING", "arguments": values}
    if command == "order.accept" and not values.get("order_no"):
        return {"action": "CLARIFY", "intent": command, "candidates": [command], "missing_fields": ["order"], "entity_status": "MISSING", "arguments": values}
    has_house_context = context.get("resolved_house") and (context.get("resident_current_house") or any(word in text for word in ("我家", "这个房", "刚才", "该房")))
    if command == "order.create" and not (values.get("building_name") and values.get("room_no") is not None) and not has_house_context:
        return {"action": "CLARIFY", "intent": command, "candidates": [command], "missing_fields": ["house"], "entity_status": "MISSING", "arguments": values}
    if command == "order.assign" and (not values.get("order_no") and not context.get("resolved_order")):
        return {"action": "CLARIFY", "intent": command, "candidates": [command], "missing_fields": ["order"], "entity_status": "MISSING", "arguments": values}
    if command == "order.assign" and not values.get("repairer_name"):
        return {"action": "CLARIFY", "intent": command, "candidates": [command], "missing_fields": ["repairer"], "entity_status": "MISSING", "arguments": values}
    has_order_context = context.get("resolved_order") and any(word in text for word in ("这个工单", "刚才", "该工单", "这张单", "记录进度", "返修", "还是漏水"))
    if command in {"order.progress", "order.finish", "order.reopen", "order.close"} and not values.get("order_no") and not has_order_context:
        return {"action": "CLARIFY", "intent": command, "candidates": [command], "missing_fields": ["order"], "entity_status": "MISSING", "arguments": values}
    if command == "complaint.create" and "登记投诉" not in text and not (values.get("building_name") and values.get("room_no") is not None):
        return {"action": "CLARIFY", "intent": command, "candidates": [command], "missing_fields": ["house"], "entity_status": "MISSING", "arguments": values}
    if command == "visitor.create" and not context.get("resolved_house") and not (values.get("building_name") and values.get("room_no") is not None):
        return {"action": "CLARIFY", "intent": command, "candidates": [command], "missing_fields": ["house"], "entity_status": "MISSING", "arguments": values}
    if command == "parking.assign" and (not values.get("space_code") or not values.get("plate")):
        return {"action": "CLARIFY", "intent": command, "candidates": [command], "missing_fields": [key for key in ("space_code", "plate") if not values.get(key)], "entity_status": "MISSING", "arguments": values}
    if command == "parking.assign":
        missing = []
        if context.get("vehicle_candidates") == 0:
            missing.append("vehicle")
        if context.get("parking_candidates") == 0:
            missing.append("space")
        if missing:
            return {"action": "CLARIFY", "intent": command, "candidates": [command], "missing_fields": missing, "entity_status": "MISSING", "arguments": values}
    if command == "payment.reverse" and not values.get("bill_id") and not context.get("resolved_payment"):
        return {"action": "CLARIFY", "intent": command, "candidates": [command], "missing_fields": ["payment"], "entity_status": "MISSING", "arguments": values}
    if command in {"bill.batch", "payment.record", "payment.reverse"}:
        action = "CONFIRM"
    else:
        action = "TOOL"
    return {"action": action, "intent": command, "candidates": [command], "missing_fields": [], "entity_status": "RESOLVED", "arguments": values}
