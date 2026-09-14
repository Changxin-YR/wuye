"""运营模块写命令：投诉 / 访客 / 车辆车位 / 设备巡检（PropertyService 的第二部分）。

与 ``services.py`` 使用**完全相同**的约定，页面表单与智能体 MCP 工具共用这一套函数：

    fn(policy, *, request_key=None, expected_version=None, source="web", **params) -> dict

固定流程：

    require(权限) → require_scope(数据范围) → 幂等（request_key 命中则回放）
    → 参数校验 → 状态机 → 乐观锁（expected_version 不符 → 409）
    → 写库 + audit_log → 回读 → 返回 {..., "message", "version"}

- 业务错误统一抛 :class:`services.ServiceError`（通俗中文），web 层转 flash。
- 权限/数据范围越权由 :class:`permissions.Policy` 抛 ``abort(401/403/404)``。
- 风险级别（R0–R3）登记在 ``risk.py``，本模块只实现业务，不碰风险表。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import func, select

import queries_ops as qo
from audit import write_audit
from models import (
    Building,
    Complaint,
    Device,
    House,
    Inspection,
    ParkingSpace,
    Person,
    User,
    Vehicle,
    Visitor,
    utcnow,
)
from permissions import Policy
from services import (
    ServiceError,
    _choice,
    _commit,
    _identifier,
    _int,
    _phone,
    _pick,
    _text,
    resolve_house,
)

#: 幂等键写进 audit_log.detail 的字段名
_IDEMPOTENT_FIELD = "request_key"

COMPLAINT_CATEGORIES = {
    "noise": "noise",
    "噪音": "noise",
    "扰民": "noise",
    "clean": "clean",
    "卫生": "clean",
    "环境": "clean",
    "elevator": "elevator",
    "电梯": "elevator",
    "public": "public",
    "公共设施": "public",
    "parking": "parking",
    "停车": "parking",
    "other": "other",
    "其他": "other",
}

DEVICE_CATEGORIES = {
    "elevator": "elevator",
    "电梯": "elevator",
    "access": "access",
    "门禁": "access",
    "fire": "fire",
    "消防": "fire",
    "water": "water",
    "供水": "water",
    "power": "power",
    "供电": "power",
    "other": "other",
    "其他": "other",
}

#: 投诉状态机：当前状态 → {动作: 目标状态}
COMPLAINT_TRANSITIONS: dict[int, dict[str, int]] = {
    0: {"assign": 1, "cancel": 3},
    1: {"handle": 2, "close": 2, "cancel": 3},
    2: {},
    3: {},
}

#: 访客状态机
VISITOR_TRANSITIONS: dict[int, dict[str, int]] = {
    0: {"enter": 1, "cancel": 3},
    1: {"leave": 2, "cancel": 3},
    2: {},
    3: {},
}

#: 巡检状态机
INSPECTION_TRANSITIONS: dict[int, dict[str, int]] = {
    0: {"complete": 1, "reopen": 2},
    1: {},
    2: {},
}


# --------------------------------------------------------------------------
# 通用工具
# --------------------------------------------------------------------------
def _replay(actor: Policy, action: str, request_key: Any) -> Optional[dict]:
    """幂等回放：同一个 request_key 只执行一次，命中就返回上次结果说明。"""
    key = "" if request_key is None else str(request_key).strip()
    if not key:
        return None
    from audit import AuditLog

    row = actor.db.execute(
        select(AuditLog)
        .where(AuditLog.action == action, AuditLog.source == (actor.source or "web"))
        .order_by(AuditLog.id.desc())
        .limit(50)
    ).scalars().all()
    for item in row:
        detail = item.detail or {}
        if isinstance(detail, dict) and str(detail.get(_IDEMPOTENT_FIELD)) == key:
            return {
                "ok": True,
                "id": int(item.target_id) if str(item.target_id).isdigit() else item.target_id,
                "target_type": item.target_type,
                "message": (detail or {}).get("message") or "该请求已经处理过，未重复执行",
                "version": (detail or {}).get("version"),
                "replayed": True,
            }
    return None


def _audit_detail(actor: Policy, payload: dict, request_key: Any, message: str, version: Any) -> dict:
    detail = dict(payload)
    if request_key not in (None, ""):
        detail[_IDEMPOTENT_FIELD] = str(request_key).strip()
    detail["message"] = message
    detail["version"] = version
    return detail


def _check_version(row: Any, expected_version: Any) -> None:
    """乐观锁：调用方给了 expected_version 就必须与库里一致。"""
    if expected_version in (None, ""):
        return
    try:
        want = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ServiceError("invalid", "版本号必须是整数") from exc
    if int(getattr(row, "version", 0) or 0) != want:
        raise ServiceError("conflict", "这条数据刚刚被其他人修改过，请刷新后重试")


def _resolve_ref(actor: Policy, model: type, ref: Any, label: str, name_field: str = "name") -> Any:
    """按 id 或名称/编号解析一行（带数据范围）。"""
    ref = _identifier(ref, label)
    if isinstance(ref, int):
        row = actor.db.execute(actor.query(model).where(model.id == ref)).scalars().first()
        if row is None:
            raise ServiceError("not_found", f"没有找到编号为 {ref} 的{label}，或者它不在你可见的范围内")
        return row
    text = str(ref).strip()
    rows = actor.db.execute(
        actor.query(model).where(getattr(model, name_field).like(f"%{text}%"))
    ).scalars().unique().all()
    exact = [item for item in rows if text in {getattr(item, name_field, ""), getattr(item, "code", "")}]
    return _pick(list(exact or rows), f"{label}「{text}」")


def _next_no(db, model: type, prefix: str, moment: datetime) -> str:
    """生成业务单号：``前缀 + YYYYMMDD + 4 位序号``。"""
    head = f"{prefix}{moment.strftime('%Y%m%d')}"
    latest = db.execute(select(func.max(model.no)).where(model.no.like(f"{head}%"))).scalar()
    seq = 1
    if latest and str(latest)[len(head):].isdigit():
        seq = int(str(latest)[len(head):]) + 1
    return f"{head}{seq:04d}"


def _house_of(actor: Policy, house_id: Any = None, house: Any = None, *, required: bool = True) -> Optional[House]:
    """解析房屋（可空）。"""
    ref = house_id if house_id not in (None, "") else house
    if ref in (None, ""):
        if required:
            raise ServiceError("invalid", "请选择房屋")
        return None
    return resolve_house(actor, ref)


# --------------------------------------------------------------------------
# 投诉
# --------------------------------------------------------------------------
def _complaint_house(actor: Policy, house_id: Any = None, house: Any = None) -> Any:
    """解析投诉的「涉事房屋」。

    投诉对象允许**不是投诉人自己的房子**（典型场景：业主投诉楼上装修噪音，
    举报对象是邻居家），所以业主不能走 ``resolve_house``——那条路按「本人房屋」
    范围取，邻居家根本取不到。这里改成「按编号直取 + 由调用方校验小区」，
    业主最多只能在本小区里指认，跨小区仍会被 require_scope 挡掉。
    物业内部角色行为不变，仍走原来的房屋范围。
    """
    from models import House

    if actor.primary_scope != "self":
        return _house_of(actor, house_id, house)

    ref = house_id if house_id not in (None, "") else house
    if ref in (None, ""):
        raise ServiceError("invalid", "请选择房屋")
    try:
        key = int(ref)
    except (TypeError, ValueError):
        key = None
    row = actor.db.get(House, key) if key is not None else None
    if row is None or row.deleted:
        raise ServiceError("not_found", f"没有找到编号为 {ref} 的房屋")
    return row


def create_complaint(
    actor: Policy,
    *,
    request_key: Any = None,
    expected_version: Any = None,
    source: str = "web",
    house_id: Any = None,
    content: Any = None,
    category: Any = None,
    house: Any = None,
    reporter: Any = None,
) -> dict:
    """登记投诉（客服/经理/业主都能报）。

    ``house`` 是**涉事房屋**而不是投诉人自己的房子——典型场景是楼上装修噪音，
    业主得能选邻居家。所以这里按「本小区」判定，而不是按「自己的房子」判定。
    """
    actor.require("complaint.create")
    db = actor.db
    cached = _replay(actor, "complaint.create", request_key)
    if cached:
        return cached

    target = _complaint_house(actor, house_id, house)
    # 涉事房屋必须落在投诉人所在的小区里：
    #   内部角色（all/community）→ 自己数据范围内的小区；
    #   业主（self）→ 由自有房屋推导出的小区（可以选本小区任意一套，包括邻居家）。
    # 这里用读级别的 require_scope，业主的 self 范围对小区是成立的。
    actor.require_scope(target.community_id, None)

    text = _text(content, "投诉内容", maxlen=1000, minlen=2)
    code = _choice(category, COMPLAINT_CATEGORIES, set(qo.COMPLAINT_CATEGORY_TEXT), "投诉类型", default="other")

    reporter_person = None
    if reporter not in (None, ""):
        reporter_person = _resolve_ref(actor, Person, reporter, "投诉人")
    if reporter_person is None:
        reporter_person = _my_person(actor)
    moment = utcnow()
    row = Complaint(
        no=_next_no(db, Complaint, "TS", moment),
        community_id=target.community_id,
        building_id=target.building_id,
        house_id=target.id,
        reporter_id=reporter_person.id if reporter_person is not None else None,
        content=text,
        category=code,
        status=0,
    )
    db.add(row)
    db.flush()
    payload = {
        "no": row.no,
        "house": target.full_name,
        "category": code,
        "reporter": reporter_person.name if reporter_person is not None else "",
    }
    message = f"投诉 {row.no} 已登记，房屋 {target.full_name}"
    write_audit(
        db,
        actor,
        "complaint.create",
        "complaint",
        row.id,
        _audit_detail(actor, payload, request_key, message, row.version),
        source=source,
    )
    _commit(db)
    result = qo.complaint_to_dict(actor, row)
    result.update({"message": message, "version": row.version})
    return result


def assign_complaint(
    actor: Policy,
    *,
    request_key: Any = None,
    expected_version: Any = None,
    source: str = "web",
    complaint_id: Any = None,
    handler: Any = None,
    note: Any = "",
) -> dict:
    """把投诉分配给处理人（待处理 → 处理中）。"""
    actor.require("complaint.handle")
    db = actor.db
    cached = _replay(actor, "complaint.assign", request_key)
    if cached:
        return cached

    row = _resolve_ref(actor, Complaint, complaint_id, "投诉", name_field="no")
    actor.require_scope(row.community_id, None)
    _check_version(row, expected_version)
    if row.status not in (0,):
        raise ServiceError("state", f"投诉当前是「{qo.complaint_status_text(row.status)}」，不能再分配")

    target_user = _resolve_ref(actor, User, handler, "处理人", name_field="real_name") if handler not in (None, "") else None
    if target_user is None:
        target_user = actor.user
    if target_user is None:
        raise ServiceError("invalid", "请指定处理人")

    from_status = int(row.status)
    row.handler_id = target_user.id
    row.status = COMPLAINT_TRANSITIONS[from_status]["assign"]
    row.touch()
    note_text = _text(note, "处理说明", required=False, maxlen=500)
    payload = {
        "no": row.no,
        "handler": target_user.display_name(),
        "from_status": qo.complaint_status_text(from_status),
        "to_status": qo.complaint_status_text(row.status),
        "note": note_text,
    }
    message = f"投诉 {row.no} 已分配给 {target_user.display_name()}"
    write_audit(
        db, actor, "complaint.assign", "complaint", row.id,
        _audit_detail(actor, payload, request_key, message, row.version), source=source,
    )
    _commit(db)
    result = qo.complaint_to_dict(actor, row)
    result.update({"message": message, "version": row.version})
    return result


def handle_complaint(
    actor: Policy,
    *,
    request_key: Any = None,
    expected_version: Any = None,
    source: str = "web",
    complaint_id: Any = None,
    result: Any = "",
    note: Any = "",
) -> dict:
    """记录处理结果（处理中 → 保持处理中，或直接结案）。"""
    actor.require("complaint.handle")
    db = actor.db
    cached = _replay(actor, "complaint.handle", request_key)
    if cached:
        return cached

    row = _resolve_ref(actor, Complaint, complaint_id, "投诉", name_field="no")
    actor.require_scope(row.community_id, None)
    _check_version(row, expected_version)
    if row.status not in (0, 1):
        raise ServiceError("state", f"投诉当前是「{qo.complaint_status_text(row.status)}」，不能再记录处理")
    text = _text(result, "处理结果", maxlen=1000, minlen=2)
    if row.status == 0:
        row.status = 1
        row.handler_id = row.handler_id or actor.user_id
    row.result = text
    row.touch()
    payload = {"no": row.no, "result": text, "note": _text(note, "备注", required=False, maxlen=500)}
    message = f"投诉 {row.no} 已记录处理结果"
    write_audit(
        db, actor, "complaint.handle", "complaint", row.id,
        _audit_detail(actor, payload, request_key, message, row.version), source=source,
    )
    _commit(db)
    out = qo.complaint_to_dict(actor, row)
    out.update({"message": message, "version": row.version})
    return out


def close_complaint(
    actor: Policy,
    *,
    request_key: Any = None,
    expected_version: Any = None,
    source: str = "web",
    complaint_id: Any = None,
    result: Any = "",
) -> dict:
    """结案（处理中 → 已结案）。"""
    actor.require("complaint.handle")
    db = actor.db
    cached = _replay(actor, "complaint.close", request_key)
    if cached:
        return cached

    row = _resolve_ref(actor, Complaint, complaint_id, "投诉", name_field="no")
    actor.require_scope(row.community_id, None)
    _check_version(row, expected_version)
    if row.status not in (1,):
        raise ServiceError("state", f"投诉当前是「{qo.complaint_status_text(row.status)}」，只有处理中的投诉才能结案")
    if result not in (None, ""):
        row.result = _text(result, "结案说明", maxlen=1000, minlen=2)
    row.status = COMPLAINT_TRANSITIONS[1]["close"]
    row.touch()
    payload = {"no": row.no, "result": row.result}
    message = f"投诉 {row.no} 已结案"
    write_audit(
        db, actor, "complaint.close", "complaint", row.id,
        _audit_detail(actor, payload, request_key, message, row.version), source=source,
    )
    _commit(db)
    out = qo.complaint_to_dict(actor, row)
    out.update({"message": message, "version": row.version})
    return out


def cancel_complaint(
    actor: Policy,
    *,
    request_key: Any = None,
    expected_version: Any = None,
    source: str = "web",
    complaint_id: Any = None,
    reason: Any = None,
) -> dict:
    """取消投诉（非终态 → 已取消），原因必填。"""
    actor.require("complaint.handle")
    db = actor.db
    cached = _replay(actor, "complaint.cancel", request_key)
    if cached:
        return cached

    row = _resolve_ref(actor, Complaint, complaint_id, "投诉", name_field="no")
    actor.require_scope(row.community_id, None)
    _check_version(row, expected_version)
    if row.status not in (0, 1):
        raise ServiceError("state", f"投诉当前是「{qo.complaint_status_text(row.status)}」，不能再取消")
    reason_text = _text(reason, "取消原因", maxlen=500)
    row.status = COMPLAINT_TRANSITIONS[int(row.status)]["cancel"]
    row.result = f"已取消：{reason_text}"
    row.touch()
    payload = {"no": row.no, "reason": reason_text}
    message = f"投诉 {row.no} 已取消"
    write_audit(
        db, actor, "complaint.cancel", "complaint", row.id,
        _audit_detail(actor, payload, request_key, message, row.version), source=source,
    )
    _commit(db)
    out = qo.complaint_to_dict(actor, row)
    out.update({"message": message, "version": row.version})
    return out


# --------------------------------------------------------------------------
# 访客
# --------------------------------------------------------------------------
def register_visitor(
    actor: Policy,
    *,
    request_key: Any = None,
    expected_version: Any = None,
    source: str = "web",
    house_id: Any = None,
    name: Any = None,
    phone: Any = None,
    purpose: Any = "",
    visit_at: Any = None,
    house: Any = None,
) -> dict:
    """登记访客（默认待进）。"""
    actor.require("visitor.write")
    db = actor.db
    cached = _replay(actor, "visitor.register", request_key)
    if cached:
        return cached

    target = _house_of(actor, house_id, house)
    actor.require_house(target)  # 同上：访客登记同样支持业主自助
    name_text = _text(name, "访客姓名", maxlen=64)
    phone_text = _phone(phone, "访客电话")
    purpose_text = _text(purpose, "来访事由", required=False, maxlen=255)
    when = utcnow()
    if visit_at not in (None, ""):
        moment = _parse_time(visit_at)
        if moment is not None:
            when = moment

    row = Visitor(
        community_id=target.community_id,
        building_id=target.building_id,
        house_id=target.id,
        name=name_text,
        phone=phone_text,
        visit_at=when,
        purpose=purpose_text,
        status=0,
        operator_id=actor.user_id,
    )
    db.add(row)
    db.flush()
    payload = {"name": name_text, "phone": phone_text, "house": target.full_name, "purpose": purpose_text}
    message = f"访客 {name_text} 已登记，到访 {target.full_name}"
    write_audit(
        db, actor, "visitor.register", "visitor", row.id,
        _audit_detail(actor, payload, request_key, message, row.version), source=source,
    )
    _commit(db)
    out = qo.visitor_to_dict(actor, row)
    out.update({"message": message, "version": row.version})
    return out


def enter_visitor(
    actor: Policy,
    *,
    request_key: Any = None,
    expected_version: Any = None,
    source: str = "web",
    visitor_id: Any = None,
) -> dict:
    """访客进门（待进 → 已进）。"""
    actor.require("visitor.write")
    db = actor.db
    cached = _replay(actor, "visitor.enter", request_key)
    if cached:
        return cached
    row = _resolve_ref(actor, Visitor, visitor_id, "访客")
    actor.require_scope(row.community_id, None)
    _check_version(row, expected_version)
    if row.status != 0:
        raise ServiceError("state", f"访客当前是「{qo.visitor_status_text(row.status)}」，不能办理进门")
    row.status = VISITOR_TRANSITIONS[0]["enter"]
    row.touch()
    message = f"访客 {row.name} 已进门"
    write_audit(
        db, actor, "visitor.enter", "visitor", row.id,
        _audit_detail(actor, {"name": row.name}, request_key, message, row.version), source=source,
    )
    _commit(db)
    out = qo.visitor_to_dict(actor, row)
    out.update({"message": message, "version": row.version})
    return out


def leave_visitor(
    actor: Policy,
    *,
    request_key: Any = None,
    expected_version: Any = None,
    source: str = "web",
    visitor_id: Any = None,
) -> dict:
    """访客离开（已进 → 已离）。"""
    actor.require("visitor.write")
    db = actor.db
    cached = _replay(actor, "visitor.leave", request_key)
    if cached:
        return cached
    row = _resolve_ref(actor, Visitor, visitor_id, "访客")
    actor.require_scope(row.community_id, None)
    _check_version(row, expected_version)
    if row.status != 1:
        raise ServiceError("state", f"访客当前是「{qo.visitor_status_text(row.status)}」，不能办理离开")
    row.status = VISITOR_TRANSITIONS[1]["leave"]
    row.touch()
    message = f"访客 {row.name} 已离开"
    write_audit(
        db, actor, "visitor.leave", "visitor", row.id,
        _audit_detail(actor, {"name": row.name}, request_key, message, row.version), source=source,
    )
    _commit(db)
    out = qo.visitor_to_dict(actor, row)
    out.update({"message": message, "version": row.version})
    return out


def cancel_visitor(
    actor: Policy,
    *,
    request_key: Any = None,
    expected_version: Any = None,
    source: str = "web",
    visitor_id: Any = None,
    reason: Any = "",
) -> dict:
    """取消访客登记（非终态 → 已取消）。"""
    actor.require("visitor.write")
    db = actor.db
    cached = _replay(actor, "visitor.cancel", request_key)
    if cached:
        return cached
    row = _resolve_ref(actor, Visitor, visitor_id, "访客")
    actor.require_scope(row.community_id, None)
    _check_version(row, expected_version)
    if row.status not in (0, 1):
        raise ServiceError("state", f"访客当前是「{qo.visitor_status_text(row.status)}」，不能再取消")
    reason_text = _text(reason, "取消原因", required=False, maxlen=255)
    row.status = VISITOR_TRANSITIONS[int(row.status)]["cancel"]
    if reason_text:
        row.purpose = f"{row.purpose}（{reason_text}）" if row.purpose else reason_text
    row.touch()
    message = f"访客 {row.name} 的登记已取消"
    write_audit(
        db, actor, "visitor.cancel", "visitor", row.id,
        _audit_detail(actor, {"name": row.name, "reason": reason_text}, request_key, message, row.version),
        source=source,
    )
    _commit(db)
    out = qo.visitor_to_dict(actor, row)
    out.update({"message": message, "version": row.version})
    return out


# --------------------------------------------------------------------------
# 车辆 / 车位
# --------------------------------------------------------------------------
def create_vehicle(
    actor: Policy,
    *,
    request_key: Any = None,
    expected_version: Any = None,
    source: str = "web",
    plate: Any = None,
    house_id: Any = None,
    brand: Any = "",
    owner_person_id: Any = None,
    house: Any = None,
) -> dict:
    """登记车辆（车牌唯一）。"""
    actor.require("vehicle.write")
    db = actor.db
    cached = _replay(actor, "vehicle.create", request_key)
    if cached:
        return cached

    plate_text = _text(plate, "车牌号", maxlen=16).upper()
    target = resolve_house(actor, house_id) if house_id not in (None, "") else None
    if target is None and house not in (None, ""):
        target = resolve_house(actor, house)
    if target is not None:
        actor.require_scope(target.community_id, target.building_id, write=True)
    exists = db.execute(
        select(Vehicle.id).where(Vehicle.plate == plate_text, Vehicle.deleted.is_(False))
    ).scalars().first()
    if exists:
        raise ServiceError("conflict", f"车牌 {plate_text} 已经登记过了")

    owner_id = None
    if owner_person_id not in (None, ""):
        owner_id = _resolve_ref(actor, Person, owner_person_id, "车主").id

    row = Vehicle(
        community_id=target.community_id if target else None,
        house_id=target.id if target else None,
        plate=plate_text,
        brand=_text(brand, "品牌", required=False, maxlen=64),
        owner_person_id=owner_id,
        status=0,
    )
    db.add(row)
    db.flush()
    payload = {"plate": plate_text, "house": target.full_name if target else "", "brand": row.brand}
    message = f"车辆 {plate_text} 已登记"
    write_audit(
        db, actor, "vehicle.create", "vehicle", row.id,
        _audit_detail(actor, payload, request_key, message, row.version), source=source,
    )
    _commit(db)
    out = qo.vehicle_to_dict(actor, row)
    out.update({"message": message, "version": row.version})
    return out


def update_vehicle(
    actor: Policy,
    *,
    request_key: Any = None,
    expected_version: Any = None,
    source: str = "web",
    vehicle_id: Any = None,
    plate: Any = None,
    brand: Any = None,
    house_id: Any = None,
    owner_person_id: Any = None,
    _unset: Any = None,
) -> dict:
    """修改车辆信息（只改传了的字段）。"""
    actor.require("vehicle.write")
    db = actor.db
    cached = _replay(actor, "vehicle.update", request_key)
    if cached:
        return cached
    row = _resolve_ref(actor, Vehicle, vehicle_id, "车辆", name_field="plate")
    actor.require_scope(row.community_id, None)
    _check_version(row, expected_version)

    changed: dict = {}
    if plate not in (None, ""):
        new_plate = _text(plate, "车牌号", maxlen=16).upper()
        if new_plate != row.plate:
            dup = db.execute(
                select(Vehicle.id).where(
                    Vehicle.plate == new_plate, Vehicle.deleted.is_(False), Vehicle.id != row.id
                )
            ).scalars().first()
            if dup:
                raise ServiceError("conflict", f"车牌 {new_plate} 已经登记过了")
            changed["plate"] = f"{row.plate} → {new_plate}"
            row.plate = new_plate
    if brand not in (None, ""):
        row.brand = _text(brand, "品牌", required=False, maxlen=64)
        changed["brand"] = row.brand
    if house_id not in (None, ""):
        target = resolve_house(actor, house_id)
        actor.require_scope(target.community_id, target.building_id, write=True)
        row.house_id = target.id
        row.community_id = target.community_id
        changed["house"] = target.full_name
    if owner_person_id not in (None, ""):
        row.owner_person_id = _resolve_ref(actor, Person, owner_person_id, "车主").id
        changed["owner_person_id"] = row.owner_person_id
    if not changed:
        raise ServiceError("invalid", "没有需要修改的内容")
    row.touch()
    message = f"车辆 {row.plate} 已更新"
    write_audit(
        db, actor, "vehicle.update", "vehicle", row.id,
        _audit_detail(actor, changed, request_key, message, row.version), source=source,
    )
    _commit(db)
    out = qo.vehicle_to_dict(actor, row)
    out.update({"message": message, "version": row.version})
    return out


def archive_vehicle(
    actor: Policy,
    *,
    request_key: Any = None,
    expected_version: Any = None,
    source: str = "web",
    vehicle_id: Any = None,
    reason: Any = "",
) -> dict:
    """归档车辆（软删：status=1 + deleted），并释放占用的车位。"""
    actor.require("vehicle.write")
    db = actor.db
    cached = _replay(actor, "vehicle.archive", request_key)
    if cached:
        return cached
    row = _resolve_ref(actor, Vehicle, vehicle_id, "车辆", name_field="plate")
    actor.require_scope(row.community_id, None)
    _check_version(row, expected_version)
    if int(row.status) == 1:
        raise ServiceError("state", "这辆车已经归档过了")
    plate = row.plate
    row.status = 1
    row.deleted = True
    row.touch()
    released = []
    spaces = db.execute(
        select(ParkingSpace).where(ParkingSpace.vehicle_id == row.id, ParkingSpace.deleted.is_(False))
    ).scalars().all()
    for space in spaces:
        space.vehicle_id = None
        space.house_id = None
        space.status = 0
        space.touch()
        released.append(space.code)
    payload = {"plate": plate, "reason": _text(reason, "归档原因", required=False, maxlen=255), "released": released}
    message = f"车辆 {plate} 已归档" + (f"，释放车位 {'、'.join(released)}" if released else "")
    write_audit(
        db, actor, "vehicle.archive", "vehicle", row.id,
        _audit_detail(actor, payload, request_key, message, row.version), source=source,
    )
    _commit(db)
    return {"ok": True, "id": row.id, "plate": plate, "released": released, "message": message, "version": row.version}


def assign_parking(
    actor: Policy,
    *,
    request_key: Any = None,
    expected_version: Any = None,
    source: str = "web",
    space_id: Any = None,
    vehicle_id: Any = None,
    house_id: Any = None,
) -> dict:
    """分配车位：车位必须空闲，车辆必须未归档且未占用其它车位。"""
    actor.require("parking.write")
    db = actor.db
    cached = _replay(actor, "parking.assign", request_key)
    if cached:
        return cached

    space = _resolve_ref(actor, ParkingSpace, space_id, "车位", name_field="code")
    actor.require_scope(space.community_id, None, write=True)
    _check_version(space, expected_version)
    if int(space.status) == 1:
        raise ServiceError("state", f"车位 {space.code} 已被占用，请先释放")

    vehicle = _resolve_ref(actor, Vehicle, vehicle_id, "车辆", name_field="plate")
    if int(vehicle.status) == 1:
        raise ServiceError("state", f"车辆 {vehicle.plate} 已归档，不能分配车位")

    used = db.execute(
        select(ParkingSpace)
        .where(ParkingSpace.vehicle_id == vehicle.id, ParkingSpace.status == 1, ParkingSpace.deleted.is_(False))
    ).scalars().first()
    if used is not None:
        raise ServiceError("conflict", f"车辆 {vehicle.plate} 已经占用车位 {used.code}，请先释放")

    target_house = vehicle.house_id
    if house_id not in (None, ""):
        house_row = resolve_house(actor, house_id)
        actor.require_scope(house_row.community_id, house_row.building_id, write=True)
        target_house = house_row.id

    space.vehicle_id = vehicle.id
    space.house_id = target_house
    space.status = 1
    space.touch()
    payload = {"space": space.code, "plate": vehicle.plate}
    message = f"车位 {space.code} 已分配给 {vehicle.plate}"
    write_audit(
        db, actor, "parking.assign", "parking_space", space.id,
        _audit_detail(actor, payload, request_key, message, space.version), source=source,
    )
    _commit(db)
    out = qo.parking_to_dict(actor, space)
    out.update({"message": message, "version": space.version})
    return out


def release_parking(
    actor: Policy,
    *,
    request_key: Any = None,
    expected_version: Any = None,
    source: str = "web",
    space_id: Any = None,
) -> dict:
    """释放车位（占用 → 空闲）。"""
    actor.require("parking.write")
    db = actor.db
    cached = _replay(actor, "parking.release", request_key)
    if cached:
        return cached
    space = _resolve_ref(actor, ParkingSpace, space_id, "车位", name_field="code")
    actor.require_scope(space.community_id, None, write=True)
    _check_version(space, expected_version)
    if int(space.status) == 0:
        raise ServiceError("state", f"车位 {space.code} 本来就是空闲的")
    plate = ""
    if space.vehicle_id:
        vehicle = db.get(Vehicle, space.vehicle_id)
        plate = vehicle.plate if vehicle else ""
    space.vehicle_id = None
    space.house_id = None
    space.status = 0
    space.touch()
    message = f"车位 {space.code} 已释放" + (f"（原车牌 {plate}）" if plate else "")
    write_audit(
        db, actor, "parking.release", "parking_space", space.id,
        _audit_detail(actor, {"space": space.code, "plate": plate}, request_key, message, space.version),
        source=source,
    )
    _commit(db)
    out = qo.parking_to_dict(actor, space)
    out.update({"message": message, "version": space.version})
    return out


# --------------------------------------------------------------------------
# 设备 / 巡检
# --------------------------------------------------------------------------
def apply_vehicle(
    actor: Policy,
    *,
    request_key: Any = None,
    source: str = "web",
    plate: Any = None,
    brand: Any = None,
    house_id: Any = None,
) -> dict:
    """业主申请登记车辆 → 落一条「待审批」的车辆，等物业审核。

    与 ``create_vehicle``（物业代登记，直接生效）是两条不同的路：
    自助申请走 ``resident.self``（只有业主有这个权限点，物业内部角色没有），
    并且只能申请自己名下的房屋。
    """
    actor.require("resident.self")
    db = actor.db
    cached = _replay(actor, "vehicle.apply", request_key)
    if cached:
        return cached
    target = _house_of(actor, house_id)
    actor.require_house(target)
    plate_text = _text(plate, "车牌号", maxlen=16)
    dup = db.execute(
        select(Vehicle.id).where(Vehicle.plate == plate_text, Vehicle.deleted.is_(False))
    ).scalars().first()
    if dup:
        raise ServiceError("conflict", f"车牌 {plate_text} 已经登记过或正在审批中")

    person = _my_person(actor)
    row = Vehicle(
        community_id=target.community_id, house_id=target.id, plate=plate_text,
        brand=_text(brand, "品牌", required=False, maxlen=32),
        owner_person_id=person.id if person is not None else None,
        status=2,
    )
    db.add(row)
    db.flush()
    message = f"车辆 {plate_text}（{target.full_name}）的登记申请已提交，等待物业审批"
    write_audit(
        db, actor, "vehicle.apply", "vehicle", row.id,
        _audit_detail(actor, {"plate": plate_text, "house": target.full_name}, request_key, message, row.version),
        source=source,
    )
    _commit(db)
    out = qo.vehicle_to_dict(actor, row)
    out.update({"message": message, "version": row.version})
    return out


def review_vehicle(
    actor: Policy,
    *,
    request_key: Any = None,
    expected_version: Any = None,
    source: str = "web",
    vehicle_id: Any = None,
    approve: Any = True,
    reason: Any = "",
) -> dict:
    """物业审批车辆登记申请：通过 → 正常；驳回 → 归档并留原因。"""
    actor.require("vehicle.write")
    db = actor.db
    row = _resolve_ref(actor, Vehicle, vehicle_id, "车辆", name_field="plate")
    actor.require_scope(row.community_id, None, write=True)
    _check_version(row, expected_version)
    if int(row.status) != 2:
        raise ServiceError("state", f"这条申请已经处理过了（当前：{qo.vehicle_status_text(row.status)}）")

    ok = str(approve).lower() in ("1", "true", "yes", "on", "y", "是", "通过")
    reason_text = _text(reason, "审批说明", required=False, maxlen=255)
    if ok:
        row.status = 0
        message = f"车辆 {row.plate} 的登记申请已通过"
    else:
        row.status = 1
        row.deleted = True
        message = f"车辆 {row.plate} 的登记申请已驳回" + (f"（{reason_text}）" if reason_text else "")
    row.touch()
    write_audit(
        db, actor, "vehicle.review", "vehicle", row.id,
        _audit_detail(actor, {"approve": ok, "reason": reason_text}, request_key, message, row.version),
        source=source,
    )
    _commit(db)
    out = qo.vehicle_to_dict(actor, row)
    out.update({"message": message, "version": row.version})
    return out


def apply_parking(
    actor: Policy,
    *,
    request_key: Any = None,
    source: str = "web",
    house_id: Any = None,
    note: Any = "",
) -> dict:
    """业主申请车位 → 落一条「待审批」的申请单（复用 parking_space.status=2）。

    申请单只是「我要一个车位」的凭证，不代表具体车位；物业审批通过时再指定
    一个空闲车位分配（见 ``review_parking``）。车位编号是物业的资产编号，
    所以申请单用 ``REQ-<房屋编号>`` 作为占位编号，不会和真实车位号混。
    """
    actor.require("resident.self")
    db = actor.db
    cached = _replay(actor, "parking.apply", request_key)
    if cached:
        return cached
    target = _house_of(actor, house_id)
    actor.require_house(target)
    pending = db.execute(
        select(ParkingSpace.id).where(
            ParkingSpace.house_id == target.id, ParkingSpace.status == 2, ParkingSpace.deleted.is_(False)
        )
    ).scalars().first()
    if pending:
        raise ServiceError("conflict", f"{target.full_name} 已经有一条待审批的车位申请了")

    row = ParkingSpace(
        community_id=target.community_id, code=f"REQ-{target.id:04d}", status=2,
        house_id=target.id, vehicle_id=None,
    )
    db.add(row)
    db.flush()
    message = f"{target.full_name} 的车位申请已提交，等待物业审批" + (
        f"（{_text(note, '备注', required=False, maxlen=100)}）" if note not in (None, "") else ""
    )
    write_audit(
        db, actor, "parking.apply", "parking_space", row.id,
        _audit_detail(actor, {"house": target.full_name, "code": row.code}, request_key, message, row.version),
        source=source,
    )
    _commit(db)
    out = qo.parking_to_dict(actor, row)
    out.update({"message": message, "version": row.version})
    return out


def review_parking(
    actor: Policy,
    *,
    request_key: Any = None,
    expected_version: Any = None,
    source: str = "web",
    space_id: Any = None,
    approve: Any = True,
    space_code: Any = "",
    reason: Any = "",
) -> dict:
    """物业审批车位申请。

    通过：必须指定一个**空闲的真实车位**，把它分配给申请房屋，申请单归档；
    驳回：申请单归档 + 审计留原因。
    """
    actor.require("parking.write")
    db = actor.db
    application = _resolve_ref(actor, ParkingSpace, space_id, "车位申请", name_field="code")
    actor.require_scope(application.community_id, None, write=True)
    _check_version(application, expected_version)
    if int(application.status) != 2:
        raise ServiceError("state", f"这条申请已经处理过了（当前：{qo.parking_status_text(application.status)}）")

    ok = str(approve).lower() in ("1", "true", "yes", "on", "y", "是", "通过")
    reason_text = _text(reason, "审批说明", required=False, maxlen=255)
    target_house = db.get(House, application.house_id)
    if not ok:
        application.deleted = True
        message = f"车位申请已驳回（{target_house.full_name if target_house else ''}）" + (
            f"：{reason_text}" if reason_text else ""
        )
    else:
        if space_code in (None, ""):
            raise ServiceError("invalid", "通过车位申请要指定分给哪个车位编号")
        space = _resolve_ref(actor, ParkingSpace, space_code, "车位", name_field="code")
        if int(space.status) == 1:
            raise ServiceError("state", f"车位 {space.code} 已被占用，换一个空闲车位")
        if int(space.status) == 2:
            raise ServiceError("state", f"{space.code} 是一条申请单，不是可分配的车位")
        space.house_id = application.house_id
        space.status = 1
        space.touch()
        application.deleted = True
        message = f"已通过车位申请：把 {space.code} 分配给 {target_house.full_name if target_house else '该房屋'}"

    write_audit(
        db, actor, "parking.review", "parking_space", application.id,
        _audit_detail(actor, {"approve": ok, "reason": reason_text, "space_code": str(space_code or "")},
                      request_key, message, application.version),
        source=source,
    )
    _commit(db)
    return {"message": message, "approve": ok, "application_id": application.id}


def create_device(
    actor: Policy,
    *,
    request_key: Any = None,
    expected_version: Any = None,
    source: str = "web",
    name: Any = None,
    community_id: Any = None,
    building_id: Any = None,
    category: Any = None,
    location: Any = "",
) -> dict:
    """新增设备。"""
    actor.require("device.write")
    db = actor.db
    cached = _replay(actor, "device.create", request_key)
    if cached:
        return cached

    name_text = _text(name, "设备名称", maxlen=64)
    from models import Community

    community = _resolve_ref(actor, Community, community_id, "小区") if community_id not in (None, "") else None
    if community is None:
        raise ServiceError("invalid", "请选择设备所在小区")
    building = None
    if building_id not in (None, ""):
        building = _resolve_ref(actor, Building, building_id, "楼栋")
    # 新增设备是唯一没有「既有对象」可校验的动作，只能按小区/楼栋级写范围来管：
    # 因此它天然是管理岗（all / community）的动作，维修工（assigned）做不了，
    # 页面上的「＋ 新增设备」入口也按这个口径隐藏，不让人点了才吃 403。
    actor.require_scope(community.id, building.id if building else None, write=True)
    code = _choice(category, DEVICE_CATEGORIES, set(qo.DEVICE_CATEGORY_TEXT), "设备类型", default="other")

    row = Device(
        community_id=community.id,
        building_id=building.id if building else None,
        name=name_text,
        category=code,
        status=0,
        location=_text(location, "安装位置", required=False, maxlen=128),
    )
    db.add(row)
    db.flush()
    payload = {"name": name_text, "category": code, "location": row.location, "community": community.name}
    message = f"设备「{name_text}」已新增"
    write_audit(
        db, actor, "device.create", "device", row.id,
        _audit_detail(actor, payload, request_key, message, row.version), source=source,
    )
    _commit(db)
    out = qo.device_to_dict(actor, row)
    out.update({"message": message, "version": row.version})
    return out


def update_device(
    actor: Policy,
    *,
    request_key: Any = None,
    expected_version: Any = None,
    source: str = "web",
    device_id: Any = None,
    name: Any = None,
    category: Any = None,
    status: Any = None,
    location: Any = None,
) -> dict:
    """修改设备（名称/类型/状态/位置，只改传了的字段）。"""
    actor.require("device.write")
    db = actor.db
    cached = _replay(actor, "device.update", request_key)
    if cached:
        return cached
    row = _resolve_ref(actor, Device, device_id, "设备")
    # 对象级范围校验：_resolve_ref 走的是 actor.query(Device)，维修工（scope=assigned）
    # 只能解析到自己巡检任务涉及的那几台设备。
    # 这里**不能**用 require_scope(write=True)：那一档只认 all/community，
    # 维修工拿着 device.write 也会被一律挡住，页面上每个按钮点了都是 403。
    actor.require_visible(row)
    _check_version(row, expected_version)

    changed: dict = {}
    if name not in (None, ""):
        row.name = _text(name, "设备名称", maxlen=64)
        changed["name"] = row.name
    if category not in (None, ""):
        row.category = _choice(category, DEVICE_CATEGORIES, set(qo.DEVICE_CATEGORY_TEXT), "设备类型")
        changed["category"] = row.category
    if status not in (None, ""):
        code = _int(status, "设备状态", minimum=0, maximum=2)
        row.status = code
        changed["status"] = qo.device_status_text(code)
    if location not in (None, ""):
        row.location = _text(location, "安装位置", required=False, maxlen=128)
        changed["location"] = row.location
    if not changed:
        raise ServiceError("invalid", "没有需要修改的内容")
    row.touch()
    message = f"设备「{row.name}」已更新"
    write_audit(
        db, actor, "device.update", "device", row.id,
        _audit_detail(actor, changed, request_key, message, row.version), source=source,
    )
    _commit(db)
    out = qo.device_to_dict(actor, row)
    out.update({"message": message, "version": row.version})
    return out


def archive_device(
    actor: Policy,
    *,
    request_key: Any = None,
    expected_version: Any = None,
    source: str = "web",
    device_id: Any = None,
    reason: Any = "",
) -> dict:
    """归档设备（软删），有未完成巡检时拒绝。"""
    actor.require("device.write")
    db = actor.db
    cached = _replay(actor, "device.archive", request_key)
    if cached:
        return cached
    row = _resolve_ref(actor, Device, device_id, "设备")
    actor.require_visible(row)  # 对象级：设备必须在自己可见范围内
    # 归档是 R3 破坏性台账动作（契约 §6）：对象在范围内还不够，还要小区级写范围，
    # 即只有管理岗（all / community）能归档；维修工只能更新设备状态，不能把台账归档掉。
    actor.require_scope(row.community_id, None, write=True)
    _check_version(row, expected_version)
    if int(row.status) == 2:
        raise ServiceError("state", "这台设备已经归档过了")
    pending = int(
        db.execute(
            select(func.count())
            .select_from(Inspection)
            .where(Inspection.device_id == row.id, Inspection.status == 0, Inspection.deleted.is_(False))
        ).scalar()
        or 0
    )
    if pending:
        raise ServiceError("conflict", f"设备「{row.name}」还有 {pending} 条待巡检任务，请先处理")
    name = row.name
    row.status = 2
    row.deleted = True
    row.touch()
    payload = {"name": name, "reason": _text(reason, "归档原因", required=False, maxlen=255)}
    message = f"设备「{name}」已归档"
    write_audit(
        db, actor, "device.archive", "device", row.id,
        _audit_detail(actor, payload, request_key, message, row.version), source=source,
    )
    _commit(db)
    return {"ok": True, "id": row.id, "name": name, "message": message, "version": row.version}


def create_inspection(
    actor: Policy,
    *,
    request_key: Any = None,
    expected_version: Any = None,
    source: str = "web",
    device_id: Any = None,
    assignee_id: Any = None,
    plan_at: Any = None,
) -> dict:
    """安排巡检任务（默认待巡检）。

    这一条是「派活」：谁巡检由这里决定，所以挂 ``inspection.assign`` 而不是
    ``inspection.write``——维修工有 inspection.write（能完成指派给自己的任务），
    但没有 inspection.assign，不能给自己派活、更不能给别人派。
    """
    actor.require("inspection.assign")
    db = actor.db
    cached = _replay(actor, "inspection.create", request_key)
    if cached:
        return cached
    device = _resolve_ref(actor, Device, device_id, "设备")
    if int(device.status) == 2:
        raise ServiceError("state", f"设备「{device.name}」已归档，不能安排巡检")
    # 对象级：设备已由 _resolve_ref 限定在可见范围内；巡检人也只能解析到自己
    # （User 的行级范围对维修工只有本人），所以维修工最多给自己加巡检任务，派不了别人。
    # 小区/楼栋级的 require_scope(write=True) 会把维修工全挡掉，这里不适用。
    actor.require_visible(device)

    assignee = None
    if assignee_id not in (None, ""):
        assignee = _resolve_ref(actor, User, assignee_id, "巡检人", name_field="real_name")
    when = utcnow()
    if plan_at not in (None, ""):
        parsed = _parse_time(plan_at)
        if parsed is not None:
            when = parsed

    row = Inspection(
        device_id=device.id,
        community_id=device.community_id,
        assignee_id=assignee.id if assignee else None,
        plan_at=when,
        status=0,
        result="",
    )
    db.add(row)
    db.flush()
    payload = {
        "device": device.name,
        "assignee": assignee.display_name() if assignee else "",
        "plan_at": when.strftime("%Y-%m-%d %H:%M"),
    }
    message = f"已安排巡检：{device.name}" + (f"（{assignee.display_name()}）" if assignee else "")
    write_audit(
        db, actor, "inspection.create", "inspection", row.id,
        _audit_detail(actor, payload, request_key, message, row.version), source=source,
    )
    _commit(db)
    out = qo.inspection_to_dict(actor, row)
    out.update({"message": message, "version": row.version})
    return out


def complete_inspection(
    actor: Policy,
    *,
    request_key: Any = None,
    expected_version: Any = None,
    source: str = "web",
    inspection_id: Any = None,
    result: Any = "",
    has_fault: Any = False,
    order_id: Any = None,
) -> dict:
    """完成巡检；``has_fault=True`` 时状态转「已转报修」并可关联工单。"""
    actor.require("inspection.write")
    db = actor.db
    cached = _replay(actor, "inspection.complete", request_key)
    if cached:
        return cached
    row = _resolve_ref(actor, Inspection, inspection_id, "巡检记录")
    # 契约口径是「inspection.* + assignee scope」：维修工只能完成指派给自己的任务，
    # 而这一点 _resolve_ref 的行级范围（Inspection.assignee_id == 我）已经保证了。
    actor.require_visible(row)
    _check_version(row, expected_version)
    if int(row.status) != 0:
        raise ServiceError("state", f"这条巡检已经是「{qo.inspection_status_text(row.status)}」，不能重复完成")

    fault = str(has_fault).lower() in ("1", "true", "yes", "on", "y", "是", "有")
    text = _text(result, "巡检结果", required=False, maxlen=1000)
    if not text and not fault:
        raise ServiceError("invalid", "请填写巡检结果")
    row.result = text or "发现故障，已转报修"
    row.status = 2 if fault else INSPECTION_TRANSITIONS[0]["complete"]
    if order_id not in (None, ""):
        from models import WorkOrder

        order = _resolve_ref(actor, WorkOrder, order_id, "工单", name_field="no")
        row.order_id = order.id
    row.touch()

    device = db.get(Device, row.device_id)
    if fault and device is not None:
        device.status = 1  # 设备转「维修中」
        device.touch()

    payload = {
        "device": device.name if device else "",
        "result": row.result,
        "status": qo.inspection_status_text(row.status),
    }
    message = (
        f"巡检完成：{device.name if device else '设备'}"
        + ("（已转报修）" if fault else "（一切正常）")
    )
    write_audit(
        db, actor, "inspection.complete", "inspection", row.id,
        _audit_detail(actor, payload, request_key, message, row.version), source=source,
    )
    _commit(db)
    out = qo.inspection_to_dict(actor, row)
    out.update({"message": message, "version": row.version})
    return out


# --------------------------------------------------------------------------
# 内部小工具
# --------------------------------------------------------------------------
def _parse_time(value: Any) -> Optional[datetime]:
    """解析 ``YYYY-MM-DD[ HH:MM[:SS]]``（解析不了返回 None，由调用方用「现在」兜底）。"""
    text = _text(value, "时间", required=False)
    if not text:
        return None
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(text.replace("T", " "), pattern)
        except ValueError:
            continue
    return None


def _my_person(actor: Policy):
    """当前登录用户绑定的人员档案（没有则 None）。"""
    if not actor.user_id:
        return None
    return actor.db.execute(
        select(Person).where(Person.user_id == actor.user_id, Person.deleted.is_(False))
    ).scalars().first()


#: 命令清单（智能体工具目录按这份清单登记风险级别）
COMMANDS = {
    "create_complaint": "complaint.create",
    "assign_complaint": "complaint.handle",
    "handle_complaint": "complaint.handle",
    "close_complaint": "complaint.handle",
    "cancel_complaint": "complaint.handle",
    "register_visitor": "visitor.write",
    "enter_visitor": "visitor.write",
    "leave_visitor": "visitor.write",
    "cancel_visitor": "visitor.write",
    "apply_vehicle": "resident.self",
    "review_vehicle": "vehicle.write",
    "apply_parking": "resident.self",
    "review_parking": "parking.write",
    "create_vehicle": "vehicle.write",
    "update_vehicle": "vehicle.write",
    "archive_vehicle": "vehicle.write",
    "assign_parking": "parking.write",
    "release_parking": "parking.write",
    "create_device": "device.write",
    "update_device": "device.write",
    "archive_device": "device.write",
    "create_inspection": "inspection.assign",
    "complete_inspection": "inspection.write",
}

__all__ = ["COMMANDS", "COMPLAINT_TRANSITIONS", "VISITOR_TRANSITIONS", "INSPECTION_TRANSITIONS"] + sorted(COMMANDS)
