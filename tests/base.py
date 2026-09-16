"""测试公共设施：内存 SQLite + 契约形状的固定数据集 + Flask 测试客户端。

设计要点：
- 测试**永远不连 MySQL**：导入任何业务模块前把 ``DATABASE_URL`` 指向内存 SQLite。
- 数据集由本模块自己构造（不依赖 ``seed_demo``），字段与数量固定，断言可以写得很精确。
- 每个用例重新建表 + 重新构造数据，用例之间互不影响。
- 页面渲染用「占位模板」保证与前端进度解耦，同时用 ``template_rendered`` 信号断言真实上下文变量。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for path in (str(ROOT), str(ROOT / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

# 必须在导入 config/db/app 之前设置：测试库固定为内存 SQLite
TEST_DATABASE_URL = "sqlite:///:memory:"
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unittest")
os.environ.setdefault("COOKIE_SECURE", "0")
os.environ["APP_ENV"] = "testing"

from sqlalchemy import func, select  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

import db as app_db  # noqa: E402
import models  # noqa: E402
from models import (  # noqa: E402
    AuditLog,
    Building,
    Community,
    House,
    HousePerson,
    OrderLog,
    Person,
    RbacRole,
    RolePermission,
    Unit,
    User,
    UserRole,
    UserScope,
    WorkOrder,
    utcnow,
)
from permissions import ROLES, Policy  # noqa: E402

TEST_CSRF = "test-csrf-token"
TEST_PASSWORD = "Demo-only-292!"

#: 数据集里的账号（username, 姓名, 角色, 数据范围, 范围小区）
ACCOUNT_SPECS = [
    ("admin", "系统管理员", "admin", "all", None),
    ("manager01", "王经理", "manager", "community", "c1"),
    ("service01", "陈客服", "service", "community", "c1"),
    ("engineer01", "黄磊", "engineer", "assigned", None),
    ("owner01", "张伟", "owner", "self", None),
    ("manager02", "别区经理", "manager", "community", "c2"),
    ("finance01", "赵会计", "finance", "community", "c1"),
]

#: 契约第 7 节的页面模板
TEMPLATE_NAMES = (
    "base.html",
    "login.html",
    "dashboard.html",
    "houses.html",
    "persons.html",
    "orders.html",
    "order_form.html",
    "order_detail.html",
    "ai.html",
    "audit.html",
    "error.html",
)
REAL_TEMPLATE_DIR = ROOT / "templates"

STUB_TEMPLATE = (
    "<!doctype html><html><head><title>{{ app_name }}</title></head><body>stub-template"
    "{% for message, category in get_flashed_messages(with_categories=true) %}"
    "<span class='flash-{{ category }}'>{{ message }}</span>{% endfor %}</body></html>"
)


def install_stub_templates(app) -> Path:
    """把模板指向临时目录：真实模板**全部**复制过来，缺失的必需页面用占位模板补上。

    复制整个目录（而不是白名单里的 11 个）是因为模板之间会共用 `_macros.html`
    这类片段；只挑主模板会导致 {% from '_macros.html' import ... %} 找不到文件。
    """
    tmp = Path(tempfile.mkdtemp(prefix="wuye-test-templates-"))
    copied: set[str] = set()
    if REAL_TEMPLATE_DIR.is_dir():
        for source in sorted(REAL_TEMPLATE_DIR.glob("*.html")):
            shutil.copyfile(source, tmp / source.name)
            copied.add(source.name)
    for name in TEMPLATE_NAMES:
        if name not in copied:
            (tmp / name).write_text(STUB_TEMPLATE, encoding="utf-8")
    from jinja2 import FileSystemLoader

    app.jinja_loader = FileSystemLoader(str(tmp))
    return tmp


def make_app(overrides: dict | None = None):
    """构造绑定测试库的 Flask 应用（``USE_AGENT_ROUTES=False`` 保证 /ai 走内置路由）。"""
    import app as app_module

    config = {
        "TESTING": True,
        "DATABASE_URL": TEST_DATABASE_URL,
        "SECRET_KEY": "test-secret-key-for-unittest",
        "SESSION_COOKIE_SECURE": False,
    }
    config.update(overrides or {})
    application = app_module.create_app(config)
    install_stub_templates(application)
    return application


def capture_templates(app):
    """捕获渲染上下文：返回 (记录列表, 解除函数)。"""
    from flask import template_rendered

    records: list[tuple[str, dict]] = []

    def record(sender, template, context, **extra):
        records.append((template.name, dict(context)))

    template_rendered.connect(record, app)
    return records, lambda: template_rendered.disconnect(record, app)


def build_fixture(session) -> dict:
    """构造固定数据集，返回常用对象的字典（h1..h6 / c1 / c2 / users / persons / orders）。"""
    data: dict = {}

    # 角色与权限矩阵（来自 permissions.ROLES，即契约第 3 节）
    for code, meta in ROLES.items():
        session.add(RbacRole(code=code, name=meta["name"], builtin=True))
        for permission in sorted(meta["permissions"]):
            session.add(RolePermission(role_code=code, permission=permission))
    session.flush()

    # 小区 / 楼栋 / 房屋
    c1 = Community(name="云邻花园", address="云邻路 88 号")
    c2 = Community(name="隔壁小区", address="隔壁路 1 号")
    session.add_all([c1, c2])
    session.flush()
    b1 = Building(community_id=c1.id, name="1栋")
    b2 = Building(community_id=c1.id, name="2栋")
    b3 = Building(community_id=c2.id, name="3栋")
    session.add_all([b1, b2, b3])
    session.flush()

    house_specs = [
        ("h1", b1, "1", "101", 89.5, 1),
        ("h2", b1, "1", "102", 89.5, 0),
        ("h3", b1, "1", "201", 92.0, 1),
        ("h4", b1, "2", "101", 78.0, 0),
        ("h5", b2, "1", "101", 110.0, 1),
        ("h6", b3, "1", "101", 90.0, 0),
    ]
    houses: dict[str, House] = {}
    units: dict[tuple[int, str], models.Unit] = {}
    for key, building, unit, room, area, status in house_specs:
        # 契约 §2：单元是独立表（community—building—unit—house 的第三层），这里同步建出来
        unit_row = units.get((building.id, unit))
        if unit_row is None:
            unit_row = models.Unit(building_id=building.id, name=unit)
            session.add(unit_row)
            session.flush()
            units[(building.id, unit)] = unit_row
        house = House(
            community_id=building.community_id,
            building_id=building.id,
            unit_id=unit_row.id,
            unit=unit,
            room=room,
            area=area,
            status=status,
        )
        session.add(house)
        houses[key] = house
    session.flush()

    # 账号 + 角色 + 数据范围
    users: dict[str, User] = {}
    for username, real_name, role, kind, community_key in ACCOUNT_SPECS:
        user = User(
            username=username,
            real_name=real_name,
            phone="13900000000",
            password_hash=generate_password_hash(TEST_PASSWORD),
            active=True,
            auth_version=1,
        )
        session.add(user)
        session.flush()
        session.add(UserRole(user_id=user.id, role_code=role))
        session.add(
            UserScope(
                user_id=user.id,
                kind=kind,
                community_id=(c1.id if community_key == "c1" else c2.id) if community_key else None,
            )
        )
        users[username] = user
    session.flush()

    # 人员档案
    person_specs = [
        ("张伟", "13900000001", "owner01"),
        ("李秀兰", "13900000002", None),
        ("赵敏", "13900000003", None),
        ("李娜", "13800000011", None),
        ("李娜", "13800000012", None),
        ("刘建国", "13900000004", None),
        ("黄磊", "13700000001", "engineer01"),
        ("隔壁王", "13900000005", None),
    ]
    persons: dict[str, Person] = {}
    for name, phone, username in person_specs:
        person = Person(name=name, phone=phone, user_id=users[username].id if username else None)
        session.add(person)
        session.flush()
        persons[f"{name}|{phone}"] = person

    # 房屋人员关系
    relation_specs = [
        ("h1", "张伟|13900000001", "owner"),
        ("h1", "李秀兰|13900000002", "family"),
        ("h3", "赵敏|13900000003", "owner"),
        ("h3", "李娜|13800000011", "tenant"),
        ("h2", "李娜|13800000012", "family"),
        ("h4", "刘建国|13900000004", "owner"),
        ("h5", "黄磊|13700000001", "family"),
        ("h6", "隔壁王|13900000005", "owner"),
    ]
    for house_key, person_key, relation in relation_specs:
        house = houses[house_key]
        person = persons[person_key]
        session.add(
            HousePerson(
                house_id=house.id,
                person_id=person.id,
                relation=relation,
                status="active",
                start_at=utcnow(),
                active_key=f"{house.id}:{person.id}",
            )
        )
    session.flush()

    # 工单：8 张覆盖 6 个状态，5 张派给 engineer01，1 张在隔壁小区
    order_specs = [
        ("WO-TEST-0001", "h1", "water", "厨房水管漏水，地面有积水", 1, 0, False, "张伟", "13900000001", None),
        ("WO-TEST-0002", "h3", "door", "阳台推拉门卡死，关不严", 0, 1, True, "赵敏", "13900000003", None),
        ("WO-TEST-0003", "h2", "electric", "卧室灯不亮，怀疑线路接触不良", 0, 2, True, "李娜", "13800000012", None),
        ("WO-TEST-0004", "h5", "public", "单元门禁对讲机没有声音", 0, 3, True, "黄磊", "13700000001", None),
        ("WO-TEST-0005", "h3", "water", "暖气不热，室温偏低", 0, 4, True, "赵敏", "13900000003", 5),
        ("WO-TEST-0006", "h6", "other", "隔壁小区的一单报修", 0, 0, False, "隔壁王", "13900000005", None),
        ("WO-TEST-0007", "h1", "electric", "客厅插座频繁跳闸", 0, 2, True, "张伟", "13900000001", None),
        ("WO-TEST-0008", "h4", "other", "业主误报，无需维修", 0, 5, False, "刘建国", "13900000004", None),
    ]
    orders: list[WorkOrder] = []
    for (
        no,
        house_key,
        category,
        description,
        urgency,
        status,
        assigned,
        contact_name,
        contact_phone,
        rating,
    ) in order_specs:
        house = houses[house_key]
        owner_id = users["owner01"].id if house_key == "h1" else None
        if owner_id is None:
            for person in persons.values():
                if person.name == contact_name and person.user_id:
                    owner_id = person.user_id
                    break
        order = WorkOrder(
            no=no,
            community_id=house.community_id,
            building_id=house.building_id,
            house_id=house.id,
            owner_id=owner_id,
            contact_name=contact_name,
            contact_phone=contact_phone,
            category=category,
            description=description,
            urgency=urgency,
            status=status,
            repairer_id=users["engineer01"].id if assigned else None,
            rating=rating,
            finished_at=utcnow() if status in (3, 4) else None,
            closed_at=utcnow() if status == 4 else None,
        )
        session.add(order)
        session.flush()
        session.add(
            OrderLog(
                order_id=order.id,
                action="create",
                from_status=None,
                to_status=0,
                operator_id=owner_id,
                note=f"{contact_name} 报修：{description}",
            )
        )
        orders.append(order)

    # 审计：本人操作 + 同小区用户操作 + 隔壁小区经理操作
    session.add_all(
        [
            AuditLog(
                user_id=users["owner01"].id,
                action="order.create",
                target_type="work_order",
                target_id="1",
                source="web",
            ),
            AuditLog(
                user_id=users["service01"].id,
                action="order.assign",
                target_type="work_order",
                target_id="2",
                source="web",
            ),
            AuditLog(
                user_id=users["manager02"].id,
                action="house.update",
                target_type="house",
                target_id="6",
                source="agent",
            ),
        ]
    )
    session.commit()

    data.update(
        {"c1": c1, "c2": c2, "b1": b1, "b2": b2, "b3": b3, "users": users, "persons": persons, "orders": orders}
    )
    data.update(houses)
    return data


class DbTestCase(unittest.TestCase):
    """带固定数据集的内存 SQLite 用例基类。"""

    engine = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = app_db.get_engine(TEST_DATABASE_URL)

    def setUp(self) -> None:
        models.Base.metadata.drop_all(self.engine)
        models.Base.metadata.create_all(self.engine)
        self.session = app_db.get_sessionmaker(self.engine)()
        self.fx = build_fixture(self.session)

    def tearDown(self) -> None:
        self.session.close()

    # -- 常用对象 ---------------------------------------------------------
    def user(self, username: str = "admin") -> User:
        row = self.session.execute(select(User).where(User.username == username)).scalars().first()
        self.assertIsNotNone(row, f"数据集里应该有 {username} 账号")
        return row

    def actor(self, username: str = "admin", source: str = "web") -> Policy:
        return Policy(self.session, self.user(username), source=source)

    def order(self, no: str = "WO-TEST-0001") -> WorkOrder:
        row = self.session.execute(select(WorkOrder).where(WorkOrder.no == no)).scalars().first()
        self.assertIsNotNone(row, f"数据集里应该有工单 {no}")
        return row

    def house(self, key: str = "h1") -> House:
        return self.fx[key]

    def count(self, model) -> int:
        return int(self.session.execute(select(func.count()).select_from(model)).scalar() or 0)

    # -- 路由辅助 ---------------------------------------------------------
    def client(self, username: str | None = None, app=None, auth_version: int | None = None):
        """取测试客户端；``username`` 非空时直接写入登录态（不走登录表单）。"""
        application = app or make_app()
        client = application.test_client()
        if username:
            self.login(client, username, auth_version=auth_version)
        return client

    def login(self, client, username: str = "admin", auth_version: int | None = None) -> str:
        user = self.user(username)
        with client.session_transaction() as session:
            session["user_id"] = user.id
            session["auth_version"] = user.auth_version if auth_version is None else auth_version
            session["csrf_token"] = TEST_CSRF
        return TEST_CSRF

    @contextmanager
    def captured(self, app):
        records, disconnect = capture_templates(app)
        try:
            yield records
        finally:
            disconnect()

    def context_for(self, records, name: str) -> dict:
        for template_name, context in records:
            if template_name == name:
                return context
        self.fail(f"{name} 没有被渲染，实际渲染了：{[item for item, _ in records]}")


def real_templates_ready() -> bool:
    return all((REAL_TEMPLATE_DIR / name).is_file() for name in TEMPLATE_NAMES)
