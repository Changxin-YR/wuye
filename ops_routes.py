"""运营模块页面路由（Flask 蓝图 ``ops_bp``）。

页面：投诉 / 访客 / 车辆车位 / 设备巡检 / 收费。
写操作是普通表单 POST（无 fetch、无 ``_method``），统一挂在 ``*_command`` 端点上；
路由层保持薄：收参数 → 调 ``services_ops`` / ``services`` / ``queries_ops`` / ``queries``
→ flash → redirect / render。

端点名与模板 ``url_for`` 严格一致：
``complaints, complaint_detail, complaints_command, visitors, visitors_command,
vehicles, vehicles_command, parking_command, devices, devices_command,
inspections_command, bills, bill_detail, bills_command``
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for

import queries
import queries_ops as qo
import services
import services_ops as so
from permissions import Policy

log = logging.getLogger(__name__)



CSRF_FIELD = "csrf_token"
SESSION_USER_ID = "user_id"


class Row(dict):
    """模板里 ``item.key`` 与 ``item['key']`` 都能用（Jinja 对 dict 的 getattr 会撞方法名）。"""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


#: 各模块状态 → 语义样式类（模板用 ``status-{{ item.status_class }}``，CSS 里定义的就是这些名字）
COMPLAINT_STATUS_CLASS = {0: "pending", 1: "working", 2: "closed", 3: "cancelled"}
VISITOR_STATUS_CLASS = {0: "pending", 1: "working", 2: "closed", 3: "cancelled"}
DEVICE_STATUS_CLASS = {0: "closed", 1: "working", 2: "cancelled"}
INSPECTION_STATUS_CLASS = {0: "pending", 1: "closed", 2: "working"}
VEHICLE_STATUS_CLASS = {0: "closed", 1: "cancelled", 2: "verifying"}
PARKING_STATUS_CLASS = {0: "pending", 1: "closed", 2: "verifying"}


def status_class_of(item, mapping, fallback="pending"):
    """给一行数据算语义样式类（模板读 item.status_class）。"""
    try:
        return mapping.get(int(item.get("status")), fallback)
    except (TypeError, ValueError, AttributeError):
        return fallback


def with_status_class(items, mapping, fallback="pending"):
    """批量补 status_class；queries 已经给了就沿用，不覆盖。"""
    out = []
    for item in items:
        data = Row(item)
        data.setdefault("status_class", status_class_of(item, mapping, fallback))
        out.append(data)
    return out


#: 投诉动作文案（详情页按钮）
COMPLAINT_ACTION_TEXT = {
    "assign": "分配给处理人",
    "handle": "记录处理结果",
    "close": "结案",
    "cancel": "取消投诉",
}

#: 收费状态与费用类型文案（bills.html 用）
BILL_STATUS_TEXT = {0: "待缴", 1: "部分缴", 2: "已缴", 3: "已作废"}
BILL_STATUS_CLASS = {0: "pending", 1: "dispatched", 2: "closed", 3: "cancelled"}
# 模板按 {value, text} 迭代渲染 <option>，这里保持同样的结构
FEE_TYPES = [
    {"value": name, "text": name}
    for name in ("物业费", "水费", "电费", "停车费", "电梯费", "装修管理费", "其他")
]
PAYMENT_METHODS = [{"value": 0, "text": "现金"}, {"value": 1, "text": "银行转账"}, {"value": 2, "text": "其他"}]


# --------------------------------------------------------------------------
# 通用工具
# --------------------------------------------------------------------------
def _db():
    import db as app_db

    return app_db.get_session()


def _policy(source: str = "web") -> Policy:
    """当前请求的 Policy（与 app.py 同源：session 里的 user_id）。"""
    return Policy(_db(), session.get(SESSION_USER_ID), source=source)


def _csrf_ok() -> bool:
    import secrets

    sent = (request.form.get(CSRF_FIELD) or request.headers.get("X-CSRF-Token") or "").strip()
    token = session.get(CSRF_FIELD) or ""
    return bool(sent) and bool(token) and secrets.compare_digest(sent, token)


def _check_csrf() -> None:
    if not _csrf_ok():
        abort(400, description="页面已过期，请刷新后重新提交")


def _form(name: str, default: Any = None) -> Any:
    value = request.form.get(name)
    if value is None or str(value).strip() == "":
        return default
    return value


def _int_arg(name: str) -> Optional[int]:
    text = (request.args.get(name) or "").strip()
    return int(text) if text.lstrip("-").isdigit() else None


def _page_of(result: dict) -> dict:
    """queries 的分页结构 → 模板需要的一组变量。"""
    return {
        "page": result.get("page", 1),
        "pages": result.get("pages", 1),
        "total": result.get("total", 0),
        "page_size": result.get("page_size", 20),
    }


def _endpoint_name(name: str) -> str:
    """本模块的路由是用**裸端点名**注册的（不是蓝图），这里统一把 ops. 前缀剥掉。"""
    return name[4:] if name.startswith("ops.") else name


def _back(default_endpoint: str, **values: Any) -> str:
    """回到来源页（只接受同站地址）。端点名写错也不能 500，兜底回工作台。"""
    referrer = request.referrer or ""
    if referrer.startswith("/") and not referrer.startswith("//"):
        return referrer
    try:
        return url_for(_endpoint_name(default_endpoint), **values)
    except Exception:  # noqa: BLE001 - 跳转目标不可用时不阻塞业务，退回家页
        return url_for("dashboard")


def _community_of_building(policy: Policy, building_ref: Any):
    """从楼栋反查小区 id（模板只给 building_id 时用）。"""
    if building_ref in (None, ""):
        return None
    try:
        building_id = int(str(building_ref).strip())
    except (TypeError, ValueError):
        return None
    from models import Building

    row = _db().get(Building, building_id)
    return row.community_id if row is not None else None


def _person_id_by_name(policy: Policy, name: Any):
    """按姓名解析人员档案 id（重名时返回 None，交给服务层提示）。"""
    text = "" if name is None else str(name).strip()
    if not text:
        return None
    from sqlalchemy import select

    from models import Person

    rows = _db().execute(
        select(Person).where(Person.name == text, Person.deleted.is_(False))
    ).scalars().unique().all()
    return rows[0].id if len(rows) == 1 else None


def _space_id_by_code(policy: Policy, code: Any):
    """按车位编号解析车位 id。"""
    text = "" if code is None else str(code).strip()
    if not text:
        return None
    for item in qo.list_parking_spaces(policy, keyword=text, page_size=20)["items"]:
        if str(item.get("code", "")).strip() == text:
            return item.get("id")
    return None


def _complaint_houses(policy: Policy) -> list[Row]:
    """登记投诉时可以选的「涉事房屋」。

    投诉对象是**涉事房屋**（楼上装修噪音这类要能选邻居家），不是投诉人自己的房子：
    - 物业内部角色：仍限定在自己数据范围内的房屋；
    - 业主（self）：放宽到「自己所在的小区」里的全部房屋——这是刻意的，
      否则业主永远只能投诉自己，业务上说不通。
    """
    if not policy.has("complaint.create"):
        return []
    if policy.primary_scope != "self":
        return _house_options(policy)

    from sqlalchemy import select

    from models import House

    db = _db()
    my_house_ids = services.my_house_ids(policy)
    if not my_house_ids:
        return []
    community_ids = set(
        db.execute(select(House.community_id).where(House.id.in_(my_house_ids))).scalars().all()
    )
    if not community_ids:
        return []
    rows = db.execute(
        select(House)
        .where(House.community_id.in_(community_ids), House.deleted.is_(False))
        .order_by(House.community_id, House.id)
    ).scalars().all()
    return [Row({"id": row.id, "full_name": row.full_name, "label": row.full_name}) for row in rows]


def _house_options(policy: Policy) -> list[Row]:
    rows = queries.list_houses(policy, page_size=200)["items"]
    return [Row({"id": item["id"], "full_name": item["house_text"] if "house_text" in item else item.get("full_name", ""), "label": item.get("full_name", "")}) for item in rows]


def _handle(fn: Callable[[], Any], *, endpoint: str, **values: Any):
    """统一执行写命令：业务错误 flash + 回来源页。"""
    try:
        return fn()
    except services.ServiceError as exc:
        _db().rollback()
        flash(exc.message, "error")
        return redirect(_back(endpoint, **values))
    except Exception as exc:  # 权限/数据范围等 HTTPException 交给 app.py 的错误处理
        from werkzeug.exceptions import HTTPException

        if isinstance(exc, HTTPException):
            raise
        log.exception("运营模块命令执行失败")
        raise


def _ok(message: str, endpoint: str, **values: Any):
    flash(message or "操作成功", "success")
    return redirect(_back(endpoint, **values))


# --------------------------------------------------------------------------
# 投诉
# --------------------------------------------------------------------------
# GET /complaints
def complaints():
    """投诉列表：状态页签 + 关键词 + 分页。"""
    policy = _policy()
    policy.require("complaint.read")
    status_filter = (request.args.get("status") or "").strip()
    keyword = (request.args.get("keyword") or "").strip()
    page = _int_arg("page") or 1

    data = qo.list_complaints(policy, status=status_filter or None, keyword=keyword or None, page=page, page_size=20)
    counts = qo.complaint_status_summary(policy)
    tabs = [
        Row({"value": None, "label": "全部", "count": sum(counts.values()), "active": status_filter in ("", None)})
    ] + [
        Row(
            {
                "value": code,
                "label": qo.COMPLAINT_STATUS_TEXT[code],
                "count": counts.get(code, 0),
                "active": str(status_filter) == str(code),
            }
        )
        for code in sorted(qo.COMPLAINT_STATUS_TEXT)
    ]
    return render_template(
        "complaints.html",
        items=with_status_class(data["items"], COMPLAINT_STATUS_CLASS),
        houses=_complaint_houses(policy),
        status_counts=tabs,
        status_filter=status_filter,
        keyword=keyword,
        category_options=qo.options_ops()["complaint_category"],
        **_page_of(data),
    )


# GET /complaints/<int:complaint_id>
def complaint_detail(complaint_id: int):
    """投诉详情：时间线条目 + 后端算好的可执行动作。"""
    policy = _policy()
    complaint = qo.get_complaint(policy, complaint_id)
    status = int(complaint.get("status", 0))
    can_handle = policy.has("complaint.handle")

    actions: list[Row] = []

    def add(name: str, style: str = "secondary", need_note: bool = False) -> None:
        target = url_for("complaints_command", complaint_id=complaint_id, action=name)
        actions.append(
            Row(
                {
                    "name": name,
                    "label": COMPLAINT_ACTION_TEXT.get(name, name),
                    "style": style,
                    "need_note": need_note,
                    "target": target,
                }
            )
        )

    if can_handle and status == 0:
        add("assign", "primary")
    if can_handle and status in (0, 1):
        add("handle", "secondary", need_note=True)
    if can_handle and status == 1:
        add("close", "success", need_note=True)
    if can_handle and status in (0, 1):
        add("cancel", "danger", need_note=True)

    logs = []
    if complaint.get("created_at"):
        logs.append(
            Row(
                {
                    "action_text": "投诉登记",
                    "from_status_text": "—",
                    "to_status_text": qo.complaint_status_text(0),
                    "operator_name": complaint.get("reporter_name") or "业主",
                    "note": complaint.get("content", ""),
                    "created_at": complaint.get("created_at"),
                }
            )
        )
    if complaint.get("handler_name"):
        logs.append(
            Row(
                {
                    "action_text": "分配处理人",
                    "from_status_text": qo.complaint_status_text(0),
                    "to_status_text": qo.complaint_status_text(1),
                    "operator_name": complaint.get("handler_name"),
                    "note": "",
                    "created_at": complaint.get("updated_at") or complaint.get("created_at"),
                }
            )
        )
    if complaint.get("result"):
        logs.append(
            Row(
                {
                    "action_text": "处理结果",
                    "from_status_text": qo.complaint_status_text(1),
                    "to_status_text": complaint.get("status_text", ""),
                    "operator_name": complaint.get("handler_name") or "处理人",
                    "note": complaint.get("result", ""),
                    "created_at": complaint.get("updated_at") or complaint.get("created_at"),
                }
            )
        )

    staff: list[Row] = []
    if any(item["name"] == "assign" for item in actions) and policy.has("staff.read"):
        staff = [
            Row({"id": item.get("id"), "display_name": item.get("display_name") or item.get("real_name"), "open_orders": item.get("open_orders")})
            for item in queries.list_staff(policy, page_size=50)["items"]
        ]
    return render_template(
        "complaint_detail.html",
        complaint=Row({**complaint, "status_class": status_class_of(complaint, COMPLAINT_STATUS_CLASS)}),
        logs=logs,
        actions=actions,
        staff=staff,
    )


def complaints_create():
    """登记投诉（``POST /complaints``，列表页的新增表单提交到这里）。"""
    _check_csrf()
    policy = _policy()
    result = _handle(
        lambda: so.create_complaint(
            policy,
            house_id=_form("house_id"),
            content=_form("content"),
            category=_form("category", "other"),
        ),
        endpoint="complaints",
    )
    if isinstance(result, dict):
        return _ok(result.get("message", ""), "complaints")
    return result


COMPLAINT_ACTIONS = {"assign", "handle", "close", "cancel"}


# POST /complaints/<int:complaint_id>/<action>
def complaints_command(complaint_id: int, action: str):
    """投诉动作：assign / handle / close / cancel。"""
    _check_csrf()
    if action not in COMPLAINT_ACTIONS:
        abort(404)
    policy = _policy()
    form = request.form

    if action == "assign":
        result = _handle(
            lambda: so.assign_complaint(policy, complaint_id=complaint_id, handler=_form("handler"), note=_form("note", "")),
            endpoint="complaint_detail",
            complaint_id=complaint_id,
        )
    elif action == "handle":
        result = _handle(
            lambda: so.handle_complaint(policy, complaint_id=complaint_id, result=_form("result"), note=_form("note", "")),
            endpoint="complaint_detail",
            complaint_id=complaint_id,
        )
    elif action == "close":
        result = _handle(
            lambda: so.close_complaint(policy, complaint_id=complaint_id, result=_form("result", "")),
            endpoint="complaint_detail",
            complaint_id=complaint_id,
        )
    else:
        result = _handle(
            lambda: so.cancel_complaint(policy, complaint_id=complaint_id, reason=_form("reason")),
            endpoint="complaint_detail",
            complaint_id=complaint_id,
        )
    if isinstance(result, dict):
        return _ok(result.get("message", ""), "complaint_detail", complaint_id=complaint_id)
    return result


# --------------------------------------------------------------------------
# 访客
# --------------------------------------------------------------------------
# GET /visitors
def visitors():
    """访客列表：状态页签 + 关键词 + 分页 + 登记表单。"""
    policy = _policy()
    policy.require("visitor.read")
    status_filter = (request.args.get("status") or "").strip()
    keyword = (request.args.get("keyword") or "").strip()
    page = _int_arg("page") or 1

    data = qo.list_visitors(policy, status=status_filter or None, keyword=keyword or None, page=page, page_size=20)
    counts = {code: qo.list_visitors(policy, status=code, page_size=1)["total"] for code in qo.VISITOR_STATUS_TEXT}
    tabs = [
        Row({"value": None, "label": "全部", "count": sum(counts.values()), "active": status_filter in ("", None)})
    ] + [
        Row(
            {
                "value": code,
                "label": qo.VISITOR_STATUS_TEXT[code],
                "count": counts.get(code, 0),
                "active": str(status_filter) == str(code),
            }
        )
        for code in sorted(qo.VISITOR_STATUS_TEXT)
    ]
    return render_template(
        "visitors.html",
        items=with_status_class(data["items"], VISITOR_STATUS_CLASS),
        houses=_house_options(policy) if policy.has("visitor.write") else [],
        status_filter=status_filter,
        keyword=keyword,
        status_counts=tabs,
        **_page_of(data),
    )


VISITOR_ACTIONS = {"check-in": "enter", "check-out": "leave", "cancel": "cancel"}


# POST /visitors/<int:visitor_id>/<action>
def visitors_command(visitor_id: int, action: str):
    """访客动作：check-in / check-out / cancel（模板用连字符，命令用单词）。"""
    _check_csrf()
    if action not in VISITOR_ACTIONS:
        abort(404)
    policy = _policy()
    if action == "check-in":
        result = _handle(lambda: so.enter_visitor(policy, visitor_id=visitor_id), endpoint="visitors")
    elif action == "check-out":
        result = _handle(lambda: so.leave_visitor(policy, visitor_id=visitor_id), endpoint="visitors")
    else:
        result = _handle(
            lambda: so.cancel_visitor(policy, visitor_id=visitor_id, reason=_form("reason", "访客临时取消")),
            endpoint="visitors",
        )
    if isinstance(result, dict):
        return _ok(result.get("message", ""), "visitors")
    return result


# POST /visitors
def visitors_create():
    """登记访客（列表页表单）。"""
    _check_csrf()
    policy = _policy()
    result = _handle(
        lambda: so.register_visitor(
            policy,
            house_id=_form("house_id"),
            name=_form("name"),
            phone=_form("phone"),
            purpose=_form("purpose", ""),
            visit_at=_form("visit_at"),
        ),
        endpoint="visitors",
    )
    if isinstance(result, dict):
        return _ok(result.get("message", ""), "visitors")
    return result


# --------------------------------------------------------------------------
# 车辆 / 车位
# --------------------------------------------------------------------------
# GET /vehicles
def vehicles():
    """车辆列表 + 车位列表（同页）。"""
    policy = _policy()
    policy.require("vehicle.read")
    keyword = (request.args.get("keyword") or "").strip()
    page = _int_arg("page") or 1

    can_parking = policy.has("parking.write")
    # 业主自助申请（resident.self 是业主专属权限点，物业内部角色没有）
    can_apply = policy.has("resident.self")
    if can_parking:
        spaces = qo.list_parking_spaces(policy, page_size=100)["items"]
    elif can_apply:
        # 业主看不到车位台账（没有 parking.read），只让他看到自己那条申请单
        from models import ParkingSpace

        rows = _db().execute(
            policy.query(ParkingSpace).where(ParkingSpace.status == 2).order_by(ParkingSpace.id.desc())
        ).scalars().all()
        spaces = [qo.parking_to_dict(policy, row) for row in rows]
    else:
        spaces = []
    data = qo.list_vehicles(policy, keyword=keyword or None, page=page, page_size=20)
    return render_template(
        "vehicles.html",
        vehicles=with_status_class(data["items"], VEHICLE_STATUS_CLASS, "closed"),
        spaces=[Row({**item, "label": f"{item.get('code')}（{item.get('status_text')}）"}) for item in with_status_class(spaces, PARKING_STATUS_CLASS)],
        houses=_house_options(policy) if (policy.has("vehicle.write") or can_apply) else [],
        vehicle_status_options=qo.options_ops()["vehicle_status"],
        keyword=keyword,
        can_edit=policy.has("vehicle.write"),
        can_parking=can_parking,
        can_apply=can_apply,
        **_page_of(data),
    )


VEHICLE_ACTIONS = {"create", "update", "archive", "apply", "review"}


# POST /vehicles/<action>
def vehicles_command(action: str):
    """车辆动作：create / update / archive（物业）/ apply / review（业主申请与物业审批）。"""
    _check_csrf()
    if action not in VEHICLE_ACTIONS:
        abort(404)
    policy = _policy()
    form = request.form

    if action == "apply":
        # 业主自助申请：落一条「待审批」的车辆
        result = _handle(
            lambda: so.apply_vehicle(
                policy, plate=form.get("plate"), brand=_form("brand", ""), house_id=_form("house_id"),
            ),
            endpoint="vehicles",
        )
    elif action == "review":
        # 物业审批车辆登记申请
        result = _handle(
            lambda: so.review_vehicle(
                policy, vehicle_id=_form("id"), approve=_form("approve", "1"), reason=_form("reason", ""),
            ),
            endpoint="vehicles",
        )
    elif action == "create":
        result = _handle(
            lambda: so.create_vehicle(
                policy,
                plate=form.get("plate"),
                house_id=_form("house_id"),
                brand=_form("brand", ""),
                owner_person_id=_form("person_id") or _person_id_by_name(policy, _form("owner_name")),
            ),
            endpoint="vehicles",
        )
    elif action == "update":
        result = _handle(
            lambda: so.update_vehicle(
                policy,
                vehicle_id=_form("id"),
                plate=_form("plate"),
                brand=_form("brand"),
                house_id=_form("house_id"),
                owner_person_id=_form("person_id") or _person_id_by_name(policy, _form("owner_name")),
            ),
            endpoint="vehicles",
        )
    else:
        result = _handle(
            lambda: so.archive_vehicle(policy, vehicle_id=_form("id"), reason=_form("reason", "")),
            endpoint="vehicles",
        )
    if isinstance(result, dict):
        return _ok(result.get("message", ""), "vehicles")
    return result


PARKING_ACTIONS = {"assign", "release", "apply", "review"}


# POST /parking/<action>
def parking_command(action: str):
    """车位动作：assign / release。"""
    _check_csrf()
    if action not in PARKING_ACTIONS:
        abort(404)
    policy = _policy()
    form = request.form

    if action == "apply":
        result = _handle(
            lambda: so.apply_parking(policy, house_id=_form("house_id"), note=_form("note", "")),
            endpoint="vehicles",
        )
    elif action == "review":
        result = _handle(
            lambda: so.review_parking(
                policy, space_id=_form("id"), approve=_form("approve", "1"),
                space_code=_form("space_code", ""), reason=_form("reason", ""),
            ),
            endpoint="vehicles",
        )
    elif action == "assign":
        result = _handle(
            lambda: so.assign_parking(
                policy,
                space_id=_form("id") or _form("space_id") or _space_id_by_code(policy, _form("code")),
                vehicle_id=_form("vehicle_id"),
                house_id=_form("house_id"),
            ),
            endpoint="vehicles",
        )
    else:
        result = _handle(
            lambda: so.release_parking(
                policy, space_id=_form("id") or _form("space_id") or _space_id_by_code(policy, _form("code"))
            ),
            endpoint="vehicles",
        )
    if isinstance(result, dict):
        return _ok(result.get("message", ""), "vehicles")
    return result


# --------------------------------------------------------------------------
# 设备 / 巡检
# --------------------------------------------------------------------------
# GET /devices
def devices():
    """设备列表 + 巡检列表（同页）。"""
    policy = _policy()
    policy.require("device.read")
    keyword = (request.args.get("keyword") or "").strip()
    status_filter = (request.args.get("status") or "").strip()
    page = _int_arg("page") or 1

    data = qo.list_devices(policy, status=status_filter or None, keyword=keyword or None, page=page, page_size=20)
    inspections = (
        qo.list_inspections(policy, page_size=50)["items"] if policy.has("inspection.read") else []
    )
    communities = queries.list_communities(policy, page_size=50)["items"]
    buildings: list[dict] = []
    if communities:
        buildings = queries.list_buildings(policy, community_id=communities[0]["id"], page_size=200)["items"]
    return render_template(
        "devices.html",
        devices=with_status_class(data["items"], DEVICE_STATUS_CLASS, "closed"),
        inspections=with_status_class(inspections, INSPECTION_STATUS_CLASS),
        buildings=[Row(item) for item in buildings],
        communities=[Row(item) for item in communities],
        staff=[
            Row({"id": item.get("id"), "display_name": item.get("display_name") or item.get("real_name")})
            for item in (queries.list_staff(policy, page_size=50)["items"] if policy.has("staff.read") else [])
        ],
        device_status_options=qo.options_ops()["device_status"],
        device_category_options=qo.options_ops()["device_category"],
        status_filter=status_filter,
        keyword=keyword,
        can_device=policy.has("device.write"),
        can_inspection=policy.has("inspection.write"),
        # 另外两个入口光看权限点不够，还要看数据范围/相邻权限点，否则会出现
        # 「按钮看得见、点了必然 403」：
        #   ＋ 新增设备   —— 小区级台账维护，维修工（scope=assigned）过不去 require_scope(write=True)
        #   发现故障转报修 —— 要建报修工单，得有 order.create，而维修工角色没有这个权限点
        # 「新建巡检」是派活，挂 inspection.assign；维修工只有 inspection.write，
        # 能完成自己的任务但看不到这个入口
        can_inspection_assign=policy.has("inspection.assign"),
        can_device_ledger=policy.has("device.write") and (
            policy.super or (bool(communities) and policy.within(communities[0]["id"], None, write=True))
        ),
        can_to_order=policy.has("inspection.write") and policy.has("order.create"),
        **_page_of(data),
    )


DEVICE_ACTIONS = {"create", "update", "archive"}


# POST /devices/<action>
def devices_command(action: str):
    """设备动作：create / update / archive。"""
    _check_csrf()
    if action not in DEVICE_ACTIONS:
        abort(404)
    policy = _policy()
    form = request.form

    if action == "create":
        result = _handle(
            lambda: so.create_device(
                policy,
                name=form.get("name"),
                # 模板只给 building_id，没有 community_id → 从楼栋反查小区
                community_id=_form("community_id") or _community_of_building(policy, _form("building_id")),
                building_id=_form("building_id"),
                category=form.get("category"),
                location=_form("location", ""),
            ),
            endpoint="devices",
        )
    elif action == "update":
        result = _handle(
            lambda: so.update_device(
                policy,
                device_id=_form("id"),
                name=_form("name"),
                category=_form("category"),
                status=_form("status"),
                location=_form("location"),
            ),
            endpoint="devices",
        )
    else:
        result = _handle(
            lambda: so.archive_device(policy, device_id=_form("id"), reason=_form("reason", "")),
            endpoint="devices",
        )
    if isinstance(result, dict):
        return _ok(result.get("message", ""), "devices")
    return result


#: 巡检动作 → 处理方式
INSPECTION_ACTIONS = {"create", "complete", "to-order"}


# POST /inspections/<action>
def inspections_command(inspection_id=None, action: str = ""):
    """巡检动作：`/inspections/<action>`（create）与 `/inspections/<id>/<action>`（complete/to-order）。"""
    _check_csrf()
    if action == "create":
        return _inspections_create()
    if action not in ("complete", "to-order") or inspection_id is None:
        abort(404)
    return _inspections_action(int(inspection_id), action)


def _inspections_create():
    """安排巡检（`/inspections/create`）。"""
    policy = _policy()
    """安排巡检（``/inspections/create``）。"""
    result = _handle(
        lambda: so.create_inspection(
            policy,
            device_id=_form("device_id") or _form("id"),
            assignee_id=_form("assignee") or _form("assignee_id"),
            plan_at=_form("plan_at"),
        ),
        endpoint="devices",
    )
    if isinstance(result, dict):
        return _ok(result.get("message", ""), "devices")
    return result


# POST /inspections/<int:inspection_id>/<action>
def _inspections_action(inspection_id: int, action: str):
    """巡检动作：complete（完成）/ to-order（按巡检结果生成报修工单）。"""
    policy = _policy()
    note = _form("note", "")

    if action == "complete":
        # 模板字段是 result（早期实现取 note），两者都读，result 优先
        outcome = _form("result") or note or "巡检完成，运行正常"
        result = _handle(
            lambda: so.complete_inspection(policy, inspection_id=inspection_id, result=outcome),
            endpoint="devices",
        )
    else:
        # 生成报修工单：房屋取该设备所在小区的第一套（设备的巡检故障按公共设施报修）
        inspection = qo.get_inspection(policy, inspection_id)
        device = None
        from models import Device

        row = _db().get(Device, inspection.get("device_id")) if inspection.get("device_id") else None
        device = row
        if device is None:
            flash("这条巡检没有关联设备，无法生成报修工单", "error")
            return redirect(_back("devices"))
        house = _first_house(policy, device.community_id)
        if house is None:
            flash("没有找到可以报修的房屋", "error")
            return redirect(_back("devices"))
        created = _handle(
            lambda: services.create_work_order(
                policy,
                house_id=house.id,
                contact_name="物业工程",
                contact_phone="13700000001",
                category="public",
                description=note or f"{device.name} 巡检发现故障：{inspection.get('result') or '需检修'}",
                urgency=1,
            ),
            endpoint="devices",
        )
        if isinstance(created, dict):
            order_id = created.get("id") or created.get("order_id")
            _handle(
                lambda: so.complete_inspection(
                    policy,
                    inspection_id=inspection_id,
                    result=note or inspection.get("result") or "发现故障，已转报修",
                    has_fault=True,
                    order_id=order_id,
                ),
                endpoint="devices",
            )
            message = created.get("message") or f"已生成报修工单 {created.get('no', '')}"
            return _ok(f"{message}（巡检已标记为已转报修）", "devices")
        return created
    if isinstance(result, dict):
        return _ok(result.get("message", ""), "devices")
    return result


def _first_house(policy: Policy, community_id: Any):
    """取指定小区里第一套可见房屋（生成报修工单用）。"""
    from sqlalchemy import select

    from models import House

    return _db().execute(
        policy.query(House).where(House.community_id == int(community_id)).order_by(House.id)
    ).scalars().first()


# --------------------------------------------------------------------------
# 收费
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# 注册（由 app.py 调用）
# --------------------------------------------------------------------------
def register_ops_routes(app: Flask) -> None:
    """把运营模块页面挂到 app 上（端点名与模板 url_for 完全一致）。

    注意：这里**不用蓝图**——模板里的 `url_for('vehicles_command', ...)` 是不带命名空间的
    裸端点名，蓝图会把它们变成 `ops.vehicles_command`，导致 BuildError。
    """
    app.add_url_rule("/complaints", "complaints", complaints, methods=["GET"])
    app.add_url_rule("/complaints", "complaints_create", complaints_create, methods=["POST"])
    app.add_url_rule("/complaints/<int:complaint_id>", "complaint_detail", complaint_detail, methods=["GET"])
    app.add_url_rule(
        "/complaints/<int:complaint_id>/<action>", "complaints_command", complaints_command, methods=["POST"]
    )

    app.add_url_rule("/visitors", "visitors", visitors, methods=["GET"])
    app.add_url_rule("/visitors", "visitors_create", visitors_create, methods=["POST"])
    app.add_url_rule("/visitors/<int:visitor_id>/<action>", "visitors_command", visitors_command, methods=["POST"])

    app.add_url_rule("/vehicles", "vehicles", vehicles, methods=["GET"])
    app.add_url_rule("/vehicles/<action>", "vehicles_command", vehicles_command, methods=["POST"])
    app.add_url_rule("/parking/<action>", "parking_command", parking_command, methods=["POST"])

    app.add_url_rule("/devices", "devices", devices, methods=["GET"])
    app.add_url_rule("/devices/<action>", "devices_command", devices_command, methods=["POST"])
    app.add_url_rule(
        "/inspections/<int:inspection_id>/<action>", "inspections_command", inspections_command, methods=["POST"]
    )
    # 同理：url_for('inspections_create', action='create') 不带 inspection_id
    app.add_url_rule("/inspections/<action>", "inspections_command", inspections_command, methods=["POST"])

    app.add_url_rule("/bills", "bills", bills, methods=["GET"])
    app.add_url_rule("/bills/<int:bill_id>", "bill_detail", bill_detail, methods=["GET"])
    # 注意顺序：模板里 url_for('bills_command', action='create') 不带 bill_id，
    # 两条规则必须让「只有 action」的一条先注册，否则 Werkzeug 会挑中需要 bill_id 的那条而 BuildError。
    app.add_url_rule("/bills/<int:bill_id>/<action>", "bills_command", bills_command, methods=["POST"])
    app.add_url_rule("/bills/<action>", "bills_command", bills_command, methods=["POST"])


#: 运营模块导航项（app.py 的 build_nav 可以合并进去）
OPS_NAV_ITEMS = [
    {"key": "complaints", "endpoint": "complaints", "label": "投诉", "perm": "complaint.read"},
    {"key": "visitors", "endpoint": "visitors", "label": "访客", "perm": "visitor.read"},
    {"key": "vehicles", "endpoint": "vehicles", "label": "车辆车位", "perm": "vehicle.read"},
    {"key": "devices", "endpoint": "devices", "label": "设备巡检", "perm": "device.read"},
    {"key": "bills", "endpoint": "bills", "label": "收费", "perm": "billing.read"},
]

#: 历史脏数据：整个下拉选项字典被当成字符串存进了 fee_type
_OPTION_KEY = re.compile(r"""['"](?:text|value|label)['"]\s*:\s*['"]([^'"]+)['"]""")


def _plain_text(value: Any) -> str:
    """把误存成 ``{'value': '水费', 'text': '水费'}`` 的值还原成人话（水费）。

    新建路径已经在 ``services._fee_type`` 堵住，这里只负责让**已经落库**的旧账单
    在列表和详情页显示正常，不必为了改文案去动数据库。
    """
    text = "" if value is None else str(value).strip()
    if not text.startswith("{"):
        return text
    match = _OPTION_KEY.search(text)
    return match.group(1).strip() if match else text


def _bill_view(item: dict) -> Row:
    """账单视图：补费用类型与状态样式。"""
    data = Row(item)
    fee = _plain_text(item.get("fee_type", ""))
    data["fee_type"] = fee
    data["fee_type_text"] = fee
    data.setdefault("status_class", BILL_STATUS_CLASS.get(int(item.get("status", 0)), "pending"))
    data.setdefault("outstanding", float(item.get("amount") or 0) - float(item.get("paid_amount") or 0))
    return data


# GET /bills
def bills():
    """账单列表：欠费概览 + 状态页签 + 关键词 + 分页 + 建账单表单。"""
    policy = _policy()
    policy.require("billing.read")
    status_filter = (request.args.get("status") or "").strip()
    keyword = (request.args.get("keyword") or "").strip()
    # 「只看逾期」开关：overdue=1 / 只看逾期
    overdue_raw = (request.args.get("overdue") or "").strip().lower()
    overdue = overdue_raw in {"1", "true", "yes", "on", "只看逾期", "逾期"}
    page = _int_arg("page") or 1

    data = queries.list_bills(
        policy,
        status=status_filter or None,
        keyword=keyword or None,
        overdue=overdue,
        page=page,
        page_size=20,
    )
    arrears = queries.arrears_summary(policy)
    paid_total = 0.0
    for item in data["items"]:
        paid_total += float(item.get("paid_amount") or 0)
    counts = {}
    for code in BILL_STATUS_TEXT:
        counts[code] = queries.list_bills(policy, status=code, page_size=1)["total"]
    tabs = [
        Row({"value": None, "label": "全部", "count": sum(counts.values()), "active": status_filter in ("", None)})
    ] + [
        Row(
            {
                "value": code,
                "label": BILL_STATUS_TEXT[code],
                "text": BILL_STATUS_TEXT[code],
                "count": counts.get(code, 0),
                "active": str(status_filter) == str(code),
            }
        )
        for code in sorted(BILL_STATUS_TEXT)
    ]
    return render_template(
        "bills.html",
        items=[_bill_view(item) for item in data["items"]],
        summary=Row(
            {
                "unpaid_total": arrears.get("amount_total", 0),
                "unpaid_count": arrears.get("house_total", 0),
                "paid_total": round(paid_total, 2),
            }
        ),
        houses=_house_options(policy) if policy.has("billing.manage") else [],
        persons=[],
        # 模板读 option.value / option.text
        fee_types=[Row(option) for option in FEE_TYPES],
        status_options=tabs,
        status_filter=status_filter,
        keyword=keyword,
        overdue=overdue,
        overdue_url=url_for(
            "bills",
            overdue=None if overdue else 1,
            status=status_filter or None,
            keyword=keyword or None,
        ),
        can_manage=policy.has("billing.manage"),
        can_collect=policy.has("billing.collect"),
        can_reverse=policy.has("billing.reverse"),
        # 业主自助缴费：只有 billing.read 的账号（业主）缴自己房子的账单。
        # 真正的边界在 services.collect_payment 里（对象级 + require_house），这里只管入口。
        can_pay=policy.has("billing.read") and not policy.has("billing.collect"),
        **_page_of(data),
    )


# GET /bills/<int:bill_id>
def bill_detail(bill_id: int):
    """账单详情：收款记录 + 可执行动作。"""
    policy = _policy()
    bill = queries.get_bill(policy, bill_id)
    payments = queries.list_payments(policy, bill_id=bill_id, page_size=50)["items"]
    return render_template(
        "bill_detail.html",
        bill=_bill_view(bill),
        payments=[Row(item) for item in payments],
        payment_methods=PAYMENT_METHODS,
        can_collect=policy.has("billing.collect"),
        can_manage=policy.has("billing.manage"),
        can_reverse=policy.has("billing.reverse"),
        can_pay=policy.has("billing.read") and not policy.has("billing.collect"),
    )


BILL_ACTIONS = {"create", "collect", "void", "reverse"}


# POST /bills/<action>
def bills_command(bill_id=None, action: str = ""):
    """账单动作：`/bills/<action>`（create）与 `/bills/<bill_id>/<action>`（collect/void/reverse）。

    两条路径共用一个端点：模板里 `url_for(bills_command, action=create)` 不带 bill_id，
    拆成两个端点会让 Werkzeug 在缺 bill_id 时挑中带 bill_id 的那条规则并 BuildError。
    """
    _check_csrf()
    if action == "create":
        return _bills_create()
    if action not in ("collect", "void", "reverse") or bill_id is None:
        abort(404)
    return _bills_action(int(bill_id), action)


def _bills_create():
    """新建账单（`/bills/create`，无 bill_id）。"""
    policy = _policy()
    """新建账单（``/bills/create``，无 bill_id）。"""
    result = _handle(
        lambda: services.create_bill(
            policy,
            house_id=_form("house_id"),
            fee_type=_form("fee_type"),
            amount=_form("amount"),
            period=_form("period"),
            due_at=_form("due_at"),
        ),
        endpoint="bills",
    )
    if isinstance(result, dict):
        return _ok(result.get("message", ""), "bills")
    return result


# POST /bills/<int:bill_id>/<action>
def _bills_action(bill_id: int, action: str):
    """账单动作：collect（收款）/ void（作废）/ reverse（冲销）。"""
    policy = _policy()
    form = request.form

    if action == "collect":
        result = _handle(
            lambda: services.collect_payment(
                policy,
                bill_id=bill_id,
                amount=form.get("amount"),
                method=_form("method", 1),
                reference=_form("reference", ""),
            ),
            endpoint="bill_detail",
            bill_id=bill_id,
        )
    elif action == "void":
        result = _handle(
            lambda: services.void_bill(policy, bill_id=bill_id, reason=_form("reason", "页面作废")),
            endpoint="bill_detail",
            bill_id=bill_id,
        )
    else:
        # 冲销针对「这一张账单上最近一笔已入账的收款」
        payments = queries.list_payments(policy, bill_id=bill_id, status=0, page_size=1)["items"]
        if not payments:
            flash("这张账单没有可冲销的收款记录", "error")
            return redirect(_back("bill_detail", bill_id=bill_id))
        payment_id = payments[0]["id"]
        result = _handle(
            lambda: services.reverse_payment(
                policy, payment_id=payment_id, reason=_form("reason", "收款金额有误")
            ),
            endpoint="bill_detail",
            bill_id=bill_id,
        )
    if isinstance(result, dict):
        return _ok(result.get("message", ""), "bill_detail", bill_id=bill_id)
    return result
