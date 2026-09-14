"""权限矩阵与行级数据范围测试（契约 v2 §3、§6）。

覆盖：
- 6 个角色的权限点与数据范围（越权必须 403，越权读取必须 404）；
- 数据范围**真的下推到 SQL**（编译出的 SQL 里必须带范围条件）；
- 读查询也要过 Policy.require（模型硬调时服务端二次拒绝，§6）；
- 匿名、停用账号、无范围账号、AI 会话隔离等边界。

数据集见 ``tests/base.py``：2 个小区 / 6 套房屋 / 8 张工单（覆盖 6 种状态）。
"""
from __future__ import annotations

import unittest

from sqlalchemy import select
from werkzeug.exceptions import Forbidden, NotFound, Unauthorized

import models
import queries
import services
from models import House, UserScope, WorkOrder
from permissions import ALL_PERMISSIONS, Policy

try:  # `python -m unittest discover -s tests` 与 `pytest tests/` 两种入口都能跑
    from base import DbTestCase
except ImportError:  # pragma: no cover
    from tests.base import DbTestCase

#: 契约 v2 §3 的 35 个权限点（唯一口径）
CONTRACT_PERMISSIONS = frozenset(
    {
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
)
#: 客服：community/house/person/relation/lease 读写 + 工单部分 + 投诉/访客全部 + 车辆车位 + staff.read
CONTRACT_SERVICE = frozenset(
    {
        "community.read", "community.write", "house.read", "house.write",
        "person.read", "person.write", "relation.write", "lease.write",
        "order.read", "order.create", "order.dispatch", "order.verify", "order.cancel",
        "complaint.read", "complaint.create", "complaint.handle",
        "visitor.read", "visitor.write",
        "vehicle.read", "vehicle.write", "parking.read", "parking.write",
        "staff.read",
    }
)
#: 工程维修：工单 + 设备巡检 + community.read
CONTRACT_ENGINEER = frozenset(
    {
        "community.read", "order.read", "order.work",
        "device.read", "device.write", "inspection.read", "inspection.write",
    }
)
#: 财务：收费 + 房屋/人员/小区只读
CONTRACT_FINANCE = frozenset(
    {
        "community.read", "house.read", "person.read",
        "billing.read", "billing.manage", "billing.collect", "billing.reverse",
    }
)
#: 业主：自助 + 只读 + 报修/验收/取消 + 投诉/访客
CONTRACT_OWNER = frozenset(
    {
        "resident.self", "house.read", "person.read",
        "order.read", "order.create", "order.verify", "order.cancel",
        "complaint.read", "complaint.create",
        "visitor.read", "visitor.write",
        "billing.read", "vehicle.read",
    }
)

#: 固定数据集里各角色可见的工单数（8 张：小区1 七张、小区2 一张、engineer01 被派 5 张、owner01 两套单）
EXPECTED_ORDER_SCOPE = {
    "admin": 8,
    "manager01": 7,
    "service01": 7,
    "manager02": 1,
    "engineer01": 5,
    "owner01": 2,
}


class PermissionMatrixTests(DbTestCase):
    """角色 → 权限矩阵（契约 v2 §3）。"""

    def assert_matrix(self, username: str, expected: frozenset) -> None:
        policy = self.actor(username)
        missing = sorted(expected - policy.permissions)
        extra = sorted(policy.permissions - expected)
        self.assertEqual(missing, [], f"{username} 缺少契约权限点：{missing}")
        self.assertEqual(extra, [], f"{username} 多出契约外权限点：{extra}")

    def test_permission_points_match_contract_exactly(self):
        self.assertEqual(sorted(ALL_PERMISSIONS), sorted(CONTRACT_PERMISSIONS))
        self.assertEqual(len(ALL_PERMISSIONS), 35)

    def test_six_roles_with_expected_scopes(self):
        from permissions import ROLES

        self.assertEqual(sorted(ROLES), ["admin", "engineer", "finance", "manager", "owner", "service"])
        expected = {
            "admin": "all",
            "manager": "community",
            "service": "community",
            "engineer": "assigned",
            "finance": "community",
            "owner": "self",
        }
        for code, scope in expected.items():
            self.assertEqual(ROLES[code]["scope"], scope, code)
            self.assertEqual(self.actor(code + "01" if code != "admin" else "admin").primary_scope, scope, code)

    def test_every_role_has_a_chinese_display_name(self):
        """每个角色都要有人话显示名，页面上不能出现角色代码。

        回归：ROLE_NAMES 在模块上部按当时的 ROLES 算好，而 finance 是文件末尾
        ROLES.update(_V2_ROLES) 才加进来的，于是它不在 ROLE_NAMES 里，
        Policy.role_names 退化成代码——财务登录后身份条显示「角色 finance」。
        """
        from permissions import ROLE_NAMES, ROLES

        self.assertEqual(sorted(ROLE_NAMES), sorted(ROLES), "ROLE_NAMES 必须覆盖 ROLES 里的每个角色")
        for code, meta in ROLES.items():
            self.assertEqual(ROLE_NAMES.get(code), meta["name"], code)
            self.assertNotEqual(ROLE_NAMES.get(code), code, f"{code} 的显示名退化成了代码")
        # 落到具体账号：财务的身份条要写「财务」
        self.assertEqual(self.actor("finance01").role_names, ["财务"])
        self.assertEqual(self.actor("finance01").identity()["role_names"], ["财务"])

    def test_admin_has_all_35(self):
        self.assertTrue(self.actor("admin").super)
        self.assert_matrix("admin", CONTRACT_PERMISSIONS)

    def test_manager_has_all_but_resident_self(self):
        policy = self.actor("manager01")
        self.assertFalse(policy.has("resident.self"))
        self.assert_matrix("manager01", CONTRACT_PERMISSIONS - {"resident.self"})

    def test_service_matrix(self):
        policy = self.actor("service01")
        self.assertFalse(policy.has("order.work"))
        self.assert_matrix("service01", CONTRACT_SERVICE)

    def test_engineer_matrix(self):
        self.assert_matrix("engineer01", CONTRACT_ENGINEER)

    def test_finance_matrix(self):
        policy = self.actor("finance01")
        self.assertFalse(policy.has("order.read"))
        self.assertTrue(policy.has("billing.collect"))
        self.assert_matrix("finance01", CONTRACT_FINANCE)

    def test_owner_matrix(self):
        policy = self.actor("owner01")
        self.assertFalse(policy.has("order.work"))
        self.assertFalse(policy.has("order.dispatch"))
        self.assert_matrix("owner01", CONTRACT_OWNER)

    def test_identity_contains_contract_keys(self):
        identity = self.actor("owner01").identity()
        for key in ("userId", "username", "roles", "roleNames", "permissions", "data_scope", "dataScopeText"):
            self.assertIn(key, identity)
        self.assertEqual(identity["userId"], self.user("owner01").id)
        self.assertEqual(identity["roles"], ["owner"])
        self.assertEqual(identity["roleNames"], ["业主"])
        self.assertEqual(identity["data_scope"], "self")
        self.assertEqual(identity["real_name"], "张伟")
        self.assertIn("order.create", identity["permissions"])

    def test_require_raises_403_with_plain_language(self):
        policy = self.actor("owner01")
        with self.assertRaises(Forbidden) as ctx:
            policy.require("order.dispatch")
        self.assertIn("派单", str(ctx.exception.description))
        self.assertNotIn("RBAC", str(ctx.exception.description))

    def test_anonymous_is_denied_everything(self):
        policy = Policy(self.session, None)
        self.assertEqual(policy.permissions, frozenset())
        with self.assertRaises(Unauthorized):
            policy.require("order.read")
        self.assertEqual(self.session.execute(policy.query(WorkOrder)).scalars().all(), [])

    def test_inactive_account_loses_permissions(self):
        user = self.user("manager01")
        user.active = False
        self.session.commit()
        policy = Policy(self.session, user)
        self.assertEqual(policy.permissions, frozenset())
        with self.assertRaises(Forbidden):
            policy.require("order.read")

    def test_no_scope_row_means_no_data(self):
        user = self.user("manager01")
        for scope in self.session.execute(select(UserScope).where(UserScope.user_id == user.id)).scalars().all():
            scope.deleted = True
        self.session.commit()
        policy = Policy(self.session, user)
        self.assertEqual(policy.primary_scope, "none")
        self.assertTrue(policy.has("order.read"))
        self.assertEqual(self.session.execute(policy.query(WorkOrder)).scalars().all(), [])


class DataScopeTests(DbTestCase):
    """六种角色在行级数据范围上的实际效果。"""

    def test_order_counts_per_role(self):
        for username, expected in EXPECTED_ORDER_SCOPE.items():
            with self.subTest(username=username):
                self.assertEqual(queries.list_work_orders(self.actor(username))["total"], expected)

    def test_admin_sees_every_order(self):
        self.assertEqual(self.count(WorkOrder), 8)
        self.assertEqual(queries.list_work_orders(self.actor("admin"))["total"], 8)

    def test_community_scope_isolates_other_community(self):
        manager = self.actor("manager01")
        self.assertEqual(queries.list_work_orders(manager)["total"], 7)
        with self.assertRaises(NotFound):
            queries.get_work_order(manager, self.order("WO-TEST-0006").id)
        with self.assertRaises(Forbidden):
            manager.require_scope(self.fx["c2"].id)
        self.assertEqual(queries.list_work_orders(self.actor("manager02"))["total"], 1)

    def test_engineer_sees_only_assigned_orders(self):
        policy = self.actor("engineer01")
        rows = self.session.execute(policy.query(WorkOrder)).scalars().unique().all()
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row.repairer_id == policy.user_id for row in rows))
        with self.assertRaises(NotFound):
            queries.get_work_order(policy, self.order("WO-TEST-0001").id)

    def test_owner_sees_only_own_house(self):
        policy = self.actor("owner01")
        rows = self.session.execute(policy.query(WorkOrder)).scalars().unique().all()
        self.assertEqual(sorted(row.no for row in rows), ["WO-TEST-0001", "WO-TEST-0007"])
        houses = self.session.execute(policy.query(House)).scalars().unique().all()
        self.assertEqual([house.id for house in houses], [self.house("h1").id])

    def test_condition_is_pushed_into_sql(self):
        engineer_sql = str(self.actor("engineer01").query(WorkOrder).compile(self.session.bind))
        self.assertIn("repairer_id", engineer_sql)
        owner_sql = str(self.actor("owner01").query(WorkOrder).compile(self.session.bind))
        self.assertIn("house_person", owner_sql)
        manager_sql = str(self.actor("manager01").query(WorkOrder).compile(self.session.bind))
        self.assertIn("community_id", manager_sql)

    def test_house_scope_and_within(self):
        policy = self.actor("owner01")
        my_house = self.house("h1")
        self.assertTrue(policy.house_in_scope(my_house))
        self.assertTrue(policy.within(my_house.community_id, my_house.building_id))
        with self.assertRaises(Forbidden):
            policy.require_scope(my_house.community_id, write=True)
        neighbour = self.house("h2")
        self.assertFalse(policy.house_in_scope(neighbour))
        with self.assertRaises(Forbidden):
            policy.require_house(neighbour)

    def test_engineer_write_scope_is_narrower(self):
        policy = self.actor("engineer01")
        order = self.order("WO-TEST-0003")
        self.assertTrue(policy.within(order.community_id, order.building_id))
        self.assertFalse(policy.within(order.community_id, order.building_id, write=True))
        self.assertFalse(policy.within(order.community_id, write=True))

    def test_person_scope(self):
        self.assertEqual(queries.list_persons(self.actor("admin"))["total"], 8)
        owner_names = {item["name"] for item in queries.list_persons(self.actor("owner01"))["items"]}
        self.assertEqual(owner_names, {"张伟", "李秀兰"})
        manager_names = {item["name"] for item in queries.list_persons(self.actor("manager01"))["items"]}
        self.assertIn("赵敏", manager_names)
        self.assertNotIn("隔壁王", manager_names)

    def test_audit_scope(self):
        with self.assertRaises(Forbidden):
            queries.list_audit_logs(self.actor("owner01"))
        with self.assertRaises(Forbidden):
            queries.list_audit_logs(self.actor("engineer01"))
        self.assertGreater(queries.list_audit_logs(self.actor("manager01"))["total"], 0)

    def test_ai_sessions_are_private(self):
        admin = self.actor("admin")
        owner = self.actor("owner01")
        created = services.create_ai_session(admin, "管理员自己的会话")
        with self.assertRaises(NotFound):
            queries.get_session_messages(owner, created["id"])
        self.assertEqual(queries.list_sessions(owner), [])
        self.assertEqual(len(queries.list_sessions(admin)), 1)

    def test_whoami_and_dashboard_are_scoped(self):
        owner = self.actor("owner01")
        card = queries.whoami(owner)
        self.assertEqual(card["identity"]["data_scope"], "self")
        self.assertEqual(len(card["houses"]), 1)
        dashboard = queries.dashboard(owner)
        self.assertEqual(dashboard["my_stats"]["total_orders"], 2)
        self.assertEqual(len(dashboard["recent_orders"]), 2)

    def test_staff_read_requires_permission(self):
        with self.assertRaises(Forbidden):
            queries.list_staff(self.actor("engineer01"))
        usernames = {item["username"] for item in queries.list_staff(self.actor("manager01"))["items"]}
        self.assertIn("engineer01", usernames)


