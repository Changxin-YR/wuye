"""前端共享桩数据：严格对齐 backend-engineer 的 app.py / queries.py 实际上下文契约。

渲染自检（render_check_frontend.py）与静态预览（build_preview_site.py）共用本模块。
后端一旦改字段名，只改这里就能同时验证所有页面 —— 本文件即"模板 ↔ 上下文"契约的可执行副本。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOW = dt.datetime(2026, 9, 12, 10, 0, 0)

APP_NAME = "云邻AI智脑"
STATUS_TEXT = {0: "待派单", 1: "已派单", 2: "维修中", 3: "待验收", 4: "已关闭", 5: "已取消"}
STATUS_CLASS = {0: "pending", 1: "dispatched", 2: "working", 3: "verifying", 4: "closed", 5: "cancelled"}
HOUSE_STATUS_TEXT = {0: "空置", 1: "自住", 2: "出租"}
RELATION_TEXT = {"owner": "业主", "tenant": "租户", "family": "家庭成员"}
RELATION_STATUS_TEXT = {"active": "有效", "ended": "已结束"}
URGENCY_TEXT = {0: "普通", 1: "紧急"}
SOURCE_TEXT = {"web": "网页操作", "agent": "AI 助手"}
CATEGORY_TEXT = {"water": "给排水", "power": "电气", "door": "门窗", "wall": "墙面",
                 "elevator": "电梯", "public": "公共设施", "other": "其他"}
SCOPE_TEXT = {"all": "全部数据", "community": "本小区", "building": "本楼栋",
              "assigned": "我负责的工单", "self": "仅本人相关"}

ROLES = {
    "admin": (["admin"], ["系统管理员"], [
        "community.read", "community.write", "house.read", "house.write", "person.read",
        "person.write", "relation.write", "lease.write", "order.read", "order.create",
        "order.dispatch", "order.work", "order.verify", "order.cancel",
        "complaint.read", "complaint.create", "complaint.handle",
        "visitor.read", "visitor.write", "vehicle.read", "vehicle.write",
        "parking.read", "parking.write", "device.read", "device.write",
        "inspection.read", "inspection.write", "inspection.assign",
        "billing.read", "billing.manage", "billing.collect", "billing.reverse",
        "staff.read", "audit.read",
    ]),
    "manager": (["manager"], ["物业经理"], [
        "community.read", "community.write", "house.read", "house.write", "person.read",
        "person.write", "relation.write", "lease.write", "order.read", "order.create",
        "order.dispatch", "order.work", "order.verify", "order.cancel",
        "complaint.read", "complaint.create", "complaint.handle",
        "visitor.read", "visitor.write", "vehicle.read", "vehicle.write",
        "parking.read", "parking.write", "device.read", "device.write",
        "inspection.read", "inspection.write", "inspection.assign",
        "billing.read", "billing.manage", "billing.collect", "billing.reverse",
        "staff.read", "audit.read",
    ]),
    "service": (["service"], ["客服"], [
        "community.read", "house.read", "person.read", "person.write", "relation.write",
        "lease.write", "order.read", "order.create", "order.dispatch", "order.verify",
        "order.cancel", "complaint.read", "complaint.create", "complaint.handle",
        "visitor.read", "visitor.write", "vehicle.read", "vehicle.write",
        "parking.read", "parking.write", "staff.read",
    ]),
    "engineer": (["engineer"], ["工程维修"], [
        "order.read", "order.work", "device.read", "device.write",
        "inspection.read", "inspection.write", "community.read",
    ]),
    "finance": (["finance"], ["财务"], [
        "billing.read", "billing.manage", "billing.collect", "billing.reverse",
        "house.read", "person.read", "community.read", "staff.read",
    ]),
    "owner": (["owner"], ["业主"], [
        "resident.self", "house.read", "person.read", "order.read", "order.create",
        "order.verify", "order.cancel", "complaint.create", "complaint.read",
        "visitor.write", "visitor.read", "billing.read", "vehicle.read",
    ]),
}

REAL_NAMES = {"admin": "系统管理员", "manager": "王经理", "service": "小美", "finance": "周会计",
              "engineer": "李师傅", "owner": "张伟"}
SCOPES = {"admin": "all", "manager": "community", "service": "community", "finance": "community",
          "engineer": "assigned", "owner": "self"}

# 与 app.py build_nav() 的 NAV_ITEMS 一致
NAV_ITEMS = [
    ("dashboard", "工作台", "/", None),
    ("houses", "房屋", "/houses", "house.read"),
    ("persons", "人员关系", "/persons", "person.read"),
    ("leases", "租赁", "/leases", "lease.write"),
    ("orders", "维修工单", "/orders", "order.read"),
    ("complaints", "投诉", "/complaints", "complaint.read"),
    ("visitors", "访客", "/visitors", "visitor.read"),
    ("vehicles", "车辆车位", "/vehicles", "vehicle.read"),
    ("devices", "设备巡检", "/devices", "device.read"),
    ("bills", "收费", "/bills", "billing.read"),
    ("ai", "AI 助手", "/ai", None),
    ("audit", "操作审计", "/audit", "audit.read"),
]

PATHS = {
    "dashboard": "/", "login": "/login", "logout": "/logout", "health": "/health",
    "houses": "/houses", "houses_command": "/houses/{entity}/{action}",
    "persons": "/persons", "persons_command": "/persons/{entity}/{action}",
    "orders": "/orders", "order_new": "/orders/new", "order_create": "/orders",
    "order_detail": "/orders/{order_id}", "order_command": "/orders/{order_id}/{action}",
    "ai_page": "/ai", "ai_session_create": "/ai/sessions",
    "ai_session_messages": "/ai/sessions/{session_id}", "ai_chat": "/ai/chat",
    "leases": "/leases",
    "leases_command": "/leases/{action}",
    "complaints": "/complaints",
    "complaints_command": "/complaints/{complaint_id}/{action}",
    "complaints_create": "/complaints",
    "complaint_detail": "/complaints/{complaint_id}",
    "visitors": "/visitors",
    "visitors_command": "/visitors/{visitor_id}/{action}",
    "vehicles": "/vehicles",
    "vehicles_command": "/vehicles/{action}",
    "parking_command": "/parking/{action}",
    "devices": "/devices",
    "devices_command": "/devices/{action}",
    "inspections_command": "/inspections/{inspection_id}/{action}",
    "bills": "/bills",
    "bills_command": "/bills/{bill_id}/{action}",
    "bill_detail": "/bills/{bill_id}",
    "ai_actions_confirm": "/ai/actions/{action_id}/confirm",
    "ai_actions_cancel": "/ai/actions/{action_id}/cancel",
    "audit": "/audit", "static": "/static/{filename}",
}


def stub_url_for(endpoint, **values):
    template = PATHS.get(endpoint, "/" + str(endpoint))
    try:
        return template.format(**values)
    except (KeyError, IndexError):
        return "/unknown-endpoint"


def current_user_for(role):
    """= Policy.identity()：契约字段 + 模板友好字段（严格照 permissions.py）。"""
    codes, names, perms = ROLES[role]
    scope = SCOPES[role]
    return {
        "userId": 1, "username": role, "roles": codes, "roleNames": names,
        "permissions": sorted(perms), "data_scope": scope, "dataScopeText": SCOPE_TEXT[scope],
        "id": 1, "user_id": 1, "real_name": REAL_NAMES[role], "realName": REAL_NAMES[role],
        "phone": "13800000001", "role_names": names, "data_scope_text": SCOPE_TEXT[scope],
        "is_admin": role == "admin", "source": "web",
        "scopes": [{"kind": scope, "kind_text": SCOPE_TEXT[scope],
                    "community_id": 1 if scope in ("community", "building") else None,
                    "building_id": 3 if scope == "building" else None}],
    }


def nav_for(role):
    """= app.build_nav(policy)：只包含当前用户有权限的入口。"""
    perms = ROLES[role][2]
    return [{"key": key, "label": label, "href": href}
            for key, label, href, perm in NAV_ITEMS if perm is None or perm in perms]


def base_context(role):
    perms = ROLES[role][2]
    identity = current_user_for(role)
    globals_map = {
        "can": lambda perm: all(item in perms for item in perm) if isinstance(perm, (list, tuple)) else perm in perms,
        "has_role": lambda code: code in ROLES[role][0],
        "app_name": APP_NAME,
        "now": NOW,
        "nav": nav_for(role),
        "current_user": identity,
        "csrf_token": "stub-token",
        "url_for": stub_url_for,
    }
    return globals_map


def status_counts_for(role, count=3):
    return [{"status": code, "text": STATUS_TEXT[code], "count": count, "class": STATUS_CLASS[code]}
            for code in sorted(STATUS_TEXT)]


def order(oid=1024, status=2, urgency=0, actions=None, **extra):
    data = {
        "id": oid, "no": "WX20260912%04d" % oid, "status": status, "status_text": STATUS_TEXT[status],
        "status_class": STATUS_CLASS[status], "urgency": urgency, "urgency_text": URGENCY_TEXT[urgency],
        "category": "water", "category_text": CATEGORY_TEXT["water"],
        "description": "厨房水槽下面漏水，地面已经积水",
        "house_id": 12, "house_full": "云邻花园 1 栋 1 单元 101",
        "community_name": "云邻花园", "building_name": "1 栋", "unit": "1 单元", "room": "101",
        "contact_name": "张伟", "contact_phone": "13800000001",
        "owner_id": 1, "owner_name": "张伟",
        "repairer_id": 9 if status else None, "repairer_name": "李师傅" if status else "",
        "requester_person_id": 7, "requester_name": "张伟",
        "rating": 5 if status == 4 else None, "rating_note": "师傅上门很快" if status == 4 else "",
        "created_at": NOW, "updated_at": NOW,
        "finished_at": NOW if status >= 3 else None, "closed_at": NOW if status == 4 else None,
    }
    if actions is None:
        actions = [action_item("progress"), action_item("finish"), action_item("cancel")]
    data["actions"] = actions
    data.update(extra)
    return data


ACTION_TEXT = {"assign": "派单", "accept": "接单", "progress": "登记进度", "finish": "完工",
               "verify": "验收通过", "cancel": "取消工单", "rate": "评价"}
ACTION_PERM = {"assign": "order.dispatch", "accept": "order.work", "progress": "order.work",
               "finish": "order.work", "verify": "order.verify", "cancel": "order.cancel",
               "rate": "order.create"}


def action_item(name):
    """= queries.order_actions() 的产出：{name, label, style, perm, need_note}。"""
    style = {"assign": "primary", "accept": "primary", "finish": "primary", "verify": "primary",
             "cancel": "danger"}.get(name, "secondary")
    return {"name": name, "label": ACTION_TEXT.get(name, name), "style": style,
            "perm": ACTION_PERM.get(name), "need_note": name != "accept"}


def resident(pid=7, name="张伟", relation="owner", status="active"):
    return {"person_id": pid, "name": name, "phone": "13800000001", "relation": relation,
            "relation_text": RELATION_TEXT[relation], "status": status,
            "status_text": RELATION_STATUS_TEXT[status], "start_at": "2026-01-01 00:00:00"}


def house(hid=12, room="101", **extra):
    data = {"id": hid, "full_name": "云邻花园 1 栋 1 单元 " + room, "community_id": 1,
            "community_name": "云邻花园", "building_id": 3, "building_name": "1 栋",
            "unit": "1 单元", "room": room, "area": 89.5, "status": 2,
            "status_text": HOUSE_STATUS_TEXT[2], "residents": [resident()]}
    data.update(extra)
    return data


def person_house(house_id=12, relation="owner", status="active"):
    return {"house_id": house_id, "full_name": "云邻花园 1 栋 1 单元 101",
            "relation": relation, "relation_text": RELATION_TEXT[relation],
            "status": status, "status_text": RELATION_STATUS_TEXT[status],
            "start_at": "2026-01-01 00:00:00", "end_at": ""}


def person(pid=7, name="张伟", **extra):
    data = {"id": pid, "name": name, "phone": "13800000001", "user_id": 1, "username": "owner",
            "role_names": ["业主"], "house_count": 1, "houses": [person_house()]}
    data.update(extra)
    return data


def relation(rid=21, **extra):
    data = {"id": rid, "house_id": 12, "full_name": "云邻花园 1 栋 1 单元 101", "person_id": 7,
            "person_name": "张伟", "person_phone": "13800000001", "relation": "owner",
            "relation_text": RELATION_TEXT["owner"], "status": "active",
            "status_text": RELATION_STATUS_TEXT["active"], "start_at": "2026-01-01 00:00:00",
            "end_at": ""}
    data.update(extra)
    return data


def option_list(mapping):
    return [{"value": key, "text": text} for key, text in mapping.items()]


def pagination(page=1, page_size=20, total=12):
    pages = max(1, (total + page_size - 1) // page_size) if total else 1
    return {"page": page, "page_size": page_size, "pages": pages, "total": total}


#: 登录页「演示账号」桩数据（真实来源：seed_demo.ACCOUNT_SPECS + seed_demo.DEMO_PASSWORD）
DEMO_LOGIN_ACCOUNTS = [
    {"username": "admin", "name": "系统管理员", "role": "系统管理员", "password": "Demo-only-292!"},
    {"username": "manager01", "name": "王经理", "role": "物业经理", "password": "Demo-only-292!"},
    {"username": "service01", "name": "陈客服", "role": "客服", "password": "Demo-only-292!"},
    {"username": "engineer01", "name": "黄磊", "role": "工程维修", "password": "Demo-only-292!"},
    {"username": "finance01", "name": "周会计", "role": "财务", "password": "Demo-only-292!"},
    {"username": "owner01", "name": "张伟", "role": "业主", "password": "Demo-only-292!"},
]


def case_login(role="owner"):
    context = base_context("owner")
    context.update({"current_user": None, "nav": [], "error": "用户名或密码不正确", "next": "/orders",
                    "demo_accounts": DEMO_LOGIN_ACCOUNTS})
    return context


def case_dashboard(role):
    context = base_context(role)
    context.update({
        "identity": context["current_user"],
        "counts": {code: 3 for code in STATUS_TEXT},
        "status_counts": status_counts_for(role),
        "recent_orders": [order(1024, 0, 1), order(1025, 4)],
        "my_stats": {"my_houses": 1, "my_orders": 3, "my_todo": 2, "total_orders": 12},
    })
    return context


def case_houses(role):
    context = base_context(role)
    pag = pagination()
    context.update({
        "communities": [{"id": 1, "name": "云邻花园", "address": "文化路 88 号",
                         "building_count": 2, "house_count": 24}],
        "buildings": [{"id": 3, "name": "1 栋", "community_id": 1, "community_name": "云邻花园",
                       "house_count": 24}],
        "houses": [house(), house(13, room="102", status=0, status_text="空置", residents=[])],
        "pagination": pag, **pag,
        "keyword": "", "status_filter": "",
        "selected_community_id": 1, "selected_building_id": 3,
        "filters": {"community": "1", "building": "3", "keyword": "", "status": ""},
        "can_edit": "house.write" in ROLES[role][2],
        "house_status_options": option_list(HOUSE_STATUS_TEXT),
    })
    return context


def case_persons(role):
    context = base_context(role)
    pag = pagination()
    context.update({
        "persons": [person(), person(8, name="李娜", user_id=None, username="", role_names=[],
                                     house_count=0, houses=[])],
        "relations": [relation()],
        "houses": [house()],
        "pagination": pag, **pag,
        "keyword": "", "filters": {"keyword": ""},
        "can_edit": "person.write" in ROLES[role][2],
        "relation_options": option_list(RELATION_TEXT),
    })
    return context


def case_orders(role):
    context = base_context(role)
    pag = pagination()
    context.update({
        "orders": [order(1024, 0, 1), order(1025, 3, 0)],
        "pagination": pag, **pag,
        "status_counts": status_counts_for(role),
        "status_options": [{"value": code, "text": text} for code, text in sorted(STATUS_TEXT.items())],
        "status_filter": "2", "keyword": "", "mine": "",
        "filters": {"status": "2", "keyword": "", "community": "", "mine": ""},
    })
    return context


def case_order_form(role):
    context = base_context(role)
    context.update({
        "houses": [house()],
        "categories": option_list(CATEGORY_TEXT), "category_options": option_list(CATEGORY_TEXT),
        "urgencies": option_list(URGENCY_TEXT), "urgency_options": option_list(URGENCY_TEXT),
        "form": {"house_id": 12, "contact_name": "张伟", "contact_phone": "13800000001",
                 "category": "water", "description": "", "urgency": 0},
        "error": None,
    })
    return context


def case_order_detail(role):
    context = base_context(role)
    item = order(1024, 2, 1, [action_item("progress"), action_item("finish"), action_item("cancel")])
    context.update({
        "order": item,
        "actions": item["actions"],
        "logs": [{"id": 1, "action": "create", "action_text": "提交报修", "from_status": None,
                  "from_status_text": "", "to_status": 0, "to_status_text": "待派单",
                  "operator_name": "张伟", "note": "", "created_at": NOW},
                 {"id": 2, "action": "assign", "action_text": "派单", "from_status": 0,
                  "from_status_text": "待派单", "to_status": 1, "to_status_text": "已派单",
                  "operator_name": "小美", "note": "派给李师傅", "created_at": NOW}],
        "staff": [{"id": 9, "display_name": "李师傅", "name": "李师傅",
                   "role_names": ["工程维修"], "open_orders": 2}],
    })
    return context


def case_order_detail_no_staff(role):
    """`assign` 在场但后端没给师傅列表时，必须降级为手填姓名（不崩、不空按钮）。"""
    context = case_order_detail(role)
    context["actions"] = [action_item("assign")]
    context["staff"] = []
    return context


def case_ai(role):
    context = base_context(role)
    session = {"id": 3, "title": "帮我查一下漏水工单", "dsh_session_id": "dsh-3",
               "created_at": NOW, "updated_at": NOW, "message_count": 4}
    context.update({
        "sessions": [session],
        "active_session": session,
        "messages": [{"id": 1, "role": "user", "role_text": "我",
                      "content": "我报的漏水工单到哪一步了", "created_at": NOW},
                     {"id": 2, "role": "assistant", "role_text": "AI 助手",
                      "content": "你的工单正在维修中，师傅已经接单。", "created_at": NOW}],
        "stream_url": "/ai/chat", "new_session_url": "/ai/sessions",
        "agent_ready": True,
    })
    return context


def case_audit(role):
    context = base_context(role)
    pag = pagination(page=1, page_size=20, total=1)
    context.update({
        "logs": [{"id": 1, "created_at": NOW, "user_id": 2, "username": "service",
                  "real_name": "小美", "action": "work_order.create", "action_text": "创建工单",
                  "target_type": "work_order", "target_type_text": "维修工单", "target_id": 1024,
                  "source": "agent", "source_text": "AI 助手",
                  "detail": '{"house_id": 12}'}],
        "pagination": pag, **pag,
        "keyword": "", "source_filter": "agent", "filters": {"keyword": "", "source": "agent"},
    })
    return context


def case_error(role):
    context = base_context(role)
    context.update({"code": 403, "title": "没有权限", "message": "你没有查看这个页面的权限。"})
    return context



# --------------------------------------------------------------------------
# v2 增量：租赁 / 投诉 / 访客 / 车辆车位 / 设备巡检 / 账单
# --------------------------------------------------------------------------
def lease_item(lid=31, status="active"):
    return {"id": lid, "house_id": 12, "house_text": "云邻花园 1 栋 1 单元 101",
            "person_id": 7, "person_name": "张伟", "phone": "13800000001",
            "relation": "tenant", "relation_text": "租户", "status": status,
            "status_text": "在租" if status == "active" else "已退租", "rent": 3200.0, "rent_text": "3200",
            "start_at": "2026-03-01 00:00:00", "end_at": "" if status == "active" else "2026-09-01 00:00:00"}


def complaint_item(cid=41, status=0):
    text = {0: "待处理", 1: "处理中", 2: "已结案", 3: "已取消"}[status]
    return {"id": cid, "no": "TS20260912%04d" % cid, "house_id": 12,
            "house_full": "云邻花园 1 栋 1 单元 101", "house_label": "云邻花园 1 栋 1 单元 101", "category": "noise", "category_text": "噪音扰民",
            "content": "楼上装修噪音很大，晚上十点还在施工", "reporter_id": 7, "reporter_name": "张伟",
            "contact_phone": "13800000001", "reporter_phone": "13800000001", "handler_id": 9 if status == 1 else None,
            "handler_name": "黄磊" if status in (1, 2) else "", "status": status, "status_text": text,
            "status_class": "complaint", "result": "已上门沟通" if status == 2 else "",
            "created_at": NOW, "closed_at": NOW if status == 2 else None}


def visitor_item(vid=51, status=0):
    text = {0: "待进", 1: "已进", 2: "已离", 3: "已取消"}[status]
    return {"id": vid, "community_id": 1, "building_id": 3, "house_id": 12,
            "house_full": "云邻花园 1 栋 1 单元 101", "house_label": "云邻花园 1 栋 1 单元 101",
            "name": "李娜", "phone": "13900000002", "operator_id": 9, "operator_name": "小美",
            "visit_at": NOW, "purpose": "走亲访友", "status": status, "status_text": text,
            "status_class": "visitor"}


def vehicle_item(vid=61, status=0):
    return {"id": vid, "community_id": 1, "house_id": 12, "house_full": "云邻花园 1 栋 1 单元 101",
            "house_label": "云邻花园 1 栋 1 单元 101", "plate": "沪A12345", "brand": "大众 朗逸",
            "owner_person_id": 7, "owner_name": "张伟", "owner_phone": "13800000001",
            "space_code": "A-012" if status == 0 else "",
            "status": status, "status_text": "正常" if status == 0 else "已归档"}


def parking_item(pid=71, status=0):
    return {"id": pid, "community_id": 1, "code": "A-012", "status": status,
            "status_text": "占用" if status == 1 else "空闲",
            "house_id": 12 if status else None,
            "house_full": "云邻花园 1 栋 1 单元 101" if status else "",
            "house_label": "云邻花园 1 栋 1 单元 101" if status else "",
            "vehicle_id": 61 if status else None, "plate": "沪A12345" if status else ""}


def device_item(did=81, status=0):
    return {"id": did, "community_id": 1, "building_id": 3, "building_name": "1 栋",
            "name": "1 栋 1 单元电梯", "category": "elevator", "category_text": "电梯",
            "location": "1 栋负一层", "status": status, "inspection_count": 2,
            "last_inspection_at": "2026-09-01 09:00:00",
            "last_inspection_status": "已完成",
            "status_text": {0: "正常", 1: "维修中", 2: "已归档"}[status]}


def inspection_item(iid=91, status=0):
    return {"id": iid, "device_id": 81, "device_name": "1 栋 1 单元电梯", "community_id": 1,
            "assignee_id": 9, "assignee_name": "黄磊", "plan_at": NOW, "status": status,
            "status_text": {0: "待巡检", 1: "已完成", 2: "已转报修"}[status],
            "status_class": "inspection", "result": "运行正常" if status >= 1 else "",
            "order_id": 1024 if status == 2 else None,
            "order_no": "WX202609120024" if status == 2 else "",
            "device_location": "1 栋负一层"}


def bill_item(bid=101, status=0):
    return {"id": bid, "no": "ZD20260912%04d" % bid, "house_id": 12,
            "house_text": "云邻花园 1 栋 1 单元 101", "person_id": 7, "person_name": "张伟",
            "fee_type": "property", "fee_type_text": "物业费", "period": "2026-09",
            "amount": 320.0, "paid_amount": 0.0 if status == 0 else 320.0, "status": status,
            "status_text": {0: "待缴", 1: "部分缴纳", 2: "已缴", 3: "已作废"}[status],
            "status_class": "bill", "due_at": NOW, "created_at": NOW, "remark": ""}


def payment_item(pid=201, status=0):
    return {"id": pid, "no": "SK20260912%04d" % pid, "bill_id": 101, "amount": 320.0, "method": 1,
            "method_text": "银行转账", "reference": "", "status": status,
            "status_text": "已入账" if status == 0 else "已冲销", "status_class": "payment",
            "created_at": NOW}


def case_leases(role):
    context = base_context(role)
    pag = pagination()
    context.update({
        "items": [lease_item(), lease_item(32, "ended")],
        "houses": [house()],
        "persons": [person()],
        "keyword": "",
        "can_edit": "lease.write" in ROLES[role][2],
        **pag,
    })
    return context


def case_complaints(role):
    context = base_context(role)
    pag = pagination()
    context.update({
        "items": [complaint_item(41, 0), complaint_item(42, 1), complaint_item(43, 2)],
        "houses": [house()],
        "category_options": option_list({"noise": "噪音扰民", "parking": "车辆占位", "service": "服务态度", "other": "其他"}),
        "status_counts": [{"status": code, "text": text, "count": code, "class": "complaint"}
                          for code, text in {0: "待处理", 1: "处理中", 2: "已结案", 3: "已取消"}.items()],
        "status_filter": "", "keyword": "", **pag,
    })
    return context


def case_complaint_detail(role):
    context = base_context(role)
    context.update({
        "complaint": complaint_item(41, 1),
        "logs": [{"id": 1, "action": "create", "action_text": "登记投诉", "from_status": None,
                  "from_status_text": "", "to_status": 0, "to_status_text": "待处理",
                  "operator_name": "张伟", "note": "", "created_at": NOW},
                 {"id": 2, "action": "assign", "action_text": "分配处理人", "from_status": 0,
                  "from_status_text": "待处理", "to_status": 1, "to_status_text": "处理中",
                  "operator_name": "小美", "note": "分配给黄磊", "created_at": NOW}],
        "actions": [{"name": "handle", "label": "填写处理结果", "style": "primary", "need_note": True},
                    {"name": "close", "label": "结案", "style": "primary", "need_note": False},
                    {"name": "cancel", "label": "取消投诉", "style": "danger", "need_note": False}],
        "staff": [{"id": 9, "display_name": "黄磊", "name": "黄磊"}],
    })
    return context


def case_visitors(role):
    context = base_context(role)
    pag = pagination()
    context.update({
        "items": [visitor_item(51, 0), visitor_item(52, 1), visitor_item(53, 2)],
        "houses": [house()],
        "status_filter": "",
        "status_options": option_list({0: "待进", 1: "已进", 2: "已离", 3: "已取消"}),
        "keyword": "", **pag,
    })
    return context


def case_vehicles(role):
    context = base_context(role)
    pag = pagination()
    context.update({
        "vehicles": [vehicle_item(), vehicle_item(62, 1)],
        "spaces": [parking_item(71, 1), parking_item(72, 0)],
        "houses": [house()],
        "keyword": "",
        "can_edit": "vehicle.write" in ROLES[role][2],
        "can_parking": "parking.write" in ROLES[role][2],
        **pag,
    })
    return context


def case_devices(role):
    context = base_context(role)
    context.update({
        "devices": [device_item(81, 0), device_item(82, 1)],
        "inspections": [inspection_item(91, 0), inspection_item(92, 1), inspection_item(93, 2)],
        "staff": [{"id": 9, "display_name": "黄磊", "name": "黄磊"}],
        "buildings": [{"id": 3, "name": "1 栋"}],
        "can_device": "device.write" in ROLES[role][2],
        "can_inspection": "inspection.write" in ROLES[role][2],
    })
    return context


def case_bills(role):
    context = base_context(role)
    pag = pagination()
    context.update({
        "items": [bill_item(101, 0), bill_item(102, 1), bill_item(103, 2), bill_item(104, 3)],
        "summary": {"unpaid_total": 640.0, "unpaid_count": 2, "paid_total": 1280.0},
        "houses": [house()],
        "persons": [person()],
        "fee_types": option_list({"property": "物业费", "parking": "车位费", "water": "水费", "repair": "维修费"}),
        "status_options": option_list({0: "待缴", 1: "部分缴纳", 2: "已缴", 3: "已作废"}),
        "status_filter": "", "keyword": "", "overdue": "", **pag,
    })
    return context


def case_bill_detail(role):
    context = base_context(role)
    context.update({
        "bill": bill_item(101, 1),
        "payments": [payment_item(201, 0), payment_item(202, 1)],
    })
    return context


def case_order_detail_reopen(role):
    """返修：actions 里出现 reopen（工单状态 3 待验收 → 2 维修中）。"""
    context = case_order_detail(role)
    order_row = order(1024, 3, 0, [action_item("verify"), action_item("reopen"), action_item("cancel")])
    context["order"] = order_row
    context["actions"] = order_row["actions"]
    return context



def case_dashboard_frozen(role):
    """主契约冻结名的写法：current_user.name / scope_text。"""
    context = case_dashboard(role)
    user = dict(context["current_user"])
    user["name"] = user.pop("real_name")
    user["real_name"] = ""
    user["scope_text"] = user.pop("data_scope_text")
    user["data_scope_text"] = ""
    context["current_user"] = user
    context["identity"] = user
    return context


def case_ai_frozen(role):
    """冻结名：current_session（而不是后端的 active_session）。"""
    context = case_ai(role)
    context["current_session"] = context.pop("active_session")
    return context


def case_houses_frozen(role):
    """冻结名：selected_community / selected_building（而不是后端的 *_id）。"""
    context = case_houses(role)
    context["selected_community"] = context.pop("selected_community_id")
    context["selected_building"] = context.pop("selected_building_id")
    return context


def case_audit_frozen(role):
    """冻结名：audit_logs（而不是后端的 logs）。"""
    context = case_audit(role)
    context["audit_logs"] = context.pop("logs")
    return context



def case_ai_first_visit(role):
    """首次进 /ai：没有会话、没有消息（后端此时 active_session=None）。"""
    context = base_context(role)
    context.update({
        "sessions": [], "active_session": None, "current_session": None, "messages": [],
        "stream_url": "/ai/chat", "new_session_url": "/ai/sessions",
        "agent_ready": False,
    })
    return context


CASES = {
    "dashboard.html": case_dashboard, "houses.html": case_houses, "persons.html": case_persons,
    "orders.html": case_orders, "order_form.html": case_order_form,
    "order_detail.html": case_order_detail, "ai.html": case_ai, "audit.html": case_audit,
    "leases.html": case_leases, "complaints.html": case_complaints,
    "complaint_detail.html": case_complaint_detail, "visitors.html": case_visitors,
    "vehicles.html": case_vehicles, "devices.html": case_devices,
    "bills.html": case_bills, "bill_detail.html": case_bill_detail,
    "order_detail.html#reopen": case_order_detail_reopen,
    "dashboard.html#frozen": case_dashboard_frozen,
    "ai.html#frozen": case_ai_frozen,
    "ai.html#first": case_ai_first_visit,
    "houses.html#frozen": case_houses_frozen,
    "audit.html#frozen": case_audit_frozen,
    "error.html": case_error, "login.html": case_login, "base.html": case_dashboard,
}

# ---------------------------------------------------------------- 渲染环境
class ContextGlobals:
    """模拟 app.py 的 context_processor：`can`/`app_name`/`nav` 等从当前用例上下文取。

    Jinja 会把 `Environment.globals` 当映射用（`dict.fromkeys(...)`, `len()`, `iter()`），
    所以这里必须完整实现映射协议，只把"上下文里已有的键"优先，其余委托给原生 globals。
    """

    def __init__(self, base):
        # 必须持有原始 dict：env.globals 之后会被换成 self，直接引用会无限递归
        self._base = base
        self.context: dict = {}

    def __getitem__(self, key):
        if key in self.context:
            return self.context[key]
        return self._base[key]

    def __contains__(self, key):
        return key in self.context or key in self._base

    def __iter__(self):
        seen = set(self.context)
        for key in self._base:
            if key not in seen:
                seen.add(key)
                yield key

    def __len__(self):
        return len(set(self.context) | set(self._base))

    def get(self, key, default=None):
        if key in self.context:
            return self.context[key]
        return self._base.get(key, default)

    def keys(self):
        return list(iter(self))

    def items(self):
        for key in self:
            yield key, self[key]


def install_globals(env):
    """把 env.globals 换成上下文感知版本，并注册 app.py 同款 filter。"""
    env.filters["cn_time"] = (
        lambda value: value.strftime("%Y-%m-%d %H:%M") if isinstance(value, dt.datetime) else (value or "—")
    )
    env.filters["status_class"] = lambda value: STATUS_CLASS.get(int(value), "")
    holder = ContextGlobals(dict(env.globals))
    env.globals = holder
    return holder
