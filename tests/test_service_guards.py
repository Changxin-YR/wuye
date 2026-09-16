"""服务层护栏测试（契约 v2 §4）。

覆盖写命令的统一机制：**事务、乐观锁、版本校验、幂等、回滚**。
- 幂等：同一 ``request_key`` 回放上次结果（只落一次库），不同键/不同用户互不干扰；
- 乐观锁：``expected_version`` 不符 → ``ServiceError`` 且 ``status_code == 409``，库里不变；
- 事务：任何校验失败的写命令都不留痕（业务行 / order_log / audit / 幂等记录都不新增），失败后会话仍可用；
- 返回值契约：写命令都带 ``message`` 与 ``version``；查询都带 ``items`` 与 ``total``；
- 授权：写命令离开权限或数据范围一律拒绝，且 ``source="agent"`` 要落到 audit_log.source。
"""
from __future__ import annotations

import unittest

from sqlalchemy import func, select
from werkzeug.exceptions import Forbidden, NotFound

import queries
import risk
import models
import services
from models import AiAction, AuditLog, House, OrderLog, User, WorkOrder
from services import ServiceError

try:
    from base import TEST_CSRF, TEST_PASSWORD, DbTestCase, make_app  # noqa: F401
except ImportError:  # pragma: no cover
    from tests.base import TEST_CSRF, TEST_PASSWORD, DbTestCase, make_app  # noqa: F401

WRITE_COMMANDS = (
    "create_community",
    "update_community",
    "create_building",
    "update_building",
    "create_house",
    "update_house",
    "create_person",
    "update_person",
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
    "delete_house",
    "delete_person",
)


class ReturnContractTests(DbTestCase):
    """返回值契约：message / version / items / total / status_code。"""

    def test_write_commands_expose_message_version_and_metadata(self):
        import inspect

        for name in WRITE_COMMANDS:
            function = getattr(services, name)
            parameters = inspect.signature(function).parameters
            for extra in ("source", "request_key", "expected_version"):
                self.assertIn(extra, parameters, f"{name} 缺少统一参数 {extra}")

    def test_write_results_carry_message_and_version(self):
        service = self.actor("service01")
        created = services.create_house(service, building="2栋", unit="2", room="701", area=66)
        self.assertIn("message", created)
        self.assertIn("version", created)
        self.assertIsInstance(created["message"], str)
        updated = services.update_house(service, created["id"], room="702")
        self.assertEqual(updated["version"], 2)
        self.assertIn("702", updated["message"])
        deleted = services.delete_house(service, created["id"])
        self.assertIn("message", deleted)
        self.assertIn("version", deleted)

    def test_order_flow_results_carry_message_and_version(self):
        service = self.actor("service01")
        engineer = self.actor("engineer01")
        manager = self.actor("manager01")
        order = services.create_work_order(
            service, house_id=self.house("h2").id, contact_name="李娜", contact_phone="13800000012",
            category="water", description="护栏测试：漏水",
        )
        self.assertEqual(order["message"], f"工单 {order['no']} 已创建")
        assigned = services.assign_work_order(service, order["id"], "黄磊")
        self.assertEqual(assigned["message"], f"工单 {order['no']} 已派给 黄磊")
        self.assertEqual(services.accept_work_order(engineer, order["id"])["version"], 3)
        services.add_order_progress(engineer, order["id"], "已上门")
        services.finish_work_order(engineer, order["id"], "完工")
        verified = services.verify_work_order(manager, order["id"], "验收通过")
        self.assertIn("已验收", verified["message"])
        self.assertIn("version", verified)

    def test_query_results_carry_items_and_total(self):
        admin = self.actor("admin")
        for result in (
            queries.list_houses(admin),
            queries.list_persons(admin),
            queries.list_work_orders(admin),
            queries.list_communities(admin),
        ):
            self.assertIn("items", result)
            self.assertIn("total", result)
            self.assertIsInstance(result["items"], list)

    def test_service_error_exposes_http_semantics(self):
        service = self.actor("service01")
        conflict = ServiceError("conflict", "x")
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(ServiceError("invalid", "x").status_code, 400)
        self.assertEqual(ServiceError("not_found", "x").status_code, 404)
        self.assertEqual(ServiceError("forbidden", "x").status_code, 403)
        payload = conflict.as_dict()
        self.assertEqual(payload["status"], 409)
        self.assertFalse(payload["ok"])
        with self.assertRaises(ServiceError) as ctx:
            services.cancel_work_order(service, self.order("WO-TEST-0001").id, "")
        self.assertIn("取消原因", ctx.exception.message)


