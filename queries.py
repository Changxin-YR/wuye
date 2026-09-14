"""只读查询：列表 / 详情 / 看板（页面与智能体工具共用）。

规矩：
- 所有查询先过 :class:`permissions.Policy` 的 ``query``/``get``，行级数据范围由 SQL 完成，不做事后过滤。
- 列表统一返回 ``{items, total, page, page_size, pages}``，统一支持 ``keyword`` 与分页参数。
- 工单列表会 join 出房屋全称（小区+楼栋+单元+房号）与状态中文名，页面和智能体看到的是同一份数据。
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

from sqlalchemy import and_, false, func, or_, select
from sqlalchemy.orm import Session, selectinload
from werkzeug.exceptions import BadRequest

import models
from models import (
    HOUSE_STATUS_TEXT,
    ORDER_CATEGORY_TEXT,
    ORDER_STATUS_CLASS,
    ORDER_STATUS_TEXT,
    ORDER_TERMINAL_STATUS,
    ORDER_TRANSITIONS,
    RELATION_STATUS_TEXT,
    RELATION_TEXT,
    SOURCE_TEXT,
    AgentMessage,
    AgentSession,
    AuditLog,
    Building,
    Community,
    House,
    HousePerson,
    OrderLog,
    Person,
    User,
    UserRole,
    WorkOrder,
    category_text,
    house_status_text,
    log_action_text,
    order_status_class,
    order_status_text,
    order_urgency_text,
    relation_status_text,
    relation_text,
    source_text,
)
from permissions import (
    ORDER_CANCEL,
    ORDER_CREATE,
    ORDER_DISPATCH,
    ORDER_VERIFY,
    ORDER_WORK,
    STAFF_READ,
    Policy,
    ROLE_NAMES,
    SCOPE_TEXT,
)

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


# --------------------------------------------------------------------------
# 分页与筛选小工具
# --------------------------------------------------------------------------
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


def page_meta(page: int, page_size: int, total: int) -> dict:
    pages = max(1, (total + page_size - 1) // page_size) if total else 1
    return {
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "total": total,
        "has_prev": page > 1,
        "has_next": page < pages,
    }


def _paginate(
    actor: Policy,
    model: type,
    conditions: list,
    page: Any,
    page_size: Any,
    serialize: Callable[[Any], dict],
    order_by: Optional[list] = None,
    options: Optional[list] = None,
    joins: Optional[list] = None,
) -> dict:
    """统一分页：先算总数，再取当前页（都带数据范围）。

    ``joins`` 显式给出 (表, ON 条件)，避免在条件/排序里引用未 join 的表而产生笛卡尔积。
    """
    page_no, size = _page_args(page, page_size)
    stmt = actor.query(model)
    for target, onclause in joins or []:
        stmt = stmt.join(target, onclause)
    stmt = stmt.where(*conditions)
    if options:
        stmt = stmt.options(*options)
    if order_by:
        stmt = stmt.order_by(*order_by)
    total = int(actor.db.execute(select(func.count()).select_from(stmt.order_by(None).subquery())).scalar() or 0)
    rows = actor.db.execute(stmt.limit(size).offset((page_no - 1) * size)).scalars().unique().all()
    data = page_meta(page_no, size, total)
    data["items"] = [serialize(row) for row in rows]
    return data


def _keyword_expr(columns: list, keyword: Any):
    """多列关键字 OR 表达式（无关键字返回 None）。"""
    text = "" if keyword is None else str(keyword).strip()
    if not text:
        return None
    pattern = f"%{text}%"
    return or_(*[column.like(pattern) for column in columns])


def _keyword_like(columns: list, keyword: Any) -> list:
    expr = _keyword_expr(columns, keyword)
    return [expr] if expr is not None else []


def house_name_expr():
    """房屋全称的 SQL 表达式（小区+楼栋+单元+房号）。

    ``+`` 在 MySQL 上编译成 ``concat(...)``、在 SQLite 上编译成 ``||``，两种库都能用。
    """
    return Community.name + Building.name + House.unit + House.room


def normalize_house_query(keyword: Any) -> str:
    """把「1栋1单元101」这类口语输入归一成能匹配「小区+楼栋+单元+房号」拼接串的形式。"""
    text = "" if keyword is None else str(keyword).strip()
    for token in ("小区", "号楼", "单元", "室", "号", "—", "－", "-", " "):
        text = text.replace(token, "")
    return text


def house_match_expr(keyword: Any, fuzzy: bool = False):
    """房屋关键字匹配表达式（无关键字返回 None）。

    默认按「原样」与「去掉单元号后的全称」匹配，即「1栋1单元101」「1栋1101」都能命中；
    ``fuzzy=True`` 时再加一层「按数字片段顺序匹配」的兜底（如「1栋101」省略了单元号），
    只给 :func:`services.resolve_house` 在精确匹配查不到时使用，避免列表搜索过宽。
    需要调用方已经 join 了 Building 与 Community。
    """
    raw = "" if keyword is None else str(keyword).strip()
    if not raw:
        return None
    normalized = normalize_house_query(raw) or raw
    columns = [House.room, House.unit, Building.name, Community.name]
    name_expr = house_name_expr()
    primary = or_(
        *[column.like(f"%{raw}%") for column in columns],
        name_expr.like(f"%{raw}%"),
        name_expr.like(f"%{normalized}%"),
    )
    if not fuzzy:
        return primary
    tokens = re.findall(r"\d+|[A-Za-z]+", normalized)
    if len(tokens) > 1:
        ordered = "%".join(tokens)
        return or_(primary, name_expr.like(f"%{ordered}%"))
    return primary


def house_search_conditions(keyword: Any, fuzzy: bool = False) -> list:
    """兼容调用：返回 ``[表达式]`` 或 ``[]``。"""
    expr = house_match_expr(keyword, fuzzy=fuzzy)
    return [expr] if expr is not None else []


def _status_code(value: Any, label: str = "工单状态") -> Optional[int]:
    if value in (None, "", "all", "全部", "所有"):
        return None
    text = str(value).strip()
    if text.isdigit() and int(text) in ORDER_STATUS_TEXT:
        return int(text)
    for code, name in ORDER_STATUS_TEXT.items():
        if text == name:
            return code
    raise BadRequest(description=f"{label}「{text}」无效")


def _house_status_code(value: Any) -> Optional[int]:
    if value in (None, "", "all", "全部", "所有"):
        return None
    text = str(value).strip()
    if text.isdigit() and int(text) in HOUSE_STATUS_TEXT:
        return int(text)
    for code, name in HOUSE_STATUS_TEXT.items():
        if text == name:
            return code
    raise BadRequest(description=f"房屋状态「{text}」无效")


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y") if value is not None else False


# --------------------------------------------------------------------------
# 序列化（页面与智能体共用一份字段）
# --------------------------------------------------------------------------
def community_to_dict(actor: Policy, row: Community, with_counts: bool = True) -> dict:
    data = row.to_dict()
    if with_counts:
        data["building_count"] = int(
            actor.db.execute(
                select(func.count())
                .select_from(Building)
                .where(Building.community_id == row.id, Building.deleted.is_(False))
            ).scalar()
            or 0
        )
        data["house_count"] = int(
            actor.db.execute(
                select(func.count())
                .select_from(House)
                .where(House.community_id == row.id, House.deleted.is_(False))
            ).scalar()
            or 0
        )
    return data


def building_to_dict(actor: Policy, row: Building, with_counts: bool = True) -> dict:
    data = row.to_dict()
    data["community_name"] = row.community.name if row.community else ""
    if with_counts:
        data["house_count"] = int(
            actor.db.execute(
                select(func.count())
                .select_from(House)
                .where(House.building_id == row.id, House.deleted.is_(False))
            ).scalar()
            or 0
        )
    return data


def relation_to_dict(actor: Policy, row: HousePerson) -> dict:
    data = row.to_dict(exclude=("active_key",))
    house = row.house
    data.update(
        {
            "house_full": house.full_name if house else "",
            "full_name": house.full_name if house else "",
            "person_name": row.person.name if row.person else "",
            "person_phone": row.person.phone if row.person else "",
            "relation_text": relation_text(row.relation),
            "status_text": relation_status_text(row.status),
        }
    )
    return data


def house_to_dict(actor: Policy, row: House, with_residents: bool = True) -> dict:
    data = row.to_dict()
    data.update(
        {
            "full_name": row.full_name,
            "community_name": row.community.name if row.community else "",
            "building_name": row.building.name if row.building else "",
            "status_text": house_status_text(row.status),
            "residents": [],
            "person_count": 0,
        }
    )
    if with_residents:
        relations = [item for item in row.relations if not item.deleted]
        relations.sort(key=lambda item: (item.status != "active", item.id))
        data["residents"] = [
            {
                "relation_id": item.id,
                "person_id": item.person_id,
                "name": item.person.name if item.person else "",
                "phone": item.person.phone if item.person else "",
                "relation": item.relation,
                "relation_text": relation_text(item.relation),
                "status": item.status,
                "status_text": relation_status_text(item.status),
                "start_at": item.start_at.strftime("%Y-%m-%d %H:%M:%S") if item.start_at else "",
                "end_at": item.end_at.strftime("%Y-%m-%d %H:%M:%S") if item.end_at else "",
            }
            for item in relations
        ]
        data["person_count"] = len([item for item in relations if item.status == "active"])
    return data


def person_to_dict(actor: Policy, row: Person, with_houses: bool = True) -> dict:
    data = row.to_dict()
    user = row.user
    data.update(
        {
            "username": user.username if user else "",
            "real_name": user.real_name if user else "",
            "has_account": bool(user),
            "role_names": _user_role_names(actor, user.id) if user else [],
            "houses": [],
            "house_count": 0,
        }
    )
    if with_houses:
        relations = [item for item in row.relations if not item.deleted]
        relations.sort(key=lambda item: (item.status != "active", item.id))
        data["houses"] = [
            {
                "relation_id": item.id,
                "house_id": item.house_id,
                "full_name": item.house.full_name if item.house else "",
                "relation": item.relation,
                "relation_text": relation_text(item.relation),
                "status": item.status,
                "status_text": relation_status_text(item.status),
                "start_at": item.start_at.strftime("%Y-%m-%d %H:%M:%S") if item.start_at else "",
                "end_at": item.end_at.strftime("%Y-%m-%d %H:%M:%S") if item.end_at else "",
            }
            for item in relations
        ]
        data["house_count"] = len([item for item in relations if item.status == "active"])
    return data


def _user_role_names(actor: Policy, user_id: int) -> list[str]:
    codes = actor.db.execute(
        select(UserRole.role_code).where(UserRole.user_id == user_id, UserRole.deleted.is_(False))
    ).scalars()
    return [ROLE_NAMES.get(code, code) for code in codes]


def _user_scopes(actor: Policy, user_id: int) -> list[dict]:
    rows = actor.db.execute(
        select(models.UserScope).where(models.UserScope.user_id == user_id, models.UserScope.deleted.is_(False))
    ).scalars()
    out = []
    for row in rows:
        text = SCOPE_TEXT.get(row.kind, row.kind)
        if row.kind == "community" and row.community_id:
            community = actor.db.get(Community, row.community_id)
            if community:
                text = f"{text}（{community.name}）"
        if row.kind == "building" and row.building_id:
            building = actor.db.get(Building, row.building_id)
            if building:
                text = f"{text}（{building.community.name if building.community else ''}{building.name}）"
        out.append({"kind": row.kind, "kind_text": text, "community_id": row.community_id, "building_id": row.building_id})
    return out


def order_log_to_dict(row: OrderLog) -> dict:
    return {
        "id": row.id,
        "order_id": row.order_id,
        "action": row.action,
        "action_text": log_action_text(row.action),
        "from_status": row.from_status,
        "from_status_text": order_status_text(row.from_status) if row.from_status is not None else "",
        "to_status": row.to_status,
        "to_status_text": order_status_text(row.to_status) if row.to_status is not None else "",
        "operator_id": row.operator_id,
        "operator_name": row.operator.display_name() if row.operator else "系统",
        "note": row.note,
        "created_at": row.created_at.strftime("%Y-%m-%d %H:%M:%S") if row.created_at else "",
    }


def order_to_dict(actor: Policy, row: WorkOrder, with_logs: bool = False) -> dict:
    """工单统一序列化：房屋全称、状态中文名、可执行操作都在这里算好。"""
    house = row.house
    requester = actor.db.get(Person, row.requester_person_id) if row.requester_person_id else None
    data = {
        "id": row.id,
        "no": row.no,
        "status": row.status,
        "status_text": order_status_text(row.status),
        "status_class": order_status_class(row.status),
        "urgency": row.urgency,
        "urgency_text": order_urgency_text(row.urgency),
        "category": row.category,
        "category_text": category_text(row.category),
        "description": row.description,
        "house_id": row.house_id,
        "house_full": house.full_name if house else "",
        "community_id": row.community_id,
        "community_name": house.community.name if house and house.community else "",
        "building_id": row.building_id,
        "building_name": house.building.name if house and house.building else "",
        "unit": house.unit if house else "",
        "room": house.room if house else "",
        "contact_name": row.contact_name,
        "contact_phone": row.contact_phone,
        "owner_id": row.owner_id,
        "owner_name": row.owner.display_name() if row.owner else "",
        "repairer_id": row.repairer_id,
        "repairer_name": row.repairer.display_name() if row.repairer else "",
        "requester_person_id": row.requester_person_id,
        "requester_name": requester.name if requester else "",
        "rating": row.rating,
        "rating_note": row.rating_note,
        "created_at": row.created_at.strftime("%Y-%m-%d %H:%M:%S") if row.created_at else "",
        "updated_at": row.updated_at.strftime("%Y-%m-%d %H:%M:%S") if row.updated_at else "",
        "finished_at": row.finished_at.strftime("%Y-%m-%d %H:%M:%S") if row.finished_at else "",
        "closed_at": row.closed_at.strftime("%Y-%m-%d %H:%M:%S") if row.closed_at else "",
        "version": row.version,
        "actions": order_actions(actor, row),
    }
    if with_logs:
        data["logs"] = [order_log_to_dict(item) for item in row.logs]
    return data


def staff_to_dict(actor: Policy, row: User, open_orders: Optional[int] = None) -> dict:
    data = row.to_dict()
    if open_orders is None:
        open_orders = int(
            actor.db.execute(
                select(func.count())
                .select_from(WorkOrder)
                .where(
                    WorkOrder.repairer_id == row.id,
                    WorkOrder.deleted.is_(False),
                    WorkOrder.status.notin_(ORDER_TERMINAL_STATUS),
                )
            ).scalar()
            or 0
        )
    data.update(
        {
            "role_names": _user_role_names(actor, row.id),
            "role_codes": list(
                actor.db.execute(
                    select(UserRole.role_code).where(UserRole.user_id == row.id, UserRole.deleted.is_(False))
                ).scalars()
            ),
            "scopes": _user_scopes(actor, row.id),
            "display_name": row.display_name(),
            "open_orders": open_orders,
        }
    )
    return data


#: 审计动作（业务服务里的 action 代码）→ 人话
AUDIT_ACTION_TEXT = {
    "order.create": "报修登记", "order.assign": "派单", "order.accept": "接单",
    "order.progress": "维修进度", "order.finish": "完工", "order.verify": "验收通过",
    "order.reopen": "重新返修", "order.cancel": "取消工单", "order.rate": "业主评价",
    "community.create": "新增小区", "community.update": "修改小区", "community.delete": "删除小区",
    "building.create": "新增楼栋", "building.update": "修改楼栋", "building.delete": "删除楼栋",
    "unit.create": "新增单元", "house.create": "新增房屋", "house.update": "修改房屋",
    "house.delete": "删除房屋", "person.create": "新增人员", "person.update": "修改人员",
    "person.delete": "删除人员", "relation.bind": "登记房屋关系", "relation.end": "解除房屋关系",
    "lease.check_in": "办理入住", "lease.check_out": "办理退租",
    "complaint.create": "登记投诉", "complaint.assign": "分配投诉", "complaint.handle": "记录投诉处理",
    "complaint.close": "投诉结案", "complaint.cancel": "取消投诉",
    "visitor.register": "登记访客", "visitor.enter": "访客进入", "visitor.leave": "访客离开",
    "visitor.cancel": "取消访客登记",
    "vehicle.create": "登记车辆", "vehicle.update": "修改车辆", "vehicle.archive": "归档车辆",
    "parking.assign": "分配车位", "parking.release": "释放车位",
    "device.create": "新增设备", "device.update": "更新设备", "device.archive": "归档设备",
    "inspection.create": "安排巡检", "inspection.complete": "完成巡检",
    "bill.create": "创建账单", "bill.create_batch": "批量创建账单", "bill.void": "作废账单",
    "payment.collect": "登记收款", "payment.reverse": "冲销收款",
}

#: 审计详情里这些 key 是内部字段，不展示给用户
_AUDIT_HIDDEN_KEYS = {
    "params", "payload", "payload_hash", "hash", "risk", "action_id", "trace", "trace_id",
    "version", "message", "ok", "id", "count", "created", "skipped", "deleted", "is_error",
}

#: 审计详情的字段名 → 人话标签
_AUDIT_DETAIL_LABEL = {
    "name": "名称", "address": "地址", "room": "房号", "unit": "单元", "area": "面积",
    "status": "状态", "reason": "原因", "note": "说明", "amount": "金额", "fee_type": "费用类型",
    "period": "账期", "method": "方式", "reference": "流水号", "plate": "车牌", "content": "内容",
    "result": "处理结果", "category": "类别", "urgency": "紧急程度", "description": "问题描述",
    "contact_name": "联系人", "contact_phone": "联系电话", "phone": "电话", "relation": "关系",
    "house_id": "房屋编号", "person_id": "人员编号", "order_id": "工单编号", "bill_id": "账单编号",
    "community_id": "小区编号", "building_id": "楼栋编号", "device_id": "设备编号",
    "repairer": "维修师傅", "handler": "处理人", "assignee": "巡检人", "rating": "评分",
    "rent": "月租金", "start_at": "开始日期", "end_at": "结束日期", "due_at": "截止日期",
    "plan_at": "计划时间", "visit_at": "来访时间", "purpose": "来访事由", "location": "位置",
    "house": "房屋", "person": "人员", "device": "设备", "community": "小区", "building": "楼栋",
    "no": "单号", "period_text": "账期", "preview": "", "detail": "详情",
}

#: 值翻译：关系、状态等代码 → 中文
_AUDIT_VALUE_TEXT = {
    "owner": "业主", "tenant": "租户", "family": "家庭成员", "active": "有效", "ended": "已结束",
    "web": "网页操作", "agent": "AI 助手", "cash": "现金", "bank": "银行", "other": "其他",
    "water": "水暖", "electric": "电路", "door": "门窗", "elevator": "电梯", "public": "公共设施",
    "noise": "噪音扰民", "normal": "普通", "urgent": "紧急",
}


def _audit_value_text(value: Any) -> str:
    text = str(value)
    return _AUDIT_VALUE_TEXT.get(text, text)


def _humanize_detail(detail: Any) -> str:
    """把审计详情 dict 变成一句人话。

    优先使用业务消息（``message``，本身就是完整句子），再补充用户关心的字段；
    内部字段（version/params/payload 等）直接不展示。
    """
    if not detail:
        return ""
    if isinstance(detail, str):
        return detail
    if not isinstance(detail, dict):
        return str(detail)

    head = ""
    raw_message = detail.get("message")
    if isinstance(raw_message, str) and raw_message.strip():
        head = raw_message.strip().rstrip("。")

    parts: list[str] = []
    for key, value in detail.items():
        if key in _AUDIT_HIDDEN_KEYS or value in (None, "", [], {}):
            continue
        label = _AUDIT_DETAIL_LABEL.get(key)
        if label is None:
            continue  # 没登记过名字的字段不猜，避免又变成代码
        if isinstance(value, (list, tuple, set)):
            text = "、".join(_audit_value_text(item) for item in list(value)[:6])
        elif isinstance(value, dict):
            text = "、".join(f"{_AUDIT_DETAIL_LABEL.get(k, '')}{_audit_value_text(v)}" for k, v in list(value.items())[:4])
        else:
            text = _audit_value_text(value)
        if not label:  # preview 这类本身就是人话
            text = text if not head or text not in head else ""
            if text:
                parts.append(text)
            continue
        parts.append(f"{label}：{text}")

    # 已在业务消息里出现过的值不再重复罗列，避免"名称：张三…（名称：张三）"
    deduped = []
    for part in parts:
        value_text = part.split("：", 1)[-1]
        if head and value_text and value_text in head:
            continue
        deduped.append(part)
    tail = "；".join(part for part in deduped[:5] if part)
    if head and tail:
        return f"{head}（{tail}）"
    return head or tail


def _humanize_action(action: str, fallback: str) -> str:
    """把 action 代码翻成人话；AI 发起的动作标注来源，工具名走 agent 能力目录的标签。"""
    if not action:
        return fallback or "操作"
    text = action
    prefix = ""
    for marker, human in (("ai_propose:", "AI 提议："), ("ai_execute:", "AI 执行："), ("ai_cancel:", "AI 取消：")):
        if text.startswith(marker):
            prefix = human
            text = text.split(":", 1)[1]
            break
    label = AUDIT_ACTION_TEXT.get(text)
    if not label:
        try:  # 工具名（例如 assign_work_order）直接用能力目录里的中文标签
            from agent.tools import TOOLS_BY_NAME

            spec = TOOLS_BY_NAME.get(text)
            label = spec.label if spec else None
        except Exception:  # noqa: BLE001 - agent 不可用时退回通用文案
            label = None
    label = label or fallback or text
    return prefix + label


def audit_to_dict(row: AuditLog) -> dict:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "username": row.user.username if row.user else "",
        "real_name": row.user.display_name() if row.user else "系统",
        "action": row.action,
        "action_text": _humanize_action(row.action, log_action_text(row.action.split(".")[-1])),
        "target_type": row.target_type,
        "target_type_text": AUDIT_TARGET_TEXT.get(row.target_type, row.target_type),
        "target_id": row.target_id,
        "detail": row.detail or {},
        "detail_text": _humanize_detail(row.detail),
        "source": row.source,
        "source_text": source_text(row.source),
        "created_at": row.created_at.strftime("%Y-%m-%d %H:%M:%S") if row.created_at else "",
    }


AUDIT_TARGET_TEXT = {
    "community": "小区",
    "building": "楼栋",
    "house": "房屋",
    "person": "人员",
    "house_person": "房屋人员关系",
    "work_order": "维修工单",
}


def session_to_dict(row: AgentSession, message_count: Optional[int] = None) -> dict:
    data = {
        "id": row.id,
        "title": row.title or "新对话",
        "dsh_session_id": row.dsh_session_id,
        "created_at": row.created_at.strftime("%Y-%m-%d %H:%M:%S") if row.created_at else "",
        "updated_at": row.updated_at.strftime("%Y-%m-%d %H:%M:%S") if row.updated_at else "",
    }
    if message_count is not None:
        data["message_count"] = message_count
    return data


def message_to_dict(row: AgentMessage) -> dict:
    role_text = {"user": "我", "assistant": "AI 助手", "tool": "工具调用", "system": "系统"}.get(row.role, row.role)
    return {
        "id": row.id,
        "session_id": row.session_id,
        "role": row.role,
        "role_text": role_text,
        "content": row.content,
        "created_at": row.created_at.strftime("%Y-%m-%d %H:%M:%S") if row.created_at else "",
    }


# --------------------------------------------------------------------------
# 工单可执行操作（页面按钮 + 智能体判断）
# --------------------------------------------------------------------------
ORDER_ACTION_TEXT = {
    "assign": "派单",
    "accept": "接单",
    "progress": "登记进度",
    "finish": "完工",
    "verify": "验收通过",
    # 验收不通过 → 退回返修。漏了这一条时按钮会直接显示英文 reopen
    "reopen": "退回返修",
    "cancel": "取消工单",
    "rate": "评价",
}


def order_actions(actor: Policy, order: WorkOrder) -> list[dict]:
    """按「状态机 + 权限 + 是否被派人/本人单」算出可执行操作，模板直接循环渲染。"""
    status = int(order.status)
    actions: list[dict] = []

    def add(name: str, style: str = "secondary", need_note: bool = False, label: str | None = None) -> None:
        actions.append(
            {
                "name": name,
                "label": label or ORDER_ACTION_TEXT.get(name, name),
                "style": style,
                "need_note": need_note,
                "target": f"/orders/{order.id}/{name}",
            }
        )

    is_repairer = bool(actor.user_id and order.repairer_id == actor.user_id)
    if actor.has(ORDER_DISPATCH) and status in (0, 1):
        add("assign", "primary", need_note=True)
    if actor.has(ORDER_WORK) and is_repairer:
        if status == 1:
            add("accept", "primary")
        if status == 2:
            add("progress", "secondary", need_note=True)
            add("finish", "primary", need_note=True)
    if actor.has(ORDER_VERIFY) and status == 3 and not is_repairer:
        add("verify", "success", need_note=True)
    if actor.has(ORDER_VERIFY) and status == 3:
        # 验收不通过 → 退回返修（待验收 → 维修中）
        add("reopen", "secondary", need_note=True)
    if actor.has(ORDER_CANCEL) and status in (0, 1, 2, 3):
        add("cancel", "danger", need_note=True)
    if actor.has(ORDER_CREATE) and status == 4 and (actor.super or _is_owner(actor, order)):
        add("rate", "secondary", need_note=True, label="修改评价" if order.rating else "评价")
    return actions


def _is_owner(actor: Policy, order: WorkOrder) -> bool:
    if not actor.user_id:
        return False
    if order.owner_id == actor.user_id:
        return True
    house_ids = actor.db.execute(
        select(HousePerson.house_id)
        .join(Person, Person.id == HousePerson.person_id)
        .where(
            Person.user_id == actor.user_id,
            Person.deleted.is_(False),
            HousePerson.status == "active",
            HousePerson.deleted.is_(False),
        )
    ).scalars()
    return order.house_id in set(house_ids)


# --------------------------------------------------------------------------
# 小区 / 楼栋 / 房屋
# --------------------------------------------------------------------------
def list_communities(actor: Policy, keyword: Any = None, page: Any = 1, page_size: Any = 50) -> dict:
    conditions = _keyword_like([Community.name, Community.address], keyword)
    return _paginate(
        actor,
        Community,
        conditions,
        page,
        page_size,
        lambda row: community_to_dict(actor, row),
        order_by=[Community.id],
    )


def get_community(actor: Policy, community_id: Any) -> dict:
    return community_to_dict(actor, actor.get(Community, community_id))


def list_buildings(
    actor: Policy,
    community: Any = None,
    community_id: Any = None,
    keyword: Any = None,
    page: Any = 1,
    page_size: Any = 50,
) -> dict:
    ref = community_id if community_id not in (None, "") else community
    conditions = _keyword_like([Building.name], keyword)
    if ref not in (None, ""):
        if str(ref).isdigit():
            conditions.append(Building.community_id == int(ref))
        else:
            conditions.append(Building.community_id.in_(select(Community.id).where(Community.name.like(f"%{ref}%"))))
    return _paginate(
        actor,
        Building,
        conditions,
        page,
        page_size,
        lambda row: building_to_dict(actor, row),
        order_by=[Building.community_id, Building.id],
    )


def get_building(actor: Policy, building_id: Any) -> dict:
    return building_to_dict(actor, actor.get(Building, building_id))


def list_houses(
    actor: Policy,
    community: Any = None,
    building: Any = None,
    keyword: Any = None,
    status: Any = None,
    page: Any = 1,
    page_size: Any = DEFAULT_PAGE_SIZE,
    community_id: Any = None,
    building_id: Any = None,
) -> dict:
    actor.require("house.read")
    # community/building 支持「编号或名称」，community_id/building_id 是等价的编号写法（MCP 工具用）
    if community_id not in (None, ""):
        community = community_id
    if building_id not in (None, ""):
        building = building_id
    conditions = house_search_conditions(keyword)
    if community not in (None, ""):
        conditions.append(
            House.community_id.in_(select(Community.id).where(Community.name.like(f"%{community}%")))
            if not str(community).isdigit()
            else House.community_id == int(community)
        )
    if building not in (None, ""):
        conditions.append(
            House.building_id.in_(select(Building.id).where(Building.name.like(f"%{building}%")))
            if not str(building).isdigit()
            else House.building_id == int(building)
        )
    status_code = _house_status_code(status)
    if status_code is not None:
        conditions.append(House.status == status_code)
    return _paginate(
        actor,
        House,
        conditions,
        page,
        page_size,
        lambda row: house_to_dict(actor, row),
        order_by=[Community.name, Building.name, House.unit, House.room],
        options=[selectinload(House.relations), selectinload(House.relations).selectinload(HousePerson.person)],
        joins=[
            (Building, Building.id == House.building_id),
            (Community, Community.id == House.community_id),
        ],
    )


def get_house(actor: Policy, house_id: Any) -> dict:
    actor.require("house.read")
    house = actor.get(House, house_id)
    data = house_to_dict(actor, house)
    recent = actor.db.execute(
        actor.query(WorkOrder)
        .where(WorkOrder.house_id == house.id)
        .order_by(WorkOrder.created_at.desc(), WorkOrder.id.desc())
        .limit(5)
    ).scalars().unique().all()
    data["recent_orders"] = [order_to_dict(actor, row) for row in recent]
    return data


# --------------------------------------------------------------------------
# 人员与关系
# --------------------------------------------------------------------------
def list_persons(actor: Policy, keyword: Any = None, page: Any = 1, page_size: Any = DEFAULT_PAGE_SIZE) -> dict:
    actor.require("person.read")
    conditions = _keyword_like([Person.name, Person.phone], keyword)
    return _paginate(
        actor,
        Person,
        conditions,
        page,
        page_size,
        lambda row: person_to_dict(actor, row),
        order_by=[Person.id],
        options=[selectinload(Person.relations), selectinload(Person.relations).selectinload(HousePerson.house)],
    )


def get_person(actor: Policy, person_id: Any) -> dict:
    actor.require("person.read")
    return person_to_dict(actor, actor.get(Person, person_id))


def list_relations(
    actor: Policy,
    house_id: Any = None,
    person_id: Any = None,
    status: Any = None,
    page: Any = 1,
    page_size: Any = DEFAULT_PAGE_SIZE,
) -> dict:
    actor.require("person.read")
    conditions: list = []
    if house_id not in (None, ""):
        conditions.append(HousePerson.house_id == int(house_id))
    if person_id not in (None, ""):
        conditions.append(HousePerson.person_id == int(person_id))
    if status not in (None, "", "all", "全部"):
        text = str(status).strip()
        mapping = {"active": "active", "有效": "active", "ended": "ended", "已结束": "ended"}
        if text not in mapping:
            raise BadRequest(description=f"关系状态「{status}」无效")
        conditions.append(HousePerson.status == mapping[text])
    return _paginate(
        actor,
        HousePerson,
        conditions,
        page,
        page_size,
        lambda row: relation_to_dict(actor, row),
        order_by=[HousePerson.status, HousePerson.id.desc()],
    )


# --------------------------------------------------------------------------
# 工单
# --------------------------------------------------------------------------
def list_work_orders(
    actor: Policy,
    status: Any = None,
    keyword: Any = None,
    community: Any = None,
    building: Any = None,
    mine: Any = False,
    page: Any = 1,
    page_size: Any = DEFAULT_PAGE_SIZE,
    community_id: Any = None,
    building_id: Any = None,
) -> dict:
    actor.require("order.read")
    # community/building 支持「编号或名称」；community_id/building_id 是等价的编号写法（MCP 工具用）
    if community_id not in (None, ""):
        community = community_id
    if building_id not in (None, ""):
        building = building_id
    conditions = []
    own_expr = _keyword_expr(
        [WorkOrder.no, WorkOrder.contact_name, WorkOrder.contact_phone, WorkOrder.description],
        keyword,
    )
    house_expr = house_match_expr(keyword)  # 「1栋1单元101」也能搜到该房屋的工单
    parts = [expr for expr in (own_expr, house_expr) if expr is not None]
    if parts:
        conditions.append(or_(*parts))
    status_code = _status_code(status)
    if status_code is not None:
        conditions.append(WorkOrder.status == status_code)
    if community not in (None, ""):
        conditions.append(
            WorkOrder.community_id.in_(select(Community.id).where(Community.name.like(f"%{community}%")))
            if not str(community).isdigit()
            else WorkOrder.community_id == int(community)
        )
    if building not in (None, ""):
        conditions.append(
            WorkOrder.building_id.in_(select(Building.id).where(Building.name.like(f"%{building}%")))
            if not str(building).isdigit()
            else WorkOrder.building_id == int(building)
        )
    if _truthy(mine) and actor.user_id:
        conditions.append(
            or_(WorkOrder.repairer_id == actor.user_id, WorkOrder.owner_id == actor.user_id)
        )
    return _paginate(
        actor,
        WorkOrder,
        conditions,
        page,
        page_size,
        lambda row: order_to_dict(actor, row),
        order_by=[WorkOrder.created_at.desc(), WorkOrder.id.desc()],
        joins=[
            (House, House.id == WorkOrder.house_id),
            (Building, Building.id == WorkOrder.building_id),
            (Community, Community.id == WorkOrder.community_id),
        ],
    )


def get_work_order(actor: Policy, order_id: Any) -> dict:
    actor.require("order.read")
    order = actor.get(WorkOrder, order_id)
    return order_to_dict(actor, order, with_logs=True)


def order_status_summary(actor: Policy, extra_conditions: Optional[list] = None) -> dict:
    """按状态统计工单数（看板用），返回 {status: 数量}。"""
    stmt = (
        select(WorkOrder.status, func.count())
        .select_from(WorkOrder)
        .where(WorkOrder.deleted.is_(False), actor.condition(WorkOrder))
    )
    for condition in extra_conditions or []:
        stmt = stmt.where(condition)
    rows = actor.db.execute(stmt.group_by(WorkOrder.status)).all()
    counts = {code: 0 for code in ORDER_STATUS_TEXT}
    for code, total in rows:
        counts[int(code)] = int(total)
    return counts


def status_counts(actor: Policy, counts: dict) -> list[dict]:
    return [
        {"status": code, "text": ORDER_STATUS_TEXT[code], "count": counts.get(code, 0), "class": ORDER_STATUS_CLASS[code]}
        for code in sorted(ORDER_STATUS_TEXT)
    ]


# --------------------------------------------------------------------------
# 员工
# --------------------------------------------------------------------------
def list_staff(actor: Policy, keyword: Any = None, role: Any = None, page: Any = 1, page_size: Any = 50) -> dict:
    actor.require(STAFF_READ)
    conditions = _keyword_like([User.username, User.real_name, User.phone], keyword)
    if role not in (None, "", "all", "全部"):
        text = str(role).strip()
        code = text if text in ROLE_NAMES else next((key for key, name in ROLE_NAMES.items() if name == text), None)
        if code is None:
            raise BadRequest(description=f"角色「{role}」无效")
        conditions.append(User.id.in_(select(UserRole.user_id).where(UserRole.role_code == code)))
    return _paginate(
        actor,
        User,
        conditions,
        page,
        page_size,
        lambda row: staff_to_dict(actor, row),
        order_by=[User.id],
    )


# --------------------------------------------------------------------------
# 审计
# --------------------------------------------------------------------------
def list_audit_logs(
    actor: Policy,
    keyword: Any = None,
    source: Any = None,
    page: Any = 1,
    page_size: Any = DEFAULT_PAGE_SIZE,
) -> dict:
    actor.require("audit.read")
    conditions = _keyword_like([AuditLog.action, AuditLog.target_type, AuditLog.target_id], keyword)
    text = "" if keyword is None else str(keyword).strip()
    if text:
        # 关键字也支持按操作人姓名/账号搜索
        conditions.append(
            AuditLog.user_id.in_(
                select(User.id).where(or_(User.username.like(f"%{text}%"), User.real_name.like(f"%{text}%")))
            )
        )
    if source not in (None, "", "all", "全部"):
        text = str(source).strip()
        mapping = {"web": "web", "网页操作": "web", "agent": "agent", "AI 助手": "agent", "ai": "agent"}
        if text not in mapping:
            raise BadRequest(description=f"来源「{source}」无效")
        conditions.append(AuditLog.source == mapping[text])
    return _paginate(
        actor,
        AuditLog,
        conditions,
        page,
        page_size,
        audit_to_dict,
        order_by=[AuditLog.created_at.desc(), AuditLog.id.desc()],
    )


# --------------------------------------------------------------------------
# 身份 / 看板 / AI 会话
# --------------------------------------------------------------------------
def whoami(actor: Policy) -> dict:
    """当前身份 + 我的房屋 + 我的工单统计（智能体 whoami 工具与身份卡共用）。"""
    identity = actor.identity()
    houses = []
    if actor.user_id:
        rows = actor.db.execute(
            actor.query(House)
            .where(
                House.id.in_(
                    select(HousePerson.house_id)
                    .join(Person, Person.id == HousePerson.person_id)
                    .where(
                        Person.user_id == actor.user_id,
                        Person.deleted.is_(False),
                        HousePerson.status == "active",
                        HousePerson.deleted.is_(False),
                    )
                )
            )
            .options(selectinload(House.relations))
        ).scalars().unique().all()
        houses = [
            {
                "id": row.id,
                "full_name": row.full_name,
                "room": row.room,
                "unit": row.unit,
                "status": row.status,
                "status_text": house_status_text(row.status),
            }
            for row in rows
        ]
    counts = order_status_summary(actor)
    my_orders = 0
    if actor.user_id:
        my_orders = int(
            actor.db.execute(
                select(func.count())
                .select_from(WorkOrder)
                .where(
                    WorkOrder.deleted.is_(False),
                    or_(WorkOrder.owner_id == actor.user_id, WorkOrder.repairer_id == actor.user_id),
                    actor.condition(WorkOrder),
                )
            ).scalar()
            or 0
        )
    return {
        "identity": identity,
        "houses": houses,
        "order_counts": counts,
        "total_orders": sum(counts.values()),
        "my_orders": my_orders,
        "open_orders": sum(counts.get(code, 0) for code in (0, 1, 2, 3)),
    }


def dashboard(actor: Policy) -> dict:
    """工作台：工单看板 + 最近工单 + 我的身份卡。"""
    counts = order_status_summary(actor)
    recent = actor.db.execute(
        actor.query(WorkOrder).order_by(WorkOrder.created_at.desc(), WorkOrder.id.desc()).limit(5)
    ).scalars().unique().all()
    pending = actor.db.execute(
        actor.query(WorkOrder)
        .where(WorkOrder.status == 0)
        .order_by(WorkOrder.urgency.desc(), WorkOrder.created_at.asc())
        .limit(5)
    ).scalars().unique().all()
    mine_scope = []
    if actor.user_id:
        mine_scope = [or_(WorkOrder.repairer_id == actor.user_id, WorkOrder.owner_id == actor.user_id)]
    my_counts = order_status_summary(actor, mine_scope) if mine_scope else {code: 0 for code in ORDER_STATUS_TEXT}
    identity = actor.identity()
    houses = whoami(actor)["houses"]
    return {
        "identity": identity,
        "counts": counts,
        "status_counts": status_counts(actor, counts),
        "recent_orders": [order_to_dict(actor, row) for row in recent],
        "pending_orders": [order_to_dict(actor, row) for row in pending],
        "my_stats": {
            "my_houses": len(houses),
            "my_orders": sum(my_counts.values()),
            "my_todo": sum(my_counts.get(code, 0) for code in (1, 2, 3)),
            "total_orders": sum(counts.values()),
            "open_orders": sum(counts.get(code, 0) for code in (0, 1, 2, 3)),
        },
    }


def list_sessions(actor: Policy, limit: int = 50) -> list[dict]:
    """我的 AI 会话列表（永远只看自己的）。"""
    rows = actor.db.execute(
        actor.query(AgentSession).order_by(AgentSession.updated_at.desc(), AgentSession.id.desc()).limit(limit)
    ).scalars().unique().all()
    out = []
    for row in rows:
        count = int(
            actor.db.execute(
                select(func.count())
                .select_from(AgentMessage)
                .where(AgentMessage.session_id == row.id, AgentMessage.deleted.is_(False))
            ).scalar()
            or 0
        )
        out.append(session_to_dict(row, message_count=count))
    return out


def get_session(actor: Policy, session_id: Any) -> dict:
    return session_to_dict(actor.get(AgentSession, session_id))


def get_session_messages(actor: Policy, session_id: Any) -> dict:
    session = actor.get(AgentSession, session_id)
    rows = actor.db.execute(
        actor.query(AgentMessage)
        .where(AgentMessage.session_id == session.id)
        .order_by(AgentMessage.id.asc())
    ).scalars().unique().all()
    return {"session": session_to_dict(session), "items": [message_to_dict(row) for row in rows]}


def options_meta() -> dict:
    """页面下拉选项（模板直接用）。"""
    return {
        "categories": [{"value": key, "text": text} for key, text in ORDER_CATEGORY_TEXT.items()],
        "urgencies": [{"value": 0, "text": "普通"}, {"value": 1, "text": "紧急"}],
        "house_status": [{"value": key, "text": text} for key, text in HOUSE_STATUS_TEXT.items()],
        "order_status": [{"value": code, "text": text} for code, text in sorted(ORDER_STATUS_TEXT.items())],
        "relations": [{"value": key, "text": text} for key, text in RELATION_TEXT.items()],
    }


__all__ = [
    "list_communities",
    "get_community",
    "list_buildings",
    "get_building",
    "list_houses",
    "get_house",
    "list_persons",
    "get_person",
    "list_relations",
    "list_work_orders",
    "get_work_order",
    "list_staff",
    "list_audit_logs",
    "whoami",
    "dashboard",
    "list_sessions",
    "get_session",
    "get_session_messages",
    "order_actions",
    "order_to_dict",
    "house_to_dict",
    "person_to_dict",
    "relation_to_dict",
    "building_to_dict",
    "community_to_dict",
    "staff_to_dict",
    "session_to_dict",
    "message_to_dict",
    "page_meta",
    "options_meta",
    "status_counts",
    "order_status_summary",
    "house_search_conditions",
    "house_match_expr",
    "house_name_expr",
    "normalize_house_query",
]



# === v2 增量：单元 / 租赁 / 收费查询（artifact 脚本追加） ===

def _scoped_page(actor, model, scope_condition, conditions, page, page_size, serialize, order_by=None):
    """给「没有 community_id 的从属表」用的分页：用父实体的数据范围条件约束。"""
    page_no, size = _page_args(page, page_size)
    where = [scope_condition, *conditions]
    total = int(actor.db.execute(select(func.count()).select_from(model).where(*where)).scalar() or 0)
    stmt = select(model).where(*where)
    if order_by:
        stmt = stmt.order_by(*order_by)
    rows = actor.db.execute(stmt.limit(size).offset((page_no - 1) * size)).scalars().unique().all()
    data = page_meta(page_no, size, total)
    data["items"] = [serialize(row) for row in rows]
    return data

# 说明：这一段由 artifacts/agent-spec/_add_v2_finance.py 幂等追加。


def list_units(actor, building_id=None, keyword=None, page=1, page_size=DEFAULT_PAGE_SIZE):
    """单元列表（可跨楼栋按关键词查）。"""
    from models import Building, Unit

    conditions = []
    if building_id not in (None, ""):
        conditions.append(Unit.building_id == int(building_id))
    keyword_conditions = _keyword_like([Unit.name], keyword)
    if keyword_conditions:
        conditions.append(
            or_(*keyword_conditions, Unit.building_id.in_(
                select(Building.id).where(*_keyword_like([Building.name], keyword))
            ))
        )

    names = {row.id: row.name for row in actor.db.execute(select(Building)).scalars().all()}

    def serialize(row):
        return dict(row.to_dict(), building_name=names.get(row.building_id, ""),
                    full_name=f"{names.get(row.building_id, '')}{row.name}")

    scope = Unit.building_id.in_(select(Building.id).where(actor.condition(Building)))
    return _scoped_page(actor, Unit, scope, conditions, page, page_size, serialize,
                        order_by=[Unit.building_id, Unit.id])


def list_leases(actor, house_id=None, status=None, keyword=None, page=1, page_size=DEFAULT_PAGE_SIZE):
    """租赁列表（在租 / 已退租）。"""
    from models import House, Lease, Person

    conditions = []
    if house_id not in (None, ""):
        conditions.append(Lease.house_id == int(house_id))
    status_code = None
    if isinstance(status, str) and status.strip() in ("在租", "已退租"):
        status_code = 0 if status.strip() == "在租" else 1
    elif status not in (None, ""):
        status_code = None
    if isinstance(status, str) and status.strip() in ("在租", "已退租"):
        status_code = 0 if status.strip() == "在租" else 1
    elif status not in (None, ""):
        status_code = _status_code(status, "租赁状态")
    if status_code is not None:
        conditions.append(Lease.status == status_code)
    if keyword not in (None, ""):
        conditions.append(
            or_(
                Lease.house_id.in_(select(House.id).where(*house_search_conditions(keyword, fuzzy=True))),
                Lease.person_id.in_(select(Person.id).where(Person.name.like(f"%{str(keyword).strip()}%"))),
            )
        )

    houses = {row.id: row for row in actor.db.execute(select(House)).scalars().all()}
    people = {row.id: row.name for row in actor.db.execute(select(Person)).scalars().all()}
    lease_text = {0: "在租", 1: "已退租"}

    def serialize(row):
        house = houses.get(row.house_id)
        return dict(
            row.to_dict(),
            house_id=row.house_id,
            house_text=house.house_text if house is not None else f"房屋#{row.house_id}",
            person_name=people.get(row.person_id, f"人员#{row.person_id}"),
            rent=float(row.rent or 0),
            status_text=lease_text.get(int(row.status), ""),
        )

    scope = Lease.house_id.in_(select(House.id).where(actor.condition(House)))
    return _scoped_page(actor, Lease, scope, conditions, page, page_size, serialize,
                        order_by=[Lease.id.desc()])


def work_order_stats(actor, community_id=None):
    """按状态统计工单数量（看板与智能体共用）。"""
    actor.require("order.read")
    from models import WorkOrder

    stmt = actor.query(WorkOrder)
    if community_id not in (None, ""):
        stmt = stmt.where(WorkOrder.community_id == int(community_id))
    counts = {code: 0 for code in ORDER_STATUS_TEXT}
    # 先在子查询里把数据范围与过滤条件应用好，再按状态聚合；
    # 聚合必须引用子查询的列（sub.c.*），否则会与全表形成笛卡尔积，计数被放大且绕过数据范围。
    sub = stmt.subquery()
    for status_code, total in actor.db.execute(
        select(sub.c.status, func.count(sub.c.id)).group_by(sub.c.status)
    ):
        counts[int(status_code)] = int(total)
    return {
        "counts": counts,
        "items": [{"status": code, "status_text": ORDER_STATUS_TEXT.get(code, ""), "count": counts.get(code, 0)}
                  for code in sorted(counts)],
        "total": sum(counts.values()),
    }


def list_bills(actor, status=None, house_id=None, keyword=None, overdue=False, community_id=None,
               page=1, page_size=DEFAULT_PAGE_SIZE):
    """账单列表；``overdue=True`` 只看已逾期未缴。"""
    from datetime import datetime as _datetime

    from models import Bill, House, Person

    conditions = []
    if house_id not in (None, ""):
        conditions.append(Bill.house_id == int(house_id))
    if community_id not in (None, ""):
        conditions.append(Bill.community_id == int(community_id))
    bill_text = {0: "待缴", 1: "部分缴纳", 2: "已缴", 3: "已作废"}
    status_code = None
    if isinstance(status, str) and status.strip() in bill_text.values():
        status_code = [code for code, text in bill_text.items() if text == status.strip()][0]
    elif status not in (None, ""):
        status_code = None
    bill_text = {0: "待缴", 1: "部分缴纳", 2: "已缴", 3: "已作废"}
    if isinstance(status, str) and status.strip() in bill_text.values():
        status_code = [code for code, text in bill_text.items() if text == status.strip()][0]
    elif status not in (None, ""):
        status_code = _status_code(status, "账单状态")
    if status_code is not None:
        conditions.append(Bill.status == status_code)
    if _truthy(overdue):
        conditions.append(Bill.status.in_([0, 1]))
        conditions.append(Bill.due_at.isnot(None))
        conditions.append(Bill.due_at < _datetime.now())
    if keyword not in (None, ""):
        pattern = f"%{str(keyword).strip()}%"
        conditions.append(
            or_(
                Bill.no.like(pattern),
                Bill.fee_type.like(pattern),
                Bill.house_id.in_(select(House.id).where(*house_search_conditions(keyword, fuzzy=True))),
            )
        )

    houses = {row.id: row for row in actor.db.execute(select(House)).scalars().all()}
    people = {row.id: row.name for row in actor.db.execute(select(Person)).scalars().all()}
    now = _datetime.now()

    def serialize(row):
        house = houses.get(row.house_id)
        outstanding = float(row.amount or 0) - float(row.paid_amount or 0)
        return dict(
            row.to_dict(),
            amount=float(row.amount or 0),
            paid_amount=float(row.paid_amount or 0),
            outstanding=outstanding,
            house_text=house.house_text if house is not None else f"房屋#{row.house_id}",
            person_name=people.get(row.person_id, ""),
            status_text=bill_text.get(int(row.status), ""),
            overdue=bool(row.due_at and row.due_at < now and int(row.status) in (0, 1)),
        )

    return _paginate(actor, Bill, conditions, page, page_size, serialize, order_by=[Bill.id.desc()])


def get_bill(actor, bill_id):
    """账单详情（含它的收款记录）。"""
    from models import Bill, House, Person

    row = actor.get(Bill, int(bill_id))
    # 详情页标题是「房屋 · 费用类型」，少了 house_text 就只剩一个孤零零的分隔符
    house = actor.db.get(House, row.house_id)
    person = actor.db.get(Person, row.person_id) if row.person_id else None
    detail = {
        "id": row.id, "no": row.no, "house_id": row.house_id,
        "house_text": house.house_text if house is not None else f"房屋#{row.house_id}",
        # 标题与工单/投诉详情同一口径用 full_name（美家花园1栋1单元201）；
        # house_text 里的单元名是裸数字（“1栋 1 201”），放在标题里不好读
        "house_full": house.full_name if house is not None else f"房屋#{row.house_id}",
        "person_name": person.name if person is not None else "",
        "created_at": row.created_at,
        "fee_type": row.fee_type, "amount": float(row.amount or 0),
        "paid_amount": float(row.paid_amount or 0),
        "outstanding": float(row.amount or 0) - float(row.paid_amount or 0),
        "status": int(row.status),
        "status_text": {0: "待缴", 1: "部分缴纳", 2: "已缴", 3: "已作废"}.get(int(row.status), ""),
        "period": row.period, "due_at": row.due_at.strftime("%Y-%m-%d") if row.due_at else "",
        "version": row.version,
    }
    payments = list_payments(actor, bill_id=row.id, page=1, page_size=50)
    detail["payments"] = payments["items"]
    detail["payments_total"] = payments["total"]
    return detail


def list_payments(actor, bill_id=None, status=None, keyword=None, page=1, page_size=DEFAULT_PAGE_SIZE):
    """收款 / 冲销记录。"""
    from models import Bill, House, Payment, User

    conditions = []
    if bill_id not in (None, ""):
        conditions.append(Payment.bill_id == int(bill_id))
    pay_text = {0: "已入账", 1: "已冲销"}
    status_code = None
    if isinstance(status, str) and status.strip() in pay_text.values():
        status_code = [code for code, text in pay_text.items() if text == status.strip()][0]
    elif status not in (None, ""):
        status_code = None
    pay_text = {0: "已入账", 1: "已冲销"}
    if isinstance(status, str) and status.strip() in pay_text.values():
        status_code = [code for code, text in pay_text.items() if text == status.strip()][0]
    elif status not in (None, ""):
        status_code = _status_code(status, "收款状态")
    if status_code is not None:
        conditions.append(Payment.status == status_code)
    if keyword not in (None, ""):
        pattern = f"%{str(keyword).strip()}%"
        conditions.append(
            or_(
                Payment.no.like(pattern),
                Payment.reference.like(pattern),
                Payment.bill_id.in_(select(Bill.id).where(Bill.no.like(pattern))),
                Payment.bill_id.in_(
                    select(Bill.id).where(Bill.house_id.in_(
                        select(House.id).where(*house_search_conditions(keyword, fuzzy=True))
                    ))
                ),
            )
        )

    bills = {row.id: row for row in actor.db.execute(select(Bill)).scalars().all()}
    houses = {row.id: row for row in actor.db.execute(select(House)).scalars().all()}
    users = {row.id: (row.real_name or row.username) for row in actor.db.execute(select(User)).scalars().all()}
    method_text = {0: "现金", 1: "银行", 2: "其他"}

    def serialize(row):
        bill = bills.get(row.bill_id)
        house = houses.get(bill.house_id) if bill is not None else None
        return dict(
            row.to_dict(),
            amount=float(row.amount or 0),
            method_text=method_text.get(int(row.method), ""),
            status_text=pay_text.get(int(row.status), ""),
            bill_no=bill.no if bill is not None else "",
            house_text=house.house_text if house is not None else "",
            operator_name=users.get(row.operator_id, ""),
            reversed_by_name=users.get(row.reversed_by, ""),
        )

    scope = Payment.bill_id.in_(select(Bill.id).where(actor.condition(Bill)))
    return _scoped_page(actor, Payment, scope, conditions, page, page_size, serialize,
                        order_by=[Payment.id.desc()])


def arrears_summary(actor, community_id=None):
    """按小区汇总欠费（户数 + 金额）。"""
    from models import Bill, Community

    conditions = [Bill.status.in_([0, 1])]
    if community_id not in (None, ""):
        conditions.append(Bill.community_id == int(community_id))
    rows = []
    names = {row.id: row.name for row in actor.db.execute(select(Community)).scalars().all()}
    # 先在子查询里把数据范围内的欠费账单筛出来，再按小区聚合；
    # 注意聚合必须引用子查询的列（sub.c.*），否则会与全表形成笛卡尔积导致金额翻倍。
    sub = actor.query(Bill).where(*conditions).subquery()
    for community, houses, amount, paid in actor.db.execute(
        select(sub.c.community_id, func.count(func.distinct(sub.c.house_id)),
               func.sum(sub.c.amount), func.sum(sub.c.paid_amount))
        .group_by(sub.c.community_id)
    ):
        outstanding = float(amount or 0) - float(paid or 0)
        rows.append({
            "community_id": int(community),
            "community_name": names.get(int(community), f"小区#{community}"),
            "house_count": int(houses or 0),
            "amount": round(outstanding, 2),
        })
    return {
        "items": rows,
        "total": len(rows),
        "amount_total": round(sum(item["amount"] for item in rows), 2),
        "house_total": sum(item["house_count"] for item in rows),
    }
