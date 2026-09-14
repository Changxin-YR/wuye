"""写命令测试（契约第 4 节、第 8.2 节）。

覆盖：工单状态机全链路与非法流转、房屋/人员唯一性、软删除约束、
数据库约束（同一房屋同一人员只允许一条 active 关系）、
审计与流转日志的事务一致性、智能体来源标记、seed_demo 幂等。
"""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from sqlalchemy import func, select
from werkzeug.exceptions import Forbidden

import db as app_db
import models
import queries
import seed_demo
import services
from models import AuditLog, House, HousePerson, OrderLog, WorkOrder
from services import ServiceError

try:
    from base import DbTestCase
except ImportError:  # pragma: no cover
    from tests.base import DbTestCase


class OrderLifecycleTests(DbTestCase):
    """报修 → 派单 → 接单 → 进度 → 完工 → 验收 → 评价 全链路。"""

    def test_create_order_validates_required_fields(self):
        owner = self.actor("owner01")
        house_id = self.house("h1").id
        with self.assertRaises(ServiceError) as ctx:
            services.create_work_order(
                owner, house_id=house_id, contact_name="", contact_phone="13900000001", category="water", description="漏水"
            )
        self.assertIn("联系人", ctx.exception.message)
        with self.assertRaises(ServiceError) as ctx:
            services.create_work_order(
                owner, house_id=house_id, contact_name="张伟", contact_phone="abc", category="water", description="漏水"
            )
        self.assertIn("联系电话", ctx.exception.message)
        with self.assertRaises(ServiceError):
            services.create_work_order(
                owner, house_id=house_id, contact_name="张伟", contact_phone="13900000001",
                category="不存在的类型", description="漏水",
            )
        with self.assertRaises(ServiceError):
            services.create_work_order(
                owner, house_id=house_id, contact_name="张伟", contact_phone="13900000001", category="water", description=""
            )

    def test_create_order_writes_log_and_audit(self):
        owner = self.actor("owner01")
        before_logs = self.count(OrderLog)
        before_audits = self.count(AuditLog)
        result = services.create_work_order(
            owner,
            house_id=self.house("h1").id,
            contact_name="张伟",
            contact_phone="139 0000 0001",
            category="水暖",
            description="厨房下水管漏水，需要上门查看",
            urgency="紧急",
        )
        self.assertEqual(result["status"], 0)
        self.assertEqual(result["status_text"], "待派单")
        self.assertEqual(result["urgency"], 1)
        self.assertEqual(result["category"], "water")
        self.assertEqual(result["house_full"], "美家花园1栋1单元101")
        self.assertEqual(result["owner_id"], owner.user_id)
        self.assertTrue(result["no"].startswith("WO"))
        self.assertEqual(result["contact_phone"], "13900000001")  # 空格会被清理
        self.assertEqual(self.count(OrderLog), before_logs + 1)
        self.assertEqual(self.count(AuditLog), before_audits + 1)
        audit = self.session.execute(
            select(AuditLog).where(AuditLog.target_id == str(result["id"]), AuditLog.action == "order.create")
        ).scalars().first()
        self.assertEqual(audit.source, "web")

    def test_owner_cannot_report_for_other_house(self):
        owner = self.actor("owner01")
        with self.assertRaises(ServiceError) as ctx:
            services.create_work_order(
                owner,
                house_id=self.house("h3").id,
                contact_name="张伟",
                contact_phone="13900000001",
                category="water",
                description="别人家的房子",
            )
        self.assertIn("没有找到", ctx.exception.message)

    def test_full_state_machine_chain(self):
        service = self.actor("service01")
        engineer = self.actor("engineer01")
        manager = self.actor("manager01")
        owner = self.actor("owner01")

        order = services.create_work_order(
            owner,
            house_id=self.house("h1").id,
            contact_name="张伟",
            contact_phone="13900000001",
            category="electric",
            description="客厅灯闪烁，怀疑线路问题",
        )
        order_id = order["id"]

        assigned = services.assign_work_order(service, order_id, "黄磊", "请今天下午上门")
        self.assertEqual(assigned["status"], 1)
        self.assertEqual(assigned["repairer_name"], "黄磊")

        self.assertEqual(services.accept_work_order(engineer, order_id)["status"], 2)
        self.assertEqual(services.add_order_progress(engineer, order_id, "已更换损坏的开关")["status"], 2)

        finished = services.finish_work_order(engineer, order_id, "维修完成，已通电测试")
        self.assertEqual(finished["status"], 3)
        self.assertTrue(finished["finished_at"])

        verified = services.verify_work_order(manager, order_id, "已确认修复")
        self.assertEqual(verified["status"], 4)
        self.assertEqual(verified["status_text"], "已关闭")
        self.assertTrue(verified["closed_at"])

        rated = services.rate_work_order(owner, order_id, 5, "师傅很专业")
        self.assertEqual(rated["rating"], 5)
        self.assertEqual(rated["rating_note"], "师傅很专业")

        detail = queries.get_work_order(manager, order_id)
        self.assertEqual(
            [item["action"] for item in detail["logs"]],
            ["create", "assign", "accept", "progress", "finish", "verify", "rate"],
        )
        self.assertEqual(detail["logs"][-1]["action_text"], "业主评价")
        self.assertEqual(detail["actions"], [])  # 已关闭，且当前用户不是报修人

        actions = [
            row.action
            for row in self.session.execute(
                select(AuditLog).where(AuditLog.target_id == str(order_id))
            ).scalars().all()
        ]
        for expected in (
            "order.create",
            "order.assign",
            "order.accept",
            "order.progress",
            "order.finish",
            "order.verify",
            "order.rate",
        ):
            self.assertIn(expected, actions)

    def test_actions_follow_status_and_identity(self):
        service = self.actor("service01")
        engineer = self.actor("engineer01")
        owner = self.actor("owner01")
        pending = queries.get_work_order(service, self.order("WO-TEST-0001").id)
        self.assertIn("assign", [item["name"] for item in pending["actions"]])
        self.assertIn("cancel", [item["name"] for item in pending["actions"]])
        assigned = queries.get_work_order(service, self.order("WO-TEST-0002").id)
        self.assertEqual([item["name"] for item in assigned["actions"]], ["assign", "cancel"])
        engineer_view = queries.get_work_order(engineer, self.order("WO-TEST-0002").id)
        self.assertEqual([item["name"] for item in engineer_view["actions"]], ["accept"])
        closed = queries.get_work_order(owner, self.order("WO-TEST-0007").id)
        self.assertEqual([item["name"] for item in closed["actions"]], ["cancel", "finish", "progress"][:1])

    def test_assign_target_must_be_engineer(self):
        service = self.actor("service01")
        order = self.order("WO-TEST-0001")
        with self.assertRaises(ServiceError) as ctx:
            services.assign_work_order(service, order.id, "张伟")  # 有账号但不是工程维修
        self.assertIn("工程维修", ctx.exception.message)
        with self.assertRaises(ServiceError) as ctx:
            services.assign_work_order(service, order.id, "赵敏")  # 有效人员但没绑定账号
        self.assertIn("绑定登录账号", ctx.exception.message)
        with self.assertRaises(ServiceError):
            services.assign_work_order(service, order.id, "不存在的人")
        result = services.assign_work_order(service, order.id, "engineer01")
        self.assertEqual(result["repairer_id"], self.user("engineer01").id)
        self.assertEqual(result["status"], 1)

    def test_only_assignee_can_work_on_order(self):
        manager = self.actor("manager01")  # 有 order.work 权限，但不是被派人
        order = self.order("WO-TEST-0002")
        with self.assertRaises(ServiceError) as ctx:
            services.accept_work_order(manager, order.id)
        self.assertIn("被指派", ctx.exception.message)
        with self.assertRaises(Forbidden):
            services.accept_work_order(self.actor("owner01"), order.id)  # 业主连权限都没有

    def test_illegal_transitions_are_rejected(self):
        service = self.actor("service01")
        engineer = self.actor("engineer01")
        with self.assertRaises(ServiceError) as ctx:  # 已派单不能完工
            services.finish_work_order(engineer, self.order("WO-TEST-0002").id)
        self.assertIn("已派单", ctx.exception.message)
        with self.assertRaises(ServiceError) as ctx:  # 待验收不能再接单
            services.accept_work_order(engineer, self.order("WO-TEST-0004").id)
        self.assertIn("待验收", ctx.exception.message)
        with self.assertRaises(ServiceError) as ctx:  # 已派单不能登记进度
            services.add_order_progress(engineer, self.order("WO-TEST-0002").id)
        self.assertIn("维修中", ctx.exception.message)
        with self.assertRaises(ServiceError) as ctx:  # 已关闭不能再取消
            services.cancel_work_order(service, self.order("WO-TEST-0005").id, "不想要了")
        self.assertIn("已关闭", ctx.exception.message)
        with self.assertRaises(ServiceError) as ctx:  # 维修中不能评价
            services.rate_work_order(self.actor("owner01"), self.order("WO-TEST-0007").id, 5)
        self.assertIn("维修中", ctx.exception.message)
        with self.assertRaises(ServiceError):  # 未派给自己的工单看不到
            services.finish_work_order(engineer, self.order("WO-TEST-0001").id)

    def test_cancel_requires_reason(self):
        service = self.actor("service01")
        order = self.order("WO-TEST-0001")
        with self.assertRaises(ServiceError) as ctx:
            services.cancel_work_order(service, order.id, "")
        self.assertIn("取消原因", ctx.exception.message)
        result = services.cancel_work_order(service, order.id, "业主自行处理")
        self.assertEqual((result["status"], result["status_text"]), (5, "已取消"))

    def test_repairer_cannot_verify_own_order(self):
        manager = self.actor("manager01")
        order = self.order("WO-TEST-0004")  # 待验收，被派人是 engineer01
        order.repairer_id = manager.user_id
        self.session.commit()
        with self.assertRaises(ServiceError) as ctx:
            services.verify_work_order(manager, order.id)
        self.assertIn("不能验收自己", ctx.exception.message)

    def test_rate_only_owner_and_valid_range(self):
        owner = self.actor("owner01")
        manager = self.actor("manager01")
        admin = self.actor("admin")
        order = self.order("WO-TEST-0005")  # 已关闭，但这单属于 h3，不是 owner01 的家
        with self.assertRaises(ServiceError):
            services.rate_work_order(owner, order.id, 5)
        with self.assertRaises(ServiceError) as ctx:
            services.rate_work_order(manager, order.id, 5)
        self.assertIn("报修人本人", ctx.exception.message)
        with self.assertRaises(ServiceError) as ctx:
            services.rate_work_order(admin, order.id, 9)
        self.assertIn("评分", ctx.exception.message)