class IdempotencyTests(DbTestCase):
    """幂等键：命中则回放上次结果。"""

    def test_same_request_key_replays_without_second_write(self):
        service = self.actor("service01")
        before = self.count(WorkOrder)
        first = services.create_work_order(
            service, house_id=self.house("h2").id, contact_name="李娜", contact_phone="13800000012",
            category="water", description="幂等：厨房漏水", request_key="key-001",
        )
        second = services.create_work_order(
            service, house_id=self.house("h2").id, contact_name="李娜", contact_phone="13800000012",
            category="water", description="幂等：厨房漏水", request_key="key-001",
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["no"], second["no"])
        self.assertTrue(second.get("idempotent_replay"))
        self.assertEqual(self.count(WorkOrder), before + 1)
        self.assertEqual(
            self.session.execute(select(func.count()).select_from(AiAction).where(AiAction.status == 2)).scalar(), 1
        )

    def test_different_request_key_executes_again(self):
        service = self.actor("service01")
        first = services.create_work_order(
            service, house_id=self.house("h2").id, contact_name="李娜", contact_phone="13800000012",
            category="water", description="幂等：另一把钥匙", request_key="key-a",
        )
        second = services.create_work_order(
            service, house_id=self.house("h2").id, contact_name="李娜", contact_phone="13800000012",
            category="water", description="幂等：另一把钥匙", request_key="key-b",
        )
        self.assertNotEqual(first["id"], second["id"])
        self.assertIsNone(second.get("idempotent_replay"))

    def test_request_key_is_scoped_per_user(self):
        service = self.actor("service01")
        manager = self.actor("manager01")
        first = services.create_work_order(
            service, house_id=self.house("h2").id, contact_name="李娜", contact_phone="13800000012",
            category="water", description="幂等：跨用户", request_key="shared-key",
        )
        second = services.create_work_order(
            manager, house_id=self.house("h2").id, contact_name="李娜", contact_phone="13800000012",
            category="water", description="幂等：跨用户", request_key="shared-key",
        )
        self.assertNotEqual(first["id"], second["id"], "不同用户的同名幂等键不能互相回放")

    def test_replay_keeps_message_and_version(self):
        service = self.actor("service01")
        created = services.create_house(service, building="2栋", unit="2", room="801", area=70, request_key="house-1")
        replay = services.create_house(service, building="2栋", unit="2", room="801", area=70, request_key="house-1")
        self.assertTrue(replay.get("idempotent_replay"))
        self.assertEqual(replay["version"], created["version"])
        self.assertIn("message", replay)


class OptimisticLockTests(DbTestCase):
    """乐观锁：expected_version 不符 → 409，且不落任何痕迹。"""

    def test_stale_version_is_rejected(self):
        service = self.actor("service01")
        house = self.house("h4")
        with self.assertRaises(ServiceError) as ctx:
            services.update_house(service, house.id, room="101A", expected_version=house.version + 5)
        self.assertEqual(ctx.exception.code, "conflict")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("版本", ctx.exception.message)
        self.session.expire_all()
        self.assertEqual(self.session.get(House, house.id).room, "101")

    def test_correct_version_succeeds_and_bumps(self):
        service = self.actor("service01")
        house = self.house("h4")
        start = house.version
        updated = services.update_house(service, house.id, room="101B", expected_version=start)
        self.assertEqual(updated["room"], "101B")
        self.assertGreater(updated["version"], start)

    def test_omitting_version_skips_the_check(self):
        service = self.actor("service01")
        house = self.house("h4")
        updated = services.update_house(service, house.id, room="101C")
        self.assertEqual(updated["room"], "101C")

    def test_order_commands_also_check_version(self):
        service = self.actor("service01")
        order = self.order("WO-TEST-0001")
        with self.assertRaises(ServiceError) as ctx:
            services.assign_work_order(service, order.id, "黄磊", expected_version=order.version + 9)
        self.assertEqual(ctx.exception.status_code, 409)
        self.session.expire_all()
        self.assertIsNone(self.session.get(WorkOrder, order.id).repairer_id)

    def test_delete_commands_check_version(self):
        service = self.actor("service01")
        created = services.create_house(service, building="2栋", unit="2", room="901", area=60)
        with self.assertRaises(ServiceError):
            services.delete_house(service, created["id"], expected_version=created["version"] + 1)
        self.session.expire_all()
        self.assertFalse(self.session.get(House, created["id"]).deleted)


