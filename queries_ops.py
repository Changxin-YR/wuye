"""运营模块只读查询：投诉 / 访客 / 车辆车位 / 设备巡检。

约定（与 ``queries.py`` 完全一致，页面与智能体共用同一份数据）：
- 先过 :class:`permissions.Policy` 的 ``query`` / ``get``，行级数据范围由 SQL 完成；
- 列表统一返回 ``{items, total, page, page_size, pages, has_prev, has_next}``；
- 序列化里带好中文文案与关联对象名字（房屋全称、处理人、设备名…），模板与工具都不再自己拼。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from sqlalchemy import func, or_, select
from werkzeug.exceptions import BadRequest

import models
from models import (
    Complaint,
    Device,
    House,
    Inspection,
    ParkingSpace,
    Person,
    User,
    Vehicle,
    Visitor,
)
from permissions import Policy

#: 分页默认与上限（与 queries.py 保持一致）
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200

#: 各模块状态中文文案（唯一来源，页面与工具共用）
COMPLAINT_STATUS_TEXT = {0: "待处理", 1: "处理中", 2: "已结案", 3: "已取消"}
VISITOR_STATUS_TEXT = {0: "待进", 1: "已进", 2: "已离", 3: "已取消"}
VEHICLE_STATUS_TEXT = {0: "正常", 1: "已归档", 2: "待审批"}
#: 2 = 业主提交的车位申请单（审批通过后由物业分配真实车位，申请单归档）
PARKING_STATUS_TEXT = {0: "空闲", 1: "占用", 2: "待审批"}
DEVICE_STATUS_TEXT = {0: "正常", 1: "维修中", 2: "已归档"}
INSPECTION_STATUS_TEXT = {0: "待巡检", 1: "已完成", 2: "已转报修"}

COMPLAINT_CATEGORY_TEXT = {
    "noise": "噪音扰民",
    "clean": "环境卫生",
    "elevator": "电梯故障",
    "public": "公共设施",
    "parking": "车辆停放",
    "other": "其他",
}
DEVICE_CATEGORY_TEXT = {
    "elevator": "电梯",
    "access": "门禁",
    "fire": "消防",
    "water": "供水",
    "power": "供电",
    "other": "其他",
}


def _text_of(mapping: dict, value: Any) -> str:
    try:
        return mapping.get(int(value), f"未知({value})")
    except (TypeError, ValueError):
        return str(value or "")


def complaint_status_text(status: Any) -> str:
    return _text_of(COMPLAINT_STATUS_TEXT, status)


def visitor_status_text(status: Any) -> str:
    return _text_of(VISITOR_STATUS_TEXT, status)


def vehicle_status_text(status: Any) -> str:
    return _text_of(VEHICLE_STATUS_TEXT, status)


def parking_status_text(status: Any) -> str:
    return _text_of(PARKING_STATUS_TEXT, status)


def device_status_text(status: Any) -> str:
    return _text_of(DEVICE_STATUS_TEXT, status)


def inspection_status_text(status: Any) -> str:
    return _text_of(INSPECTION_STATUS_TEXT, status)


def complaint_category_text(category: Any) -> str:
    return COMPLAINT_CATEGORY_TEXT.get(str(category or ""), str(category or ""))


def device_category_text(category: Any) -> str:
    return DEVICE_CATEGORY_TEXT.get(str(category or ""), str(category or ""))


def _page_args(page: Any = 1, page_size: Any = DEFAULT_PAGE_SIZE) -> tuple[int, int]:
    try:
        page_no = max(1, int(page or 1))
    except (TypeError, ValueError):
        page_no = 1
    try:
        size = int(page_size or DEFAULT_PAGE_SIZE)
    except (TypeError, ValueError):
        size = DEFAULT_PAGE_SIZE
    return page_no, max(1, min(size, MAX_PAGE_SIZE))


def _meta(page_no: int, size: int, total: int) -> dict:
    pages = max(1, (total + size - 1) // size) if total else 1
    return {
        "page": page_no,
        "page_size": size,
        "pages": pages,
        "total": total,
        "has_prev": page_no > 1,
        "has_next": page_no < pages,
    }


def _keyword_like(columns: list, keyword: Any) -> list:
    text = "" if keyword is None else str(keyword).strip()
    if not text:
        return []
    return [or_(*[column.like(f"%{text}%") for column in columns])]


def _status_code(value: Any, mapping: dict, label: str = "状态") -> Optional[int]:
    if value in (None, "", "all", "全部", "所有"):
        return None
    text = str(value).strip()
    if text.isdigit() and int(text) in mapping:
        return int(text)
    for code, name in mapping.items():
        if text == name:
            return code
    raise BadRequest(description=f"{label}「{text}」无效")


def _paginate(
    actor: Policy,
    model: type,
    conditions: list,
    page: Any,
    page_size: Any,
    serialize: Callable[[Any], dict],
    order_by: Optional[list] = None,
) -> dict:
    """统一分页：总数与当前页都带数据范围（先 policy.query，再 where）。"""
    page_no, size = _page_args(page, page_size)
    stmt = actor.query(model).where(*conditions)
    if order_by:
        stmt = stmt.order_by(*order_by)
    total = int(actor.db.execute(select(func.count()).select_from(stmt.order_by(None).subquery())).scalar() or 0)
    rows = actor.db.execute(stmt.limit(size).offset((page_no - 1) * size)).scalars().unique().all()
    data = _meta(page_no, size, total)
    data["items"] = [serialize(row) for row in rows]
    return data


# --------------------------------------------------------------------------
# 序列化
# --------------------------------------------------------------------------
def complaint_to_dict(actor: Policy, row: Complaint) -> dict:
    """投诉：补房屋全称、投诉人、处理人中文。"""
    house = actor.db.get(House, row.house_id) if row.house_id else None
    reporter = actor.db.get(Person, row.reporter_id) if row.reporter_id else None
    handler = actor.db.get(User, row.handler_id) if row.handler_id else None
    data = row.to_dict()
    data.update(
        {
            "house_full": house.full_name if house else "",
            "house_label": house.full_name if house else "",
            "reporter_name": reporter.name if reporter else "",
            "reporter_phone": reporter.phone if reporter else "",
            "handler_name": handler.display_name() if handler else "",
            "status_text": complaint_status_text(row.status),
            "category_text": complaint_category_text(row.category),
        }
    )
    return data


def visitor_to_dict(actor: Policy, row: Visitor) -> dict:
    """访客：补房屋全称、接待人中文。"""
    house = actor.db.get(House, row.house_id) if row.house_id else None
    operator = actor.db.get(User, row.operator_id) if row.operator_id else None
    data = row.to_dict()
    data.update(
        {
            "house_full": house.full_name if house else "",
            "house_label": house.full_name if house else "",
            "operator_name": operator.display_name() if operator else "",
            "status_text": visitor_status_text(row.status),
        }
    )
    return data


def vehicle_to_dict(actor: Policy, row: Vehicle) -> dict:
    """车辆：补房屋全称、车主姓名、车位编号。"""
    house = actor.db.get(House, row.house_id) if row.house_id else None
    owner = actor.db.get(Person, row.owner_person_id) if row.owner_person_id else None
    space = actor.db.execute(
        select(ParkingSpace).where(ParkingSpace.vehicle_id == row.id, ParkingSpace.deleted.is_(False))
    ).scalars().first()
    data = row.to_dict()
    data.update(
        {
            "house_full": house.full_name if house else "",
            "house_label": house.full_name if house else "",
            "owner_name": owner.name if owner else "",
            "owner_phone": owner.phone if owner else "",
            "space_code": space.code if space else "",
            "status_text": vehicle_status_text(row.status),
        }
    )
    return data


def parking_to_dict(actor: Policy, row: ParkingSpace) -> dict:
    """车位：补占用房屋与车牌。"""
    house = actor.db.get(House, row.house_id) if row.house_id else None
    vehicle = actor.db.get(Vehicle, row.vehicle_id) if row.vehicle_id else None
    data = row.to_dict()
    data.update(
        {
            "code": row.code,
            "house_full": house.full_name if house else "",
            "house_label": house.full_name if house else "",
            "plate": vehicle.plate if vehicle else "",
            "status_text": parking_status_text(row.status),
        }
    )
    return data


def device_to_dict(actor: Policy, row: Device) -> dict:
    """设备：补楼栋名、最近一次巡检时间与结果。"""
    from models import Building

    building_row = actor.db.get(Building, row.building_id) if row.building_id else None
    latest = actor.db.execute(
        select(Inspection)
        .where(Inspection.device_id == row.id, Inspection.deleted.is_(False))
        .order_by(Inspection.plan_at.desc(), Inspection.id.desc())
    ).scalars().first()
    data = row.to_dict()
    data.update(
        {
            "building_name": building_row.name if building_row else "",
            "category_text": device_category_text(row.category),
            "status_text": device_status_text(row.status),
            "last_inspection_at": latest.plan_at.strftime("%Y-%m-%d %H:%M:%S") if latest and latest.plan_at else "",
            "last_inspection_status": inspection_status_text(latest.status) if latest else "",
            "inspection_count": int(
                actor.db.execute(
                    select(func.count())
                    .select_from(Inspection)
                    .where(Inspection.device_id == row.id, Inspection.deleted.is_(False))
                ).scalar()
                or 0
            ),
        }
    )
    return data


def inspection_to_dict(actor: Policy, row: Inspection) -> dict:
    """巡检：补设备名、巡检人、关联工单号。"""
    device = actor.db.get(Device, row.device_id) if row.device_id else None
    assignee = actor.db.get(User, row.assignee_id) if row.assignee_id else None
    order = actor.db.get(models.WorkOrder, row.order_id) if row.order_id else None
    data = row.to_dict()
    data.update(
        {
            "device_name": device.name if device else "",
            "device_location": device.location if device else "",
            "assignee_name": assignee.display_name() if assignee else "",
            "order_no": order.no if order else "",
            "status_text": inspection_status_text(row.status),
        }
    )
    return data


# --------------------------------------------------------------------------
# 投诉
# --------------------------------------------------------------------------
def list_complaints(
    actor: Policy,
    status: Any = None,
    keyword: Any = None,
    community: Any = None,
    house_id: Any = None,
    page: Any = 1,
    page_size: Any = DEFAULT_PAGE_SIZE,
) -> dict:
    """投诉列表（数据范围 + 状态 + 关键词 + 房屋）。"""
    actor.require("complaint.read")
    conditions = _keyword_like(
        [Complaint.no, Complaint.content, Complaint.category, Complaint.result], keyword
    )
    code = _status_code(status, COMPLAINT_STATUS_TEXT, "投诉状态")
    if code is not None:
        conditions.append(Complaint.status == code)
    if community not in (None, ""):
        conditions.append(Complaint.community_id == int(community))
    if house_id not in (None, ""):
        conditions.append(Complaint.house_id == int(house_id))
    return _paginate(
        actor,
        Complaint,
        conditions,
        page,
        page_size,
        lambda row: complaint_to_dict(actor, row),
        order_by=[Complaint.created_at.desc(), Complaint.id.desc()],
    )


def get_complaint(actor: Policy, complaint_id: Any) -> dict:
    """投诉详情（不在数据范围内 → 404，不泄露存在性）。"""
    actor.require("complaint.read")
    row = actor.get(Complaint, complaint_id)
    if row is None:
        from permissions import not_found

        not_found("没有找到这条投诉，或者它不在你可见的范围内")
    return complaint_to_dict(actor, row)


def complaint_status_summary(actor: Policy) -> dict:
    """投诉各状态计数（看板/页签用）。"""
    actor.require("complaint.read")
    rows = actor.db.execute(
        select(Complaint.status, func.count())
        .select_from(Complaint)
        .where(Complaint.deleted.is_(False), actor.condition(Complaint))
        .group_by(Complaint.status)
    ).all()
    counts = {code: 0 for code in COMPLAINT_STATUS_TEXT}
    for status, total in rows:
        counts[int(status)] = int(total)
    return counts


# --------------------------------------------------------------------------
# 访客
# --------------------------------------------------------------------------
def list_visitors(
    actor: Policy,
    status: Any = None,
    keyword: Any = None,
    community: Any = None,
    house_id: Any = None,
    page: Any = 1,
    page_size: Any = DEFAULT_PAGE_SIZE,
) -> dict:
    """访客列表。"""
    actor.require("visitor.read")
    conditions = _keyword_like([Visitor.name, Visitor.phone, Visitor.purpose], keyword)
    code = _status_code(status, VISITOR_STATUS_TEXT, "访客状态")
    if code is not None:
        conditions.append(Visitor.status == code)
    if community not in (None, ""):
        conditions.append(Visitor.community_id == int(community))
    if house_id not in (None, ""):
        conditions.append(Visitor.house_id == int(house_id))
    return _paginate(
        actor,
        Visitor,
        conditions,
        page,
        page_size,
        lambda row: visitor_to_dict(actor, row),
        order_by=[Visitor.visit_at.desc(), Visitor.id.desc()],
    )


def get_visitor(actor: Policy, visitor_id: Any) -> dict:
    actor.require("visitor.read")
    row = actor.get(Visitor, visitor_id)
    if row is None:
        from permissions import not_found

        not_found("没有找到这条访客登记，或者它不在你可见的范围内")
    return visitor_to_dict(actor, row)


# --------------------------------------------------------------------------
# 车辆 / 车位
# --------------------------------------------------------------------------
def list_vehicles(
    actor: Policy,
    status: Any = None,
    keyword: Any = None,
    community: Any = None,
    house_id: Any = None,
    page: Any = 1,
    page_size: Any = DEFAULT_PAGE_SIZE,
) -> dict:
    """车辆列表（车牌/品牌/车主姓名可搜）。"""
    actor.require("vehicle.read")
    conditions = _keyword_like([Vehicle.plate, Vehicle.brand], keyword)
    code = _status_code(status, VEHICLE_STATUS_TEXT, "车辆状态")
    if code is not None:
        conditions.append(Vehicle.status == code)
    if community not in (None, ""):
        conditions.append(Vehicle.community_id == int(community))
    if house_id not in (None, ""):
        conditions.append(Vehicle.house_id == int(house_id))
    if keyword:
        # 关键词也支持按车主姓名搜
        text = str(keyword).strip()
        conditions.append(Vehicle.owner_person_id.in_(select(Person.id).where(Person.name.like(f"%{text}%"))))
    return _paginate(
        actor,
        Vehicle,
        conditions,
        page,
        page_size,
        lambda row: vehicle_to_dict(actor, row),
        order_by=[Vehicle.id.desc()],
    )


def get_vehicle(actor: Policy, vehicle_id: Any) -> dict:
    actor.require("vehicle.read")
    row = actor.get(Vehicle, vehicle_id)
    if row is None:
        from permissions import not_found

        not_found("没有找到这辆车，或者它不在你可见的范围内")
    return vehicle_to_dict(actor, row)


def list_parking_spaces(
    actor: Policy,
    status: Any = None,
    keyword: Any = None,
    community: Any = None,
    page: Any = 1,
    page_size: Any = DEFAULT_PAGE_SIZE,
) -> dict:
    """车位列表（含占用情况）。"""
    actor.require("parking.read")
    conditions = _keyword_like([ParkingSpace.code], keyword)
    code = _status_code(status, PARKING_STATUS_TEXT, "车位状态")
    if code is not None:
        conditions.append(ParkingSpace.status == code)
    if community not in (None, ""):
        conditions.append(ParkingSpace.community_id == int(community))
    return _paginate(
        actor,
        ParkingSpace,
        conditions,
        page,
        page_size,
        lambda row: parking_to_dict(actor, row),
        order_by=[ParkingSpace.community_id, ParkingSpace.code],
    )


def get_parking_space(actor: Policy, space_id: Any) -> dict:
    actor.require("parking.read")
    row = actor.get(ParkingSpace, space_id)
    if row is None:
        from permissions import not_found

        not_found("没有找到这个车位，或者它不在你可见的范围内")
    return parking_to_dict(actor, row)


def parking_summary(actor: Policy) -> dict:
    """车位占用概览（空闲/占用计数）。"""
    actor.require("parking.read")
    rows = actor.db.execute(
        select(ParkingSpace.status, func.count())
        .select_from(ParkingSpace)
        .where(ParkingSpace.deleted.is_(False), actor.condition(ParkingSpace))
        .group_by(ParkingSpace.status)
    ).all()
    counts = {code: 0 for code in PARKING_STATUS_TEXT}
    for status, total in rows:
        counts[int(status)] = int(total)
    counts["total"] = sum(counts.values())
    return counts


# --------------------------------------------------------------------------
# 设备 / 巡检
# --------------------------------------------------------------------------
def list_devices(
    actor: Policy,
    status: Any = None,
    keyword: Any = None,
    community: Any = None,
    building: Any = None,
    page: Any = 1,
    page_size: Any = DEFAULT_PAGE_SIZE,
) -> dict:
    """设备列表。"""
    actor.require("device.read")
    conditions = _keyword_like([Device.name, Device.location, Device.category], keyword)
    code = _status_code(status, DEVICE_STATUS_TEXT, "设备状态")
    if code is not None:
        conditions.append(Device.status == code)
    if community not in (None, ""):
        conditions.append(Device.community_id == int(community))
    if building not in (None, ""):
        conditions.append(Device.building_id == int(building))
    return _paginate(
        actor,
        Device,
        conditions,
        page,
        page_size,
        lambda row: device_to_dict(actor, row),
        order_by=[Device.community_id, Device.building_id, Device.id],
    )


def get_device(actor: Policy, device_id: Any) -> dict:
    actor.require("device.read")
    row = actor.get(Device, device_id)
    if row is None:
        from permissions import not_found

        not_found("没有找到这台设备，或者它不在你可见的范围内")
    return device_to_dict(actor, row)


def list_inspections(
    actor: Policy,
    status: Any = None,
    keyword: Any = None,
    community: Any = None,
    device_id: Any = None,
    page: Any = 1,
    page_size: Any = DEFAULT_PAGE_SIZE,
) -> dict:
    """巡检列表（含待巡检）。"""
    actor.require("inspection.read")
    conditions = _keyword_like([Inspection.result], keyword)
    code = _status_code(status, INSPECTION_STATUS_TEXT, "巡检状态")
    if code is not None:
        conditions.append(Inspection.status == code)
    if community not in (None, ""):
        conditions.append(Inspection.community_id == int(community))
    if device_id not in (None, ""):
        conditions.append(Inspection.device_id == int(device_id))
    return _paginate(
        actor,
        Inspection,
        conditions,
        page,
        page_size,
        lambda row: inspection_to_dict(actor, row),
        order_by=[Inspection.status, Inspection.plan_at.desc(), Inspection.id.desc()],
    )


def get_inspection(actor: Policy, inspection_id: Any) -> dict:
    actor.require("inspection.read")
    row = actor.get(Inspection, inspection_id)
    if row is None:
        from permissions import not_found

        not_found("没有找到这条巡检记录，或者它不在你可见的范围内")
    return inspection_to_dict(actor, row)


def options_ops() -> dict:
    """运营模块的下拉选项（页面直接用）。"""
    return {
        "complaint_status": [{"value": k, "text": v} for k, v in sorted(COMPLAINT_STATUS_TEXT.items())],
        "complaint_category": [{"value": k, "text": v} for k, v in COMPLAINT_CATEGORY_TEXT.items()],
        "visitor_status": [{"value": k, "text": v} for k, v in sorted(VISITOR_STATUS_TEXT.items())],
        "vehicle_status": [{"value": k, "text": v} for k, v in sorted(VEHICLE_STATUS_TEXT.items())],
        "parking_status": [{"value": k, "text": v} for k, v in sorted(PARKING_STATUS_TEXT.items())],
        "device_status": [{"value": k, "text": v} for k, v in sorted(DEVICE_STATUS_TEXT.items())],
        "device_category": [{"value": k, "text": v} for k, v in DEVICE_CATEGORY_TEXT.items()],
        "inspection_status": [{"value": k, "text": v} for k, v in sorted(INSPECTION_STATUS_TEXT.items())],
    }


__all__ = [
    "COMPLAINT_STATUS_TEXT",
    "VISITOR_STATUS_TEXT",
    "VEHICLE_STATUS_TEXT",
    "PARKING_STATUS_TEXT",
    "DEVICE_STATUS_TEXT",
    "INSPECTION_STATUS_TEXT",
    "complaint_status_text",
    "visitor_status_text",
    "vehicle_status_text",
    "parking_status_text",
    "device_status_text",
    "inspection_status_text",
    "complaint_category_text",
    "device_category_text",
    "complaint_to_dict",
    "visitor_to_dict",
    "vehicle_to_dict",
    "parking_to_dict",
    "device_to_dict",
    "inspection_to_dict",
    "list_complaints",
    "get_complaint",
    "complaint_status_summary",
    "list_visitors",
    "get_visitor",
    "list_vehicles",
    "get_vehicle",
    "list_parking_spaces",
    "get_parking_space",
    "parking_summary",
    "list_devices",
    "get_device",
    "list_inspections",
    "get_inspection",
    "options_ops",
]
