"""精简数据模型（契约第 2 节，15 张表）。

- 业务表继承 :class:`Record`：``id / created_at / updated_at / version / deleted``，
  其中 ``version`` 由 SQLAlchemy 做乐观锁（``version_id_col``），并发修改会抛 ``StaleDataError``。
- 软删除统一用 ``deleted`` 标记，:class:`permissions.Policy` 的查询会自动过滤。
- 时间统一是 UTC naive 时间（秒精度）：MySQL ``DATETIME`` 不保存微秒，避免回读比较不一致。
- 工单状态机、各类枚举的中文文案也在本模块，页面与智能体共用一份。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """统一的「现在」（UTC，秒精度）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def json_value(value: Any) -> Any:
    """把列值转成可 JSON 序列化的值。"""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, Decimal):
        return float(value)
    return value


#: 永不外泄的字段（密码哈希等）
SENSITIVE_FIELDS = frozenset({"password_hash"})


class Base(DeclarativeBase):
    """声明式基类，提供 to_dict。"""

    def to_dict(self, exclude: tuple[str, ...] = (), extra: dict | None = None) -> dict:
        """转成可 JSON 序列化的 dict（自动跳过密码哈希等敏感字段）。"""
        data = {
            column.name: json_value(getattr(self, column.name))
            for column in self.__table__.columns
            if column.name not in exclude and column.name not in SENSITIVE_FIELDS
        }
        if extra:
            data.update(extra)
        return data


def to_dict(obj: Any, exclude: tuple[str, ...] = (), extra: dict | None = None) -> dict:
    """任意模型实例 → dict（None 返回空 dict）。"""
    if obj is None:
        return {}
    if isinstance(obj, Base):
        return obj.to_dict(exclude=exclude, extra=extra)
    raise TypeError(f"无法转换的对象: {type(obj)!r}")


class Record(Base):
    """业务表公共列：自增主键、时间戳、乐观锁版本、软删除标记。"""

    __abstract__ = True

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)

    __mapper_args__ = {"version_id_col": version}

    def touch(self) -> None:
        """标记更新时间（写操作里显式调用，让语义更清楚）。"""
        self.updated_at = utcnow()


# --------------------------------------------------------------------------
# 账号 / 权限（4 张辅助表）
# --------------------------------------------------------------------------
class User(Record):
    """登录账号（表名 sys_user）。"""

    __tablename__ = "sys_user"

    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    real_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    phone: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    def display_name(self) -> str:
        return self.real_name or self.username


class RbacRole(Record):
    """角色（种子固定 5 个，不可在界面改）。"""

    __tablename__ = "rbac_role"

    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class RolePermission(Record):
    """角色 → 权限点。"""

    __tablename__ = "role_permission"
    __table_args__ = (UniqueConstraint("role_code", "permission", name="uq_role_permission"),)

    role_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    permission: Mapped[str] = mapped_column(String(48), nullable=False, index=True)


class UserRole(Record):
    """账号 → 角色。"""

    __tablename__ = "user_role"
    __table_args__ = (UniqueConstraint("user_id", "role_code", name="uq_user_role"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("sys_user.id"), nullable=False, index=True)
    role_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)


