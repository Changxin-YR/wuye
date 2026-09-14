"""领域服务：**所有写操作的唯一入口**（页面表单与智能体工具共用同一套函数）。

每个写命令固定按这个顺序执行：

    require(权限) → require_scope(数据范围) → 参数校验 → 业务规则 → 写库
    → order_log / audit → 回读 → 返回可 JSON 序列化的 dict

约定：
- 业务错误统一抛 :class:`ServiceError`（``code`` + 通俗中文 ``message``），web 层转 flash。
- 权限/数据范围越权仍由 :class:`permissions.Policy` 抛 ``abort(401/403/404)``；
  智能体侧用 ``permissions.http_message()`` 转成通俗中文。
- 写操作在自己的事务里提交（同一事务内写业务行 + order_log + audit_log），保证一致性。
"""
from __future__ import annotations

import functools
import hashlib
import inspect
import json
import logging
import re
from datetime import timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

import queries
from audit import write_audit

log = logging.getLogger(__name__)
from models import (
    Unit,
    ORDER_CATEGORY_TEXT,
    ORDER_TERMINAL_STATUS,
    ORDER_TRANSITIONS,
    RELATION_STATUS_TEXT,
    RELATION_TEXT,
    AgentMessage,
    AgentSession,
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
    order_status_text,
    utcnow,
)
from permissions import (
    COMMUNITY_WRITE,
    HOUSE_WRITE,
    ORDER_CANCEL,
    ORDER_CREATE,
    ORDER_DISPATCH,
    ORDER_VERIFY,
    ORDER_WORK,
    PERSON_WRITE,
    RELATION_WRITE,
    RESIDENT_SELF,
    Policy,
)

#: 表示「没传这个参数」（用于区分「不修改」与「清空」）
_UNSET = object()