class TransactionGuardTests(DbTestCase):
    """失败不留痕、失败后会话仍可用。"""

    def test_failed_command_leaves_no_trace(self):
        service = self.actor("service01")
        order = self.order("WO-TEST-0001")
        logs, audits = self.count(OrderLog), self.count(AuditLog)
        idempotent = self.session.execute(
            select(func.count()).select_from(AiAction).where(AiAction.status == 2)
        ).scalar()
        with self.assertRaises(ServiceError):
            services.cancel_work_order(service, order.id, "", request_key="cancel-key")
        self.assertEqual(self.count(OrderLog), logs)
        self.assertEqual(self.count(AuditLog), audits)
        self.assertEqual(
            self.session.execute(select(func.count()).select_from(AiAction).where(AiAction.status == 2)).scalar(),
            idempotent,
        )
        self.assertEqual(self.session.get(WorkOrder, order.id).status, 0)

    def test_session_is_usable_after_failure(self):
        service = self.actor("service01")
        with self.assertRaises(ServiceError):
            services.create_house(service, building="2栋", unit="1", room="101")  # 房号重复
        created = services.create_house(service, building="2栋", unit="2", room="902", area=61)
        self.assertEqual(created["room"], "902")

    def test_scope_violation_writes_nothing(self):
        manager = self.actor("manager01")  # 小区范围是 c1，动不了 c2 的房屋
        other_house = self.house("h6")
        with self.assertRaises((Forbidden, NotFound, ServiceError)):
            services.update_house(manager, other_house.id, room="999")
        self.session.expire_all()
        self.assertEqual(self.session.get(House, other_house.id).room, "101")

    def test_unique_constraint_violation_is_friendly(self):
        service = self.actor("service01")
        with self.assertRaises(ServiceError) as ctx:
            services.create_house(service, building="1栋", unit="1", room="101")
        self.assertEqual(ctx.exception.code, "conflict")
        self.assertIn("已经存在", ctx.exception.message)


class AuthorizationGuardTests(DbTestCase):
    """权限 / 数据范围 / 审计来源。"""

    def test_write_without_permission_is_403(self):
        with self.assertRaises(Forbidden):
            services.delete_house(self.actor("owner01"), self.house("h1").id)
        with self.assertRaises(Forbidden):
            services.assign_work_order(self.actor("engineer01"), self.order("WO-TEST-0001").id, "黄磊")

    def test_owner_can_only_write_own_house(self):
        owner = self.actor("owner01")
        with self.assertRaises(ServiceError):
            services.create_work_order(
                owner, house_id=self.house("h3").id, contact_name="张伟", contact_phone="13900000001",
                category="water", description="别人家的房子",
            )

    def test_agent_source_lands_in_audit(self):
        agent = self.actor("service01", source="agent")
        result = services.create_work_order(
            agent, house_id=self.house("h2").id, contact_name="李娜", contact_phone="13800000012",
            category="water", description="护栏：来源标记", request_key="src-1",
        )
        audit = self.session.execute(
            select(AuditLog).where(AuditLog.target_id == str(result["id"]), AuditLog.action == "order.create")
        ).scalars().first()
        self.assertEqual(audit.source, "agent")
        # 显式 source 参数等价于 Policy(source="agent")
        again = services.create_work_order(
            self.actor("service01"), house_id=self.house("h2").id, contact_name="李娜",
            contact_phone="13800000012", category="water", description="护栏：source 参数",
            source="agent", request_key="src-2",
        )
        audit2 = self.session.execute(
            select(AuditLog).where(AuditLog.target_id == str(again["id"]), AuditLog.action == "order.create")
        ).scalars().first()
        self.assertEqual(audit2.source, "agent")

    def test_r3_commands_need_confirmation(self):
        for name in ("delete_house", "collect_payment", "reverse_payment", "void_bill"):
            self.assertEqual(risk.decision(name), risk.CONFIRM, name)