class MasterDataTests(DbTestCase):
    """空间主数据与人员关系的唯一性 / 软删除约束。"""

    def test_house_room_unique_inside_building(self):
        manager = self.actor("manager01")
        with self.assertRaises(ServiceError) as ctx:
            services.create_house(manager, building="1栋", unit="1", room="101", area=88)
        self.assertIn("已经存在", ctx.exception.message)
        created = services.create_house(manager, building="2栋", unit="2", room="202", area=120)
        self.assertEqual(created["full_name"], "美家花园2栋2单元202")
        with self.assertRaises(ServiceError) as ctx:
            services.create_house(manager, building_id=self.fx["b3"].id, unit="1", room="202")
        self.assertIn("没有找到", ctx.exception.message)  # 隔壁小区的楼栋不在范围内

    def test_update_house_conflict_and_status_rules(self):
        manager = self.actor("manager01")
        house = self.house("h2")
        with self.assertRaises(ServiceError):
            services.update_house(manager, house.id, room="101")  # 与 1栋1单元101 冲突
        with self.assertRaises(ServiceError) as ctx:
            services.update_house(manager, house.id, status=0)  # h2 有在住人员
        self.assertIn("在住人员", ctx.exception.message)
        result = services.update_house(manager, house.id, room="102A", area=90)
        self.assertEqual((result["room"], result["area"]), ("102A", 90.0))

    def test_delete_house_blocked_by_relations_then_allowed(self):
        manager = self.actor("manager01")
        house = self.house("h1")
        with self.assertRaises(ServiceError) as ctx:
            services.delete_house(manager, house.id)
        self.assertIn("在住人员", ctx.exception.message)
        for relation in self.session.execute(
            select(HousePerson).where(HousePerson.house_id == house.id, HousePerson.status == "active")
        ).scalars().all():
            services.end_relation(manager, relation.id, "退租")
        service = self.actor("service01")
        for order in self.session.execute(
            select(WorkOrder).where(
                WorkOrder.house_id == house.id, WorkOrder.deleted.is_(False), WorkOrder.status.notin_((4, 5))
            )
        ).scalars().all():
            services.cancel_work_order(service, order.id, "演示用取消")
        result = services.delete_house(manager, house.id)
        self.assertIn("已删除", result["message"])
        self.assertTrue(self.session.get(House, house.id).deleted)
        self.assertNotIn(house.id, [item["id"] for item in queries.list_houses(manager)["items"]])

    def test_delete_house_blocked_by_open_orders(self):
        manager = self.actor("manager01")
        house = self.house("h2")  # 无业主，但有维修中的工单
        for relation in self.session.execute(
            select(HousePerson).where(HousePerson.house_id == house.id, HousePerson.status == "active")
        ).scalars().all():
            services.end_relation(manager, relation.id, "退租")
        with self.assertRaises(ServiceError) as ctx:
            services.delete_house(manager, house.id)
        self.assertIn("未完结", ctx.exception.message)

    def test_relation_active_uniqueness_and_owner_uniqueness(self):
        manager = self.actor("manager01")
        with self.assertRaises(ServiceError) as ctx:
            services.bind_relation(manager, self.house("h1").id, self.fx["persons"]["李秀兰|13900000002"].id, "family")
        self.assertIn("已经是", ctx.exception.message)
        with self.assertRaises(ServiceError) as ctx:
            services.bind_relation(manager, self.house("h1").id, self.fx["persons"]["李娜|13800000012"].id, "owner")
        self.assertIn("已经有业主", ctx.exception.message)

    def test_bind_and_end_relation(self):
        manager = self.actor("manager01")
        person = self.fx["persons"]["李娜|13800000012"]
        house = self.house("h4")
        created = services.bind_relation(manager, house.id, person.id, "租户")
        self.assertEqual((created["relation"], created["status"]), ("tenant", "active"))
        relation = self.session.get(HousePerson, created["id"])
        self.assertEqual(relation.active_key, f"{house.id}:{person.id}")
        ended = services.end_relation(manager, created["id"], "到期退租")
        self.assertEqual(ended["status"], "ended")
        self.assertIsNone(self.session.get(HousePerson, created["id"]).active_key)
        with self.assertRaises(ServiceError):
            services.end_relation(manager, created["id"], "再结束一次")
        again = services.bind_relation(manager, house.id, person.id, "family")
        self.assertEqual(again["status"], "active")

    def test_owner_relation_switches_house_status(self):
        manager = self.actor("manager01")
        house = self.house("h4")
        self.assertEqual(house.status, 0)
        relation_id = self.session.execute(
            select(HousePerson.id).where(
                HousePerson.house_id == house.id,
                HousePerson.person_id == self.fx["persons"]["刘建国|13900000004"].id,
            )
        ).scalar()
        services.end_relation(manager, relation_id, "卖房")
        self.assertEqual(self.session.get(House, house.id).status, 0)
        services.bind_relation(manager, house.id, self.fx["persons"]["刘建国|13900000004"].id, "owner")
        self.assertEqual(self.session.get(House, house.id).status, 1)  # 业主入住 → 自住

    def test_person_delete_rules_and_ambiguity(self):
        manager = self.actor("manager01")
        with self.assertRaises(ServiceError) as ctx:
            services.delete_person(manager, self.fx["persons"]["赵敏|13900000003"].id)
        self.assertIn("有效的房屋关系", ctx.exception.message)
        with self.assertRaises(ServiceError) as ctx:
            services.resolve_person(manager, "李娜")
        self.assertIn("找到 2 位", ctx.exception.message)
        created = services.create_person(manager, "周阿姨", "13500000001")
        self.assertEqual(created["name"], "周阿姨")
        with self.assertRaises(ServiceError):
            services.create_person(manager, "周阿姨", "13500000001")
        deleted = services.delete_person(manager, created["id"])
        self.assertIn("已删除", deleted["message"])

    def test_community_and_building_delete_rules(self):
        admin = self.actor("admin")
        manager = self.actor("manager01")
        with self.assertRaises(ServiceError) as ctx:
            services.delete_community(admin, self.fx["c1"].id)
        self.assertIn("请先删除楼栋", ctx.exception.message)
        with self.assertRaises(ServiceError) as ctx:
            services.delete_building(manager, self.fx["b1"].id)
        self.assertIn("请先删除房屋", ctx.exception.message)
        with self.assertRaises(ServiceError) as ctx:
            services.create_community(manager, "新建小区")
        self.assertIn("管理员", ctx.exception.message)
        with self.assertRaises(ServiceError) as ctx:
            services.create_building(manager, community_id=self.fx["c2"].id, name="9栋")
        self.assertIn("没有找到", ctx.exception.message)

    def test_house_resolver_accepts_names(self):
        admin = self.actor("admin")
        self.assertEqual(services.resolve_house(admin, "1栋1单元101").id, self.house("h1").id)
        self.assertEqual(services.resolve_house(admin, "美家花园1栋1单元101").id, self.house("h1").id)
        self.assertEqual(services.resolve_house(admin, "2栋101").id, self.house("h5").id)  # 省略单元号
        with self.assertRaises(ServiceError) as ctx:
            services.resolve_house(admin, "1栋101")  # 1单元与2单元都有 101 → 需要消歧
        self.assertIn("找到多条", ctx.exception.message)
        with self.assertRaises(ServiceError) as ctx:
            services.resolve_house(admin, "不存在的楼")
        self.assertIn("没有找到", ctx.exception.message)


