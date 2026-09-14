"""Policy：RBAC + 行级数据范围，全系统唯一授权来源（契约第 3 节）。

要点：
- 权限与数据范围**每次实时从数据库算**，绝不接受提示词/请求参数里的身份。
- 数据范围真正下推到 SQL：:meth:`Policy.condition` 返回 SQLAlchemy 表达式，
  :meth:`Policy.query` / :meth:`Policy.get` 自动带上它，取出来再过滤的做法在本项目里不允许。
- 五种范围：``all``（全部）/ ``community``（指定小区）/ ``building``（指定楼栋）/
  ``assigned``（指派给我的工单）/ ``self``（本人相关）。
- 拒绝用 ``abort(403)``、找不到用 ``abort(404)``（页面需要，智能体侧再转通俗中文）。
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import Select, and_, exists, false, func, or_, select, true
from sqlalchemy.orm import Session
from werkzeug.exceptions import HTTPException, abort

import models
from models import (
    Device,
    Inspection,
    AiAction,
    AgentMessage,
    AgentSession,
    AuditLog,
    Building,
    Bill,
    Complaint,
    Community,
    House,
    Lease,
    Payment,
    ParkingSpace,
    Unit,
    HousePerson,
    OrderLog,
    Person,
    RbacRole,
    RolePermission,
    User,
    UserRole,
    UserScope,
    Vehicle,
    Visitor,
    WorkOrder,
)

# --------------------------------------------------------------------------
# 权限点与角色矩阵（契约第 3 节，种子写入数据库，界面上不可改）
# --------------------------------------------------------------------------
COMMUNITY_READ = "community.read"
COMMUNITY_WRITE = "community.write"
HOUSE_READ = "house.read"
HOUSE_WRITE = "house.write"
PERSON_READ = "person.read"
PERSON_WRITE = "person.write"
RELATION_WRITE = "relation.write"
ORDER_READ = "order.read"
ORDER_CREATE = "order.create"
ORDER_DISPATCH = "order.dispatch"
ORDER_WORK = "order.work"
ORDER_VERIFY = "order.verify"
ORDER_CANCEL = "order.cancel"
STAFF_READ = "staff.read"
AUDIT_READ = "audit.read"
RESIDENT_SELF = "resident.self"

ALL_PERMISSIONS: frozenset[str] = frozenset(
    {
        COMMUNITY_READ,
        COMMUNITY_WRITE,
        HOUSE_READ,
        HOUSE_WRITE,
        PERSON_READ,
        PERSON_WRITE,
        RELATION_WRITE,
        ORDER_READ,
        ORDER_CREATE,
        ORDER_DISPATCH,
        ORDER_WORK,
        ORDER_VERIFY,
        ORDER_CANCEL,
        STAFF_READ,
        AUDIT_READ,
        RESIDENT_SELF,
    }
)

#: 权限的中文名（用于通俗提示，避免暴露内部术语）
PERMISSION_LABELS = {
    COMMUNITY_READ: "查看小区",
    COMMUNITY_WRITE: "维护小区与楼栋",
    HOUSE_READ: "查看房屋",
    HOUSE_WRITE: "维护房屋",
    PERSON_READ: "查看人员",
    PERSON_WRITE: "维护人员档案",
    RELATION_WRITE: "维护房屋人员关系",
    ORDER_READ: "查看维修工单",
    ORDER_CREATE: "报修",
    ORDER_DISPATCH: "派单",
    ORDER_WORK: "维修处理",
    ORDER_VERIFY: "验收工单",
    ORDER_CANCEL: "取消工单",
    STAFF_READ: "查看员工",
    AUDIT_READ: "查看操作记录",
    RESIDENT_SELF: "业主自助",
}

SERVICE_PERMISSIONS = frozenset(
    {
        COMMUNITY_READ,
        HOUSE_READ,
        PERSON_READ,
        PERSON_WRITE,
        RELATION_WRITE,
        ORDER_READ,
        ORDER_CREATE,
        ORDER_DISPATCH,
        ORDER_VERIFY,
        ORDER_CANCEL,
        STAFF_READ,
    }
)

OWNER_PERMISSIONS = frozenset(
    {RESIDENT_SELF, HOUSE_READ, PERSON_READ, ORDER_READ, ORDER_CREATE, ORDER_VERIFY, ORDER_CANCEL}
)

#: 角色 → {名称, 权限, 默认数据范围}
ROLES: dict[str, dict[str, Any]] = {
    "admin": {"name": "系统管理员", "permissions": ALL_PERMISSIONS, "scope": "all"},
    "manager": {
        "name": "物业经理",
        "permissions": frozenset(ALL_PERMISSIONS - {RESIDENT_SELF}),
        "scope": "community",
    },
    "service": {"name": "客服", "permissions": SERVICE_PERMISSIONS, "scope": "community"},
    "engineer": {"name": "工程维修", "permissions": frozenset({ORDER_READ, ORDER_WORK}), "scope": "assigned"},
    "owner": {"name": "业主", "permissions": OWNER_PERMISSIONS, "scope": "self"},
}

ROLE_NAMES = {code: meta["name"] for code, meta in ROLES.items()}

SCOPE_KINDS = ("all", "community", "building", "assigned", "self")
SCOPE_TEXT = {
    "all": "全部数据",
    "community": "本小区",
    "building": "本楼栋",
    "assigned": "我负责的工单",
    "self": "仅本人相关",
    "none": "无数据范围",
}

#: 物业内部岗位（其账号在员工目录里对内部角色可见）
INTERNAL_STAFF_ROLES = ("manager", "service", "engineer")


def permission_label(permission: str) -> str:
    return PERMISSION_LABELS.get(permission, permission)


def deny(message: str) -> None:
    """403 拒绝（页面会渲染 error.html）。"""
    abort(403, description=message)


def not_found(message: str = "记录不存在或无权查看") -> None:
    """404（含「无权查看」语义，避免泄露存在性）。"""
    abort(404, description=message)


class Policy:
    """当前登录用户的授权上下文：web 路由与智能体工具都通过它读写数据。"""

    def __init__(self, db: Session, user: User | int | None, source: str = "web") -> None:
        self.db = db
        self.source = source if source in ("web", "agent") else "web"
        self._user: Optional[User] = None
        if isinstance(user, User):
            self._user = user if not user.deleted else None
        elif user is not None:
            self._user = db.get(User, int(user))
            if self._user is not None and (self._user.deleted or not self._user.active):
                self._user = None
        self._roles: Optional[list[str]] = None
        self._permissions: Optional[frozenset[str]] = None
        self._scopes: Optional[list[UserScope]] = None

    # -- 基础信息 ---------------------------------------------------------
    @property
    def user(self) -> Optional[User]:
        return self._user

    @property
    def user_id(self) -> Optional[int]:
        return self._user.id if self._user else None

    @property
    def username(self) -> str:
        return self._user.username if self._user else ""

    @property
    def roles(self) -> list[str]:
        if self._roles is None:
            if not self._user:
                self._roles = []
            else:
                rows = self.db.execute(
                    select(UserRole.role_code).where(
                        UserRole.user_id == self._user.id, UserRole.deleted.is_(False)
                    )
                ).scalars()
                self._roles = sorted({code for code in rows if code})
        return self._roles

    @property
    def role_names(self) -> list[str]:
        return [ROLE_NAMES.get(code, code) for code in self.roles]

    @property
    def permissions(self) -> frozenset[str]:
        if self._permissions is None:
            if not self._user or not self._user.active:
                self._permissions = frozenset()
            elif "admin" in self.roles:
                self._permissions = ALL_PERMISSIONS
            else:
                rows = self.db.execute(
                    select(RolePermission.permission).where(
                        RolePermission.role_code.in_(self.roles or [""]), RolePermission.deleted.is_(False)
                    )
                ).scalars()
                self._permissions = frozenset(code for code in rows if code)
        return self._permissions

    @property
    def super(self) -> bool:
        """无限制数据访问（管理员或显式 all 范围）。"""
        return "admin" in self.roles or any(scope.kind == "all" for scope in self.scopes)

    @property
    def scopes(self) -> list[UserScope]:
        if self._scopes is None:
            if not self._user:
                self._scopes = []
            else:
                self._scopes = list(
                    self.db.execute(
                        select(UserScope).where(
                            UserScope.user_id == self._user.id, UserScope.deleted.is_(False)
                        )
                    ).scalars()
                )
        return self._scopes

    @property
    def scope_kinds(self) -> list[str]:
        kinds = [scope.kind for scope in self.scopes if scope.kind in SCOPE_KINDS]
        return kinds or ["none"]

    @property
    def primary_scope(self) -> str:
        """主要数据范围（用于展示）。优先级：all > community > building > assigned > self。"""
        kinds = self.scope_kinds
        for kind in SCOPE_KINDS:
            if kind in kinds:
                return kind
        return "none"

    def _scope_item(self, scope: UserScope, camel: bool = False) -> dict:
        """把一条数据范围转成可展示的 dict（含小区/楼栋名）。"""
        community_name = ""
        building_name = ""
        if scope.community_id:
            community = self.db.get(Community, int(scope.community_id))
            community_name = community.name if community else ""
        if scope.building_id:
            building = self.db.get(Building, int(scope.building_id))
            building_name = building.name if building else ""
        text = SCOPE_TEXT.get(scope.kind, scope.kind)
        if community_name:
            text = f"{text}（{community_name}）"
        elif building_name:
            text = f"{text}（{building_name}）"
        if camel:
            return {
                "kind": scope.kind,
                "kindText": text,
                "communityId": scope.community_id,
                "communityName": community_name,
                "buildingId": scope.building_id,
                "buildingName": building_name,
            }
        return {
            "kind": scope.kind,
            "kind_text": text,
            "community_id": scope.community_id,
            "building_id": scope.building_id,
        }

    def identity(self) -> dict:
        """身份卡 / 智能体 whoami 的数据。"""
        user = self._user
        return {
            # 契约字段（智能体工具用）
            "userId": self.user_id,
            "username": self.username,
            "roles": list(self.roles),
            "roleNames": self.role_names,
            "permissions": sorted(self.permissions),
            "data_scope": self.primary_scope,
            "dataScopeText": SCOPE_TEXT.get(self.primary_scope, "无数据范围"),
            # 模板友好字段（页面用）
            "id": self.user_id,
            "user_id": self.user_id,
            "real_name": user.real_name if user else "",
            "realName": user.real_name if user else "",
            # 兼容别名：模板/契约里出现过的几种写法都给上（displayName 是契约 §3 的 camelCase 口径）
            "displayName": (user.real_name or user.username) if user else "",
            "name": (user.real_name or user.username) if user else "",
            "phone": user.phone if user else "",
            "role_names": self.role_names,
            "data_scope_text": SCOPE_TEXT.get(self.primary_scope, "无数据范围"),
            "scope_text": SCOPE_TEXT.get(self.primary_scope, "无数据范围"),
            "is_admin": self.super,
            "source": self.source,
            # 契约 §3：dataScope 是列表（含范围文本与小区/楼栋名），供智能体身份卡使用
            "dataScope": [self._scope_item(scope, camel=True) for scope in self.scopes],
            "scopes": [self._scope_item(scope) for scope in self.scopes],
        }

    # -- 权限判断 ---------------------------------------------------------
    def has(self, permission: str | list[str] | tuple[str, ...]) -> bool:
        if isinstance(permission, (list, tuple, set, frozenset)):
            return all(self.has(item) for item in permission)
        return permission in self.permissions

    def require(self, permission: str | list[str] | tuple[str, ...]) -> None:
        if self._user is None:
            abort(401, description="请先登录后再操作")
        if not self.has(permission):
            codes = [permission] if isinstance(permission, str) else list(permission)
            labels = "、".join(permission_label(code) for code in codes)
            deny(f"当前账号没有「{labels}」的权限")

    # -- 数据范围（下推到 SQL） -------------------------------------------
    def condition(self, model: type) -> Any:
        """返回 model 的行级数据范围表达式（不含 deleted 过滤）。"""
        if model in (AgentSession, AgentMessage, AiAction):
            # AI 会话与确认卡片永远只看自己的
            return self._cond_agent(model)
        if self.super:
            return true()
        if not self._user:
            return false()
        parts = []
        for scope in self.scopes:
            if scope.kind == "all":
                return true()
            if scope.kind == "community" and scope.community_id:
                parts.append(self._cond_community(model, int(scope.community_id)))
            elif scope.kind == "building" and scope.building_id:
                parts.append(self._cond_building(model, int(scope.building_id)))
            elif scope.kind == "assigned":
                parts.append(self._cond_assigned(model))
            elif scope.kind == "self":
                parts.append(self._cond_self(model))
        parts = [part for part in parts if part is not None]
        if not parts:
            return false()
        return or_(*parts) if len(parts) > 1 else parts[0]

    def query(self, model: type) -> Select:
        """自动带数据范围 + 软删除过滤的查询。"""
        stmt = select(model)
        if hasattr(model, "deleted"):
            stmt = stmt.where(model.deleted.is_(False))
        condition = self.condition(model)
        if condition is not None:
            stmt = stmt.where(condition)
        return stmt

    def get(self, model: type, obj_id: Any, lock: bool = False):
        """按主键取对象：不存在或越权一律 404。

        ``lock=True`` 时加 ``SELECT ... FOR UPDATE``（MySQL 行锁；SQLite 会忽略），
        供智能体「确认卡片」在重新校验后原子执行时使用。
        """
        if obj_id in (None, ""):
            not_found()
        try:
            key = int(obj_id)
        except (TypeError, ValueError):
            not_found()
        stmt = self.query(model).where(model.id == key)
        if lock:
            stmt = stmt.with_for_update()
        obj = self.db.execute(stmt).scalars().first()
        if obj is None:
            not_found()
        return obj

    def visible(self, obj: Any) -> bool:
        """判断一个已取到的对象是否在当前数据范围内。"""
        if obj is None:
            return False
        model = type(obj)
        if model in (AgentSession, AgentMessage):
            if model is AgentSession:
                return obj.user_id == self.user_id
            return False
        if self.super:
            return True
        stmt = select(func.count()).select_from(model).where(model.id == obj.id, self.condition(model))
        return bool(self.db.execute(stmt).scalar())

    def require_visible(self, obj: Any):
        """对象越权 → 404。"""
        if not self.visible(obj):
            not_found()
        return obj

    # -- within / require_scope ------------------------------------------
    def within(self, community_id: Any, building_id: Any = None, write: bool = False) -> bool:
        """小区/楼栋是否在数据范围内。

        ``write=True`` 表示「小区/楼栋级的写操作」（维护房屋、维护人员关系等），
        只有 ``all`` 与 ``community``（本小区）算通过；``building`` / ``assigned`` / ``self``
        一律不通过——业主与维修工的写入走工单这类「对象级」命令（对象本身已过范围校验）。
        """
        if self.super:
            return True
        if not self._user or not community_id:
            return False
        cid = int(community_id)
        bid = int(building_id) if building_id else None
        for scope in self.scopes:
            kind = scope.kind
            if kind == "all":
                return True
            if kind == "community" and scope.community_id and int(scope.community_id) == cid:
                if bid is None:
                    return True
                if self._building_in_community(bid, cid):
                    return True
            elif kind == "building" and scope.building_id and not write:
                own_bid = int(scope.building_id)
                if bid == own_bid and self._building_in_community(bid, cid):
                    return True
                if bid is None and self._building_in_community(own_bid, cid):
                    return True
            elif kind == "assigned" and not write:
                if bid is not None and self._exists(
                    select(WorkOrder.id).where(
                        WorkOrder.repairer_id == self.user_id,
                        WorkOrder.building_id == bid,
                        WorkOrder.deleted.is_(False),
                    )
                ):
                    return True
                if bid is None and self._exists(
                    select(WorkOrder.id).where(
                        WorkOrder.repairer_id == self.user_id,
                        WorkOrder.community_id == cid,
                        WorkOrder.deleted.is_(False),
                    )
                ):
                    return True
            elif kind == "self" and not write:
                my_houses = self._my_house_ids_select()
                if bid is not None and self._exists(
                    select(House.id).where(House.id.in_(my_houses), House.building_id == bid)
                ):
                    return True
                if bid is None and self._exists(
                    select(House.id).where(House.id.in_(my_houses), House.community_id == cid)
                ):
                    return True
        return False

    def require_scope(self, community_id: Any, building_id: Any = None, write: bool = False) -> None:
        """数据范围外 → 403。"""
        if self._user is None:
            abort(401, description="请先登录后再操作")
        target = "该小区" if not building_id else "该楼栋"
        if not self.within(community_id, building_id, write=write):
            deny(f"当前账号没有{target}的数据权限")

    def house_in_scope(self, house: House | int) -> bool:
        """房屋级数据范围（业主只能对自己的房屋报修/评价）。"""
        house_id = house.id if isinstance(house, House) else int(house)
        return self._exists(select(House.id).where(House.id == house_id, self.condition(House)))

    def require_house(self, house: House | int) -> None:
        if not self.house_in_scope(house):
            deny("当前账号没有该房屋的数据权限")

    # -- 内部实现 ---------------------------------------------------------
    def _exists(self, stmt: Select) -> bool:
        return bool(self.db.execute(select(func.count()).select_from(stmt.subquery())).scalar())

    @staticmethod
    def _or_in(column, subqueries: list[Select]):
        """``column IN (子查询1) OR column IN (子查询2) ...``"""
        parts = [column.in_(sub) for sub in subqueries]
        return or_(*parts) if len(parts) > 1 else parts[0]

    def _my_person_ids_select(self) -> Select:
        return select(Person.id).where(Person.user_id == self.user_id, Person.deleted.is_(False))

    def _unbound_person_expr(self):
        """还没有任何有效房屋关系的人员档案（线索/待绑定人员），对物业内部角色可见。"""
        return ~exists().where(
            and_(
                HousePerson.person_id == Person.id,
                HousePerson.status == "active",
                HousePerson.deleted.is_(False),
            )
        )

    def _my_house_ids_select(self) -> Select:
        return (
            select(HousePerson.house_id)
            .where(
                HousePerson.person_id.in_(self._my_person_ids_select()),
                HousePerson.status == "active",
                HousePerson.deleted.is_(False),
            )
            .distinct()
        )

    def _my_order_ids_select(self) -> Select:
        return select(WorkOrder.id).where(
            WorkOrder.deleted.is_(False),
            or_(
                WorkOrder.repairer_id == self.user_id,
                WorkOrder.owner_id == self.user_id,
                WorkOrder.house_id.in_(self._my_house_ids_select()),
            ),
        )

    def _assigned_order_ids_select(self) -> Select:
        return select(WorkOrder.id).where(
            WorkOrder.repairer_id == self.user_id, WorkOrder.deleted.is_(False)
        )

    def _assigned_house_ids_select(self) -> Select:
        return select(WorkOrder.house_id).where(
            WorkOrder.repairer_id == self.user_id, WorkOrder.deleted.is_(False)
        )

    def _community_person_ids(self, cids: list[int]) -> Select:
        return (
            select(HousePerson.person_id)
            .join(House, House.id == HousePerson.house_id)
            .where(
                House.community_id.in_(cids),
                HousePerson.deleted.is_(False),
                House.deleted.is_(False),
            )
        )

    def _community_user_ids(self, cids: list[int], include_staff: bool = False) -> list[Select]:
        """小区范围内可见的账号：本人 + 有房屋关系的人 + 该小区工单相关的人（+ 内部员工）。"""
        subs: list[Select] = [
            select(Person.user_id).where(
                Person.id.in_(self._community_person_ids(cids)), Person.user_id.isnot(None)
            ),
            select(WorkOrder.owner_id).where(
                WorkOrder.community_id.in_(cids), WorkOrder.owner_id.isnot(None)
            ),
            select(WorkOrder.repairer_id).where(
                WorkOrder.community_id.in_(cids), WorkOrder.repairer_id.isnot(None)
            ),
        ]
        if include_staff:
            subs.append(select(UserRole.user_id).where(UserRole.role_code.in_(INTERNAL_STAFF_ROLES)))
        return subs

    def _building_in_community(self, building_id: int, community_id: int) -> bool:
        return self._exists(
            select(Building.id).where(
                Building.id == building_id,
                Building.community_id == community_id,
                Building.deleted.is_(False),
            )
        )

    def _building_person_ids(self, bids: list[int]) -> Select:
        return (
            select(HousePerson.person_id)
            .join(House, House.id == HousePerson.house_id)
            .where(
                House.building_id.in_(bids),
                HousePerson.deleted.is_(False),
                House.deleted.is_(False),
            )
        )

    def _building_user_ids(self, bids: list[int], include_staff: bool = False) -> list[Select]:
        subs: list[Select] = [
            select(Person.user_id).where(Person.id.in_(self._building_person_ids(bids)), Person.user_id.isnot(None)),
            select(WorkOrder.owner_id).where(WorkOrder.building_id.in_(bids), WorkOrder.owner_id.isnot(None)),
            select(WorkOrder.repairer_id).where(WorkOrder.building_id.in_(bids), WorkOrder.repairer_id.isnot(None)),
        ]
        if include_staff:
            subs.append(select(UserRole.user_id).where(UserRole.role_code.in_(INTERNAL_STAFF_ROLES)))
        return subs

    def _agency_tables(self) -> tuple:
        return (RbacRole, RolePermission, UserRole, UserScope)

    def _cond_agent(self, model: type):
        if not self.user_id:
            return false()
        if model is AgentSession:
            return AgentSession.user_id == self.user_id
        if model is AiAction:
            return AiAction.user_id == self.user_id
        return AgentMessage.session_id.in_(
            select(AgentSession.id).where(
                AgentSession.user_id == self.user_id, AgentSession.deleted.is_(False)
            )
        )

    def _cond_community(self, model: type, cid: int):
        cids = [cid]
        if model in self._agency_tables():
            return false()
        if model is Community:
            return Community.id.in_(cids)
        if model is Building:
            return Building.community_id.in_(cids)
        if hasattr(model, "community_id"):
            return model.community_id.in_(cids)
        if model is HousePerson:
            return HousePerson.house_id.in_(select(House.id).where(House.community_id.in_(cids)))
        if model is Person:
            return or_(Person.id.in_(self._community_person_ids(cids)), self._unbound_person_expr())
        if model is OrderLog:
            return OrderLog.order_id.in_(select(WorkOrder.id).where(WorkOrder.community_id.in_(cids)))
        if model is User:
            return or_(
                User.id == self.user_id,
                self._or_in(User.id, self._community_user_ids(cids, include_staff=True)),
            )
        if model is AuditLog:
            return or_(
                AuditLog.user_id == self.user_id,
                self._or_in(AuditLog.user_id, self._community_user_ids(cids, include_staff=True)),
            )
        if model is Unit:
            return Unit.building_id.in_(select(Building.id).where(Building.community_id.in_(cids)))
        if model is Lease:
            return Lease.house_id.in_(select(House.id).where(House.community_id.in_(cids)))
        if model is Payment:
            return Payment.bill_id.in_(select(Bill.id).where(Bill.community_id.in_(cids)))
        return false()

    def _cond_building(self, model: type, bid: int):
        bids = [bid]
        if model in self._agency_tables():
            return false()
        if model is Building:
            return Building.id.in_(bids)
        if model is Community:
            return Community.id.in_(select(Building.community_id).where(Building.id.in_(bids)))
        if hasattr(model, "building_id"):
            return model.building_id.in_(bids)
        if model is HousePerson:
            return HousePerson.house_id.in_(select(House.id).where(House.building_id.in_(bids)))
        if model is Person:
            return or_(Person.id.in_(self._building_person_ids(bids)), self._unbound_person_expr())
        if model is OrderLog:
            return OrderLog.order_id.in_(select(WorkOrder.id).where(WorkOrder.building_id.in_(bids)))
        if model is User:
            return or_(
                User.id == self.user_id,
                self._or_in(User.id, self._building_user_ids(bids, include_staff=True)),
            )
        if model is AuditLog:
            return or_(
                AuditLog.user_id == self.user_id,
                self._or_in(AuditLog.user_id, self._building_user_ids(bids, include_staff=True)),
            )
        if model is Unit:
            return Unit.building_id.in_(bids)
        if model is Lease:
            return Lease.house_id.in_(select(House.id).where(House.building_id.in_(bids)))
        if model is Payment:
            return Payment.bill_id.in_(select(Bill.id).where(Bill.house_id.in_(select(House.id).where(House.building_id.in_(bids)))))
        return false()

    def _cond_assigned(self, model: type):
        order_ids = self._assigned_order_ids_select()
        house_ids = self._assigned_house_ids_select()
        if model in self._agency_tables() or model is Community:
            return false()
        if model is WorkOrder:
            return WorkOrder.repairer_id == self.user_id
        if model is House:
            return House.id.in_(house_ids)
        if model is Building:
            return Building.id.in_(select(House.building_id).where(House.id.in_(house_ids)))
        if model is HousePerson:
            return HousePerson.house_id.in_(house_ids)
        if model is Person:
            return Person.id.in_(
                select(HousePerson.person_id).where(HousePerson.house_id.in_(house_ids))
            )
        if model is OrderLog:
            return OrderLog.order_id.in_(order_ids)
        if model is Inspection:
            # 维修工只看指派给自己的巡检任务
            return Inspection.assignee_id == self.user_id
        if model is Device:
            # 设备：只看自己巡检任务涉及的那几台（否则"我的巡检"里点不开设备）
            return Device.id.in_(
                select(Inspection.device_id).where(Inspection.assignee_id == self.user_id)
            )
        if model is User:
            return User.id == self.user_id
        if model is AuditLog:
            return AuditLog.user_id == self.user_id
        return false()

    def _cond_self(self, model: type):
        person_ids = self._my_person_ids_select()
        house_ids = self._my_house_ids_select()
        my_orders = self._my_order_ids_select()
        if model in self._agency_tables():
            return false()
        if model is Community:
            return Community.id.in_(select(House.community_id).where(House.id.in_(house_ids)))
        if model is Building:
            return Building.id.in_(select(House.building_id).where(House.id.in_(house_ids)))
        if model is House:
            return or_(
                House.id.in_(house_ids),
                House.id.in_(select(WorkOrder.house_id).where(WorkOrder.owner_id == self.user_id)),
            )
        if model is Person:
            return or_(
                Person.id.in_(person_ids),
                Person.id.in_(select(HousePerson.person_id).where(HousePerson.house_id.in_(house_ids))),
            )
        if model is HousePerson:
            return or_(HousePerson.house_id.in_(house_ids), HousePerson.person_id.in_(person_ids))
        if model is WorkOrder:
            return or_(
                WorkOrder.house_id.in_(house_ids),
                WorkOrder.owner_id == self.user_id,
                WorkOrder.requester_person_id.in_(person_ids),
            )
        if model is OrderLog:
            return OrderLog.order_id.in_(my_orders)
        if model in (User, AuditLog):
            return (User if model is User else AuditLog).user_id == self.user_id
        if model is Lease:
            return or_(
                Lease.house_id.in_(house_ids),
                Lease.person_id.in_(person_ids),
            )
        # ---- 以下 5 张表是后加的，_cond_self 一开始漏了分支，直接落到 return false()，
        #      表现为「业主看不到自己房子的账单/访客/车辆/车位/投诉」。
        #      统一口径：以房屋为主线（本人有效房屋），车辆/投诉再兼容「本人作为当事人」。
        if model is Payment:
            return Payment.bill_id.in_(select(Bill.id).where(Bill.house_id.in_(house_ids)))
        if model is Bill:
            return Bill.house_id.in_(house_ids)
        if model is Visitor:
            return Visitor.house_id.in_(house_ids)
        if model is Complaint:
            # 自己提起的投诉（可能投诉的是邻居家）+ 落到自己房子上的投诉（自己被投诉）
            return or_(
                Complaint.house_id.in_(house_ids),
                Complaint.reporter_id.in_(person_ids),
            )
        if model is Vehicle:
            return or_(
                Vehicle.house_id.in_(house_ids),
                Vehicle.owner_person_id.in_(person_ids),
            )
        if model is ParkingSpace:
            return ParkingSpace.house_id.in_(house_ids)
        return false()

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"<Policy {self.username or '匿名'} roles={self.roles} scope={self.primary_scope}>"


def anonymous(db: Session, source: str = "web") -> Policy:
    """匿名上下文（未登录）。"""
    return Policy(db, None, source=source)


def http_message(exc: Exception) -> str:
    """把 HTTPException（401/403/404）转成通俗中文，供智能体工具回话使用。"""
    if isinstance(exc, HTTPException):
        if exc.description:
            return str(exc.description)
        mapping = {401: "登录状态已失效，请重新登录", 403: "当前账号没有这项权限", 404: "没有找到这条记录"}
        return mapping.get(exc.code or 0, "操作未成功")
    return str(exc)


def visible_perm_labels(policy: Policy) -> list[str]:
    """当前账号的主要能力（用于身份卡展示）。"""
    return [permission_label(code) for code in sorted(policy.permissions)]


__all__ = [
    "Policy",
    "ROLES",
    "ROLE_NAMES",
    "ALL_PERMISSIONS",
    "SCOPE_TEXT",
    "SCOPE_KINDS",
    "PERMISSION_LABELS",
    "permission_label",
    "anonymous",
    "deny",
    "not_found",
    "http_message",
    "visible_perm_labels",
    "models",
]


def _v2_role_permissions(role_code: str) -> frozenset:
    """取某角色的权限集合（兼容 dict 与 tuple 两种写法）。"""
    entry = ROLES.get(role_code)
    if entry is None:
        return frozenset()
    if isinstance(entry, dict):
        return frozenset(entry.get("permissions") or ())
    for item in entry:
        if isinstance(item, (set, frozenset, list, tuple)) and not isinstance(item, str):
            return frozenset(item)
    return frozenset()


def _v2_role_scope(role_code: str) -> str:
    entry = ROLES.get(role_code)
    if isinstance(entry, dict):
        return str(entry.get("scope") or "community")
    return "community"


# === v2 增量：补齐权限点与 6 角色矩阵 ===
# 由 artifacts/agent-spec/_add_v2_permissions.py 幂等追加；重复执行不会重复定义。

_V2_PERMISSIONS = {
    "community.read", "community.write", "house.read", "house.write",
    "person.read", "person.write", "relation.write", "lease.write",
    "order.read", "order.create", "order.dispatch", "order.work", "order.verify", "order.cancel",
    "complaint.read", "complaint.create", "complaint.handle",
    "visitor.read", "visitor.write",
    "vehicle.read", "vehicle.write", "parking.read", "parking.write",
    "device.read", "device.write", "inspection.read", "inspection.write", "inspection.assign",
    "billing.read", "billing.manage", "billing.collect", "billing.reverse",
    "staff.read", "audit.read", "resident.self",
}
ALL_PERMISSIONS = set(ALL_PERMISSIONS) | _V2_PERMISSIONS

_V2_ROLES = {
    "admin": {
        "name": "系统管理员",
        "permissions": frozenset(ALL_PERMISSIONS),  # 契约 v2 §3：管理员 = 全部 35 个
        "scope": "all",
    },
    "manager": {
        "name": "物业经理",
        "permissions": frozenset(ALL_PERMISSIONS - {"resident.self"}),
        "scope": "community",
    },
    "service": {
        "name": "客服",
        "permissions": frozenset({
            "community.read", "community.write", "house.read", "house.write",
            "person.read", "person.write", "relation.write", "lease.write",
            "order.read", "order.create", "order.dispatch", "order.verify", "order.cancel",
            "complaint.read", "complaint.create", "complaint.handle",
            "visitor.read", "visitor.write",
            "vehicle.read", "vehicle.write", "parking.read", "parking.write",
            "staff.read",
        }),
        "scope": "community",
    },
    "engineer": {
        "name": "工程维修",
        "permissions": frozenset({
            "community.read", "order.read", "order.work",
            "device.read", "device.write", "inspection.read", "inspection.write",
        }),
        "scope": "assigned",
    },
    "finance": {
        "name": "财务",
        "permissions": frozenset({
            "community.read", "house.read", "person.read",
            "billing.read", "billing.manage", "billing.collect", "billing.reverse",
        }),
        "scope": "community",
    },
    "owner": {
        "name": "业主",
        "permissions": frozenset({
            "resident.self", "house.read", "person.read",
            "order.read", "order.create", "order.verify", "order.cancel",
            "complaint.read", "complaint.create",
            "visitor.read", "visitor.write",
            "billing.read", "vehicle.read",
        }),
        "scope": "self",
    },
}
ROLES.update(_V2_ROLES)
# ROLE_NAMES 在上面（第 139 行附近）就按当时的 ROLES 算好了，_V2_ROLES 新增的角色不在里面，
# 于是 Policy.role_names 退化成显示角色代码——财务登录后身份条上写的是「角色 finance」。
# 这里就地补齐：**不能重新赋值**，否则 `from permissions import ROLE_NAMES` 的模块还拿着旧字典。
ROLE_NAMES.update({code: meta["name"] for code, meta in _V2_ROLES.items()})