class UserScope(Record):
    """数据范围：all / community / building / assigned / self。"""

    __tablename__ = "user_scope"
    __table_args__ = (Index("ix_user_scope_user_kind", "user_id", "kind"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("sys_user.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="self")
    community_id: Mapped[Optional[int]] = mapped_column(ForeignKey("community.id"), nullable=True, index=True)
    building_id: Mapped[Optional[int]] = mapped_column(ForeignKey("building.id"), nullable=True, index=True)


# --------------------------------------------------------------------------
# 空间主数据
# --------------------------------------------------------------------------
class Community(Record):
    """小区。"""

    __tablename__ = "community"

    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    address: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    buildings = relationship("Building", back_populates="community", lazy="select")


class Building(Record):
    """楼栋。"""

    __tablename__ = "building"
    __table_args__ = (UniqueConstraint("community_id", "name", name="uq_building_name"),)

    community_id: Mapped[int] = mapped_column(ForeignKey("community.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    community = relationship("Community", back_populates="buildings", lazy="joined")
    houses = relationship("House", back_populates="building", lazy="select")


class House(Record):
    """房屋（房号在楼栋内唯一）。"""

    __tablename__ = "house"
    __table_args__ = (
        UniqueConstraint("building_id", "unit", "room", name="uq_house_room"),
        Index("ix_house_building_status", "building_id", "status"),
    )

    community_id: Mapped[int] = mapped_column(ForeignKey("community.id"), nullable=False, index=True)
    building_id: Mapped[int] = mapped_column(ForeignKey("building.id"), nullable=False, index=True)
    #: 契约 §2：单元用 unit 表关联（unit_id 为准），unit 字符串保留兼容与快速展示
    unit_id: Mapped[Optional[int]] = mapped_column(ForeignKey("unit.id"), nullable=True, index=True)
    unit: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    room: Mapped[str] = mapped_column(String(32), nullable=False)
    area: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2), nullable=True)
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)

    community = relationship("Community", lazy="joined")
    building = relationship("Building", back_populates="houses", lazy="joined")
    unit_ref = relationship("Unit", lazy="joined")
    relations = relationship("HousePerson", back_populates="house", lazy="select")

    @property
    def unit_name(self) -> str:
        """单元名（优先取 unit 表，退化为字符串字段）。"""
        if self.unit_ref is not None and getattr(self.unit_ref, "name", ""):
            return self.unit_ref.name
        return self.unit or ""

    @property
    def full_name(self) -> str:
        """房屋全称：小区 + 楼栋 + 单元 + 房号。"""
        community = self.community.name if self.community else ""
        building = self.building.name if self.building else ""
        unit = self.unit_name
        return f"{community}{building}{unit}单元{self.room}" if unit else f"{community}{building}{self.room}"


# --------------------------------------------------------------------------
# 人员与关系
# --------------------------------------------------------------------------
class Person(Record):
    """人员档案（同名人员靠房号/关系消歧）。"""

    __tablename__ = "person"

    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    phone: Mapped[str] = mapped_column(String(32), nullable=False, default="", index=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"), nullable=True, index=True)

    user = relationship("User", lazy="joined")
    relations = relationship("HousePerson", back_populates="person", lazy="select")


class HousePerson(Record):
    """房屋人员关系。

    ``active_key`` 是「同一房屋同一人员只允许一条有效关系」的数据库级保障：
    有效时为 ``house_id:person_id``，结束时置 NULL（唯一约束允许多个 NULL）。
    """

    __tablename__ = "house_person"
    __table_args__ = (
        Index("ix_house_person_house_status", "house_id", "status"),
        Index("ix_house_person_person_status", "person_id", "status"),
    )

    house_id: Mapped[int] = mapped_column(ForeignKey("house.id"), nullable=False, index=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id"), nullable=False, index=True)
    relation: Mapped[str] = mapped_column(String(16), nullable=False, default="family")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    end_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    note: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    active_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, unique=True, index=True)

    house = relationship("House", back_populates="relations", lazy="joined")
    person = relationship("Person", back_populates="relations", lazy="joined")


# --------------------------------------------------------------------------
# 维修工单闭环
# --------------------------------------------------------------------------
class WorkOrder(Record):
    """维修工单。"""

    __tablename__ = "work_order"
    __table_args__ = (
        Index("ix_work_order_status_created", "status", "created_at"),
        Index("ix_work_order_house_status", "house_id", "status"),
    )

    no: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    community_id: Mapped[int] = mapped_column(ForeignKey("community.id"), nullable=False, index=True)
    building_id: Mapped[int] = mapped_column(ForeignKey("building.id"), nullable=False, index=True)
    house_id: Mapped[int] = mapped_column(ForeignKey("house.id"), nullable=False, index=True)
    requester_person_id: Mapped[Optional[int]] = mapped_column(ForeignKey("person.id"), nullable=True)
    owner_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"), nullable=True, index=True)
    contact_name: Mapped[str] = mapped_column(String(64), nullable=False)
    contact_phone: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="other")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    urgency: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    repairer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"), nullable=True, index=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    rating: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    rating_note: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    house = relationship("House", lazy="joined")
    owner = relationship("User", foreign_keys=[owner_id], lazy="joined")
    repairer = relationship("User", foreign_keys=[repairer_id], lazy="joined")
    logs = relationship(
        "OrderLog",
        back_populates="order",
        lazy="select",
        order_by="OrderLog.id",
    )


class OrderLog(Record):
    """工单流转日志。"""

    __tablename__ = "order_log"

    order_id: Mapped[int] = mapped_column(ForeignKey("work_order.id"), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    from_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    to_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    operator_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"), nullable=True, index=True)
    note: Mapped[str] = mapped_column(String(500), nullable=False, default="")

    order = relationship("WorkOrder", back_populates="logs", lazy="select")
    operator = relationship("User", lazy="joined")


class AuditLog(Record):
    """审计日志（web 与 agent 同一张表，用 source 区分）。"""

    __tablename__ = "audit_log"

    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    target_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    detail: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="web", index=True)

    user = relationship("User", lazy="joined")


# --------------------------------------------------------------------------
# AI 会话（供页面刷新后回看）
# --------------------------------------------------------------------------
class AgentSession(Record):
    """AI 会话。"""

    __tablename__ = "agent_session"

    user_id: Mapped[int] = mapped_column(ForeignKey("sys_user.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    dsh_session_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    messages = relationship("AgentMessage", back_populates="session", lazy="select")


class AgentMessage(Record):
    """AI 消息。"""

    __tablename__ = "agent_message"

    session_id: Mapped[int] = mapped_column(ForeignKey("agent_session.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")

    session = relationship("AgentSession", back_populates="messages", lazy="select")


# --------------------------------------------------------------------------
# 枚举与文案（页面 / 智能体共用）
# --------------------------------------------------------------------------
ORDER_STATUS_TEXT = {0: "待派单", 1: "已派单", 2: "维修中", 3: "待验收", 4: "已关闭", 5: "已取消"}
ORDER_STATUS_CLASS = {0: "pending", 1: "dispatched", 2: "working", 3: "verifying", 4: "closed", 5: "cancelled"}
ORDER_TERMINAL_STATUS = (4, 5)
ORDER_URGENCY_TEXT = {0: "普通", 1: "紧急"}
HOUSE_STATUS_TEXT = {0: "空置", 1: "自住", 2: "出租"}
RELATION_TEXT = {"owner": "业主", "tenant": "租户", "family": "家庭成员"}
RELATION_STATUS_TEXT = {"active": "有效", "ended": "已结束"}
ORDER_CATEGORY_TEXT = {
    "water": "水暖",
    "electric": "电路",
    "door": "门窗",
    "elevator": "电梯",
    "public": "公共设施",
    "other": "其他",
}
SOURCE_TEXT = {"web": "网页操作", "agent": "AI 助手"}
LOG_ACTION_TEXT = {
    "create": "报修登记",
    "assign": "派单",
    "accept": "接单",
    "progress": "维修进度",
    "finish": "完工",
    "verify": "验收通过",
    "reopen": "重新返修",
    "cancel": "取消工单",
    "rate": "业主评价",
}

#: 工单状态机（契约第 2 节）：status → {动作: 目标状态}
ORDER_TRANSITIONS: dict[int, dict[str, int]] = {
    0: {"assign": 1, "cancel": 5},
    1: {"accept": 2, "cancel": 5},
    2: {"finish": 3, "cancel": 5},
    3: {"verify": 4, "reopen": 2, "cancel": 5},
    4: {"rate": 4},
    5: {},
}


def order_status_text(status: Any) -> str:
    return ORDER_STATUS_TEXT.get(int(status), f"未知状态({status})")


def order_status_class(status: Any) -> str:
    return ORDER_STATUS_CLASS.get(int(status), "pending")


def order_urgency_text(urgency: Any) -> str:
    return ORDER_URGENCY_TEXT.get(int(urgency), "普通")


def house_status_text(status: Any) -> str:
    return HOUSE_STATUS_TEXT.get(int(status), "未知")


def relation_text(relation: Any) -> str:
    return RELATION_TEXT.get(str(relation), str(relation or ""))


def relation_status_text(status: Any) -> str:
    return RELATION_STATUS_TEXT.get(str(status), str(status or ""))


def category_text(category: Any) -> str:
    return ORDER_CATEGORY_TEXT.get(str(category), str(category or "其他"))


def source_text(source: Any) -> str:
    return SOURCE_TEXT.get(str(source), str(source or ""))


def log_action_text(action: Any) -> str:
    return LOG_ACTION_TEXT.get(str(action), str(action or ""))


# === v2 增量表（unit / 租赁 / 投诉 / 访客 / 车辆车位 / 设备巡检 / 收费 / AI 待确认） ===
# 说明：这一段由 artifacts/agent-spec/_add_v2_models.py 幂等追加，重复执行不会重复建表。


class Unit(Record):
    """楼栋下的单元档案（社区—楼栋—单元—房屋 的第三层）。"""

    __tablename__ = "unit"
    __table_args__ = (UniqueConstraint("building_id", "name", name="uq_unit_building_name"),)

    building_id: Mapped[int] = mapped_column(ForeignKey("building.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(32), nullable=False)


class Lease(Record):
    """租赁记录：入住登记与退租历史。"""

    __tablename__ = "lease"

    house_id: Mapped[int] = mapped_column(ForeignKey("house.id"), nullable=False, index=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id"), nullable=False, index=True)
    rent: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
    start_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    end_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)  # 0 在租 / 1 已退租
    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class Complaint(Record):
    """投诉：登记 → 分配 → 处理 → 回访结案。"""

    __tablename__ = "complaint"

    no: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    community_id: Mapped[int] = mapped_column(ForeignKey("community.id"), nullable=False, index=True)
    building_id: Mapped[Optional[int]] = mapped_column(ForeignKey("building.id"), nullable=True)
    house_id: Mapped[Optional[int]] = mapped_column(ForeignKey("house.id"), nullable=True, index=True)
    reporter_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)  # 0 待处理 / 1 处理中 / 2 已结案 / 3 已取消
    handler_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"), nullable=True, index=True)
    result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class Visitor(Record):
    """访客登记：待进 → 已进 → 已离 / 已取消。"""

    __tablename__ = "visitor"

    community_id: Mapped[int] = mapped_column(ForeignKey("community.id"), nullable=False, index=True)
    building_id: Mapped[Optional[int]] = mapped_column(ForeignKey("building.id"), nullable=True)
    house_id: Mapped[Optional[int]] = mapped_column(ForeignKey("house.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    visit_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    purpose: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)  # 0 待进 / 1 已进 / 2 已离 / 3 已取消
    operator_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"), nullable=True)


class Vehicle(Record):
    """车辆登记。"""

    __tablename__ = "vehicle"

    community_id: Mapped[int] = mapped_column(ForeignKey("community.id"), nullable=False, index=True)
    house_id: Mapped[Optional[int]] = mapped_column(ForeignKey("house.id"), nullable=True, index=True)
    plate: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    brand: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    owner_person_id: Mapped[Optional[int]] = mapped_column(ForeignKey("person.id"), nullable=True)
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)  # 0 正常 / 1 已归档


class ParkingSpace(Record):
    """车位档案与占用。"""

    __tablename__ = "parking_space"
    __table_args__ = (UniqueConstraint("community_id", "code", name="uq_parking_community_code"),)

    community_id: Mapped[int] = mapped_column(ForeignKey("community.id"), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)  # 0 空闲 / 1 占用
    house_id: Mapped[Optional[int]] = mapped_column(ForeignKey("house.id"), nullable=True)
    vehicle_id: Mapped[Optional[int]] = mapped_column(ForeignKey("vehicle.id"), nullable=True)


class Device(Record):
    """设备台账。"""

    __tablename__ = "device"

    community_id: Mapped[int] = mapped_column(ForeignKey("community.id"), nullable=False, index=True)
    building_id: Mapped[Optional[int]] = mapped_column(ForeignKey("building.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)  # 0 正常 / 1 维修中 / 2 已归档
    location: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class Inspection(Record):
    """设备巡检任务；发现故障可转成报修工单。"""

    __tablename__ = "inspection"

    device_id: Mapped[int] = mapped_column(ForeignKey("device.id"), nullable=False, index=True)
    community_id: Mapped[int] = mapped_column(ForeignKey("community.id"), nullable=False, index=True)
    assignee_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"), nullable=True, index=True)
    plan_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)  # 0 待巡检 / 1 已完成 / 2 已转报修
    result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("work_order.id"), nullable=True)


class Bill(Record):
    """账单：应收、已收、账期、状态。"""

    __tablename__ = "bill"

    no: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    community_id: Mapped[int] = mapped_column(ForeignKey("community.id"), nullable=False, index=True)
    house_id: Mapped[int] = mapped_column(ForeignKey("house.id"), nullable=False, index=True)
    person_id: Mapped[Optional[int]] = mapped_column(ForeignKey("person.id"), nullable=True)
    fee_type: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
    paid_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)  # 0 待缴 / 1 部分 / 2 已缴 / 3 已作废
    period: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    due_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class Payment(Record):
    """收款与冲销记录。"""

    __tablename__ = "payment"

    no: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    bill_id: Mapped[int] = mapped_column(ForeignKey("bill.id"), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0"))
    method: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 0 现金 / 1 银行 / 2 其他
    reference: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)  # 0 已入账 / 1 已冲销
    operator_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"), nullable=True)
    reversed_by: Mapped[Optional[int]] = mapped_column(ForeignKey("sys_user.id"), nullable=True)


class AiAction(Record):
    """智能体的待确认动作（高风险操作的人工闸门）。

    浏览器只提交 ``id``，参数以服务端保存的 ``payload`` 为准；
    ``payload_hash`` 用于确认时核对参数没有被替换。
    """

    __tablename__ = "ai_action"

    user_id: Mapped[int] = mapped_column(ForeignKey("sys_user.id"), nullable=False, index=True)
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    session_id: Mapped[Optional[int]] = mapped_column(ForeignKey("agent_session.id"), nullable=True, index=True)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    preview: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    risk_level: Mapped[str] = mapped_column(String(2), nullable=False, default="R1")
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)  # 0 待确认 / 1 已执行 / 2 已取消 / 3 已过期 / 4 执行失败
    result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


# ---- 展示属性：让页面与"确认卡片"能把内部编号翻译成人话 ----

def _house_text(self) -> str:
    """房屋全称：小区 + 楼栋 + 单元 + 房号。"""
    community = getattr(self, "community", None)
    building = getattr(self, "building", None)
    parts = []
    if community is not None:
        parts.append(getattr(community, "name", "") or "")
    if building is not None:
        parts.append(getattr(building, "name", "") or "")
    unit = getattr(self, "unit", None) or getattr(self, "unit_name", None)
    if unit:
        parts.append(str(unit))
    parts.append(str(getattr(self, "room", "") or ""))
    return " ".join(part for part in parts if part)


def _house_person_display(self) -> str:
    """关系：人员 + 房屋 + 关系名。"""
    relation_text = {"owner": "业主", "tenant": "租户", "family": "家庭成员"}.get(
        getattr(self, "relation", ""), getattr(self, "relation", "")
    )
    person = getattr(self, "person", None)
    house = getattr(self, "house", None)
    who = getattr(person, "name", "") if person is not None else f"人员#{getattr(self, 'person_id', '')}"
    where = house.house_text if house is not None and hasattr(house, "house_text") else f"房屋#{getattr(self, 'house_id', '')}"
    return f"{who} · {where} · {relation_text}"


def _lease_display(self) -> str:
    house = getattr(self, "house", None)
    person = getattr(self, "person", None)
    where = house.house_text if house is not None and hasattr(house, "house_text") else f"房屋#{getattr(self, 'house_id', '')}"
    who = getattr(person, "name", "") if person is not None else f"人员#{getattr(self, 'person_id', '')}"
    return f"{who} 租 {where}（租金 {getattr(self, 'rent', 0)}）"


def _inspection_display(self) -> str:
    device = getattr(self, "device", None)
    name = getattr(device, "name", "") if device is not None else f"设备#{getattr(self, 'device_id', '')}"
    return f"巡检 {name}"


def _visitor_display(self) -> str:
    return f"访客 {getattr(self, 'name', '')}（{getattr(self, 'phone', '')}）"


if not hasattr(House, "house_text"):
    House.house_text = property(_house_text)
if not hasattr(HousePerson, "display"):
    HousePerson.display = property(_house_person_display)
if not hasattr(Lease, "display"):
    Lease.display = property(_lease_display)
if not hasattr(Inspection, "display"):
    Inspection.display = property(_inspection_display)
if not hasattr(Visitor, "display"):
    Visitor.display = property(_visitor_display)