class TransactionAuditTests(DbTestCase):
    """写操作的事务一致性与审计来源。"""

    def test_failed_validation_leaves_no_trace(self):
        service = self.actor("service01")
        before_logs = self.count(OrderLog)
        before_audits = self.count(AuditLog)
        with self.assertRaises(ServiceError):
            services.cancel_work_order(service, self.order("WO-TEST-0001").id, "")
        self.assertEqual(self.count(OrderLog), before_logs)
        self.assertEqual(self.count(AuditLog), before_audits)
        result = services.cancel_work_order(service, self.order("WO-TEST-0001").id, "重复报修")
        self.assertEqual(result["status"], 5)

    def test_agent_source_is_recorded(self):
        agent_owner = self.actor("owner01", source="agent")
        result = services.create_work_order(
            agent_owner,
            house_id=self.house("h1").id,
            contact_name="张伟",
            contact_phone="13900000001",
            category="电路",
            description="AI 助手代报修：客厅灯闪烁",
        )
        audit = self.session.execute(
            select(AuditLog).where(AuditLog.target_id == str(result["id"]), AuditLog.action == "order.create")
        ).scalars().first()
        self.assertEqual(audit.source, "agent")
        self.assertEqual(queries.list_audit_logs(self.actor("admin"), source="agent")["total"] >= 1, True)
        log = self.session.execute(
            select(OrderLog).where(OrderLog.order_id == result["id"], OrderLog.action == "create")
        ).scalars().first()
        self.assertEqual(log.operator_id, agent_owner.user_id)

    def test_order_no_is_unique_and_incremental(self):
        service = self.actor("service01")
        first = services.create_work_order(
            service, house_id=self.house("h2").id, contact_name="李娜", contact_phone="13800000012",
            category="water", description="第一次报修",
        )
        second = services.create_work_order(
            service, house_id=self.house("h2").id, contact_name="李娜", contact_phone="13800000012",
            category="water", description="第二次报修",
        )
        self.assertNotEqual(first["no"], second["no"])
        self.assertLess(first["no"], second["no"])


class SeedIdempotencyTests(DbTestCase):
    """``seed_demo`` 必须幂等（可以重复执行）。"""

    def _order_total(self) -> int:
        session = app_db.get_sessionmaker(self.engine)()
        try:
            return int(session.execute(select(func.count()).select_from(WorkOrder)).scalar() or 0)
        finally:
            session.close()

    def test_seed_twice_does_not_duplicate(self):
        models.Base.metadata.drop_all(self.engine)
        models.Base.metadata.create_all(self.engine)
        with redirect_stdout(io.StringIO()):
            first = seed_demo.seed(self.engine)
        first_total = self._order_total()
        with redirect_stdout(io.StringIO()):
            second = seed_demo.seed(self.engine)
        self.assertGreater(first_total, 0)
        self.assertEqual(self._order_total(), first_total)  # 第二次不再新增工单
        self.assertIsInstance(first, dict)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
