"""运营模块测试：六个模块的状态机与越权用例（SQLite + Policy，不连 MySQL）。

两层覆盖：
1. **服务层直调**（``services_ops``）：19 个命令的状态机、幂等、乐观锁、权限、数据范围；
2. **HTTP 层**（``ops_routes.ops_bp`` 注册到真实 app）：五个页面 200、表单 POST 生效、
   未登录/无权限被挡、CSRF 缺失被拒。

用法::

    .venv\\Scripts\\python.exe -m unittest tests.test_ops_modules -v
    .venv\\Scripts\\python.exe tests\\test_ops_modules.py       # 也可直接跑
"""
from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB = ROOT / "artifacts" / "platform" / "test_ops.db"
DB.parent.mkdir(parents=True, exist_ok=True)
for suffix in ("", "-wal", "-shm"):
    target = Path(str(DB) + suffix)
    if target.exists():
        target.unlink()
os.environ["DATABASE_URL"] = "sqlite:///" + DB.as_posix()
os.environ["APP_ENV"] = "testing"
os.environ["COOKIE_SECURE"] = "0"

from sqlalchemy import select  # noqa: E402
from werkzeug.exceptions import Forbidden, HTTPException  # noqa: E402

import db as app_db  # noqa: E402
import models  # noqa: E402
import permissions  # noqa: E402
import queries_ops as qo  # noqa: E402
import seed_demo  # noqa: E402
import services  # noqa: E402
import services_ops as so  # noqa: E402

PASSWORD = "Demo-only-292!"


def _build_app():
    """构造一个最小 app：只装配 db + 我写的 ops 蓝图 + 错误处理。"""
    from flask import Flask

    import app as app_module
    from ops_routes import register_ops_routes

    application = app_module.create_app()
    register_ops_routes(application)
    return application


