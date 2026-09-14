"""演示数据种子（幂等：重复执行不会产生重复数据）。

覆盖内容：
- 小区：美家花园（主演示）+ 美家花园二期（用于对照数据范围隔离）
- 楼栋 / 单元 / 房屋：1栋、2栋 + 二期3栋，共 16 套房屋
- 账号：admin / manager01 / service01 / engineer01 / owner01，统一口令 ``Demo-only-292!``
- 人员档案与房屋关系：张伟（owner01，1栋1单元101 业主）、李娜 ×2（同名消歧）、黄磊（engineer01）
- 工单：9 张，覆盖 6 个状态（0 待派单 / 1 已派单 / 2 维修中 / 3 待验收 / 4 已关闭 / 5 已取消）
- 审计日志：演示「页面操作」的留痕（含报修、派单、接单、完工、验收、评价、取消）
- AI 会话：1 条含 4 条消息的演示会话

数据范围（与 ``permissions.ROLES`` 的默认范围一致）：
- admin = all；manager01 / service01 = community（美家花园）；engineer01 = assigned；owner01 = self

用法::

    python seed_demo.py                 # 幂等写入演示数据
    python seed_demo.py check           # 自检并打印 [OK] 行（失败返回非 0 退出码）
    python seed_demo.py reset           # 删除全部业务表后重新建表
    python seed_demo.py reset --seed    # 删表重建后立即写入演示数据
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta

from sqlalchemy import select
from werkzeug.security import check_password_hash, generate_password_hash

import db as db_mod
import models
import permissions
from models import (
    AgentMessage,
    AgentSession,
    AuditLog,
    Bill,
    Building,
    Community,
    Complaint,
    Device,
    House,
    HousePerson,
    Lease,
    Inspection,
    OrderLog,
    ParkingSpace,
    Payment,
    Person,
    RbacRole,
    RolePermission,
    Unit,
    User,
    UserRole,
    UserScope,
    Vehicle,
    Visitor,
    WorkOrder,
    utcnow,
)

#: 演示统一口令（真实项目当然不会写死在代码里）
DEMO_PASSWORD = "Demo-only-292!"

COMMUNITY_NAME = "美家花园"
COMMUNITY_ADDRESS = "美家路 88 号"
SECOND_COMMUNITY_NAME = "美家花园二期"
SECOND_COMMUNITY_ADDRESS = "美家路 100 号"

#: 账号：(用户名, 姓名, 手机号, 角色, 数据范围)
ACCOUNT_SPECS = [
    {"username": "admin", "real_name": "系统管理员", "phone": "13600000001", "role": "admin", "scope": "all"},
    {"username": "manager01", "real_name": "王经理", "phone": "13600000002", "role": "manager", "scope": "community"},
    {"username": "service01", "real_name": "陈客服", "phone": "13600000003", "role": "service", "scope": "community"},
    {"username": "engineer01", "real_name": "黄磊", "phone": "13700000001", "role": "engineer", "scope": "assigned"},
    {"username": "engineer02", "real_name": "陈志强", "phone": "13700000002", "role": "engineer", "scope": "assigned"},
    {"username": "engineer03", "real_name": "吴海涛", "phone": "13700000003", "role": "engineer", "scope": "assigned"},
    {"username": "finance01", "real_name": "周会计", "phone": "13500000001", "role": "finance", "scope": "community"},
    {"username": "owner01", "real_name": "张伟", "phone": "13900000001", "role": "owner", "scope": "self"},
]

#: 楼栋：小区名 → 楼栋名列表
BUILDING_SPECS = {
    COMMUNITY_NAME: ["1栋", "2栋"],
    SECOND_COMMUNITY_NAME: ["3栋"],
}

#: 房屋：(小区, 楼栋, 单元, 房号, 面积, 状态 0空置/1自住/2出租)
HOUSE_SPECS = [
    (COMMUNITY_NAME, "1栋", "1", "101", 89.5, 1),
    (COMMUNITY_NAME, "1栋", "1", "102", 89.5, 0),
    (COMMUNITY_NAME, "1栋", "1", "201", 92.0, 1),
    (COMMUNITY_NAME, "1栋", "1", "202", 92.0, 0),
    (COMMUNITY_NAME, "1栋", "1", "301", 92.0, 0),
    (COMMUNITY_NAME, "1栋", "1", "302", 92.0, 0),
    (COMMUNITY_NAME, "1栋", "1", "401", 105.0, 0),
    (COMMUNITY_NAME, "1栋", "1", "402", 105.0, 0),
    (COMMUNITY_NAME, "1栋", "2", "101", 78.0, 0),
    (COMMUNITY_NAME, "1栋", "2", "102", 78.0, 2),
    (COMMUNITY_NAME, "1栋", "2", "201", 78.0, 0),
    (COMMUNITY_NAME, "1栋", "2", "202", 78.0, 0),
    (COMMUNITY_NAME, "2栋", "1", "101", 110.0, 1),
    (COMMUNITY_NAME, "2栋", "1", "102", 110.0, 0),
    (COMMUNITY_NAME, "2栋", "1", "201", 110.0, 2),
    (COMMUNITY_NAME, "2栋", "1", "202", 110.0, 0),
    # 二期用于数据范围对照：manager / service 看不到这几套
    (SECOND_COMMUNITY_NAME, "3栋", "1", "101", 120.0, 1),
    (SECOND_COMMUNITY_NAME, "3栋", "1", "102", 120.0, 0),
]

#: 人员档案：(姓名, 电话, 绑定账号用户名或 None)
PERSON_SPECS = [
    ("张伟", "13900000001", "owner01"),
    ("李秀兰", "13900000002", None),
    ("赵敏", "13900000003", None),
    ("李娜", "13800000011", None),
    ("李娜", "13800000012", None),
    ("刘建国", "13900000004", None),
    ("孙倩", "13800000013", None),
    ("黄磊", "13700000001", "engineer01"),
    ("陈志强", "13700000002", "engineer02"),
    ("吴海涛", "13700000003", "engineer03"),
    ("周涛", "13900000005", None),  # 二期住户，验证跨小区隔离
]

#: 房屋人员关系：(小区, 楼栋, 单元, 房号, 姓名, 电话, 身份)
RELATION_SPECS = [
    (COMMUNITY_NAME, "1栋", "1", "101", "张伟", "13900000001", "owner"),
    (COMMUNITY_NAME, "1栋", "1", "101", "李秀兰", "13900000002", "family"),
    (COMMUNITY_NAME, "1栋", "1", "201", "赵敏", "13900000003", "owner"),
    (COMMUNITY_NAME, "1栋", "1", "202", "李娜", "13800000012", "family"),
    (COMMUNITY_NAME, "1栋", "2", "102", "孙倩", "13800000013", "tenant"),
    (COMMUNITY_NAME, "2栋", "1", "101", "刘建国", "13900000004", "owner"),
    (COMMUNITY_NAME, "2栋", "1", "101", "黄磊", "13700000001", "family"),
    (COMMUNITY_NAME, "2栋", "1", "201", "刘建国", "13900000004", "owner"),
    (COMMUNITY_NAME, "2栋", "1", "201", "李娜", "13800000011", "tenant"),
    (SECOND_COMMUNITY_NAME, "3栋", "1", "101", "周涛", "13900000005", "owner"),
]

#: 工单：(小区, 楼栋, 单元, 房号, 分类, 描述, 紧急 0/1, 状态, 联系人, 电话,
#:        业主账号, 报修人, 几天前创建, 评分, 评语, 取消原因)
ORDER_SPECS = [
    (COMMUNITY_NAME, "1栋", "1", "101", "water", "厨房水管漏水，地面有积水", 1, 0, "张伟", "13900000001",
     "owner01", "张伟", 1, None, "", ""),
    (COMMUNITY_NAME, "2栋", "1", "201", "electric", "客厅插座频繁跳闸，无法用电", 0, 0, "李娜", "13800000011",
     None, "李娜", 2, None, "", ""),
    (COMMUNITY_NAME, "1栋", "1", "201", "door", "阳台推拉门卡死，关不严", 0, 1, "赵敏", "13900000003",
     None, "赵敏", 3, None, "", ""),
    (COMMUNITY_NAME, "1栋", "2", "102", "water", "卫生间马桶堵塞返水", 1, 1, "孙倩", "13800000013",
     None, "孙倩", 3, None, "", ""),
    (COMMUNITY_NAME, "2栋", "1", "101", "electric", "卧室灯不亮，怀疑线路接触不良", 0, 2, "刘建国", "13900000004",
     None, "刘建国", 5, None, "", ""),
    (COMMUNITY_NAME, "1栋", "1", "101", "public", "单元门禁对讲机没有声音", 0, 2, "张伟", "13900000001",
     "owner01", "张伟", 6, None, "", ""),
    (COMMUNITY_NAME, "2栋", "1", "102", "door", "入户门锁芯损坏，钥匙转不动", 1, 3, "刘建国", "13900000004",
     None, "刘建国", 8, None, "", ""),
    (COMMUNITY_NAME, "1栋", "1", "201", "water", "暖气不热，室温偏低", 0, 4, "赵敏", "13900000003",
     None, "赵敏", 12, 5, "师傅上门很快，问题解决了", ""),
    (COMMUNITY_NAME, "2栋", "1", "201", "other", "业主误报，实际无需维修", 0, 5, "李娜", "13800000011",
     None, "李娜", 15, None, "", "业主自行处理，取消工单"),
]

#: 工单流转日志链：状态 → [(动作, 备注)]
LOG_CHAIN: dict[int, list[tuple[str, str]]] = {
    0: [],
    1: [("assign", "派单给 黄磊")],
    2: [("assign", "派单给 黄磊"), ("accept", "已接单，下午上门")],
    3: [
        ("assign", "派单给 黄磊"),
        ("accept", "已接单，马上安排"),
        ("progress", "已更换损坏配件"),
        ("finish", "维修完成，等待业主验收"),
    ],
    4: [
        ("assign", "派单给 黄磊"),
        ("accept", "已接单，按约定时间上门"),
        ("progress", "已更换损坏配件并试水"),
        ("finish", "维修完成，等待验收"),
        ("verify", "验收通过，工单关闭"),
        ("rate", "业主评价"),
    ],
    5: [("cancel", "业主自行处理，取消工单")],
}

#: 动作执行后的工单状态
ACTION_TARGET = {
    "create": 0,
    "assign": 1,
    "accept": 2,
    "progress": 2,
    "finish": 3,
    "verify": 4,
    "rate": 4,
    "cancel": 5,
}

#: 工单动作 → 审计动作码
ORDER_AUDIT_ACTION = {
    "create": "order.create",
    "assign": "order.assign",
    "accept": "order.accept",
    "progress": "order.progress",
    "finish": "order.finish",
    "verify": "order.verify",
    "cancel": "order.cancel",
    "rate": "order.rate",
}

#: AI 演示会话
AGENT_SESSION_TITLE = "1栋1单元101 厨房漏水"
AGENT_MESSAGES = [
    ("user", "1 栋 101 厨房漏水了，帮我报修"),
    ("assistant", "已经为张伟报修：美家花园1栋1单元101 厨房水管漏水，工单已生成，当前状态是「待派单」。"),
    ("user", "现在到哪一步了"),
    ("assistant", "这张工单目前是「待派单」，还没有指派维修师傅。需要我派给黄磊吗？"),
]


# --------------------------------------------------------------------------
# 通用写入工具
# --------------------------------------------------------------------------
def _first(session, model, **filters):
    conditions = [getattr(model, key) == value for key, value in filters.items()]
    return session.execute(select(model).where(*conditions)).scalars().first()


def _upsert(session, model, filters: dict, values: dict):
    """按 ``filters`` 找行：存在就更新 ``values``，不存在就新建（幂等的关键）。"""
    row = _first(session, model, **filters)
    if row is None:
        row = model(**{**filters, **values})
        session.add(row)
        session.flush()
        return row
    for key, value in values.items():
        if getattr(row, key) != value:
            setattr(row, key, value)
    session.flush()
    return row


# --------------------------------------------------------------------------
# 各段种子
# --------------------------------------------------------------------------
def seed_roles(session) -> None:
    """角色与权限矩阵：以 ``permissions.ROLES`` 为唯一事实，多余权限会被清理。"""
    for code, meta in permissions.ROLES.items():
        _upsert(session, RbacRole, {"code": code}, {"name": meta["name"], "builtin": True})
        allowed = set(meta["permissions"])
        rows = session.execute(
            select(RolePermission).where(RolePermission.role_code == code, RolePermission.deleted.is_(False))
        ).scalars().all()
        for row in rows:
            if row.permission not in allowed:
                row.deleted = True
        existing = {row.permission for row in rows if row.permission in allowed}
        for permission in sorted(allowed - existing):
            session.add(RolePermission(role_code=code, permission=permission))
    session.flush()


def seed_accounts(session, community=None) -> dict[str, User]:
    """账号：口令统一重置为演示口令，避免旧库里的密码打不开演示站。"""
    password_hash = generate_password_hash(DEMO_PASSWORD)
    users: dict[str, User] = {}
    for spec in ACCOUNT_SPECS:
        user = _upsert(
            session,
            User,
            {"username": spec["username"]},
            {
                "real_name": spec["real_name"],
                "phone": spec["phone"],
                "password_hash": password_hash,
                "active": True,
                "deleted": False,
            },
        )
        users[spec["username"]] = user
        session.execute(UserRole.__table__.delete().where(UserRole.user_id == user.id))
        session.add(UserRole(user_id=user.id, role_code=spec["role"]))
        session.execute(UserScope.__table__.delete().where(UserScope.user_id == user.id))
        # community 范围必须落具体的 community_id，否则 Policy.condition 会过滤掉全部数据
        session.add(
            UserScope(
                user_id=user.id,
                kind=spec["scope"],
                community_id=community.id if (spec["scope"] == "community" and community is not None) else None,
            )
        )
    session.flush()
    return users


def seed_space(session) -> dict[tuple[str, str, str], House]:
    """小区 / 楼栋 / 房屋。返回 {(楼栋, 单元, 房号): House}。"""
    houses: dict[tuple[str, str, str], House] = {}
    for community_name, building_names in BUILDING_SPECS.items():
        address = COMMUNITY_ADDRESS if community_name == COMMUNITY_NAME else SECOND_COMMUNITY_ADDRESS
        community = _upsert(session, Community, {"name": community_name}, {"address": address})
        for building_name in building_names:
            building = _upsert(session, Building, {"community_id": community.id, "name": building_name}, {})
            for spec in HOUSE_SPECS:
                spec_community, spec_building, unit, room, area, status = spec
                if spec_community != community_name or spec_building != building_name:
                    continue
                house = _upsert(
                    session,
                    House,
                    {"building_id": building.id, "unit": unit, "room": room},
                    {"community_id": community.id, "area": area, "status": status},
                )
                houses[(building_name, unit, room)] = house
    session.flush()
    return houses


def seed_persons(session, users: dict[str, User]) -> dict[tuple[str, str], Person]:
    """人员档案：同名靠电话区分（李娜 ×2 用于演示消歧）。"""
    persons: dict[tuple[str, str], Person] = {}
    for name, phone, username in PERSON_SPECS:
        user_id = users[username].id if username else None
        persons[(name, phone)] = _upsert(session, Person, {"phone": phone}, {"name": name, "user_id": user_id})
    session.flush()
    return persons


def seed_relations(
    session, houses: dict[tuple[str, str, str], House], persons: dict[tuple[str, str], Person]
) -> None:
    """房屋人员关系：业主入住把空置房置为自住，租户置为出租。"""
    for _community, building, unit, room, name, phone, relation in RELATION_SPECS:
        house = houses.get((building, unit, room))
        person = persons.get((name, phone))
        if house is None or person is None:
            continue
        _upsert(
            session,
            HousePerson,
            {"house_id": house.id, "person_id": person.id},
            {
                "relation": relation,
                "status": "active",
                "start_at": utcnow() - timedelta(days=200),
                "end_at": None,
                "note": "演示数据",
                "active_key": f"{house.id}:{person.id}",
            },
        )
        if relation == "owner" and house.status in (0, 2):
            house.status = 1
        elif relation == "tenant" and house.status in (0, 1):
            house.status = 2
            # 租户同时补一条租赁合同记录（AI 的"查租赁/list_leases"要有数据）
            _upsert(
                session,
                Lease,
                {"house_id": house.id, "person_id": person.id, "status": 0},
                {
                    "rent": 2600 if room == "102" else 2200,
                    "start_at": utcnow() - timedelta(days=200),
                    "end_at": None,
                    "note": "演示租约",
                },
            )
    session.flush()


def seed_orders(
    session,
    houses: dict[tuple[str, str, str], House],
    users: dict[str, User],
    persons: dict[tuple[str, str], Person],
) -> None:
    """工单：9 张覆盖 6 个状态，并补齐流转日志与审计留痕。"""
    engineer = users["engineer01"]
    manager = users["manager01"]
    service = users["service01"]
    now = utcnow()
    for index, spec in enumerate(ORDER_SPECS, start=1):
        (
            _community, building, unit, room, category, description, urgency, status,
            contact_name, contact_phone, owner_username, requester, days_ago, rating, rating_note, cancel_reason,
        ) = spec
        house = houses.get((building, unit, room))
        if house is None:
            continue
        created_at = (now - timedelta(days=days_ago)).replace(microsecond=0)
        order_no = f"WO{created_at.strftime('%Y%m%d')}{index:04d}"
        owner_id = users[owner_username].id if owner_username else None
        requester_person = persons.get((requester, contact_phone))
        order = _upsert(
            session,
            WorkOrder,
            {"no": order_no},
            {
                "community_id": house.community_id,
                "building_id": house.building_id,
                "house_id": house.id,
                "requester_person_id": requester_person.id if requester_person else None,
                "owner_id": owner_id,
                "contact_name": contact_name,
                "contact_phone": contact_phone,
                "category": category,
                "description": description,
                "urgency": urgency,
                "status": status,
                "repairer_id": engineer.id if status in (1, 2, 3, 4) else None,
                "finished_at": created_at + timedelta(days=1) if status >= 3 else None,
                "closed_at": created_at + timedelta(days=2) if status == 4 else None,
                "rating": rating,
                "rating_note": rating_note or "",
                "created_at": created_at,
            },
        )
        # 流转日志与审计：先删后写，保证重复执行不会累积
        session.execute(OrderLog.__table__.delete().where(OrderLog.order_id == order.id))
        session.execute(
            AuditLog.__table__.delete().where(
                AuditLog.target_type == "work_order", AuditLog.target_id == str(order.id)
            )
        )
        chain = [("create", f"{contact_name} 报修：{description[:40]}")] + LOG_CHAIN.get(status, [])
        moment = created_at
        for action, note in chain:
            operator_id = (
                engineer.id if action in ("accept", "progress", "finish")
                else owner_id if action in ("verify", "rate", "cancel")
                else service.id
            )
            session.add(
                OrderLog(
                    order_id=order.id,
                    action=action,
                    from_status=None if action == "create" else ACTION_TARGET.get(action),
                    to_status=ACTION_TARGET.get(action),
                    operator_id=operator_id or manager.id,
                    note=note[:500],
                    created_at=moment,
                )
            )
            audit_action = ORDER_AUDIT_ACTION.get(action)
            if audit_action:
                session.add(
                    AuditLog(
                        user_id=operator_id or manager.id,
                        action=audit_action,
                        target_type="work_order",
                        target_id=str(order.id),
                        detail={
                            "no": order_no,
                            "house": house.full_name,
                            "category": category,
                            "urgency": urgency,
                            "note": note,
                        },
                        source="web",
                        created_at=moment,
                    )
                )
            moment += timedelta(hours=6)
    session.flush()



def seed_units(session, houses) -> None:
    """单元表（契约里单元是独立表；House.unit 字段保留兼容页面显示）。"""
    seen: set[tuple[int, str]] = set()
    for house in houses.values():
        key = (house.building_id, house.unit or "1")
        if key in seen:
            continue
        seen.add(key)
        _upsert(session, Unit, {"building_id": house.building_id, "name": house.unit or "1"}, {})
    session.flush()


#: 投诉：(小区, 楼栋, 单元, 房号, 内容, 分类, 状态, 几天前)
COMPLAINT_SPECS = [
    (COMMUNITY_NAME, "1栋", "1", "201", "楼上装修噪音从早到晚，影响休息", "noise", 0, 2),
    (COMMUNITY_NAME, "2栋", "1", "101", "单元门口垃圾长期不清理，有异味", "clean", 1, 4),
    (COMMUNITY_NAME, "1栋", "1", "101", "电梯经常停在这一层不开门", "elevator", 2, 9),
    (COMMUNITY_NAME, "1栋", "2", "102", "楼道声控灯坏了，晚上很黑", "public", 3, 12),
]

#: 访客：(小区, 楼栋, 单元, 房号, 姓名, 电话, 来访事由, 状态, 几小时前)
VISITOR_SPECS = [
    (COMMUNITY_NAME, "1栋", "1", "101", "王小明", "13811110001", "送快递", 0, 2),
    (COMMUNITY_NAME, "2栋", "1", "201", "刘芳", "13811110002", "看望家人", 1, 5),
    (COMMUNITY_NAME, "1栋", "1", "201", "陈师傅", "13811110003", "上门维修空调", 2, 26),
]

#: 车辆：(小区, 楼栋, 单元, 房号, 车牌, 品牌, 车主姓名, 状态)
VEHICLE_SPECS = [
    (COMMUNITY_NAME, "1栋", "1", "101", "京A12345", "大众", "张伟", 0),
    (COMMUNITY_NAME, "2栋", "1", "101", "京B67890", "丰田", "刘建国", 0),
    (COMMUNITY_NAME, "1栋", "1", "201", "京C24680", "比亚迪", "赵敏", 1),
]

#: 车位：(小区, 车位编号, 状态)
PARKING_SPECS = [
    (COMMUNITY_NAME, "A-001", 1),
    (COMMUNITY_NAME, "A-002", 1),
    (COMMUNITY_NAME, "A-003", 0),
]

#: 设备：(小区, 楼栋, 设备名, 分类, 状态, 位置)
DEVICE_SPECS = [
    (COMMUNITY_NAME, "1栋", "1 号电梯", "elevator", 0, "1栋 1 单元"),
    (COMMUNITY_NAME, "1栋", "单元门禁", "access", 0, "1栋 1 单元门口"),
    (COMMUNITY_NAME, "2栋", "消防水泵", "fire", 1, "2栋地下一层"),
]

#: 巡检：(设备名, 状态, 结果, 几天前)
INSPECTION_SPECS = [
    ("1 号电梯", 0, "", 1),
    ("单元门禁", 1, "运行正常，已清洁", 3),
    ("消防水泵", 1, "发现异响，已登记待修", 5),
]

#: 账单：(小区, 楼栋, 单元, 房号, 费用类型, 金额, 已收, 状态, 账期, 几天后到期)
#: 到期天数用负数 = 已逾期，演示"欠费催收 / 只看逾期"用
BILL_SPECS = [
    (COMMUNITY_NAME, "1栋", "1", "101", "物业费", 268.50, 0, 0, "2026-08", -12),
    (COMMUNITY_NAME, "1栋", "1", "201", "物业费", 276.00, 100.00, 1, "2026-08", -5),
    (COMMUNITY_NAME, "2栋", "1", "101", "物业费", 330.00, 330.00, 2, "2026-09", 10),
    (COMMUNITY_NAME, "2栋", "1", "201", "水费", 86.40, 0, 3, "2026-09", 10),
]


def seed_leases(session, houses, persons) -> None:
    """按「租户」关系补租赁记录（幂等）：同一房屋同一租户只保留一条在租。"""
    rent_by_room = {"102": 3200.0, "201": 4500.0, "101": 3800.0}
    for _community, building, unit, room, name, phone, relation in RELATION_SPECS:
        if relation != "tenant":
            continue
        house = houses.get((building, unit, room))
        person = persons.get((name, phone))
        if house is None or person is None:
            continue
        _upsert(
            session,
            Lease,
            {"house_id": house.id, "person_id": person.id, "status": 0},
            {
                "rent": rent_by_room.get(room, 3000.0),
                "start_at": utcnow() - timedelta(days=120),
                "end_at": utcnow() + timedelta(days=245),
                "note": "演示租赁合同",
            },
        )
    session.flush()


def seed_operations(session, houses, users, persons) -> None:
    """投诉 / 访客 / 车辆车位 / 设备巡检 / 账单收款 的演示数据（幂等）。"""
    now = utcnow()
    service = users.get("service01")
    engineer = users.get("engineer01")
    finance = users.get("finance01")
    manager = users.get("manager01")

    # 投诉
    for index, (community, building, unit, room, content, category, status, days) in enumerate(COMPLAINT_SPECS, 1):
        house = houses.get((building, unit, room))
        if house is None:
            continue
        reporter = persons.get(("张伟", "13900000001"))
        _upsert(
            session,
            Complaint,
            {"no": f"TS{now.strftime('%Y%m%d')}{index:04d}"},
            {
                "community_id": house.community_id,
                "building_id": house.building_id,
                "house_id": house.id,
                "reporter_id": reporter.id if reporter else None,
                "content": content,
                "category": category,
                "status": status,
                "handler_id": manager.id if status in (1, 2) else None,
                "result": "已与业主沟通，装修方承诺限时施工" if status == 2 else "",
                "created_at": (now - timedelta(days=days)).replace(microsecond=0),
            },
        )

    # 访客
    for index, (community, building, unit, room, name, phone, purpose, status, hours) in enumerate(VISITOR_SPECS, 1):
        house = houses.get((building, unit, room))
        if house is None:
            continue
        _upsert(
            session,
            Visitor,
            {"name": name, "phone": phone},
            {
                "community_id": house.community_id,
                "building_id": house.building_id,
                "house_id": house.id,
                "visit_at": (now - timedelta(hours=hours)).replace(microsecond=0),
                "purpose": purpose,
                "status": status,
                "operator_id": service.id if service else None,
            },
        )

    # 车辆 + 车位
    for community, building, unit, room, plate, brand, owner_name, status in VEHICLE_SPECS:
        house = houses.get((building, unit, room))
        if house is None:
            continue
        person = next((p for (nm, _ph), p in persons.items() if nm == owner_name), None)
        vehicle = _upsert(
            session,
            Vehicle,
            {"plate": plate},
            {
                "community_id": house.community_id,
                "house_id": house.id,
                "brand": brand,
                "owner_person_id": person.id if person else None,
                "status": status,
            },
        )
        if status == 0:
            space = _first(session, ParkingSpace, community_id=house.community_id, status=0)
            if space is not None:
                space.house_id = house.id
                space.vehicle_id = vehicle.id
                space.status = 1
                session.flush()

    for community_name, code, status in PARKING_SPECS:
        community = _first(session, Community, name=community_name)
        if community is None:
            continue
        _upsert(session, ParkingSpace, {"community_id": community.id, "code": code}, {"status": status})

    # 设备 + 巡检
    for community_name, building_name, name, category, status, location in DEVICE_SPECS:
        community = _first(session, Community, name=community_name)
        if community is None:
            continue
        building = _first(session, Building, community_id=community.id, name=building_name)
        _upsert(
            session,
            Device,
            {"community_id": community.id, "name": name},
            {
                "building_id": building.id if building else None,
                "category": category,
                "status": status,
                "location": location,
            },
        )
    for device_name, status, result, days in INSPECTION_SPECS:
        device = _first(session, Device, name=device_name)
        if device is None:
            continue
        _upsert(
            session,
            Inspection,
            {"device_id": device.id, "status": status},
            {
                "community_id": device.community_id,
                "assignee_id": engineer.id if engineer else None,
                "plan_at": (now - timedelta(days=days)).replace(microsecond=0),
                "result": result,
            },
        )

    # 账单 + 收款
    for index, (community, building, unit, room, fee_type, amount, paid, status, period, due_days) in enumerate(
        BILL_SPECS, 1
    ):
        house = houses.get((building, unit, room))
        if house is None:
            continue
        bill_no = f"ZD{now.strftime('%Y%m')}{index:04d}"
        bill = _upsert(
            session,
            Bill,
            {"no": bill_no},
            {
                "community_id": house.community_id,
                "house_id": house.id,
                "person_id": None,
                "fee_type": fee_type,
                "amount": amount,
                "paid_amount": paid,
                "status": status,
                "period": period,
                "due_at": (now + timedelta(days=due_days)).replace(microsecond=0),
            },
        )
        if paid > 0 and status in (1, 2):
            _upsert(
                session,
                Payment,
                {"no": f"SK{now.strftime('%Y%m')}{index:04d}"},
                {
                    "bill_id": bill.id,
                    "amount": paid,
                    "method": 1 if index % 2 else 0,
                    "reference": "演示收款",
                    "status": 0,
                    "operator_id": finance.id if finance else None,
                },
            )
    session.flush()


def seed_agent_session(session, users: dict[str, User]) -> None:
    """一条 AI 演示会话（用户消息 + 助手回复），演示时页面上直接能看到历史。"""
    owner = users["owner01"]
    agent_session = _upsert(
        session, AgentSession, {"user_id": owner.id, "title": AGENT_SESSION_TITLE}, {"dsh_session_id": None}
    )
    session.execute(AgentMessage.__table__.delete().where(AgentMessage.session_id == agent_session.id))
    moment = utcnow() - timedelta(hours=2)
    for role, content in AGENT_MESSAGES:
        session.add(AgentMessage(session_id=agent_session.id, role=role, content=content, created_at=moment))
        moment += timedelta(minutes=2)
    session.flush()


def seed(engine=None) -> dict:
    """写入全部演示数据（幂等）。"""
    with db_mod.db_session(engine) as session:
        db_mod.create_all(engine)
        seed_roles(session)
        houses = seed_space(session)
        main_community = _first(session, Community, name=COMMUNITY_NAME)
        users = seed_accounts(session, community=main_community)
        persons = seed_persons(session, users)
        seed_relations(session, houses, persons)
        seed_leases(session, houses, persons)
        seed_orders(session, houses, users, persons)
        seed_units(session, houses)
        seed_operations(session, houses, users, persons)
        seed_agent_session(session, users)
        session.flush()
    return {"ok": True, "houses": len(houses), "accounts": len(ACCOUNT_SPECS)}


def reset(engine=None) -> list[str]:
    """删除全部业务表后重新建表（``create_all`` 不会改旧表结构，所以必须先删）。"""
    dropped = db_mod.reset_tables(engine=engine)
    db_mod.create_all(engine)
    return dropped


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def check(engine=None) -> bool:
    """自检：账号与口令、数据范围隔离、房屋/人员/关系、工单状态覆盖、审计与 AI 会话。"""
    import queries
    from permissions import Policy

    ok = True

    def report(condition: bool, message: str) -> None:
        nonlocal ok
        if not condition:
            ok = False
        print(f"{'[OK]' if condition else '[FAIL]'} {message}")

    with db_mod.db_session(engine) as session:
        # 账号、口令、角色
        for spec in ACCOUNT_SPECS:
            user = _first(session, User, username=spec["username"])
            if user is None:
                report(False, f"账号 {spec['username']} 存在")
                continue
            report(True, f"账号 {spec['username']} 存在（{spec['real_name']} / {spec['role']}）")
            report(
                check_password_hash(user.password_hash or "", DEMO_PASSWORD),
                f"账号 {spec['username']} 口令 = 演示口令",
            )
            roles = [
                row.role_code
                for row in session.execute(
                    select(UserRole).where(UserRole.user_id == user.id, UserRole.deleted.is_(False))
                ).scalars()
            ]
            report(roles == [spec["role"]], f"账号 {spec['username']} 角色 = {spec['role']}（实际 {roles}）")

        # 空间、人员、关系
        communities = session.execute(select(Community).where(Community.deleted.is_(False))).scalars().all()
        report(len(communities) >= 2, f"小区 >= 2 个（实际 {len(communities)}）")
        main = _first(session, Community, name=COMMUNITY_NAME)
        report(main is not None, f"小区「{COMMUNITY_NAME}」存在")
        houses = session.execute(
            select(House).where(House.deleted.is_(False), House.community_id == (main.id if main else -1))
        ).scalars().all()
        report(len(houses) >= 16, f"「{COMMUNITY_NAME}」房屋 >= 16 套（实际 {len(houses)}）")
        persons = session.execute(select(Person).where(Person.deleted.is_(False))).scalars().all()
        report(len(persons) >= 8, f"人员档案 >= 8 人（实际 {len(persons)}）")
        same_name = [item for item in persons if item.name == "李娜"]
        report(len(same_name) >= 2, f"同名「李娜」>= 2 条用于消歧（实际 {len(same_name)}）")
        relations = session.execute(
            select(HousePerson).where(HousePerson.deleted.is_(False), HousePerson.status == "active")
        ).scalars().all()
        report(len(relations) >= 9, f"有效房屋关系 >= 9 条（实际 {len(relations)}）")

        # 工单覆盖 6 个状态
        orders = session.execute(select(WorkOrder).where(WorkOrder.deleted.is_(False))).scalars().all()
        report(len(orders) >= 9, f"工单 >= 9 张（实际 {len(orders)}）")
        statuses = {row.status for row in orders}
        report(statuses == set(models.ORDER_STATUS_TEXT), f"工单覆盖全部 6 个状态（实际 {sorted(statuses)}）")
        logs = session.execute(select(OrderLog).where(OrderLog.deleted.is_(False))).scalars().all()
        report(len(logs) >= 15, f"工单流转日志 >= 15 条（实际 {len(logs)}）")
        audits = session.execute(select(AuditLog).where(AuditLog.deleted.is_(False))).scalars().all()
        report(len(audits) >= 9, f"审计记录 >= 9 条（实际 {len(audits)}）")
        for model, label, least in (
            (Lease, "租赁", 1),
            (Complaint, "投诉", 3),
            (Visitor, "访客", 3),
            (Vehicle, "车辆", 3),
            (ParkingSpace, "车位", 3),
            (Device, "设备", 3),
            (Inspection, "巡检", 3),
            (Bill, "账单", 3),
            (Payment, "收款", 1),
        ):
            count = session.execute(
                select(model).where(model.deleted.is_(False))
            ).scalars().all()
            report(len(count) >= least, f"{label}数据 >= {least} 条（实际 {len(count)}）")
        report(
            _first(session, AgentSession, title=AGENT_SESSION_TITLE) is not None,
            f"AI 演示会话「{AGENT_SESSION_TITLE}」存在",
        )

        # 数据范围隔离
        by_username = {spec["username"]: _first(session, User, username=spec["username"]) for spec in ACCOUNT_SPECS}
        admin_id = by_username["admin"].id
        manager_communities = queries.list_communities(Policy(session, by_username["manager01"]), page_size=50)["items"]
        report(
            all(item["name"] != SECOND_COMMUNITY_NAME for item in manager_communities),
            "经理账号看不到「美家花园二期」（小区隔离）",
        )
        owner_orders = queries.list_work_orders(Policy(session, by_username["owner01"]), page_size=50)["items"]
        report(
            all(item["owner_id"] == by_username["owner01"].id for item in owner_orders),
            f"业主账号只看到本人相关工单（{len(owner_orders)} 张）",
        )
        engineer_orders = queries.list_work_orders(Policy(session, by_username["engineer01"]), page_size=50)["items"]
        report(
            all(item["repairer_id"] == by_username["engineer01"].id for item in engineer_orders),
            f"维修师傅只看到被派给他的工单（{len(engineer_orders)} 张）",
        )
        admin_orders = queries.list_work_orders(Policy(session, admin_id), page_size=50)["items"]
        report(len(admin_orders) == len(orders), f"管理员看到全部工单（{len(admin_orders)} 张）")

    print("[OK] 演示数据自检通过" if ok else "[FAIL] 演示数据自检未通过")
    return ok


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="演示数据种子（幂等）")
    parser.add_argument("command", nargs="?", default="seed", choices=("seed", "check", "reset"))
    parser.add_argument("--seed", action="store_true", help="reset 之后立即写入演示数据")
    args = parser.parse_args(argv)

    if args.command == "check":
        return 0 if check() else 1
    if args.command == "reset":
        dropped = reset()
        print(f"[OK] 已删除 {len(dropped)} 张表并重新建表：{'、'.join(dropped) if dropped else '（原本没有表）'}")
        if not args.seed:
            return 0
    result = seed()
    print(
        f"[OK] 演示数据就绪：账号 {result['accounts']} 个、房屋 {result['houses']} 套（口令 {DEMO_PASSWORD}）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