class ReadPermissionGuardTests(DbTestCase):
    """契约 v2 §6：读查询也要过 Policy.require（模型硬调时服务端二次拒绝）。"""

    def test_reads_require_their_permission(self):
        cases = [
            ("finance01", lambda actor: queries.list_work_orders(actor), "order.read"),
            ("engineer01", lambda actor: queries.list_persons(actor), "person.read"),
            ("engineer01", lambda actor: queries.list_houses(actor), "house.read"),
            ("finance01", lambda actor: queries.list_work_orders(actor), "order.read"),
        ]
        for username, call, permission in cases:
            with self.subTest(username=username, permission=permission):
                with self.assertRaises(Forbidden):
                    call(self.actor(username))

    def test_owner_can_read_own_records_only(self):
        owner = self.actor("owner01")
        self.assertEqual(queries.list_houses(owner)["total"], 1)
        self.assertEqual(queries.list_work_orders(owner)["total"], 2)
        self.assertEqual(queries.list_persons(owner)["total"], 2)

    def test_finance_can_read_property_records(self):
        finance = self.actor("finance01")
        self.assertGreaterEqual(queries.list_houses(finance)["total"], 1)
        self.assertGreaterEqual(queries.list_persons(finance)["total"], 1)
        self.assertGreaterEqual(queries.list_communities(finance)["total"], 1)