class BillingGuardTests(DbTestCase):
    """收费：部分收款累加、作废/冲销回退（契约 §4）。"""

    def _bill(self, amount=1000):
        finance = self.actor("finance01")
        return services.create_bill(
            finance, house_id=self.house("h2").id, fee_type="物业费", amount=amount, period="2026-09",
            due_at="2026-09-30",
        )

    def test_partial_collection_accumulates_paid_amount(self):
        finance = self.actor("finance01")
        bill = self._bill(1000)
        self.assertEqual(bill["version"], 1)
        first = services.collect_payment(finance, bill_id=bill["id"], amount=300, method="微信")
        self.assertIn("收款", first["message"])
        row = self.session.get(models.Bill, bill["id"])
        self.assertEqual(float(row.paid_amount), 300.0)
        self.assertEqual(int(row.status), 1)  # 部分缴纳
        second = services.collect_payment(finance, bill_id=bill["id"], amount=700, method="银行")
        self.session.expire_all()
        row = self.session.get(models.Bill, bill["id"])
        self.assertEqual(float(row.paid_amount), 1000.0)
        self.assertEqual(int(row.status), 2)  # 已缴
        self.assertEqual(first["method"], 2)  # 微信 → 其他
        self.assertEqual(second["method"], 1)  # 银行

    def test_serialized_option_dict_is_rejected(self):
        """费用类型必须是能直接给人看的词，不能是下拉选项字典的字符串形式。

        回归：建账单表单的 <option> 曾把整个选项字典渲染出来（value 和 text 都是字典），
        提交后原样落库，账单列表与详情页就把 "{'value': '水费', 'text': '水费'}" 显示给了用户。
        """
        finance = self.actor("finance01")
        for bad in ("{'value': '水费', 'text': '水费'}", '{"value": "水费"}', "<b>水费</b>"):
            with self.assertRaises(ServiceError, msg=bad) as ctx:
                services.create_bill(
                    finance, house_id=self.house("h2").id, fee_type=bad,
                    amount=100, period="2026-09",
                )
            self.assertIn("费用类型不正确", ctx.exception.message)
        # 正常的人话值仍然能建
        self.assertEqual(self._bill(100)["fee_type"], "物业费")

    def test_overpayment_is_rejected(self):
        finance = self.actor("finance01")
        bill = self._bill(1000)
        services.collect_payment(finance, bill_id=bill["id"], amount=1000)
        with self.assertRaises(ServiceError) as ctx:
            services.collect_payment(finance, bill_id=bill["id"], amount=1)
        self.assertIn("超过欠费", ctx.exception.message)
        self.session.expire_all()
        self.assertEqual(float(self.session.get(models.Bill, bill["id"]).paid_amount), 1000.0)

    def test_reverse_payment_rolls_back_bill(self):
        finance = self.actor("finance01")
        bill = self._bill(1000)
        payment = services.collect_payment(finance, bill_id=bill["id"], amount=600)
        reversed_row = services.reverse_payment(finance, payment_id=payment["id"], reason="重复收款")
        self.assertIn("冲销", reversed_row["message"])
        self.session.expire_all()
        self.assertEqual(int(self.session.get(models.Payment, payment["id"]).status), 1)
        row = self.session.get(models.Bill, bill["id"])
        self.assertEqual(float(row.paid_amount), 0.0)
        self.assertEqual(int(row.status), 0)  # 回到待缴

    def test_void_bill_rules(self):
        finance = self.actor("finance01")
        clean = self._bill(100)
        voided = services.void_bill(finance, bill_id=clean["id"], reason="开错费用")
        self.assertIn("作废", voided["message"])
        self.session.expire_all()
        self.assertEqual(int(self.session.get(models.Bill, clean["id"]).status), 3)

        paid = self._bill(200)
        services.collect_payment(finance, bill_id=paid["id"], amount=200)
        with self.assertRaises(ServiceError) as ctx:
            services.void_bill(finance, bill_id=paid["id"], reason="想作废")
        self.assertIn("先冲销", ctx.exception.message)

    def test_batch_billing_is_idempotent(self):
        finance = self.actor("finance01")
        first = services.create_bills_batch(finance, community_id=self.fx["c1"].id, fee_type="物业费",
                                            amount=120, period="2026-11")
        second = services.create_bills_batch(finance, community_id=self.fx["c1"].id, fee_type="物业费",
                                             amount=120, period="2026-11")
        self.assertGreater(first["created"], 0)
        self.assertEqual(second["created"], 0)
        self.assertIn("message", first)

    def test_arrears_summary_shape(self):
        finance = self.actor("finance01")
        bill = self._bill(500)
        summary = queries.arrears_summary(finance)
        self.assertIn("items", summary)
        self.assertIn("amount_total", summary)
        self.assertTrue(any(item["community_id"] == self.fx["c1"].id for item in summary["items"]))
        services.collect_payment(finance, bill_id=bill["id"], amount=500)
        after = queries.arrears_summary(finance)
        self.assertLessEqual(after["amount_total"], summary["amount_total"])