class OpsTestCase(unittest.TestCase):
    """公共装置：建库 + 造演示数据 + 建 app。"""

    @classmethod
    def setUpClass(cls):
        app_db.create_all()
        seed_demo.seed()
        cls.app = _build_app()
        cls.app.config.update(TESTING=True)
        cls.session = app_db.get_session()

    # -- 服务层取样 --------------------------------------------------------
    def policy(self, username: str) -> permissions.Policy:
        user = self.session.execute(select(models.User).where(models.User.username == username)).scalars().first()
        return permissions.Policy(self.session, user)

    def first_house(self):
        return self.session.execute(select(models.House).where(models.House.deleted.is_(False))).scalars().first()

    # -- HTTP 装置 ---------------------------------------------------------
    def client_for(self, username: str):
        client = self.app.test_client()
        token = self._csrf(client, "/login")
        response = client.post(
            "/login", data={"username": username, "password": PASSWORD, "csrf_token": token}, follow_redirects=False
        )
        self.assertIn(response.status_code, (302, 303), f"{username} 登录失败")
        return client

    @staticmethod
    def _csrf(client, path: str) -> str:
        text = client.get(path).get_data(as_text=True)
        match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', text)
        return match.group(1) if match else ""

    # ------------------------------------------------------------------
    # 1. 服务层：投诉状态机
    # ------------------------------------------------------------------
    def test_complaint_state_machine(self):
        service = self.policy("service01")
        house = self.first_house()
        created = so.create_complaint(service, house_id=house.id, content="单元门口堆物影响通行", category="公共设施")
        self.assertEqual(created["status"], 0)
        self.assertTrue(created.get("message"))

        assigned = so.assign_complaint(service, complaint_id=created["id"])
        self.assertEqual(assigned["status"], 1)

        # 已处理的投诉不能重复分配
        with self.assertRaises(services.ServiceError):
            so.assign_complaint(service, complaint_id=created["id"])

        handled = so.handle_complaint(service, complaint_id=created["id"], result="已联系业主清理")
        self.assertEqual(handled["status"], 1)

        closed = so.close_complaint(service, complaint_id=created["id"])
        self.assertEqual(closed["status"], 2)

        # 终态不可再取消
        with self.assertRaises(services.ServiceError):
            so.cancel_complaint(service, complaint_id=created["id"], reason="不办了")

    def test_complaint_cancel_requires_reason(self):
        service = self.policy("service01")
        house = self.first_house()
        created = so.create_complaint(service, house_id=house.id, content="测试取消原因必填校验")
        with self.assertRaises(services.ServiceError):
            so.cancel_complaint(service, complaint_id=created["id"], reason="")
        cancelled = so.cancel_complaint(service, complaint_id=created["id"], reason="业主自行处理")
        self.assertEqual(cancelled["status"], 3)

    def test_complaint_idempotent(self):
        service = self.policy("service01")
        house = self.first_house()
        payload = dict(house_id=house.id, content="幂等测试：重复提交只执行一次", category="other")
        first = so.create_complaint(service, request_key="ops-idem-1", **payload)
        second = so.create_complaint(service, request_key="ops-idem-1", **payload)
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second.get("replayed"))

    def test_complaint_optimistic_lock(self):
        service = self.policy("service01")
        house = self.first_house()
        created = so.create_complaint(service, house_id=house.id, content="乐观锁测试内容")
        with self.assertRaises(services.ServiceError) as ctx:
            so.assign_complaint(service, complaint_id=created["id"], expected_version=9999)
        self.assertEqual(ctx.exception.code, "conflict")

    # ------------------------------------------------------------------
    # 2. 服务层：访客状态机
    # ------------------------------------------------------------------
    def test_visitor_state_machine(self):
        service = self.policy("service01")
        house = self.first_house()
        visitor = so.register_visitor(service, house_id=house.id, name="测试访客", phone="13800001111", purpose="送快递")
        self.assertEqual(visitor["status"], 0)

        # 未进门不能直接离开
        with self.assertRaises(services.ServiceError):
            so.leave_visitor(service, visitor_id=visitor["id"])

        self.assertEqual(so.enter_visitor(service, visitor_id=visitor["id"])["status"], 1)
        self.assertEqual(so.leave_visitor(service, visitor_id=visitor["id"])["status"], 2)
        # 已离开不能再进门
        with self.assertRaises(services.ServiceError):
            so.enter_visitor(service, visitor_id=visitor["id"])

    def test_visitor_invalid_phone(self):
        service = self.policy("service01")
        house = self.first_house()
        with self.assertRaises(services.ServiceError):
            so.register_visitor(service, house_id=house.id, name="手机号错", phone="123")

    # ------------------------------------------------------------------
    # 3. 服务层：车辆 / 车位
    # ------------------------------------------------------------------
    def test_vehicle_and_parking_flow(self):
        service = self.policy("service01")
        house = self.first_house()
        plate = "京E12345"
        vehicle = so.create_vehicle(service, plate=plate, house_id=house.id, brand="测试品牌")
        self.assertEqual(vehicle["plate"], plate)

        with self.assertRaises(services.ServiceError):
            so.create_vehicle(service, plate=plate.lower(), house_id=house.id)

        space = self.session.execute(
            select(models.ParkingSpace).where(
                models.ParkingSpace.status == 0, models.ParkingSpace.deleted.is_(False)
            )
        ).scalars().first()
        if space is None:
            space = models.ParkingSpace(community_id=house.community_id, code="T-OPS-1", status=0)
            self.session.add(space)
            self.session.commit()

        assigned = so.assign_parking(service, space_id=space.id, vehicle_id=vehicle["id"])
        self.assertEqual(assigned["status"], 1)
        # 同一辆车不能再占第二个车位
        other = models.ParkingSpace(community_id=house.community_id, code="T-OPS-2", status=0)
        self.session.add(other)
        self.session.commit()
        with self.assertRaises(services.ServiceError):
            so.assign_parking(service, space_id=other.id, vehicle_id=vehicle["id"])

        self.assertEqual(so.release_parking(service, space_id=space.id)["status"], 0)
        # 重复释放被拒
        with self.assertRaises(services.ServiceError):
            so.release_parking(service, space_id=space.id)

        archived = so.archive_vehicle(service, vehicle_id=vehicle["id"], reason="测试归档")
        self.assertTrue(archived["ok"])

    def test_archive_vehicle_releases_space(self):
        service = self.policy("service01")
        house = self.first_house()
        vehicle = so.create_vehicle(service, plate="京F54321", house_id=house.id)
        space = models.ParkingSpace(community_id=house.community_id, code="T-OPS-3", status=0)
        self.session.add(space)
        self.session.commit()
        so.assign_parking(service, space_id=space.id, vehicle_id=vehicle["id"])
        result = so.archive_vehicle(service, vehicle_id=vehicle["id"], reason="车辆出售")
        self.assertIn("T-OPS-3", result.get("released") or [])
        self.session.refresh(space)
        self.assertEqual(space.status, 0)

    # ------------------------------------------------------------------
    # 4. 服务层：设备 / 巡检
    # ------------------------------------------------------------------
    def test_device_and_inspection_flow(self):
        admin = self.policy("admin")
        house = self.first_house()
        device = so.create_device(
            admin, name="测试水泵", community_id=house.community_id, building_id=house.building_id,
            category="供水", location="测试位置",
        )
        self.assertEqual(device["status"], 0)

        task = so.create_inspection(admin, device_id=device["id"])
        self.assertEqual(task["status"], 0)

        # 有未完成巡检时不能归档设备
        with self.assertRaises(services.ServiceError):
            so.archive_device(admin, device_id=device["id"], reason="报废")

        done = so.complete_inspection(admin, inspection_id=task["id"], result="运行正常")
        self.assertEqual(done["status"], 1)
        # 不能重复完成
        with self.assertRaises(services.ServiceError):
            so.complete_inspection(admin, inspection_id=task["id"], result="再来一次")

        self.assertTrue(so.archive_device(admin, device_id=device["id"], reason="报废")["ok"])

    def test_inspection_fault_marks_device(self):
        admin = self.policy("admin")
        house = self.first_house()
        device = so.create_device(
            admin, name="测试电梯", community_id=house.community_id, building_id=house.building_id, category="电梯"
        )
        task = so.create_inspection(admin, device_id=device["id"])
        fault = so.complete_inspection(admin, inspection_id=task["id"], result="有异响", has_fault=True)
        self.assertEqual(fault["status"], 2)
        self.session.refresh(self.session.get(models.Device, device["id"]))
        self.assertEqual(self.session.get(models.Device, device["id"]).status, 1)

    # ------------------------------------------------------------------
    # 4b. 维修工（scope=assigned）在设备巡检里能干什么
    #     回归：这些写操作曾经一律调 require_scope(write=True)，而 assigned 范围
    #     永远过不去那一档（它只认 all/community），于是三个维修工明明拿着
    #     device.write / inspection.write，在设备巡检页上点什么都 403。
    #     契约口径是「inspection.* + assignee scope」，所以改成对象级校验。
    # ------------------------------------------------------------------
    def _engineer_task(self, username="engineer02"):
        """经理给指定维修工派一条巡检任务，返回 (该维修工的 Policy, device_id, inspection_id)。"""
        manager = self.policy("manager01")
        engineer = self.policy(username)
        house = self.first_house()
        device = so.create_device(
            manager, name=f"测试设备-{username}", community_id=house.community_id,
            building_id=house.building_id, category="其他",
        )
        task = so.create_inspection(manager, device_id=device["id"], assignee_id=engineer.user_id)
        return engineer, device["id"], task["id"]

    def test_engineer_can_complete_own_inspection(self):
        """核心动线：维修工能完成指派给自己的巡检任务。"""
        engineer, _device_id, task_id = self._engineer_task("engineer01")
        done = so.complete_inspection(engineer, inspection_id=task_id, result="运行正常，无异响")
        self.assertEqual(done["status"], 1)
        row = self.session.get(models.Inspection, task_id)
        self.assertEqual(int(row.status), 1)
        self.assertEqual(row.result, "运行正常，无异响")

    def test_engineer_can_update_device_in_own_scope(self):
        """维修工能改自己巡检范围内那台设备（例如标成维修中）。"""
        engineer, device_id, _task_id = self._engineer_task("engineer02")
        out = so.update_device(engineer, device_id=device_id, location="1 栋负一层")
        self.assertIn("已更新", out["message"])

    def test_engineer_cannot_create_inspection(self):
        """派活（新建巡检）挂 inspection.assign：维修工只有 inspection.write，派不了任务。"""
        engineer, device_id, _task_id = self._engineer_task("engineer02")
        with self.assertRaises(HTTPException) as ctx:
            so.create_inspection(engineer, device_id=device_id, assignee_id=engineer.user_id)
        self.assertEqual(ctx.exception.code, 403)

    def test_manager_can_create_inspection(self):
        """管理岗有 inspection.assign，能派活（包括派给别人）。"""
        manager = self.policy("manager01")
        engineer = self.policy("engineer02")
        house = self.first_house()
        device = so.create_device(
            manager, name="经理新建的巡检设备", community_id=house.community_id,
            building_id=house.building_id, category="其他",
        )
        task = so.create_inspection(manager, device_id=device["id"], assignee_id=engineer.user_id)
        self.assertEqual(task["status"], 0)
        self.assertEqual(int(self.session.get(models.Inspection, task["id"]).assignee_id), engineer.user_id)

    def test_engineer_cannot_archive_device(self):
        """归档是 R3 破坏性台账动作：维修工能更新设备，但不能归档。"""
        engineer, device_id, task_id = self._engineer_task("engineer02")
        # 先了结待巡检，确保拦住他的是权限而不是「还有待巡检任务」
        so.complete_inspection(engineer, inspection_id=task_id, result="运行正常")
        with self.assertRaises(HTTPException) as ctx:
            so.archive_device(engineer, device_id=device_id, reason="越权归档")
        self.assertEqual(ctx.exception.code, 403)
        self.session.expire_all()
        self.assertIs(self.session.get(models.Device, device_id).deleted, False)

    def test_engineer_cannot_complete_others_inspection(self):
        """指派给别人的巡检任务：既看不到也动不了，且按 not_found 不泄漏存在性。"""
        manager = self.policy("manager01")
        house = self.first_house()
        device = so.create_device(
            manager, name="别人负责的设备", community_id=house.community_id,
            building_id=house.building_id, category="其他",
        )
        other = so.create_inspection(
            manager, device_id=device["id"], assignee_id=self.policy("engineer01").user_id
        )
        engineer, _device_id, _task_id = self._engineer_task("engineer03")
        with self.assertRaises(services.ServiceError) as ctx:
            so.complete_inspection(engineer, inspection_id=other["id"], result="越权测试")
        self.assertEqual(ctx.exception.code, "not_found")

    def test_engineer_cannot_create_device(self):
        """新增设备没有「既有对象」可做对象级校验，仍按小区级写范围管：维修工做不了。"""
        engineer, _device_id, _task_id = self._engineer_task("engineer02")
        house = self.first_house()
        with self.assertRaises(services.ServiceError) as ctx:
            so.create_device(
                engineer, name="工程私加的设备", community_id=house.community_id,
                building_id=house.building_id, category="其他",
            )
        self.assertEqual(ctx.exception.code, "not_found")

    def test_device_page_hides_entries_engineer_cannot_use(self):
        """页面上不能出现「点了必然 403」的入口。

        维修工（scope=assigned）看得到设备与巡检，但：新增设备是小区级台账动作、
        归档是 R3 破坏性动作、派活要 inspection.assign、转报修要 order.create——
        这四样他都没有，入口必须藏起来；「完成巡检」必须有。
        """
        self._engineer_task("engineer02")  # 先让维修工名下有设备与任务，断言才有意义
        html = self.client_for("engineer02").get("/devices").get_data(as_text=True)
        for needle, why in (
            ("/devices/create", "新增设备是小区级台账动作"),
            ("/devices/archive", "归档是 R3 破坏性动作，只有管理岗能做"),
            ("/inspections/create", "派活要 inspection.assign，维修工没有"),
            ("/to-order", "转报修要 order.create，维修工没有"),
        ):
            self.assertEqual(
                len(re.findall(re.escape(needle), html)), 0,
                f"维修工不该看到这个入口（{why}）：{needle}",
            )
        self.assertGreater(len(re.findall(r"/complete", html)), 0, "维修工应能看到自己任务的「完成巡检」")

        manager_html = self.client_for("manager01").get("/devices").get_data(as_text=True)
        for needle in ("/devices/create", "/devices/archive", "/inspections/create"):
            self.assertIn(needle, manager_html, f"管理岗应该看到入口：{needle}")

    def test_engineer_can_complete_inspection_through_http(self):
        """浏览器动线：维修工登录 → /devices → 点「标记完成」→ 302 且任务真的完成。"""
        _engineer, _device_id, task_id = self._engineer_task("engineer03")
        client = self.client_for("engineer03")
        page = client.get("/devices").get_data(as_text=True)
        self.assertIn(
            f"/inspections/{task_id}/complete", page,
            "指派给自己的待巡检任务应该出现「完成巡检」表单",
        )
        token = self._csrf(client, "/devices")
        response = client.post(
            f"/inspections/{task_id}/complete",
            data={"result": "HTTP 层提交：运行正常，无异响", "csrf_token": token},
            follow_redirects=False,
        )
        self.assertIn(response.status_code, (302, 303), "完成巡检应 302 回设备页")
        self.session.expire_all()
        row = self.session.get(models.Inspection, task_id)
        self.assertEqual(int(row.status), 1, "巡检任务应已变成「已完成」")
        self.assertIn("运行正常", row.result or "")

    def test_owner_vehicle_application_flow(self):
        """业主申请登记车辆（待审批）→ 经理审批通过 → 车辆生效。"""
        owner, house_id = self._owner_house_id()
        manager = self.policy("manager01")

        applied = so.apply_vehicle(owner, plate="沪Z88888", brand="比亚迪", house_id=house_id)
        self.assertIn("等待物业审批", applied["message"])
        self.assertEqual(int(applied["status"]), 2)
        self.session.expire_all()
        self.assertEqual(int(self.session.get(models.Vehicle, applied["id"]).status), 2)

        reviewed = so.review_vehicle(manager, vehicle_id=applied["id"], approve=True)
        self.assertIn("已通过", reviewed["message"])
        self.session.expire_all()
        self.assertEqual(int(self.session.get(models.Vehicle, applied["id"]).status), 0)

    def test_owner_vehicle_application_can_be_rejected(self):
        owner, house_id = self._owner_house_id()
        manager = self.policy("manager01")
        applied = so.apply_vehicle(owner, plate="沪Z77777", house_id=house_id)
        reviewed = so.review_vehicle(manager, vehicle_id=applied["id"], approve=False, reason="车牌信息不全")
        self.assertIn("已驳回", reviewed["message"])
        self.session.expire_all()
        row = self.session.get(models.Vehicle, applied["id"])
        self.assertTrue(row.deleted, "驳回后申请应归档")
        # 已经处理过的申请不能再审一次
        with self.assertRaises(services.ServiceError):
            so.review_vehicle(manager, vehicle_id=applied["id"], approve=True)

    def test_owner_parking_application_flow(self):
        """业主申请车位（待审批单）→ 经理指定空闲车位 → 分配生效、申请单归档。"""
        owner, house_id = self._owner_house_id()
        manager = self.policy("manager01")
        community_id = self.session.get(models.House, house_id).community_id

        applied = so.apply_parking(owner, house_id=house_id, note="家里两辆车")
        self.assertIn("等待物业审批", applied["message"])
        self.assertEqual(int(applied["status"]), 2)
        # 同一房屋不能重复提交
        with self.assertRaises(services.ServiceError):
            so.apply_parking(owner, house_id=house_id)

        space = models.ParkingSpace(community_id=community_id, code="T-001", status=0)
        self.session.add(space)
        self.session.flush()

        reviewed = so.review_parking(
            manager, space_id=applied["id"], approve=True, space_code="T-001"
        )
        self.assertIn("已通过车位申请", reviewed["message"])
        self.session.expire_all()
        assigned = self.session.get(models.ParkingSpace, space.id)
        self.assertEqual(int(assigned.status), 1)
        self.assertEqual(assigned.house_id, house_id)
        self.assertTrue(self.session.get(models.ParkingSpace, applied["id"]).deleted,
                        "审批通过后申请单应归档，避免混进车位台账")

    def test_owner_cannot_review_own_application(self):
        """审批是物业的权限：业主不能自己批自己的申请。"""
        owner, house_id = self._owner_house_id()
        applied = so.apply_vehicle(owner, plate="沪Z66666", house_id=house_id)
        with self.assertRaises(HTTPException):
            so.review_vehicle(owner, vehicle_id=applied["id"], approve=True)

    # ------------------------------------------------------------------
    # 4c. 业主（scope=self）的数据范围与自助能力
    #     回归：_cond_self 只实现了工单/房屋/人员/租赁/收款，后加的 bill / visitor /
    #     vehicle / parking_space / complaint 没有分支、直接落到 return false()，
    #     于是业主看不到自己房子的账单和访客——缴费、放行访客全都无从谈起。
    # ------------------------------------------------------------------
    def _owner_house_id(self):
        owner = self.policy("owner01")
        ids = sorted(services.my_house_ids(owner))
        self.assertTrue(ids, "业主应有本人相关的房屋")
        return owner, ids[0]

    def test_owner_sees_own_bills_visitors_vehicles(self):
        owner, house_id = self._owner_house_id()
        bills = self.session.execute(owner.query(models.Bill)).scalars().all()
        self.assertTrue(bills, "业主应能看到自己房子的账单")
        for bill in bills:
            self.assertIn(bill.house_id, services.my_house_ids(owner))
        for visitor in self.session.execute(owner.query(models.Visitor)).scalars().all():
            self.assertIn(visitor.house_id, services.my_house_ids(owner))
        for vehicle in self.session.execute(owner.query(models.Vehicle)).scalars().all():
            self.assertIn(vehicle.house_id, services.my_house_ids(owner))

    def test_owner_scope_does_not_leak_other_houses(self):
        owner = self.policy("owner01")
        mine = services.my_house_ids(owner)
        other_bills = self.session.execute(
            select(models.Bill).where(models.Bill.house_id.notin_(mine))
        ).scalars().all()
        self.assertTrue(other_bills, "数据集里应该有别人家的账单，否则这条断言没有意义")
        visible = self.session.execute(owner.query(models.Bill)).scalars().all()
        self.assertTrue(visible, "业主至少能看到自己房子的账单")
        for bill in visible:
            self.assertIn(bill.house_id, mine, f"越权看到别人家的账单 #{bill.id}")

    def test_owner_can_complain_about_neighbour_house(self):
        """投诉的 house 是「涉事房屋」：业主可以投诉本小区的邻居家（楼上装修噪音）。"""
        owner, house_id = self._owner_house_id()
        neighbour = self.session.execute(
            select(models.House).where(
                models.House.deleted.is_(False),
                models.House.id.notin_(services.my_house_ids(owner)),
                models.House.community_id == self.session.get(models.House, house_id).community_id,
            )
        ).scalars().first()
        self.assertIsNotNone(neighbour, "同小区应该还有别的房子")
        out = so.create_complaint(
            owner, house_id=neighbour.id,
            content="楼上装修噪音很大，晚上十点还在施工", category="noise",
        )
        self.assertIn(neighbour.full_name, out["message"])

    def test_owner_can_check_in_own_visitor(self):
        """业主放行本房间访客：待进 → 已进。"""
        owner, house_id = self._owner_house_id()
        visitor = so.register_visitor(
            owner, house_id=house_id, name="放行测试访客", phone="13900000009", purpose="走亲访友",
        )
        entered = so.enter_visitor(owner, visitor_id=visitor["id"])
        self.assertIn("已进门", entered["message"])
        self.session.expire_all()
        self.assertEqual(int(self.session.get(models.Visitor, visitor["id"]).status), 1)

    def test_owner_can_pay_own_bill(self):
        """业主自助缴费：自己能缴本户账单（没有 billing.collect，走对象级校验）。"""
        owner, house_id = self._owner_house_id()
        finance = self.policy("finance01")
        created = services.create_bill(
            finance, house_id=house_id, fee_type="物业费", amount=200, period="2026-11",
        )
        self.session.expire_all()
        out = services.collect_payment(owner, bill_id=created["id"], amount=200, method=1)
        self.assertIn("已入账", out["message"])
        self.session.expire_all()
        self.assertEqual(int(self.session.get(models.Bill, created["id"]).status), 2)

    def test_owner_cannot_pay_other_house_bill(self):
        """别人家房子的账单：业主连取都取不到（not_found），更缴不了。"""
        owner, _house_id = self._owner_house_id()
        mine = services.my_house_ids(owner)
        other = self.session.execute(
            select(models.Bill).where(models.Bill.house_id.notin_(mine))
        ).scalars().first()
        if other is None:
            finance = self.policy("finance01")
            far_house = self.session.execute(
                select(models.House).where(models.House.id.notin_(mine))
            ).scalars().first()
            if far_house is None:
                self.skipTest("数据集里没有别人家的房子")
            created = services.create_bill(
                finance, house_id=far_house.id, fee_type="物业费", amount=50, period="2026-11",
            )
            other_id = created["id"]
        else:
            other_id = other.id
        with self.assertRaises(HTTPException):
            services.collect_payment(owner, bill_id=other_id, amount=1, method=1)

    # ------------------------------------------------------------------
    # 5. 越权
    # ------------------------------------------------------------------
    def test_engineer_cannot_create_complaint(self):
        engineer = self.policy("engineer01")
        house = self.first_house()
        with self.assertRaises(HTTPException):
            so.create_complaint(engineer, house_id=house.id, content="工程不该能登记投诉")

    def test_engineer_cannot_create_vehicle(self):
        engineer = self.policy("engineer01")
        house = self.first_house()
        with self.assertRaises(HTTPException):
            so.create_vehicle(engineer, plate="京G00001", house_id=house.id)

    def test_owner_cannot_touch_other_community(self):
        """业主投诉对象的边界是「本小区」，不是「本人房屋」。

        口径变化记录：早期实现按「本人房屋」卡投诉对象，于是业主只能投诉自己，
        业务上说不通（投诉楼上装修噪音 = 举报邻居家）。现在放宽到本小区，
        但跨小区仍然被拒——这才是真正的越权边界。
        """
        owner = self.policy("owner01")
        owned = services.my_house_ids(owner)
        self.assertTrue(owned)
        my_communities = {h.community_id for h in self.session.execute(
            select(models.House).where(models.House.id.in_(owned))
        ).scalars().all()}
        other = self.session.execute(
            select(models.House).where(
                models.House.deleted.is_(False),
                models.House.community_id.notin_(my_communities),
            )
        ).scalars().first()
        if other is None:
            self.skipTest("数据集里只有一个小区，跨小区断言不成立")
        # 服务层会把「不在可见范围」转成通俗中文的 ServiceError；Policy 层面则是 403，两种都算通过
        with self.assertRaises((HTTPException, services.ServiceError)):
            so.create_complaint(owner, house_id=other.id, content="跨小区投诉应该被拒")

    def test_finance_cannot_handle_complaint(self):
        finance = self.policy("finance01")
        house = self.first_house()
        with self.assertRaises(HTTPException):
            so.create_complaint(finance, house_id=house.id, content="财务不该能登记投诉")

    # ------------------------------------------------------------------
    # 6. HTTP 层：页面与表单
    # ------------------------------------------------------------------
    def test_pages_render(self):
        for username, paths in (
            ("admin", ["/complaints", "/visitors", "/vehicles", "/devices", "/bills"]),
            ("service01", ["/complaints", "/visitors", "/vehicles"]),
            ("finance01", ["/bills"]),
        ):
            client = self.client_for(username)
            for path in paths:
                response = client.get(path)
                self.assertEqual(response.status_code, 200, f"{username} GET {path} -> {response.status_code}")
                text = response.get_data(as_text=True)
                self.assertNotIn("{{", text, f"{path} 有未渲染的模板标记")

    def test_complaint_page_has_real_data(self):
        client = self.client_for("admin")
        text = client.get("/complaints").get_data(as_text=True)
        self.assertIn("投诉", text)
        self.assertRegex(text, r"TS\d{8}\d{4}", "投诉列表应出现真实单号")

    def test_bills_page_shows_amounts(self):
        client = self.client_for("admin")
        text = client.get("/bills").get_data(as_text=True)
        self.assertRegex(text, r"ZD\d{6}\d{4}", "账单列表应出现真实单号")

    def test_post_complaint_through_http(self):
        client = self.client_for("service01")
        house = self.first_house()
        token = self._csrf(client, "/complaints")
        response = client.post(
            "/complaints",
            data={
                "house_id": house.id,
                "content": "HTTP 层提交的投诉：电梯按键失灵",
                "category": "elevator",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        self.assertIn(response.status_code, (302, 303), "投诉登记应 302 回列表")
        admin = self.client_for("admin")
        self.assertIn("电梯按键失灵", admin.get("/complaints").get_data(as_text=True))

    def test_post_without_csrf_rejected(self):
        client = self.client_for("service01")
        house = self.first_house()
        response = client.post(
            "/complaints",
            data={"house_id": house.id, "content": "缺 CSRF 应被拒", "category": "other"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 400, "缺 CSRF 的写请求必须被拒")

    def test_anonymous_redirected(self):
        client = self.app.test_client()
        for path in ("/complaints", "/visitors", "/vehicles", "/devices", "/bills"):
            response = client.get(path, follow_redirects=False)
            self.assertIn(response.status_code, (301, 302, 303, 401), f"{path} 未登录应被挡")

    def test_visitor_page_action_buttons_follow_state(self):
        service = self.policy("service01")
        house = self.first_house()
        visitor = so.register_visitor(service, house_id=house.id, name="按钮测试", phone="13800002222", purpose="测试")
        client = self.client_for("service01")
        text = client.get("/visitors").get_data(as_text=True)
        self.assertIn(f"/visitors/{visitor['id']}/check-in", text, "待进访客应出现「进门」按钮")
        self.assertNotIn(f"/visitors/{visitor['id']}/check-out", text, "待进访客不应出现「离开」按钮")

    def test_queries_ops_pagination_shape(self):
        admin = self.policy("admin")
        for label, fn in (
            ("投诉", qo.list_complaints),
            ("访客", qo.list_visitors),
            ("车辆", qo.list_vehicles),
            ("车位", qo.list_parking_spaces),
            ("设备", qo.list_devices),
            ("巡检", qo.list_inspections),
        ):
            data = fn(admin, page_size=5)
            for key in ("items", "total", "page", "pages"):
                self.assertIn(key, data, f"{label} 缺少字段 {key}")
            self.assertLessEqual(len(data["items"]), 5, f"{label} 分页 size 未生效")

    def test_queries_ops_status_filter(self):
        admin = self.policy("admin")
        pending = qo.list_complaints(admin, status="待处理")
        for item in pending["items"]:
            self.assertEqual(item["status"], 0)
        with self.assertRaises(HTTPException):
            qo.list_complaints(admin, status="不存在的状态")


if __name__ == "__main__":
    unittest.main(verbosity=2)