class PolicyQueryGuardTests(DbTestCase):
    """Policy.query/get 的软删除与 404 语义。"""

    def test_deleted_rows_are_invisible(self):
        admin = self.actor("admin")
        order = self.order("WO-TEST-0001")
        order.deleted = True
        self.session.commit()
        self.assertEqual(queries.list_work_orders(admin)["total"], 7)
        with self.assertRaises(NotFound):
            admin.get(WorkOrder, order.id)

    def test_get_rejects_bad_identifier(self):
        admin = self.actor("admin")
        for bad in (None, "", "abc"):
            with self.assertRaises(NotFound):
                admin.get(WorkOrder, bad)

    def test_user_dict_hides_password_hash(self):
        self.assertNotIn("password_hash", self.user("admin").to_dict())

    def test_keyword_search_matches_full_house_name(self):
        admin = self.actor("admin")
        houses = queries.list_houses(admin, keyword="1栋1单元101")
        self.assertEqual([item["id"] for item in houses["items"]], [self.house("h1").id])
        orders = queries.list_work_orders(admin, keyword="1栋1单元101")
        self.assertEqual(sorted(item["no"] for item in orders["items"]), ["WO-TEST-0001", "WO-TEST-0007"])
        self.assertEqual(models.House.room.key, "room")  # 保持 models 被引用


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