class UnitSyncTests(DbTestCase):
    """契约 §2：楼层第三层用 unit 表关联（House.unit_id），字符串字段保留兼容。"""

    def test_create_house_creates_and_links_unit(self):
        service = self.actor("service01")
        created = services.create_house(service, building_id=self.fx["b2"].id, unit="9", room="901", area=77)
        house = self.session.get(models.House, created["id"])
        self.assertIsNotNone(house.unit_id)
        unit = self.session.get(models.Unit, house.unit_id)
        self.assertEqual((unit.name, unit.building_id), ("9", self.fx["b2"].id))
        self.assertEqual(house.unit_name, "9")
        self.assertEqual(created["full_name"], "云邻花园2栋9单元901")

    def test_update_house_resyncs_unit(self):
        service = self.actor("service01")
        created = services.create_house(service, building_id=self.fx["b2"].id, unit="9", room="902", area=77)
        updated = services.update_house(service, created["id"], unit="10")
        house = self.session.get(models.House, created["id"])
        self.assertEqual(house.unit_name, "10")
        self.assertEqual(self.session.get(models.Unit, house.unit_id).name, "10")
        self.assertIn("10单元", updated["full_name"])

    def test_delete_unit_blocked_when_houses_exist(self):
        service = self.actor("service01")
        created_unit = services.create_unit(service, building_id=self.fx["b2"].id, name="11")
        services.create_house(service, building_id=self.fx["b2"].id, unit="11", room="1101", area=60)
        with self.assertRaises(ServiceError) as ctx:
            services.delete_unit(service, created_unit["id"])
        self.assertIn("还有 1 套房屋", ctx.exception.message)

    def test_unit_crud_and_version(self):
        service = self.actor("service01")
        created = services.create_unit(service, building_id=self.fx["b2"].id, name="12")
        renamed = services.update_unit(service, created["id"], name="12A", expected_version=created["version"])
        self.assertEqual(renamed["name"], "12A")
        with self.assertRaises(ServiceError) as ctx:
            services.update_unit(service, created["id"], name="12B", expected_version=created["version"])
        self.assertEqual(ctx.exception.status_code, 409)
        with self.assertRaises(ServiceError):
            services.update_unit(service, created["id"], name="1")  # 同楼栋重名（fixture: 2栋1单元已存在）
        deleted = services.delete_unit(service, created["id"])
        self.assertIn("已删除", deleted["message"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