class ServiceError(Exception):
    """业务错误：通俗中文，带机器可读的 code 与 HTTP 语义码。

    ``status_code``：``invalid``→400、``state``/``conflict``（含乐观锁冲突）→409、
    ``not_found``→404、``forbidden``→403；web 层可据此决定 flash 还是 409 页面。
    """

    ok = False
    STATUS_CODES = {"invalid": 400, "state": 409, "conflict": 409, "not_found": 404, "forbidden": 403}

    def __init__(self, code: str, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code or "invalid"
        self.message = message
        self.status_code = status_code or self.STATUS_CODES.get(self.code, 400)

    def as_dict(self) -> dict:
        return {"ok": False, "code": self.code, "status": self.status_code, "message": self.message}


# --------------------------------------------------------------------------
# 参数校验
# --------------------------------------------------------------------------
_PHONE_RE = re.compile(r"^(?:1[3-9]\d{9}|0\d{2,3}\d{7,8}|400\d{7}|800\d{7})$")


def _text(
    value: Any,
    label: str,
    required: bool = True,
    maxlen: int | None = None,
    minlen: int | None = None,
) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        if required:
            raise ServiceError("invalid", f"请填写{label}")
        return ""
    if maxlen and len(text) > maxlen:
        raise ServiceError("invalid", f"{label}不能超过 {maxlen} 个字")
    if minlen and len(text) < minlen:
        raise ServiceError("invalid", f"{label}至少需要 {minlen} 个字")
    return text


def _int(value: Any, label: str, required: bool = True, minimum: int | None = None, maximum: int | None = None):
    if value is None or str(value).strip() == "":
        if required:
            raise ServiceError("invalid", f"请填写{label}")
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ServiceError("invalid", f"{label}必须是整数") from exc
    if minimum is not None and number < minimum:
        raise ServiceError("invalid", f"{label}不能小于 {minimum}")
    if maximum is not None and number > maximum:
        raise ServiceError("invalid", f"{label}不能大于 {maximum}")
    return number


def _number(value: Any, label: str, required: bool = False, minimum: float | None = None, maximum: float | None = None):
    if value is None or str(value).strip() == "":
        if required:
            raise ServiceError("invalid", f"请填写{label}")
        return None
    try:
        number = round(float(str(value).strip()), 2)
    except (TypeError, ValueError) as exc:
        raise ServiceError("invalid", f"{label}必须是数字") from exc
    if minimum is not None and number < minimum:
        raise ServiceError("invalid", f"{label}不能小于 {minimum}")
    if maximum is not None and number > maximum:
        raise ServiceError("invalid", f"{label}不能大于 {maximum}")
    return number


def _phone(value: Any, label: str = "联系电话", required: bool = True) -> str:
    text = _text(value, label, required=required, maxlen=32)
    if not text:
        return ""
    cleaned = re.sub(r"[\s\-()（）]", "", text)
    if not _PHONE_RE.match(cleaned):
        raise ServiceError("invalid", f"{label}格式不正确，请填写 11 位手机号或带区号的座机号")
    return cleaned


def _identifier(value: Any, label: str) -> Any:
    """标识参数：纯数字视为编号，其它按名称处理。"""
    if value is None:
        raise ServiceError("invalid", f"请指定{label}")
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        raise ServiceError("invalid", f"请指定{label}")
    if text.isdigit():
        return int(text)
    return text


RELATION_ALIASES = {
    "owner": "owner",
    "业主": "owner",
    "户主": "owner",
    "房主": "owner",
    "产权人": "owner",
    "tenant": "tenant",
    "租户": "tenant",
    "租客": "tenant",
    "承租人": "tenant",
    "family": "family",
    "家庭成员": "family",
    "家人": "family",
    "家属": "family",
    "亲属": "family",
}

CATEGORY_ALIASES = {
    "water": "water",
    "水暖": "water",
    "水管": "water",
    "漏水": "water",
    "暖气": "water",
    "下水": "water",
    "electric": "electric",
    "电路": "electric",
    "电路维修": "electric",
    "电": "electric",
    "跳闸": "electric",
    "灯": "electric",
    "door": "door",
    "门窗": "door",
    "门": "door",
    "窗": "door",
    "elevator": "elevator",
    "电梯": "elevator",
    "public": "public",
    "公共设施": "public",
    "公共区域": "public",
    "其他": "other",
}

URGENCY_ALIASES = {"紧急": 1, "加急": 1, "急": 1, "urgent": 1, "high": 1, "普通": 0, "不急": 0, "normal": 0, "low": 0}


def _choice(value: Any, aliases: dict, allowed: Iterable[str], label: str, default: str | None = None) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        if default is None:
            raise ServiceError("invalid", f"请选择{label}")
        return default
    key = text.lower() if text.isascii() else text
    code = aliases.get(key) or aliases.get(text)
    if code is None:
        code = text if text in allowed else None
    if code is None:
        options = "、".join(f"「{item}」" for item in aliases.keys() if not item.isascii())
        raise ServiceError("invalid", f"「{text}」不是有效的{label}，可选：{options}")
    return code


def _urgency(value: Any) -> int:
    if value is None or str(value).strip() == "":
        return 0
    text = str(value).strip()
    if text in URGENCY_ALIASES:
        return int(URGENCY_ALIASES[text])
    if text.lower() in URGENCY_ALIASES:
        return int(URGENCY_ALIASES[text.lower()])
    if text.isdigit():
        number = int(text)
        if number in (0, 1):
            return number
    raise ServiceError("invalid", f"紧急程度「{text}」无效，可选：普通、紧急")


def _house_status(value: Any, default: int | None = None) -> int:
    if value is None or str(value).strip() == "":
        if default is None:
            raise ServiceError("invalid", "请选择房屋状态")
        return default
    text = str(value).strip()
    mapping = {"空置": 0, "自住": 1, "出租": 2, "0": 0, "1": 1, "2": 2}
    if text not in mapping:
        raise ServiceError("invalid", f"房屋状态「{text}」无效，可选：空置、自住、出租")
    return mapping[text]


def _check_version(row: Any, expected_version: Any, label: str) -> None:
    """乐观锁校验：智能体传了 ``expected_version`` 且与当前版本不一致时拒绝写入。"""
    if expected_version in (None, ""):
        return
    try:
        expected = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ServiceError("invalid", "版本号必须是整数") from exc
    current = int(getattr(row, "version", 0) or 0)
    if expected != current:
        raise ServiceError("conflict", f"{label}已经被别人改过了（当前版本 {current}），请重新查看后再提交")


def _commit(db: Session) -> None:
    """提交事务，把数据库异常翻译成业务错误；提交后让 ORM 重新取最新数据再回读。"""
    try:
        db.commit()
    except StaleDataError as exc:
        db.rollback()
        raise ServiceError("conflict", "这条数据刚刚被其他人修改过，请刷新后重试") from exc
    except IntegrityError as exc:
        db.rollback()
        raise ServiceError("conflict", "保存失败：数据重复或与其它记录冲突，请检查后重试") from exc
    # 回读必须拿到提交后的真实状态（含派单人、日志等关联对象）
    db.expire_all()


# --------------------------------------------------------------------------
# 对象解析（智能体可能传名称，也可能传编号）
# --------------------------------------------------------------------------
def _pick(rows: list, label: str, hint: str = "") -> Any:
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise ServiceError("not_found", f"没有找到{label}{hint}，或者它不在你可见的范围内")
    names = "、".join(getattr(item, "full_name", None) or getattr(item, "name", None) or str(item.id) for item in rows[:5])
    raise ServiceError("invalid", f"找到多条{label}：{names}，请说得更具体一些（例如带上编号或房号）")


def resolve_community(actor: Policy, ref: Any) -> Community:
    ref = _identifier(ref, "小区")
    if isinstance(ref, int):
        row = actor.db.execute(actor.query(Community).where(Community.id == ref)).scalars().first()
        if row is None:
            raise ServiceError("not_found", f"没有找到编号为 {ref} 的小区")
        return row
    rows = actor.db.execute(actor.query(Community).where(Community.name.like(f"%{ref}%"))).scalars().all()
    return _pick(list(rows), f"小区「{ref}」")


def resolve_building(actor: Policy, ref: Any, community: Community | None = None) -> Building:
    ref = _identifier(ref, "楼栋")
    stmt = actor.query(Building)
    if isinstance(ref, int):
        stmt = stmt.where(Building.id == ref)
    else:
        stmt = stmt.where(Building.name.like(f"%{ref}%"))
    if community is not None:
        stmt = stmt.where(Building.community_id == community.id)
    rows = actor.db.execute(stmt).scalars().unique().all()
    return _pick(list(rows), f"楼栋「{ref}」")


def resolve_house(actor: Policy, ref: Any) -> House:
    ref = _identifier(ref, "房屋")
    if isinstance(ref, int):
        row = actor.db.execute(actor.query(House).where(House.id == ref)).scalars().first()
        if row is None:
            raise ServiceError("not_found", f"没有找到编号为 {ref} 的房屋，或者它不在你可见的范围内")
        return row
    text = str(ref).strip()
    base = (
        actor.query(House)
        .join(Building, Building.id == House.building_id)
        .join(Community, Community.id == House.community_id)
    )
    rows = actor.db.execute(base.where(queries.house_match_expr(text))).scalars().unique().all()
    if not rows:
        # 精确匹配不到时再放宽一次（例如「1栋101」省略了单元号）
        rows = actor.db.execute(base.where(queries.house_match_expr(text, fuzzy=True))).scalars().unique().all()
    exact = [
        house
        for house in rows
        if text in {house.full_name, house.room, f"{house.building.name}{house.room}", f"{house.building.name}{house.unit}单元{house.room}"}
    ]
    return _pick(list(exact or rows), f"房屋「{text}」")


def resolve_person(actor: Policy, ref: Any) -> Person:
    ref = _identifier(ref, "人员")
    if isinstance(ref, int):
        row = actor.db.execute(actor.query(Person).where(Person.id == ref)).scalars().first()
        if row is None:
            raise ServiceError("not_found", f"没有找到编号为 {ref} 的人员")
        return row
    text = str(ref).strip()
    rows = list(
        actor.db.execute(
            actor.query(Person).where(or_(Person.name.like(f"%{text}%"), Person.phone.like(f"%{text}%")))
        )
        .scalars()
        .unique()
        .all()
    )
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise ServiceError("not_found", f"没有找到人员「{text}」")
    # 同名消歧：给出房号与身份，方便选择
    labeled = []
    for person in rows[:5]:
        houses = actor.db.execute(
            select(HousePerson).where(HousePerson.person_id == person.id, HousePerson.deleted.is_(False))
        ).scalars().all()
        where = "、".join(f"{item.house.full_name}{RELATION_TEXT.get(item.relation, '')}" for item in houses if item.house) or "暂未关联房屋"
        labeled.append(f"{person.name}（{where}）")
    raise ServiceError("invalid", f"找到 {len(rows)} 位「{text}」：{'；'.join(labeled)}，请指定人员编号")


def resolve_repairer(actor: Policy, ref: Any) -> User:
    """派单目标：必须是工程维修账号，或与工程维修账号绑定的人员。"""
    ref = _identifier(ref, "维修人员")
    user: User | None = None
    if isinstance(ref, int):
        user = actor.db.execute(actor.query(User).where(User.id == ref)).scalars().first()
    if user is None and not isinstance(ref, int):
        text = str(ref).strip()
        user = (
            actor.db.execute(
                actor.query(User).where(or_(User.username == text, User.real_name == text, User.username.like(f"%{text}%")))
            )
            .scalars()
            .first()
        )
    if user is None:
        # 也允许直接报「人员」的名字（例如「黄磊」），前提是该人员绑定了工程维修账号
        try:
            person = resolve_person(actor, ref)
        except ServiceError:
            person = None
        if person is not None and person.user_id:
            user = actor.db.execute(actor.query(User).where(User.id == person.user_id)).scalars().first()
        elif person is not None:
            raise ServiceError("invalid", f"「{person.name}」还没有绑定登录账号，不能派单")
    if user is None:
        raise ServiceError("not_found", f"没有找到维修人员「{ref}」，可以用查看员工功能确认可选人员")
    roles = set(
        actor.db.execute(select(UserRole.role_code).where(UserRole.user_id == user.id, UserRole.deleted.is_(False))).scalars()
    )
    if "engineer" not in roles:
        raise ServiceError("invalid", f"「{user.display_name()}」不是工程维修人员，不能派单")
    if not user.active:
        raise ServiceError("invalid", f"「{user.display_name()}」的账号已停用，不能派单")
    return user


def resolve_order(actor: Policy, ref: Any) -> WorkOrder:
    ref = _identifier(ref, "工单")
    if isinstance(ref, int):
        stmt = actor.query(WorkOrder).where(WorkOrder.id == ref)
    else:
        stmt = actor.query(WorkOrder).where(WorkOrder.no == str(ref).strip().upper())
    order = actor.db.execute(stmt).scalars().unique().first()
    if order is None:
        raise ServiceError("not_found", f"没有找到工单「{ref}」，或者它不在你可见的范围内")
    return order


# --------------------------------------------------------------------------
# 事务内小工具
# --------------------------------------------------------------------------
def my_house_ids(actor: Policy) -> set[int]:
    """当前账号本人（含家庭成员）的有效房屋编号。"""
    if not actor.user_id:
        return set()
    stmt = (
        select(HousePerson.house_id)
        .join(Person, Person.id == HousePerson.person_id)
        .where(
            Person.user_id == actor.user_id,
            Person.deleted.is_(False),
            HousePerson.status == "active",
            HousePerson.deleted.is_(False),
        )
    )
    return set(actor.db.execute(stmt).scalars())


def is_order_owner(actor: Policy, order: WorkOrder) -> bool:
    """是否算「本人单」（报修人本人或本人房屋的工单）。"""
    if not actor.user_id:
        return False
    if order.owner_id == actor.user_id:
        return True
    return order.house_id in my_house_ids(actor)


def _active_relations(db: Session, house_id: int) -> list[HousePerson]:
    return list(
        db.execute(
            select(HousePerson).where(
                HousePerson.house_id == house_id,
                HousePerson.status == "active",
                HousePerson.deleted.is_(False),
            )
        )
        .scalars()
        .unique()
        .all()
    )


def _open_order_count(db: Session, house_id: int) -> int:
    return int(
        db.execute(
            select(func.count())
            .select_from(WorkOrder)
            .where(
                WorkOrder.house_id == house_id,
                WorkOrder.deleted.is_(False),
                WorkOrder.status.notin_(ORDER_TERMINAL_STATUS),
            )
        ).scalar()
        or 0
    )


def _house_owner_user_id(db: Session, house_id: int) -> Optional[int]:
    return db.execute(
        select(Person.user_id)
        .join(HousePerson, HousePerson.person_id == Person.id)
        .where(
            HousePerson.house_id == house_id,
            HousePerson.relation == "owner",
            HousePerson.status == "active",
            HousePerson.deleted.is_(False),
            Person.deleted.is_(False),
            Person.user_id.isnot(None),
        )
        .limit(1)
    ).scalar()


def _my_person(actor: Policy) -> Optional[Person]:
    if not actor.user_id:
        return None
    return (
        actor.db.execute(
            select(Person).where(Person.user_id == actor.user_id, Person.deleted.is_(False)).limit(1)
        )
        .scalars()
        .first()
    )


def _next_order_no(db: Session, moment) -> str:
    prefix = f"WO{moment.strftime('%Y%m%d')}"
    latest = db.execute(select(func.max(WorkOrder.no)).where(WorkOrder.no.like(f"{prefix}%"))).scalar()
    seq = 1
    if latest and latest[len(prefix):].isdigit():
        seq = int(latest[len(prefix):]) + 1
    return f"{prefix}{seq:04d}"


def _log_order(
    db: Session,
    order: WorkOrder,
    action: str,
    from_status: int | None,
    to_status: int | None,
    operator_id: int | None,
    note: str = "",
) -> OrderLog:
    row = OrderLog(
        order_id=order.id,
        action=action,
        from_status=from_status,
        to_status=to_status,
        operator_id=operator_id,
        note=note[:500],
    )
    db.add(row)
    return row


def _transition(order: WorkOrder, action: str) -> tuple[int, int]:
    """按状态机校验流转，返回 (原状态, 新状态)。"""
    current = int(order.status)
    allowed = ORDER_TRANSITIONS.get(current, {})
    if action not in allowed:
        raise ServiceError(
            "state",
            f"工单当前是「{order_status_text(current)}」，不能执行该操作",
        )
    return current, int(allowed[action])


# --------------------------------------------------------------------------
# 空间主数据：小区 / 楼栋 / 房屋
# --------------------------------------------------------------------------
def create_community(actor: Policy, name: Any, address: Any = "") -> dict:
    """新建小区（数据范围无法覆盖「还不存在的小区」，因此只允许全量范围账号操作）。"""
    actor.require(COMMUNITY_WRITE)
    if not actor.super:
        raise ServiceError("forbidden", "只有管理员可以新建小区")
    name = _text(name, "小区名称", maxlen=64)
    address = _text(address, "小区地址", required=False, maxlen=255)
    db = actor.db
    exists = db.execute(
        select(Community.id).where(Community.name == name, Community.deleted.is_(False))
    ).scalars().first()
    if exists:
        raise ServiceError("conflict", f"小区「{name}」已经存在")
    row = Community(name=name, address=address)
    db.add(row)
    db.flush()
    write_audit(db, actor, "community.create", "community", row.id, {"name": name, "address": address})
    _commit(db)
    return queries.get_community(actor, row.id)


def update_community(actor: Policy, community_id: Any, name: Any = None, address: Any = None, expected_version: Any = None
) -> dict:
    actor.require(COMMUNITY_WRITE)
    db = actor.db
    row = resolve_community(actor, community_id)
    actor.require_scope(row.id, write=True)
    _check_version(row, expected_version, f"小区「{row.name}」")
    if name is not None:
        new_name = _text(name, "小区名称", maxlen=64)
        if new_name != row.name:
            dup = db.execute(
                select(Community.id).where(Community.name == new_name, Community.deleted.is_(False), Community.id != row.id)
            ).scalars().first()
            if dup:
                raise ServiceError("conflict", f"小区「{new_name}」已经存在")
            row.name = new_name
    if address is not None:
        row.address = _text(address, "小区地址", required=False, maxlen=255)
    row.touch()
    write_audit(db, actor, "community.update", "community", row.id, {"name": row.name, "address": row.address})
    _commit(db)
    return queries.get_community(actor, row.id)


def delete_community(actor: Policy, community_id: Any, expected_version: Any = None
) -> dict:
    actor.require(COMMUNITY_WRITE)
    db = actor.db
    row = resolve_community(actor, community_id)
    actor.require_scope(row.id, write=True)
    _check_version(row, expected_version, f"小区「{row.name}」")
    buildings = db.execute(
        select(func.count()).select_from(Building).where(Building.community_id == row.id, Building.deleted.is_(False))
    ).scalar()
    if buildings:
        raise ServiceError("conflict", f"小区「{row.name}」下还有 {buildings} 栋楼，请先删除楼栋")
    row.deleted = True
    row.touch()
    write_audit(db, actor, "community.delete", "community", row.id, {"name": row.name})
    _commit(db)
    return {"ok": True, "id": row.id, "name": row.name, "version": row.version, "message": f"小区「{row.name}」已删除"}


def create_building(actor: Policy, community_id: Any = None, name: Any = None, community: Any = None) -> dict:
    """新增楼栋（楼栋名在小区内唯一）；``community``/``community_id`` 二选一。"""
    actor.require(COMMUNITY_WRITE)
    target = resolve_community(actor, community_id if community_id not in (None, "") else community)
    actor.require_scope(target.id, write=True)
    name = _text(name, "楼栋名称", maxlen=64)
    db = actor.db
    dup = db.execute(
        select(Building.id).where(
            Building.community_id == target.id, Building.name == name, Building.deleted.is_(False)
        )
    ).scalars().first()
    if dup:
        raise ServiceError("conflict", f"{target.name}已经有「{name}」了")
    row = Building(community_id=target.id, name=name)
    db.add(row)
    db.flush()
    write_audit(db, actor, "building.create", "building", row.id, {"name": name, "community": target.name})
    _commit(db)
    return queries.building_to_dict(actor, row)


def update_building(actor: Policy, building_id: Any, name: Any = None, expected_version: Any = None
) -> dict:
    actor.require(COMMUNITY_WRITE)
    db = actor.db
    row = resolve_building(actor, building_id)
    actor.require_scope(row.community_id, row.id, write=True)
    _check_version(row, expected_version, f"楼栋「{row.name}」")
    if name is not None:
        new_name = _text(name, "楼栋名称", maxlen=64)
        if new_name != row.name:
            dup = db.execute(
                select(Building.id).where(
                    Building.community_id == row.community_id,
                    Building.name == new_name,
                    Building.deleted.is_(False),
                    Building.id != row.id,
                )
            ).scalars().first()
            if dup:
                raise ServiceError("conflict", f"该小区已经有「{new_name}」了")
            row.name = new_name
    row.touch()
    write_audit(db, actor, "building.update", "building", row.id, {"name": row.name})
    _commit(db)
    return queries.building_to_dict(actor, row)


def delete_building(actor: Policy, building_id: Any, expected_version: Any = None
) -> dict:
    actor.require(COMMUNITY_WRITE)
    db = actor.db
    row = resolve_building(actor, building_id)
    actor.require_scope(row.community_id, row.id, write=True)
    _check_version(row, expected_version, f"楼栋「{row.name}」")
    houses = db.execute(
        select(func.count()).select_from(House).where(House.building_id == row.id, House.deleted.is_(False))
    ).scalar()
    if houses:
        raise ServiceError("conflict", f"「{row.name}」下还有 {houses} 套房屋，请先删除房屋")
    row.deleted = True
    row.touch()
    write_audit(db, actor, "building.delete", "building", row.id, {"name": row.name})
    _commit(db)
    return {"ok": True, "id": row.id, "name": row.name, "version": row.version, "message": f"楼栋「{row.name}」已删除"}


def create_house(
    actor: Policy,
    building_id: Any = None,
    unit: Any = "",
    room: Any = None,
    area: Any = None,
    status: Any = 0,
    community_id: Any = None,
    community: Any = None,
    building: Any = None,
    unit_name: Any = None,
) -> dict:
    """新增房屋（房号在楼栋内唯一）。

    ``community``/``community_id``、``building``/``building_id``、``unit``/``unit_name`` 都是等价写法，
    智能体可以传名称也可以传编号。
    """
    actor.require(HOUSE_WRITE)
    if unit_name not in (None, ""):
        unit = unit_name
    room = _text(room, "房号", maxlen=32)
    unit = _text(unit, "单元", required=False, maxlen=16)
    area_value = _number(area, "建筑面积", required=False, minimum=0, maximum=100000)
    status_value = _house_status(status, default=0)
    db = actor.db

    community_ref = community_id if community_id not in (None, "") else community
    target_community = resolve_community(actor, community_ref) if community_ref not in (None, "") else None
    building_ref = building_id if building_id not in (None, "") else building
    if building_ref in (None, ""):
        raise ServiceError("invalid", "请选择楼栋")
    target_building = resolve_building(actor, building_ref, target_community)
    target_community = target_building.community
    actor.require_scope(target_community.id, target_building.id, write=True)

    dup = db.execute(
        select(House.id).where(
            House.building_id == target_building.id,
            House.unit == unit,
            House.room == room,
            House.deleted.is_(False),
        )
    ).scalars().first()
    if dup:
        raise ServiceError(
            "conflict", f"{target_community.name}{target_building.name}{unit}单元{room} 已经存在"
        )

    unit_row = _ensure_unit(db, target_building, unit)
    row = House(
        community_id=target_community.id,
        building_id=target_building.id,
        unit_id=unit_row.id if unit_row is not None else None,
        unit=unit_row.name if unit_row is not None else unit,
        room=room,
        area=area_value,
        status=status_value,
    )
    db.add(row)
    db.flush()
    write_audit(
        db,
        actor,
        "house.create",
        "house",
        row.id,
        {"full_name": row.full_name, "area": area_value, "status": status_value},
    )
    _commit(db)
    return queries.get_house(actor, row.id)


def update_house(
    actor: Policy,
    house_id: Any,
    unit: Any = None,
    room: Any = None,
    area: Any = None,
    status: Any = None,
    expected_version: Any = None,
) -> dict:
    actor.require(HOUSE_WRITE)
    db = actor.db
    row = resolve_house(actor, house_id)
    actor.require_scope(row.community_id, row.building_id, write=True)
    _check_version(row, expected_version, f"{row.full_name}")

    new_unit = row.unit if unit is None else _text(unit, "单元", required=False, maxlen=16)
    new_room = row.room if room is None else _text(room, "房号", maxlen=32)
    if new_unit != row.unit or new_room != row.room:
        dup = db.execute(
            select(House.id).where(
                House.building_id == row.building_id,
                House.unit == new_unit,
                House.room == new_room,
                House.deleted.is_(False),
                House.id != row.id,
            )
        ).scalars().first()
        if dup:
            raise ServiceError("conflict", f"房号「{new_unit}单元{new_room}」在本楼栋已经存在")
        row.unit, row.room = new_unit, new_room
        unit_row = _ensure_unit(db, db.get(Building, row.building_id), new_unit)
        row.unit_id = unit_row.id if unit_row is not None else None

    if area is not None:
        row.area = _number(area, "建筑面积", required=False, minimum=0, maximum=100000)

    if status is not None:
        new_status = _house_status(status)
        if new_status == 0 and _active_relations(db, row.id):
            raise ServiceError("state", "这套房屋还有在住人员（业主/租户/家庭成员），不能改成空置")
        row.status = new_status

    row.touch()
    write_audit(
        db,
        actor,
        "house.update",
        "house",
        row.id,
        {"full_name": row.full_name, "area": row.area, "status": row.status},
    )
    _commit(db)
    return queries.get_house(actor, row.id)


def delete_house(actor: Policy, house_id: Any, expected_version: Any = None
) -> dict:
    actor.require(HOUSE_WRITE)
    db = actor.db
    row = resolve_house(actor, house_id)
    actor.require_scope(row.community_id, row.building_id, write=True)
    _check_version(row, expected_version, row.full_name)
    relations = _active_relations(db, row.id)
    if relations:
        names = "、".join(item.person.name for item in relations if item.person)
        raise ServiceError("conflict", f"{row.full_name} 还有在住人员（{names}），请先结束房屋人员关系")
    open_orders = _open_order_count(db, row.id)
    if open_orders:
        raise ServiceError("conflict", f"{row.full_name} 还有 {open_orders} 张未完结的工单，请先处理完再删除")
    full_name = row.full_name
    row.deleted = True
    row.touch()
    write_audit(db, actor, "house.delete", "house", row.id, {"full_name": full_name})
    _commit(db)
    return {"ok": True, "id": row.id, "full_name": full_name, "version": row.version, "message": f"{full_name} 已删除"}


# --------------------------------------------------------------------------
# 人员与房屋关系
# --------------------------------------------------------------------------
def create_person(actor: Policy, name: Any, phone: Any = "", user_id: Any = None) -> dict:
    """新增人员档案（人员不挂小区，数据范围在绑定关系与查询时校验）。"""
    actor.require(PERSON_WRITE)
    name = _text(name, "姓名", maxlen=64)
    phone = _phone(phone, required=False)
    db = actor.db
    linked_user = None
    if user_id not in (None, ""):
        linked_user = actor.get(User, _identifier(user_id, "登录账号"))
    if phone:
        dup = db.execute(
            select(Person.id).where(
                Person.name == name, Person.phone == phone, Person.deleted.is_(False)
            )
        ).scalars().first()
        if dup:
            raise ServiceError("conflict", f"已经有一位同名同电话的「{name}」了")
    row = Person(name=name, phone=phone, user_id=linked_user.id if linked_user else None)
    db.add(row)
    db.flush()
    write_audit(db, actor, "person.create", "person", row.id, {"name": name, "phone": phone})
    _commit(db)
    return queries.get_person(actor, row.id)


def update_person(
    actor: Policy,
    person_id: Any,
    name: Any = None,
    phone: Any = None,
    user_id: Any = _UNSET,
    expected_version: Any = None,
) -> dict:
    actor.require(PERSON_WRITE)
    db = actor.db
    row = resolve_person(actor, person_id)
    actor.require_visible(row)
    _check_version(row, expected_version, f"人员「{row.name}」")
    if name is not None:
        row.name = _text(name, "姓名", maxlen=64)
    if phone is not None:
        row.phone = _phone(phone, required=False)
    if user_id is not _UNSET:
        if user_id in (None, ""):
            row.user_id = None
        else:
            row.user_id = actor.get(User, _identifier(user_id, "登录账号")).id
    row.touch()
    write_audit(db, actor, "person.update", "person", row.id, {"name": row.name, "phone": row.phone})
    _commit(db)
    return queries.get_person(actor, row.id)


def delete_person(actor: Policy, person_id: Any, expected_version: Any = None
) -> dict:
    actor.require(PERSON_WRITE)
    db = actor.db
    row = resolve_person(actor, person_id)
    actor.require_visible(row)
    _check_version(row, expected_version, f"人员「{row.name}」")
    active = list(
        db.execute(
            select(HousePerson).where(
                HousePerson.person_id == row.id,
                HousePerson.status == "active",
                HousePerson.deleted.is_(False),
            )
        )
        .scalars()
        .unique()
        .all()
    )
    if active:
        where = "、".join(item.house.full_name for item in active if item.house)
        raise ServiceError("conflict", f"「{row.name}」还有有效的房屋关系（{where}），请先结束关系")
    name = row.name
    row.deleted = True
    row.touch()
    write_audit(db, actor, "person.delete", "person", row.id, {"name": name})
    _commit(db)
    return {"ok": True, "id": row.id, "name": name, "version": row.version, "message": f"人员「{name}」已删除"}


def bind_relation(
    actor: Policy,
    house_id: Any,
    person_id: Any,
    relation: Any = "family",
    note: Any = "",
    start_at: Any = None,
) -> dict:
    """绑定房屋人员关系（同一房屋同一人员只允许一条有效关系）。"""
    actor.require(RELATION_WRITE)
    db = actor.db
    house = resolve_house(actor, house_id)
    actor.require_scope(house.community_id, house.building_id, write=True)
    actor.require_house(house)
    person = resolve_person(actor, person_id)
    relation_code = _choice(relation, RELATION_ALIASES, {"owner", "tenant", "family"}, "人员身份", default="family")
    note = _text(note, "备注", required=False, maxlen=255)

    existing = db.execute(
        select(HousePerson).where(
            HousePerson.house_id == house.id,
            HousePerson.person_id == person.id,
            HousePerson.status == "active",
            HousePerson.deleted.is_(False),
        )
    ).scalars().first()
    if existing:
        raise ServiceError(
            "conflict",
            f"「{person.name}」已经是 {house.full_name} 的有效住户（{RELATION_TEXT.get(existing.relation, '')}），"
            "请先结束原关系",
        )
    if relation_code == "owner":
        owner = db.execute(
            select(HousePerson)
            .where(
                HousePerson.house_id == house.id,
                HousePerson.relation == "owner",
                HousePerson.status == "active",
                HousePerson.deleted.is_(False),
            )
        ).scalars().first()
        if owner and owner.person:
            raise ServiceError("conflict", f"{house.full_name} 已经有业主「{owner.person.name}」，请先结束原业主关系")

    row = HousePerson(
        house_id=house.id,
        person_id=person.id,
        relation=relation_code,
        status="active",
        start_at=start_at or utcnow(),
        note=note,
        active_key=f"{house.id}:{person.id}",
    )
    db.add(row)
    if relation_code == "owner" and house.status == 0:
        house.status = 1  # 业主入住后房屋默认是自住
        house.touch()
    db.flush()
    write_audit(
        db,
        actor,
        "relation.bind",
        "house_person",
        row.id,
        {"house": house.full_name, "person": person.name, "relation": relation_code},
    )
    _commit(db)
    return queries.relation_to_dict(actor, row)


def end_relation(actor: Policy, relation_id: Any, reason: Any = "", expected_version: Any = None
) -> dict:
    """结束房屋人员关系。"""
    actor.require(RELATION_WRITE)
    db = actor.db
    relation = resolve_relation(actor, relation_id)
    house = relation.house
    actor.require_scope(house.community_id, house.building_id, write=True)
    _check_version(relation, expected_version, "这条房屋人员关系")
    if relation.status != "active":
        raise ServiceError("state", f"这条人员关系已经是「{RELATION_STATUS_TEXT.get(relation.status, relation.status)}」状态")
    reason = _text(reason, "结束原因", required=False, maxlen=255)
    relation.status = "ended"
    relation.end_at = utcnow()
    relation.active_key = None
    if reason:
        relation.note = reason
    relation.touch()
    remaining_owner = db.execute(
        select(HousePerson.id).where(
            HousePerson.house_id == house.id,
            HousePerson.status == "active",
            HousePerson.relation == "owner",
            HousePerson.deleted.is_(False),
            HousePerson.id != relation.id,
        )
    ).scalars().first()
    if not remaining_owner and house.status == 1:
        house.status = 0  # 业主搬走后房屋回到空置
        house.touch()
    write_audit(
        db,
        actor,
        "relation.end",
        "house_person",
        relation.id,
        {"house": house.full_name, "person": relation.person.name if relation.person else "", "reason": reason},
    )
    _commit(db)
    return queries.relation_to_dict(actor, relation)


def resolve_relation(actor: Policy, ref: Any) -> HousePerson:
    ref = _identifier(ref, "人员关系")
    if isinstance(ref, int):
        stmt = actor.query(HousePerson).where(HousePerson.id == ref)
    else:
        raise ServiceError("invalid", f"人员关系编号「{ref}」无效")
    relation = actor.db.execute(stmt).scalars().unique().first()
    if relation is None:
        raise ServiceError("not_found", f"没有找到编号为 {ref} 的人员关系，或者它不在你可见的范围内")
    return relation


# --------------------------------------------------------------------------
# 工单闭环
# --------------------------------------------------------------------------
def create_work_order(
    actor: Policy,
    house_id: Any = None,
    contact_name: Any = None,
    contact_phone: Any = None,
    category: Any = None,
    description: Any = None,
    urgency: Any = 0,
    house: Any = None,
) -> dict:
    """报修登记：联系人与电话必填，房屋必填。"""
    actor.require(ORDER_CREATE)
    db = actor.db
    target_house = resolve_house(actor, house_id if house_id not in (None, "") else house)
    actor.require_scope(target_house.community_id, target_house.building_id)
    actor.require_house(target_house)

    contact_name = _text(contact_name, "联系人", maxlen=64)
    contact_phone = _phone(contact_phone)
    category_code = _choice(category, CATEGORY_ALIASES, set(ORDER_CATEGORY_TEXT), "报修类型", default="other")
    description = _text(description, "报修内容", maxlen=1000, minlen=2)
    urgency_value = _urgency(urgency)

    if target_house.deleted:
        raise ServiceError("not_found", "这套房屋已被删除，不能报修")

    me = _my_person(actor)
    requester_id = None
    if me is not None:
        bound = db.execute(
            select(HousePerson.id).where(
                HousePerson.house_id == target_house.id,
                HousePerson.person_id == me.id,
                HousePerson.status == "active",
                HousePerson.deleted.is_(False),
            )
        ).scalars().first()
        if bound:
            requester_id = me.id
    owner_id = _house_owner_user_id(db, target_house.id)
    if owner_id is None and RESIDENT_SELF in actor.permissions:
        owner_id = actor.user_id

    moment = utcnow()
    row = WorkOrder(
        no=_next_order_no(db, moment),
        community_id=target_house.community_id,
        building_id=target_house.building_id,
        house_id=target_house.id,
        requester_person_id=requester_id,
        owner_id=owner_id,
        contact_name=contact_name,
        contact_phone=contact_phone,
        category=category_code,
        description=description,
        urgency=urgency_value,
        status=0,
    )
    db.add(row)
    db.flush()
    _log_order(db, row, "create", None, 0, actor.user_id, f"{contact_name} 报修：{description[:80]}")
    write_audit(
        db,
        actor,
        "order.create",
        "work_order",
        row.id,
        {"no": row.no, "house": target_house.full_name, "category": category_code, "urgency": urgency_value},
    )
    _commit(db)
    return queries.order_to_dict(actor, row)


def assign_work_order(actor: Policy, order_id: Any, repairer: Any = None, note: Any = "", expected_version: Any = None
) -> dict:
    """派单：目标必须是工程维修账号（或与其绑定的人员）。"""
    actor.require(ORDER_DISPATCH)
    db = actor.db
    order = resolve_order(actor, order_id)
    actor.require_scope(order.community_id, order.building_id)
    _check_version(order, expected_version, f"工单 {order.no}")
    if order.status not in (0, 1):
        raise ServiceError("state", f"工单当前是「{order_status_text(order.status)}」，不能再派单")
    user = resolve_repairer(actor, repairer)
    note_text = _text(note, "派单备注", required=False, maxlen=500)
    from_status = int(order.status)
    order.repairer_id = user.id
    order.status = 1
    order.touch()
    _log_order(
        db,
        order,
        "assign",
        from_status,
        1,
        actor.user_id,
        note_text or f"派单给 {user.display_name()}",
    )
    write_audit(
        db,
        actor,
        "order.assign",
        "work_order",
        order.id,
        {"no": order.no, "repairer": user.display_name(), "from_status": from_status},
    )
    _commit(db)
    return queries.order_to_dict(actor, order)


def _require_repairer(actor: Policy, order: WorkOrder) -> None:
    if not order.repairer_id or order.repairer_id != actor.user_id:
        raise ServiceError("forbidden", "只有被指派的维修人员才能做这个操作")


def accept_work_order(actor: Policy, order_id: Any, note: Any = "", expected_version: Any = None
) -> dict:
    """接单：仅被派人，已派单 → 维修中。"""
    actor.require(ORDER_WORK)
    db = actor.db
    order = resolve_order(actor, order_id)
    actor.require_scope(order.community_id, order.building_id)
    _check_version(order, expected_version, f"工单 {order.no}")
    _require_repairer(actor, order)
    from_status, to_status = _transition(order, "accept")
    note_text = _text(note, "备注", required=False, maxlen=500)
    order.status = to_status
    order.touch()
    _log_order(db, order, "accept", from_status, to_status, actor.user_id, note_text or "已接单，开始安排上门")
    write_audit(db, actor, "order.accept", "work_order", order.id, {"no": order.no, "to_status": to_status})
    _commit(db)
    return queries.order_to_dict(actor, order)


def add_order_progress(actor: Policy, order_id: Any, note: Any = None, expected_version: Any = None
) -> dict:
    """登记维修进度：仅被派人，工单须为维修中。"""
    actor.require(ORDER_WORK)
    db = actor.db
    order = resolve_order(actor, order_id)
    actor.require_scope(order.community_id, order.building_id)
    _check_version(order, expected_version, f"工单 {order.no}")
    _require_repairer(actor, order)
    if int(order.status) != 2:
        raise ServiceError("state", f"工单当前是「{order_status_text(order.status)}」，只有维修中的工单可以登记进度")
    note_text = _text(note, "维修进度", maxlen=500, minlen=2)
    _log_order(db, order, "progress", 2, 2, actor.user_id, note_text)
    write_audit(db, actor, "order.progress", "work_order", order.id, {"no": order.no, "note": note_text})
    _commit(db)
    return queries.order_to_dict(actor, order, with_logs=True)


def finish_work_order(actor: Policy, order_id: Any, note: Any = None, expected_version: Any = None
) -> dict:
    """完工：仅被派人，维修中 → 待验收。"""
    actor.require(ORDER_WORK)
    db = actor.db
    order = resolve_order(actor, order_id)
    actor.require_scope(order.community_id, order.building_id)
    _check_version(order, expected_version, f"工单 {order.no}")
    _require_repairer(actor, order)
    from_status, to_status = _transition(order, "finish")
    note_text = _text(note, "完工说明", required=False, maxlen=500)
    order.status = to_status
    order.finished_at = utcnow()
    order.touch()
    _log_order(db, order, "finish", from_status, to_status, actor.user_id, note_text or "维修完成，等待验收")
    write_audit(db, actor, "order.finish", "work_order", order.id, {"no": order.no, "note": note_text})
    _commit(db)
    return queries.order_to_dict(actor, order)


def verify_work_order(actor: Policy, order_id: Any, note: Any = None, expected_version: Any = None
) -> dict:
    """验收：待验收 → 已关闭。"""
    actor.require(ORDER_VERIFY)
    db = actor.db
    order = resolve_order(actor, order_id)
    actor.require_scope(order.community_id, order.building_id)
    _check_version(order, expected_version, f"工单 {order.no}")
    if order.repairer_id and order.repairer_id == actor.user_id:
        raise ServiceError("forbidden", "维修人员不能验收自己完成的工单")
    from_status, to_status = _transition(order, "verify")
    note_text = _text(note, "验收说明", required=False, maxlen=500)
    order.status = to_status
    order.closed_at = utcnow()
    order.touch()
    _log_order(db, order, "verify", from_status, to_status, actor.user_id, note_text or "验收通过，工单关闭")
    write_audit(db, actor, "order.verify", "work_order", order.id, {"no": order.no, "note": note_text})
    _commit(db)
    return queries.order_to_dict(actor, order)


def cancel_work_order(actor: Policy, order_id: Any, reason: Any = None, expected_version: Any = None
) -> dict:
    """取消：非终态 → 已取消，必须填原因。"""
    actor.require(ORDER_CANCEL)
    db = actor.db
    order = resolve_order(actor, order_id)
    actor.require_scope(order.community_id, order.building_id)
    _check_version(order, expected_version, f"工单 {order.no}")
    reason_text = _text(reason, "取消原因", maxlen=500, minlen=2)
    from_status, to_status = _transition(order, "cancel")
    order.status = to_status
    order.touch()
    _log_order(db, order, "cancel", from_status, to_status, actor.user_id, f"取消原因：{reason_text}")
    write_audit(db, actor, "order.cancel", "work_order", order.id, {"no": order.no, "reason": reason_text})
    _commit(db)
    return queries.order_to_dict(actor, order)


def rate_work_order(actor: Policy, order_id: Any, rating: Any = None, note: Any = None, expected_version: Any = None
) -> dict:
    """评价：已关闭的本人单可评价 1–5 分。"""
    actor.require(ORDER_CREATE)
    db = actor.db
    order = resolve_order(actor, order_id)
    actor.require_scope(order.community_id, order.building_id)
    _check_version(order, expected_version, f"工单 {order.no}")
    if int(order.status) != 4:
        raise ServiceError("state", f"工单当前是「{order_status_text(order.status)}」，只有已关闭的工单可以评价")
    if not is_order_owner(actor, order) and not actor.super:
        raise ServiceError("forbidden", "只有报修人本人可以评价这张工单")
    score = _int(rating, "评分", minimum=1, maximum=5)
    note_text = _text(note, "评价内容", required=False, maxlen=255)
    order.rating = score
    order.rating_note = note_text
    order.touch()
    _log_order(db, order, "rate", 4, 4, actor.user_id, f"{score} 分 {note_text}".strip())
    write_audit(db, actor, "order.rate", "work_order", order.id, {"no": order.no, "rating": score, "note": note_text})
    _commit(db)
    return queries.order_to_dict(actor, order)


# --------------------------------------------------------------------------
# AI 会话（页面与桥接层共用；智能体工具不涉及）
# --------------------------------------------------------------------------
def create_ai_session(actor: Policy, title: Any = None) -> dict:
    """新建 AI 会话。"""
    db = actor.db
    if not actor.user_id:
        raise ServiceError("forbidden", "请先登录")
    title_text = _text(title, "会话标题", required=False, maxlen=128) or f"{utcnow().strftime('%m-%d %H:%M')} 的对话"
    row = AgentSession(user_id=actor.user_id, title=title_text)
    db.add(row)
    db.flush()
    _commit(db)
    return queries.session_to_dict(row)


def append_ai_message(actor: Policy, session_id: Any, role: str, content: str) -> dict:
    """给 AI 会话追加一条消息（供 SSE 桥写库）。"""
    db = actor.db
    session = actor.get(AgentSession, session_id)
    if session.user_id != actor.user_id:
        raise ServiceError("forbidden", "只能查看自己的 AI 会话")
    role = role if role in ("user", "assistant", "tool", "system") else "user"
    row = AgentMessage(session_id=session.id, role=role, content=content or "")
    db.add(row)
    db.flush()
    if not session.title:
        session.title = (content or "")[:40]
    session.touch()
    _commit(db)
    return queries.message_to_dict(row)


def set_session_dsh_id(actor: Policy, session_id: Any, dsh_session_id: str) -> None:
    """记录 dsh 运行时会话号（桥接层用）。"""
    db = actor.db
    session = actor.get(AgentSession, session_id)
    session.dsh_session_id = dsh_session_id
    session.touch()
    _commit(db)


# --------------------------------------------------------------------------
# 写命令统一契约（智能体 MCP 侧依赖）
# --------------------------------------------------------------------------
# 两个跨切面要求，集中在这里实现，避免在 20 多个函数体里重复：
#   1. 每个写命令都接受 ``source="web"``（MCP 侧传 ``"agent"``），写进 audit_log.source；
#   2. 每个写命令的返回值都带 ``message``（通俗中文，智能体直接拿去汇报）。
# 包一层不改变原有调用方式，也不改变返回 dict 的其它字段。
_WRITE_MESSAGE_TEMPLATES: dict[str, str] = {
    "create_community": "小区「{name}」已创建",
    "update_community": "小区「{name}」已更新",
    "delete_community": "小区「{name}」已删除",
    "create_building": "楼栋「{name}」已创建",
    "update_building": "楼栋「{name}」已更新",
    "delete_building": "楼栋「{name}」已删除",
    "create_house": "{full_name} 已创建",
    "update_house": "{full_name} 已更新",
    "delete_house": "房屋已删除",
    "create_person": "人员「{name}」已创建",
    "update_person": "人员「{name}」已更新",
    "delete_person": "人员「{name}」已删除",
    "bind_relation": "房屋人员关系已登记（{relation_text}）",
    "end_relation": "房屋人员关系已结束",
    "create_work_order": "工单 {no} 已创建",
    "assign_work_order": "工单 {no} 已派给 {repairer_name}",
    "accept_work_order": "工单 {no} 已接单",
    "add_order_progress": "工单 {no} 已登记维修进度",
    "finish_work_order": "工单 {no} 已完工，等待验收",
    "verify_work_order": "工单 {no} 已验收通过并关闭",
    "cancel_work_order": "工单 {no} 已取消",
    "rate_work_order": "工单 {no} 已评价",
    "create_ai_session": "AI 会话已创建",
    "update_unit": "{building_name}{name} 已更新",
    "delete_unit": "单元已删除",
}


#: ``ai_action.status`` 取值：0 待确认、1 已执行、2 幂等回放记录、3 已过期、4 失败/取消
IDEMPOTENCY_STATUS = 2
#: 幂等回放窗口（小时）
IDEMPOTENCY_TTL_HOURS = 24


def _idempotency_digest(command: str, request_key: str) -> str:
    return hashlib.sha256(f"{command}|{request_key}".encode("utf-8")).hexdigest()


def _replay_result(actor: Policy, command: str, request_key: str) -> dict | None:
    """幂等键命中则回放上次结果（同一用户 + 同一命令 + 同一 request_key）。"""
    from models import AiAction

    if not actor.user_id:
        return None
    row = actor.db.execute(
        select(AiAction).where(
            AiAction.user_id == actor.user_id,
            AiAction.tool_name == command,
            AiAction.payload_hash == _idempotency_digest(command, request_key),
            AiAction.status == IDEMPOTENCY_STATUS,
            AiAction.deleted.is_(False),
        )
    ).scalars().first()
    if row is None:
        return None
    if row.expires_at and row.expires_at < utcnow():
        return None
    try:
        data = json.loads(row.result or "{}")
    except Exception as exc:  # 记录损坏就当没命中
        log.warning("幂等记录解析失败，按未命中处理：%s", exc)
        return None
    if not isinstance(data, dict):
        return None
    data["idempotent_replay"] = True
    data["message"] = data.get("message") or "这条请求之前已经执行过，直接返回上次结果"
    return data


def _remember_result(actor: Policy, command: str, request_key: str, data: dict) -> None:
    """记录本次结果，供同一 request_key 的重复请求回放（独立小事务，失败不影响主流程）。"""
    from models import AiAction

    if not actor.user_id or not isinstance(data, dict):
        return
    try:
        row = AiAction(
            user_id=actor.user_id,
            auth_version=int(getattr(actor.user, "auth_version", 1) or 1),
            session_id=None,
            tool_name=command,
            payload=json.dumps({"request_key": request_key}, ensure_ascii=False),
            payload_hash=_idempotency_digest(command, request_key),
            preview=f"{command} 幂等记录",
            risk_level=_risk_level(command),
            status=IDEMPOTENCY_STATUS,
            result=json.dumps(data, ensure_ascii=False, default=str),
            expires_at=utcnow() + timedelta(hours=IDEMPOTENCY_TTL_HOURS),
        )
        actor.db.add(row)
        actor.db.commit()
    except Exception as exc:  # pragma: no cover - 幂等记录失败不能影响业务结果
        log.warning("幂等记录写入失败（不影响业务结果）：%s", exc, exc_info=True)
        actor.db.rollback()


def _risk_level(command: str) -> str:
    """命令的风险等级（risk.py 是唯一来源；查不到按 R1）。"""
    try:
        import risk

        return risk.level(command) or "R1"
    except Exception:  # pragma: no cover
        return "R1"


def _wrap_write_command(fn, template: str):
    """给写命令补上 ``source`` / ``request_key`` / ``expected_version`` 与 ``message`` / ``version``。

    - ``source``：``web``（默认）或 ``agent``，写进 audit_log.source；
    - ``request_key``：幂等键，重复请求直接回放上次结果（``ai_action.status=2`` 记录）；
    - ``expected_version``：乐观锁，函数内部用 ``_check_version`` 校验，不符抛 409 冲突；
    - 返回值统一带 ``message``（通俗中文）与 ``version``（当前版本号）。
    """
    signature = inspect.signature(fn)
    parameters = list(signature.parameters.values())
    accepts_version = any(item.name == "expected_version" for item in parameters)
    for extra in ("source", "request_key", "expected_version"):
        if not any(item.name == extra for item in parameters):
            annotation = str if extra in ("source", "request_key") else int
            default = "web" if extra == "source" else None
            parameters.append(
                inspect.Parameter(extra, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=annotation)
            )
    new_signature = signature.replace(parameters=parameters)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        explicit_source = "source" in kwargs
        source = str(kwargs.pop("source", "web") or "web")
        request_key = kwargs.pop("request_key", None)
        if not accepts_version:
            kwargs.pop("expected_version", None)  # 函数没有实现乐观锁时不往下传
        actor = args[0] if args else kwargs.get("actor")
        if explicit_source and isinstance(actor, Policy) and source in ("web", "agent"):
            actor.source = source

        if request_key and isinstance(actor, Policy):
            replay = _replay_result(actor, fn.__name__, str(request_key))
            if replay is not None:
                return replay

        data = fn(*args, **kwargs)
        if isinstance(data, dict):
            if not data.get("message"):
                try:
                    data["message"] = template.format(**{**data, **kwargs})
                except Exception:  # 模板里引用了没返回的字段时兜底
                    data["message"] = "操作已完成"
            data.setdefault("version", None)
            if request_key and isinstance(actor, Policy):
                _remember_result(actor, fn.__name__, str(request_key), data)
        return data

    wrapper.__signature__ = new_signature  # type: ignore[attr-defined]
    wrapper.__doc__ = fn.__doc__
    wrapper.__wuye_write_command__ = True  # type: ignore[attr-defined]
    wrapper.__wuye_accepts_version__ = accepts_version  # type: ignore[attr-defined]
    return wrapper


for _name, _template in _WRITE_MESSAGE_TEMPLATES.items():
    _target = globals().get(_name)
    if callable(_target) and not getattr(_target, "__wuye_write_command__", False):
        globals()[_name] = _wrap_write_command(_target, _template)
del _name, _template, _target


__all__ = [
    "ServiceError",
    "resolve_community",
    "resolve_building",
    "resolve_house",
    "resolve_person",
    "resolve_relation",
    "resolve_repairer",
    "resolve_order",
    "my_house_ids",
    "is_order_owner",
    "create_community",
    "update_community",
    "delete_community",
    "create_building",
    "update_building",
    "delete_building",
    "create_house",
    "update_house",
    "delete_house",
    "create_person",
    "update_person",
    "delete_person",
    "bind_relation",
    "end_relation",
    "create_work_order",
    "assign_work_order",
    "accept_work_order",
    "add_order_progress",
    "finish_work_order",
    "verify_work_order",
    "cancel_work_order",
    "rate_work_order",
    "create_ai_session",
    "append_ai_message",
    "set_session_dsh_id",
]

BILL_STATUS_TEXT = {0: "待缴", 1: "部分缴纳", 2: "已缴", 3: "已作废"}
PAYMENT_METHOD_TEXT = {0: "现金", 1: "银行", 2: "其他"}


# === v2 增量：单元 / 租赁 / 收费写命令（artifact 脚本追加） ===
# 说明：这一段由 artifacts/agent-spec/_add_v2_finance.py 幂等追加，重复执行不会重复定义。


def _next_doc_no(db: Session, model: Any, prefix: str) -> str:
    """生成流水号，例：BILL202609120001 / PAY202609120001 / LS202609120001。"""
    head = f"{prefix}{utcnow().strftime('%Y%m%d')}"
    latest = db.execute(select(func.max(model.no)).where(model.no.like(f"{head}%"))).scalar()
    seq = 1
    if latest and latest[len(head):].isdigit():
        seq = int(latest[len(head):]) + 1
    return f"{head}{seq:04d}"


def _date_value(value: Any, label: str, required: bool = True):
    """解析日期（接受 date/datetime 或 YYYY-MM-DD [HH:MM[:SS]]）。"""
    from datetime import date as _date, datetime as _datetime

    if value in (None, ""):
        if required:
            raise ServiceError("invalid", f"请填写{label}")
        return None
    if isinstance(value, _datetime):
        return value
    if isinstance(value, _date):
        return _datetime(value.year, value.month, value.day)
    text = str(value).strip().replace("/", "-").replace("年", "-").replace("月", "-").replace("日", "")
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return _datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ServiceError("invalid", f"{label}格式应为 2026-09-12 这样的日期")


def _money(value: Any, label: str, required: bool = True, minimum: Any = 0):
    """金额一律用 Decimal，避免浮点误差。"""
    from decimal import Decimal, InvalidOperation

    if value in (None, ""):
        if required:
            raise ServiceError("invalid", f"请填写{label}")
        return Decimal("0.00")
    try:
        number = Decimal(str(value).strip()).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ServiceError("invalid", f"{label}必须是数字") from exc
    if minimum is not None and number < Decimal(str(minimum)):
        raise ServiceError("invalid", f"{label}不能小于 {minimum}")
    return number


def _payment_method(value: Any) -> int:
    """收款方式：现金 / 银行 / 其他（也接受 0/1/2）。"""
    if value in (None, ""):
        return 0
    text = str(value).strip()
    if text.isdigit() and int(text) in (0, 1, 2):
        return int(text)
    mapping = {
        "现金": 0, "cash": 0,
        "银行": 1, "转账": 1, "银行转账": 1, "bank": 1, "汇款": 1,
        "其他": 2, "other": 2,
        # 常见电子支付统一记为「其他」，具体渠道放流水号（reference）里
        "微信": 2, "微信支付": 2, "支付宝": 2, "扫码": 2, "pos": 2, "刷卡": 2, "线上": 2,
    }
    key = text.lower()
    if key in mapping:
        return mapping[key]
    raise ServiceError("invalid", "收款方式只能是 现金 / 银行 / 其他")


def resolve_unit(actor: Policy, ref: Any, building: Building | None = None) -> Any:
    """按编号或名称取单元（只能取到自己数据范围内的）。"""
    from models import Unit

    ref = _identifier(ref, "单元")
    stmt = actor.query(Unit)
    if isinstance(ref, int):
        stmt = stmt.where(Unit.id == ref)
    else:
        stmt = stmt.where(Unit.name.like(f"%{ref}%"))
    if building is not None:
        stmt = stmt.where(Unit.building_id == building.id)
    rows = list(actor.db.execute(stmt).scalars().unique().all())
    return _pick(rows, f"单元「{ref}」")


def _ensure_unit(db: Session, building: Building | None, unit_name: Any) -> Any:
    """按名称取/建 unit 表记录（House.unit_id 的同步来源）；名称为空返回 None。"""
    from models import Unit

    name = "" if unit_name is None else str(unit_name).strip()
    if not name or building is None:
        return None
    row = db.execute(
        select(Unit).where(
            Unit.building_id == building.id, Unit.name == name, Unit.deleted.is_(False)
        )
    ).scalars().first()
    if row is None:
        row = Unit(building_id=building.id, name=name)
        db.add(row)
        db.flush()
    return row


def _unit_house_count(db: Session, unit: Any) -> int:
    """该单元下还在用的房屋数量（House 用 building_id + unit 字符串关联）。"""
    return int(
        db.execute(
            select(func.count())
            .select_from(House)
            .where(
                House.deleted.is_(False),
                or_(
                    House.unit_id == unit.id,
                    (House.building_id == unit.building_id) & (House.unit == unit.name),
                ),
            )
        ).scalar()
        or 0
    )


def update_unit(
    actor: Policy,
    unit_id: Any = None,
    name: Any = None,
    expected_version: Any = None,
    unit: Any = None,
) -> dict:
    """改名单元（同一楼栋内唯一）；``unit`` 可作为 ``unit_id`` 的别名。"""
    actor.require(COMMUNITY_WRITE)
    db = actor.db
    row = resolve_unit(actor, unit_id if unit_id not in (None, "") else unit)
    building = db.get(Building, row.building_id)
    actor.require_scope(building.community_id, building.id, write=True)
    _check_version(row, expected_version, f"{building.name}{row.name} 这个单元")

    if name is not None:
        new_name = _text(name, "单元名称", maxlen=32)
        if new_name != row.name:
            dup = db.execute(
                select(Unit.id).where(
                    Unit.building_id == row.building_id,
                    Unit.name == new_name,
                    Unit.deleted.is_(False),
                    Unit.id != row.id,
                )
            ).scalars().first()
            if dup:
                raise ServiceError("conflict", f"{building.name}{new_name} 已经存在")
            row.name = new_name
    row.touch()
    write_audit(db, actor, "unit.update", "unit", row.id, {"name": row.name, "building_id": row.building_id})
    _commit(db)
    return {
        "id": row.id,
        "name": row.name,
        "building_id": row.building_id,
        "building_name": building.name,
        "version": row.version,
        "message": f"{building.name}{row.name} 已更新",
    }


def delete_unit(actor: Policy, unit_id: Any = None, expected_version: Any = None, unit: Any = None) -> dict:
    """删除单元（软删）；单元下还有房屋时拒绝。"""
    actor.require(COMMUNITY_WRITE)
    db = actor.db
    row = resolve_unit(actor, unit_id if unit_id not in (None, "") else unit)
    building = db.get(Building, row.building_id)
    actor.require_scope(building.community_id, building.id, write=True)
    _check_version(row, expected_version, f"{building.name}{row.name} 这个单元")
    houses = _unit_house_count(db, row)
    if houses:
        raise ServiceError("conflict", f"{building.name}{row.name} 下还有 {houses} 套房屋，请先删除或改房号的单元")
    label = f"{building.name}{row.name}"
    version = row.version
    row.deleted = True
    row.touch()
    write_audit(db, actor, "unit.delete", "unit", row.id, {"name": row.name, "building_id": row.building_id})
    _commit(db)
    return {"ok": True, "id": row.id, "name": row.name, "version": version, "message": f"{label} 已删除"}


def create_unit(actor: Policy, building_id: Any = None, name: Any = None, building: Any = None) -> dict:
    """新增单元（同一楼栋内名称唯一）。"""
    from models import Unit

    actor.require("community.write")
    unit_name = _text(name, "单元名称", maxlen=32)
    db = actor.db
    ref = building_id if building_id not in (None, "") else building
    target_building = resolve_building(actor, ref)
    actor.require_scope(target_building.community_id, target_building.id, write=True)
    dup = db.execute(
        select(Unit.id).where(
            Unit.building_id == target_building.id, Unit.name == unit_name, Unit.deleted.is_(False)
        )
    ).scalars().first()
    if dup:
        raise ServiceError("conflict", f"{target_building.name}{unit_name} 已经存在")
    row = Unit(building_id=target_building.id, name=unit_name)
    db.add(row)
    db.flush()
    write_audit(db, actor, "unit.create", "unit", row.id,
                {"name": unit_name, "building_id": target_building.id})
    _commit(db)
    return {
        "id": row.id, "name": row.name, "building_id": row.building_id,
        "building_name": target_building.name, "version": row.version,
        "message": f"{target_building.name}{unit_name} 已新增",
    }


def check_in_lease(
    actor: Policy,
    house_id: Any = None,
    person_id: Any = None,
    rent: Any = None,
    start_at: Any = None,
    end_at: Any = None,
    house: Any = None,
    person: Any = None,
) -> dict:
    """办理入住：写租赁记录 + 建立租户关系 + 房屋状态改为「出租」。"""
    from decimal import Decimal

    from models import HousePerson, Lease

    actor.require("lease.write")
    db = actor.db
    target_house = resolve_house(actor, house_id if house_id not in (None, "") else house)
    tenant = resolve_person(actor, person_id if person_id not in (None, "") else person)
    actor.require_scope(target_house.community_id, target_house.building_id, write=True)
    rent_value = _money(rent, "月租金", required=True, minimum=0)
    start = _date_value(start_at, "起租日期", required=True)
    end = _date_value(end_at, "到期日期", required=False)
    if end is not None and end < start:
        raise ServiceError("invalid", "到期日期不能早于起租日期")

    opened = db.execute(
        select(Lease).where(Lease.house_id == target_house.id, Lease.status == 0, Lease.deleted.is_(False))
    ).scalars().first()
    if opened is not None:
        raise ServiceError("conflict", f"{target_house.full_name} 已有在租记录，请先办理退租")

    # house_person.active_key 上有唯一约束（同一房屋同一人员只允许一条有效关系），
    # 先给一句人话错误，否则重复办入住会撞约束直接 500。
    duplicated = db.execute(
        select(HousePerson).where(
            HousePerson.house_id == target_house.id, HousePerson.person_id == tenant.id,
            HousePerson.status == "active", HousePerson.deleted.is_(False),
        )
    ).scalars().first()
    if duplicated is not None:
        raise ServiceError("conflict", f"{tenant.name} 已经是 {target_house.full_name} 的在住人员了")

    row = Lease(house_id=target_house.id, person_id=tenant.id, rent=Decimal(rent_value),
                start_at=start, end_at=end, status=0)
    db.add(row)
    db.flush()
    db.add(HousePerson(house_id=target_house.id, person_id=tenant.id, relation="tenant",
                       status="active", start_at=start, note="入住登记",
                       active_key=f"{target_house.id}:{tenant.id}"))
    target_house.status = 2
    write_audit(db, actor, "lease.check_in", "lease", row.id,
                {"house_id": target_house.id, "person_id": tenant.id, "rent": str(rent_value)})
    _commit(db)
    return {
        "id": row.id, "house_id": target_house.id, "house_text": target_house.full_name,
        "person_id": tenant.id, "person_name": tenant.name, "rent": float(rent_value),
        "status": 0, "status_text": "在租", "version": row.version,
        "message": f"{target_house.full_name} 已办理入住，租户 {tenant.name}，月租金 {rent_value}",
    }


def check_out_lease(actor: Policy, lease_id: Any = None, end_at: Any = None, reason: Any = None) -> dict:
    """办理退租：结束租赁 + 解除租户关系 + 房屋状态回到「空置」。"""
    from models import HousePerson, Lease

    actor.require("lease.write")
    db = actor.db
    row = db.get(Lease, _identifier(lease_id, "租赁记录"))
    if row is None or row.deleted:
        raise ServiceError("not_found", "租赁记录不存在")
    target_house = actor.get(House, row.house_id)
    actor.require_scope(target_house.community_id, target_house.building_id, write=True)
    if int(row.status) != 0:
        raise ServiceError("state", "这条租赁记录已经退租，不能重复办理")

    moment = _date_value(end_at, "退租日期", required=False) or utcnow()
    row.status = 1
    row.end_at = moment
    if reason:
        row.note = str(reason)[:255]
    relation = db.execute(
        select(HousePerson).where(
            HousePerson.house_id == row.house_id, HousePerson.person_id == row.person_id,
            HousePerson.relation == "tenant", HousePerson.status == "active",
            HousePerson.deleted.is_(False),
        )
    ).scalars().first()
    if relation is not None:
        relation.status = "ended"
        relation.end_at = moment
        relation.active_key = None
    remaining = db.execute(
        select(HousePerson.relation).where(
            HousePerson.house_id == row.house_id, HousePerson.status == "active",
            HousePerson.deleted.is_(False),
        )
    ).scalars().all()
    # 退租后按剩余关系重算房屋状态：有业主→自住，有租户→出租，都没有→空置
    if "tenant" in remaining:
        target_house.status = 2
    elif "owner" in remaining:
        target_house.status = 1
    else:
        target_house.status = 0
    write_audit(db, actor, "lease.check_out", "lease", row.id,
                {"house_id": row.house_id, "person_id": row.person_id, "reason": str(reason or "")})
    _commit(db)
    from models import Person

    tenant = db.get(Person, row.person_id)
    return {
        "id": row.id,
        "house_text": target_house.full_name,
        "person_name": tenant.name if tenant is not None else f"人员#{row.person_id}",
        "status": 1, "status_text": "已退租", "version": row.version,
        "message": f"{target_house.full_name} 已办理退租",
    }


def reopen_work_order(actor: Policy, order_id: Any = None, reason: Any = None) -> dict:
    """返修：待验收 → 维修中。"""
    actor.require(ORDER_VERIFY)
    db = actor.db
    order = resolve_order(actor, order_id)
    current, target = _transition(order, "reopen")
    order.status = target
    order.finished_at = None
    _log_order(db, order, "reopen", current, target, actor.user_id, str(reason or ""))
    write_audit(db, actor, "order.reopen", "work_order", order.id, {"reason": str(reason or "")})
    _commit(db)
    return {
        "id": order.id, "no": order.no, "status": target, "status_text": order_status_text(target),
        "version": order.version,
        "message": f"工单 {order.no} 已退回返修（{order_status_text(target)}）",
    }


def _fee_type(value: Any, label: str = "费用类型") -> str:
    """费用类型：必须是能直接给人看的一个词。

    曾经建账单表单的下拉框把整个选项字典渲染进了 ``<option>``，提交后原样落库，
    账单列表和详情页就把 ``{'value': '水费', 'text': '水费'}`` 显示给了用户。
    这里挡一次：宁可让用户重选，也不让这种值进库。
    """
    fee = _text(value, label, maxlen=32)
    if any(char in fee for char in "{}[]<>") or "':" in fee or '":' in fee:
        raise ServiceError("invalid", f"{label}不正确，请重新选择（例如：物业费、水费、停车费）")
    return fee


def create_bill(
    actor: Policy,
    house_id: Any = None,
    fee_type: Any = None,
    amount: Any = None,
    period: Any = None,
    due_at: Any = None,
    house: Any = None,
) -> dict:
    """给单户建账单。"""
    from decimal import Decimal

    from models import Bill, Person

    actor.require("billing.manage")
    db = actor.db
    target_house = resolve_house(actor, house_id if house_id not in (None, "") else house)
    actor.require_scope(target_house.community_id, target_house.building_id, write=True)
    fee = _fee_type(fee_type)
    money = _money(amount, "应收金额", required=True, minimum=0)
    period_text = _text(period, "账期", required=False, maxlen=16)
    due = _date_value(due_at, "缴费截止日", required=False)

    dup = db.execute(
        select(Bill.id).where(
            Bill.house_id == target_house.id, Bill.fee_type == fee, Bill.period == period_text,
            Bill.status != 3, Bill.deleted.is_(False),
        )
    ).scalars().first()
    if dup:
        raise ServiceError("conflict", f"{target_house.full_name} 的 {period_text} {fee} 账单已经存在")

    owner_person = db.execute(
        select(Person.id).join(
            HousePerson, HousePerson.person_id == Person.id
        ).where(
            HousePerson.house_id == target_house.id, HousePerson.relation == "owner",
            HousePerson.status == "active", HousePerson.deleted.is_(False),
        )
    ).scalars().first()
    row = Bill(
        no=_next_doc_no(db, Bill, "BILL"), community_id=target_house.community_id,
        house_id=target_house.id, person_id=owner_person, fee_type=fee,
        amount=Decimal(money), paid_amount=Decimal("0.00"), status=0,
        period=period_text, due_at=due,
    )
    db.add(row)
    db.flush()
    write_audit(db, actor, "bill.create", "bill", row.id,
                {"house_id": target_house.id, "fee_type": fee, "amount": str(money), "period": period_text})
    _commit(db)
    return {
        "id": row.id, "no": row.no, "house_text": target_house.full_name, "fee_type": fee,
        "amount": float(money), "paid_amount": 0.0, "status": 0, "status_text": "待缴",
        "period": period_text, "version": row.version,
        "message": f"账单 {row.no} 已创建：{target_house.full_name} {period_text} {fee} 应收 {money} 元",
    }


def create_bills_batch(
    actor: Policy,
    community_id: Any = None,
    fee_type: Any = None,
    amount: Any = None,
    period: Any = None,
    due_at: Any = None,
) -> dict:
    """按小区批量建账（自动跳过已有同账期同类型账单）。"""
    from decimal import Decimal

    from models import Bill, House

    actor.require("billing.manage")
    db = actor.db
    community = resolve_community(actor, community_id)
    actor.require_scope(community.id, write=True)
    fee = _fee_type(fee_type)
    money = _money(amount, "每户应收金额", required=True, minimum=0)
    period_text = _text(period, "账期", required=False, maxlen=16)
    due = _date_value(due_at, "缴费截止日", required=False)

    houses = db.execute(
        select(House).where(House.community_id == community.id, House.deleted.is_(False))
    ).scalars().all()
    existing = {
        row for row in db.execute(
            select(Bill.house_id).where(
                Bill.community_id == community.id, Bill.fee_type == fee, Bill.period == period_text,
                Bill.status != 3, Bill.deleted.is_(False),
            )
        ).scalars().all()
    }
    created = 0
    for item in houses:
        if item.id in existing:
            continue
        db.add(Bill(
            no=_next_doc_no(db, Bill, "BILL"), community_id=community.id, house_id=item.id,
            fee_type=fee, amount=Decimal(money), paid_amount=Decimal("0.00"), status=0,
            period=period_text, due_at=due,
        ))
        db.flush()
        created += 1
    write_audit(db, actor, "bill.create_batch", "community", community.id,
                {"fee_type": fee, "amount": str(money), "created": created, "skipped": len(houses) - created})
    _commit(db)
    skipped = len(houses) - created
    return {
        "community_id": community.id, "community_name": community.name, "created": created,
        "skipped": skipped, "amount": float(money),
        "message": f"{community.name} 批量建账完成：新增 {created} 张，跳过 {skipped} 张（已存在同账期账单）",
    }


def void_bill(actor: Policy, bill_id: Any = None, reason: Any = None) -> dict:
    """作废账单（有收款时拒绝，要求先冲销）。"""
    from models import Bill

    actor.require("billing.manage")
    db = actor.db
    row = actor.get(Bill, _identifier(bill_id, "账单"))
    actor.require_scope(row.community_id, write=True)
    if int(row.status) == 3:
        raise ServiceError("state", "这张账单已经作废")
    if row.paid_amount and float(row.paid_amount) > 0:
        raise ServiceError("state", "这张账单已经有收款，请先冲销收款再作废")
    row.status = 3
    write_audit(db, actor, "bill.void", "bill", row.id,
                {"reason": str(reason or ""), "amount": str(row.amount)})
    _commit(db)
    return {"id": row.id, "no": row.no, "status": 3, "status_text": "已作废", "version": row.version,
            "message": f"账单 {row.no} 已作废"}


def collect_payment(
    actor: Policy,
    bill_id: Any = None,
    amount: Any = None,
    method: Any = None,
    reference: Any = None,
) -> dict:
    """登记收款（支持多次部分收款）。

    两条合法路径：
    1. **代收**：财务/管理岗（``billing.collect``）——可以收本小区任意账单，按小区写范围管；
    2. **业主自助缴费**：``billing.read`` + 账单属于本人相关房屋——手机上交自己家的物业费。
       业主没有 ``billing.collect``，所以走「对象级」校验（账单本身要在他可见范围内）。
    """
    from decimal import Decimal

    from models import Bill, Payment

    proxy_collect = actor.has("billing.collect")
    if not proxy_collect:
        actor.require("billing.read")
    db = actor.db
    bill = actor.get(Bill, _identifier(bill_id, "账单"))
    if proxy_collect:
        actor.require_scope(bill.community_id, write=True)
    else:
        # 自助缴费：账单已按行级范围取出（self 范围=本人相关房屋），再按房屋级复核一次
        actor.require_house(bill.house_id)
    if int(bill.status) == 3:
        raise ServiceError("state", "这张账单已经作废，不能收款")
    money = _money(amount, "收款金额", required=True, minimum=0.01)
    outstanding = Decimal(str(bill.amount)) - Decimal(str(bill.paid_amount or 0))
    if money > outstanding:
        raise ServiceError("invalid", f"收款金额超过欠费金额（还应收 {outstanding} 元）")
    method_code = _payment_method(method)

    row = Payment(
        no=_next_doc_no(db, Payment, "PAY"), bill_id=bill.id, amount=money, method=method_code,
        reference=_text(reference, "流水号", required=False, maxlen=64), status=0,
        operator_id=actor.user_id,
    )
    db.add(row)
    db.flush()
    bill.paid_amount = Decimal(str(bill.paid_amount or 0)) + money
    bill.status = 2 if Decimal(str(bill.paid_amount)) >= Decimal(str(bill.amount)) else 1
    write_audit(db, actor, "payment.collect", "payment", row.id,
                {"bill_id": bill.id, "amount": str(money), "method": method_code})
    _commit(db)
    return {
        "id": row.id, "no": row.no, "bill_id": bill.id, "bill_no": bill.no,
        "amount": float(money), "method": method_code, "method_text": PAYMENT_METHOD_TEXT.get(method_code, ""),
        "bill_status": int(bill.status), "bill_status_text": BILL_STATUS_TEXT.get(int(bill.status), ""),
        "paid_amount": float(bill.paid_amount), "outstanding": float(Decimal(str(bill.amount)) - Decimal(str(bill.paid_amount))),
        "version": row.version,
        "message": f"收款 {money} 元已入账（{bill.no}，{BILL_STATUS_TEXT.get(int(bill.status), '')}）",
    }


def reverse_payment(actor: Policy, payment_id: Any = None, reason: Any = None) -> dict:
    """冲销收款：账单回退到收款前的状态。"""
    from decimal import Decimal

    from models import Bill, Payment

    actor.require("billing.reverse")
    db = actor.db
    row = db.get(Payment, _identifier(payment_id, "收款记录"))
    if row is None or row.deleted:
        raise ServiceError("not_found", "收款记录不存在")
    bill = actor.get(Bill, row.bill_id)
    actor.require_scope(bill.community_id, write=True)
    if int(row.status) == 1:
        raise ServiceError("state", "这笔收款已经冲销过了")
    money = Decimal(str(row.amount))
    row.status = 1
    row.reversed_by = actor.user_id
    bill.paid_amount = Decimal(str(bill.paid_amount or 0)) - money
    if bill.paid_amount < 0:
        bill.paid_amount = Decimal("0.00")
    if int(bill.status) != 3:
        bill.status = 0 if Decimal(str(bill.paid_amount)) <= 0 else 1
    write_audit(db, actor, "payment.reverse", "payment", row.id,
                {"bill_id": bill.id, "amount": str(money), "reason": str(reason or "")})
    _commit(db)
    return {
        "id": row.id, "no": row.no, "bill_id": bill.id, "bill_no": bill.no, "amount": float(money),
        "status": 1, "status_text": "已冲销", "bill_status": int(bill.status),
        "bill_status_text": BILL_STATUS_TEXT.get(int(bill.status), ""),
        "paid_amount": float(bill.paid_amount), "version": row.version,
        "message": f"收款 {row.no} 已冲销，账单 {bill.no} 回到「{BILL_STATUS_TEXT.get(int(bill.status), '')}」",
    }
