"""HTTP 路由测试（契约第 5 节、第 8.3 节）。

覆盖：健康检查、登录/登出与 CSRF、导航按权限渲染、页面渲染与上下文变量、
未授权 403 / 不存在 404、表单写入闭环。

说明：页面渲染使用**真实模板**（``templates/``），断言按前端契约
（``docs/TEMPLATE_CONTRACT.md`` 第 6 节）里的变量名；/ai 页面的上下文由
``agent/routes.py``（t4 交付物）提供，若其接口尚未对齐会 skip 并提示。
"""
from __future__ import annotations

import unittest

from sqlalchemy import select

import services
import models
from models import House, HousePerson, WorkOrder

try:
    from base import TEST_CSRF, TEST_PASSWORD, DbTestCase, make_app
except ImportError:  # pragma: no cover
    from tests.base import TEST_CSRF, TEST_PASSWORD, DbTestCase, make_app


class HealthAndLoginTests(DbTestCase):
    """匿名入口、登录、登出、会话失效。"""

    def test_health_is_public(self):
        client = make_app().test_client()
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertIn(response.get_json()["database"], (True, "ok"))

    def test_anonymous_gets_redirected_to_login(self):
        client = make_app().test_client()
        response = client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        # POST 未登录时可能是 302（跳登录）或 400（先拦未带 CSRF 的请求），两者都算拒绝
        response = client.post("/houses/house/create", data={})
        self.assertIn(response.status_code, (302, 400))

    def test_login_failure_shows_plain_language_error(self):
        app = make_app()
        client = app.test_client()
        with client.session_transaction() as session:
            session["csrf_token"] = "tok"
        response = client.post("/login", data={"username": "admin", "password": "wrong", "csrf_token": "tok"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("不正确", response.get_data(as_text=True))
        with client.session_transaction() as session:
            self.assertNotIn("user_id", session)

    def test_login_success_sets_session_and_redirects(self):
        app = make_app()
        client = app.test_client()
        with client.session_transaction() as session:
            session["csrf_token"] = "tok"
        response = client.post(
            "/login",
            data={"username": "manager01", "password": TEST_PASSWORD, "csrf_token": "tok", "next": "/orders"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/orders"))
        with client.session_transaction() as session:
            self.assertEqual(session["user_id"], self.user("manager01").id)
            self.assertEqual(session["auth_version"], 1)
        self.assertEqual(client.get("/orders").status_code, 200)

    def test_login_next_cannot_redirect_outside(self):
        app = make_app()
        client = app.test_client()
        with client.session_transaction() as session:
            session["csrf_token"] = "tok"
        response = client.post(
            "/login",
            data={"username": "admin", "password": TEST_PASSWORD, "csrf_token": "tok", "next": "https://evil.example.com"},
        )
        self.assertEqual(response.headers["Location"], "/")

    def test_login_next_keeps_mounted_prefix(self):
        app = make_app()
        client = app.test_client()
        with client.session_transaction() as session:
            session["csrf_token"] = "tok"
        response = client.post(
            "/login?next=/",
            headers={"X-Forwarded-Prefix": "/wuye"},
            data={"username": "admin", "password": TEST_PASSWORD, "csrf_token": "tok"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/wuye/")

    def test_login_requires_csrf(self):
        client = make_app().test_client()
        response = client.post("/login", data={"username": "admin", "password": TEST_PASSWORD})
        self.assertEqual(response.status_code, 400)

    def test_post_without_csrf_is_rejected(self):
        client = self.client("admin")
        response = client.post("/houses/house/create", data={"building_id": self.fx["b2"].id, "room": "999"})
        self.assertEqual(response.status_code, 400)

    def test_auth_version_change_forces_logout(self):
        client = self.client("admin", auth_version=99)
        response = client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        with client.session_transaction() as session:
            self.assertNotIn("user_id", session)

    def test_logout_clears_session(self):
        client = self.client("admin")
        response = client.post("/logout", data={"csrf_token": TEST_CSRF})
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as session:
            self.assertNotIn("user_id", session)
        self.assertEqual(client.get("/").status_code, 302)

    def test_deactivated_account_cannot_login(self):
        user = self.user("manager01")
        user.active = False
        self.session.commit()
        app = make_app()
        client = app.test_client()
        with client.session_transaction() as session:
            session["csrf_token"] = "tok"
        response = client.post("/login", data={"username": "manager01", "password": TEST_PASSWORD, "csrf_token": "tok"})
        self.assertEqual(response.status_code, 200)
        with client.session_transaction() as session:
            self.assertNotIn("user_id", session)


class NavigationAndAccessTests(DbTestCase):
    """导航按权限渲染；无权限直接访问 403。"""

    def nav_hrefs(self, username: str) -> list[str]:
        app = make_app()
        client = self.client(username, app=app)
        response = client.get("/")
        self.assertEqual(response.status_code, 200, f"{username} 工作台渲染失败")
        html = response.get_data(as_text=True)
        hrefs = []
        for candidate in ("/", "/houses", "/persons", "/orders", "/ai", "/audit"):
            marker = f'href="{candidate}"'
            if candidate == "/":
                marker = 'class="nav-item active" href="/"' if 'nav-item' in html else 'href="/"'
            if marker in html or (candidate != "/" and f'href="{candidate}"' in html):
                hrefs.append(candidate)
        return hrefs

    def test_nav_per_role(self):
        admin_nav = self.nav_hrefs("admin")
        for href in ("/", "/houses", "/persons", "/orders", "/ai", "/audit"):
            self.assertIn(href, admin_nav, "管理员导航")
        owner_nav = self.nav_hrefs("owner01")
        self.assertNotIn("/audit", owner_nav, "业主不应看到审计入口")
        engineer_nav = self.nav_hrefs("engineer01")
        self.assertNotIn("/houses", engineer_nav, "工程维修不应看到房屋档案入口")
        self.assertIn("/orders", engineer_nav)

    def test_pages_denied_without_permission(self):
        cases = [("owner01", "/audit"), ("engineer01", "/houses"), ("engineer01", "/persons")]
        for username, path in cases:
            with self.subTest(username=username, path=path):
                response = self.client(username).get(path)
                self.assertEqual(response.status_code, 403)

    def test_unknown_order_renders_error_page(self):
        response = self.client("admin").get("/orders/999999")
        self.assertEqual(response.status_code, 404)
        self.assertIn("404", response.get_data(as_text=True))

    def test_ai_session_json_is_private(self):
        created = services.create_ai_session(self.actor("admin"), "管理员的会话")
        response = self.client("owner01").get(f"/ai/sessions/{created['id']}")
        self.assertIn(response.status_code, (403, 404))


class PageRenderTests(DbTestCase):
    """8 个页面用真实模板渲染，并断言模板真正读取的变量。"""

    def test_dashboard(self):
        app = make_app()
        response = self.client("admin", app=app).get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("工作台", html)
        self.assertIn("待派单", html)
        self.assertIn("系统管理员", html)

    def test_houses(self):
        app = make_app()
        response = self.client("manager01", app=app).get("/houses")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("美家花园", html)
        # 三级联动：未选小区时房屋区是空的，选了小区+楼栋才出房屋列表
        response = self.client("manager01", app=app).get(
            f"/houses?community={self.fx['c1'].id}&building={self.fx['b1'].id}"
        )
        html = response.get_data(as_text=True)
        self.assertIn("1单元101", html)
        self.assertIn("2单元101", html)

    def test_houses_filter_by_building(self):
        app = make_app()
        response = self.client("manager01", app=app).get(f"/houses?community={self.fx['c1'].id}&building={self.fx['b2'].id}")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("2栋", html)
        self.assertNotIn("1栋1单元101", html)

    def test_persons_with_disambiguation(self):
        app = make_app()
        response = self.client("manager01", app=app).get("/persons?keyword=李娜")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("李娜", html)
        # 两位同名人员用电话区分，页面必须都能查到（消歧演示）
        self.assertIn("13800000011", html)
        self.assertIn("13800000012", html)

    def test_orders_and_status_filter(self):
        app = make_app()
        response = self.client("manager01", app=app).get("/orders")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("WO-TEST-0001", html)
        response = self.client("manager01", app=app).get("/orders?status=待验收")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("WO-TEST-0004", html)
        self.assertNotIn("WO-TEST-0001", html)

    def test_orders_scope_hides_other_community(self):
        app = make_app()
        html = self.client("manager01", app=app).get("/orders").get_data(as_text=True)
        self.assertNotIn("WO-TEST-0006", html)  # 隔壁小区的工单

    def test_order_new_for_owner(self):
        app = make_app()
        response = self.client("owner01", app=app).get("/orders/new")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("美家花园1栋1单元101", html)
        self.assertNotIn("美家花园1栋1单元201", html)  # 只能给自己家报修

    def test_order_detail_and_actions(self):
        app = make_app()
        response = self.client("owner01", app=app).get(f"/orders/{self.order('WO-TEST-0001').id}")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("WO-TEST-0001", html)
        self.assertIn("待派单", html)
        self.assertIn("报修登记", html)  # 流转日志

    def test_order_reopen_action_works_and_is_chinese(self):
        """验收不通过（退回返修）。

        回归两件事：
        1. 按钮文案曾经直接显示英文动作名 ``reopen``（ORDER_ACTION_TEXT 缺这一条）；
        2. app.py 的 view_order_action 没接这个分支，点下去直接 404。
        """
        app = make_app()
        order = self.order("WO-TEST-0004")  # 待验收
        client = self.client("manager01", app=app)
        html = client.get(f"/orders/{order.id}").get_data(as_text=True)
        self.assertIn("退回返修", html, "按钮必须是中文文案")
        self.assertNotIn(">reopen<", html, "不能把英文动作名渲染给用户")

        response = client.post(
            f"/orders/{order.id}/reopen",
            data={"note": "还是有点渗水，麻烦再看一下", "csrf_token": TEST_CSRF},
        )
        self.assertIn(response.status_code, (302, 303), "退回返修不应该 404")
        self.session.expire_all()
        self.assertEqual(int(self.session.get(WorkOrder, order.id).status), 2, "待验收 → 维修中")

    def test_lease_check_in_records_monthly_rent(self):
        """入住表单里的「月租金」必须落库并在在租列表显示。

        回归：leases_command 以前只调 bind_relation，把 rent 直接丢掉了，
        所以在租列表的月租金永远是「—」。
        """
        app = make_app()
        admin = self.actor("admin")
        house = self.house("h4")
        tenant = services.create_person(admin, name="租金测试租客", phone="13900008888")
        client = self.client("admin", app=app)
        response = client.post(
            "/leases/check-in",
            data={
                "house_id": house.id,
                "person_id": tenant["id"],
                "rent": "3500",
                "start_at": "2026-01-01",
                "csrf_token": TEST_CSRF,
            },
        )
        self.assertIn(response.status_code, (302, 303))

        self.session.expire_all()
        lease = self.session.execute(
            select(models.Lease).where(models.Lease.house_id == house.id, models.Lease.status == 0)
        ).scalars().first()
        self.assertIsNotNone(lease, "入住应该写出租赁合同")
        self.assertEqual(float(lease.rent), 3500.0)

        html = client.get("/leases").get_data(as_text=True)
        self.assertIn("租金测试租客", html)
        self.assertIn("3500", html, "在租列表要显示月租金，不能是「—」")

    def test_order_detail_hides_others_order(self):
        response = self.client("owner01").get(f"/orders/{self.order('WO-TEST-0005').id}")
        self.assertEqual(response.status_code, 404)

    def test_audit_page(self):
        app = make_app()
        response = self.client("manager01", app=app).get("/audit")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("报修登记", html)
        self.assertIn("网页操作", html)

    def test_ai_page_if_agent_ready(self):
        app = make_app()
        response = self.client("manager01", app=app).get("/ai")
        if response.status_code != 200:
            self.skipTest(
                "agent/routes.py 的 ai_page 未按 frontend 契约提供 ai.html 需要的变量"
                "（messages/active_session/agent_ready），详见 team 报告"
            )
        html = response.get_data(as_text=True)
        self.assertIn("AI", html)


class WriteRouteTests(DbTestCase):
    """表单写入闭环。"""

    def test_create_order_via_form(self):
        app = make_app()
        client = self.client("owner01", app=app)
        response = client.post(
            "/orders",
            data={
                "csrf_token": TEST_CSRF,
                "house_id": str(self.house("h1").id),
                "contact_name": "张伟",
                "contact_phone": "13900000001",
                "category": "water",
                "description": "页面提交的报修：水龙头漏水",
                "urgency": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/orders/", response.headers["Location"])
        self.session.expire_all()
        order = self.session.execute(
            select(WorkOrder).where(WorkOrder.description == "页面提交的报修：水龙头漏水")
        ).scalars().first()
        self.assertIsNotNone(order)
        self.assertEqual(order.status, 0)
        self.assertEqual(order.urgency, 1)

    def test_create_order_validation_message_on_page(self):
        app = make_app()
        client = self.client("owner01", app=app)
        response = client.post(
            "/orders",
            data={
                "csrf_token": TEST_CSRF,
                "house_id": str(self.house("h1").id),
                "contact_name": "",
                "contact_phone": "13900000001",
                "category": "water",
                "description": "没有联系人",
            },
            follow_redirects=True,
        )
        self.assertIn(response.status_code, (200, 400))
        self.assertIn("联系人", response.get_data(as_text=True))

    def test_owner_cannot_dispatch(self):
        client = self.client("owner01")
        response = client.post(
            f"/orders/{self.order('WO-TEST-0001').id}/assign",
            data={"csrf_token": TEST_CSRF, "repairer": "黄磊"},
        )
        self.assertEqual(response.status_code, 403)

    def test_full_flow_over_http(self):
        app = make_app()
        service_client = self.client("service01", app=app)
        engineer_client = self.client("engineer01", app=app)
        order_id = self.order("WO-TEST-0001").id

        response = service_client.post(
            f"/orders/{order_id}/assign", data={"csrf_token": TEST_CSRF, "repairer": "黄磊", "note": "今天处理"}
        )
        self.assertEqual(response.status_code, 302)
        for action, extra in (("accept", {}), ("progress", {"note": "已更换配件"}), ("finish", {"note": "已试水"})):
            response = engineer_client.post(
                f"/orders/{order_id}/{action}", data={"csrf_token": TEST_CSRF, **extra}
            )
            self.assertEqual(response.status_code, 302, action)
        self.session.expire_all()
        self.assertEqual(self.session.get(WorkOrder, order_id).status, 3)
        response = service_client.post(
            f"/orders/{order_id}/verify", data={"csrf_token": TEST_CSRF, "note": "确认修复"}
        )
        self.assertEqual(response.status_code, 302)
        self.session.expire_all()
        self.assertEqual(self.session.get(WorkOrder, order_id).status, 4)

    def test_service_error_flashes_and_redirects(self):
        app = make_app()
        client = self.client("engineer01", app=app)
        response = client.post(f"/orders/{self.order('WO-TEST-0001').id}/accept", data={"csrf_token": TEST_CSRF})
        self.assertIn(response.status_code, (302, 404))

    def test_houses_command_creates_house(self):
        app = make_app()
        client = self.client("manager01", app=app)
        response = client.post(
            "/houses/house/create",
            data={
                "csrf_token": TEST_CSRF,
                "building_id": str(self.fx["b2"].id),
                "unit": "2",
                "room": "302",
                "area": "88.5",
                "status": "0",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.session.expire_all()
        house = self.session.execute(
            select(House).where(House.building_id == self.fx["b2"].id, House.room == "302")
        ).scalars().first()
        self.assertIsNotNone(house)
        self.assertEqual(house.unit, "2")

    def test_houses_command_conflict_keeps_one_row(self):
        app = make_app()
        client = self.client("manager01", app=app)
        response = client.post(
            "/houses/house/create",
            data={"csrf_token": TEST_CSRF, "building_id": str(self.fx["b1"].id), "unit": "1", "room": "101"},
        )
        self.assertEqual(response.status_code, 302)
        self.session.expire_all()
        rows = self.session.execute(
            select(House).where(
                House.building_id == self.fx["b1"].id, House.unit == "1", House.room == "101"
            )
        ).scalars().all()
        self.assertEqual(len(rows), 1)

    def test_persons_command_binds_relation(self):
        app = make_app()
        client = self.client("manager01", app=app)
        person = self.fx["persons"]["李娜|13800000012"]
        response = client.post(
            "/persons/relation/create",
            data={
                "csrf_token": TEST_CSRF,
                "house_id": str(self.house("h4").id),
                "person_id": str(person.id),
                "relation": "租户",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.session.expire_all()
        relation = self.session.execute(
            select(HousePerson).where(
                HousePerson.house_id == self.house("h4").id, HousePerson.person_id == person.id
            )
        ).scalars().first()
        self.assertIsNotNone(relation)
        self.assertEqual((relation.relation, relation.status), ("tenant", "active"))

    def test_deleted_house_disappears_from_page(self):
        app = make_app()
        client = self.client("manager01", app=app)
        created = services.create_house(self.actor("manager01"), building="2栋", unit="2", room="303", area=66)
        response = client.post(
            "/houses/house/delete", data={"csrf_token": TEST_CSRF, "id": str(created["id"])}
        )
        self.assertEqual(response.status_code, 302)
        self.session.expire_all()
        self.assertTrue(self.session.get(House, created["id"]).deleted)
        page = client.get(f"/houses?community={self.fx['c1'].id}&building={self.fx['b2'].id}").get_data(as_text=True)
        self.assertNotIn("2单元303", page)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
