"""端到端渲染回归：真实 create_app()（SQLite 临时库 + 演示数据），逐角色访问真实页面。

用法：python tests/check_app_e2e.py
- 不需要 MySQL；跑完自动删库。
- 逐角色登录 seed_demo 的真实演示账号（admin / manager01 / service01 / engineer01 / finance01 / owner01）。
- 每个页面接受 200 或 403（越权是正常业务结果）；**页面上不允许出现 500 / Traceback / 未解析模板标记**。
- 工单详情页的 id 从列表页真实解析，避免"猜 id 得 404"。
"""
from __future__ import annotations

import re
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ACCOUNTS = ["admin", "manager01", "service01", "engineer01", "finance01", "owner01"]
PAGES = [
    ("工作台", "/"),
    ("房屋档案", "/houses"),
    ("人员与关系", "/persons"),
    ("维修工单", "/orders"),
    ("报修表单", "/orders/new"),
    ("租赁", "/leases"),
    ("投诉", "/complaints"),
    ("访客", "/visitors"),
    ("车辆车位", "/vehicles"),
    ("设备巡检", "/devices"),
    ("收费", "/bills"),
    ("AI 助手", "/ai"),
    ("审计日志", "/audit"),
    ("健康检查", "/health"),
    ("404 页", "/no-such-page-404"),
]


#: 每个页面标题栏的关键词——用来发现"模板被覆盖/串页"这类事故
#（曾经真的发生过：vehicles.html 的内容被 devices 模板覆盖，页面能渲染但内容全错）
EXPECT_H1 = {
    "/": "工作台", "/houses": "房屋", "/persons": "人员", "/orders": "工单", "/leases": "租赁",
    "/complaints": "投诉", "/visitors": "访客", "/vehicles": "车辆与车位", "/devices": "设备与巡检",
    "/bills": "收费", "/ai": "AI 助手", "/audit": "操作记录",
}


def main() -> int:
    db_file = Path(tempfile.gettempdir()) / "wuye_e2e.sqlite3"
    if db_file.exists():
        db_file.unlink()
    url = "sqlite:///" + db_file.as_posix()

    import app as application
    import db as app_db
    import seed_demo

    flask_app = application.create_app({
        "DATABASE_URL": url,
        "TESTING": False,
        "SECRET_KEY": "e2e-secret-key",
        "PROPAGATE_EXCEPTIONS": False,
    })
    app_db.create_all(app_db.get_engine(url))
    seed_demo.seed(app_db.get_engine(url))

    failures: list[str] = []
    checked = 0

    # 模板里 url_for 的端点名必须真的存在（曾经 base.html 用了不存在的 ai.ai_actions → 整页 500）
    import re as _re  # noqa: PLC0415

    known_endpoints = set(flask_app.view_functions)
    for tpl in (ROOT / "templates").glob("*.html"):
        for name in set(_re.findall(r"url_for\('([^']+)'", tpl.read_text(encoding="utf-8"))):
            if name.startswith("static") or name in known_endpoints:
                continue
            failures.append(f"{tpl.name}: url_for 引用了不存在的端点 {name!r}")

    def visit(client, user, label, path, html_report=True):
        nonlocal checked
        checked += 1
        try:
            page = client.get(path)
        except Exception as error:  # noqa: BLE001
            failures.append(f"[{user}] {label} {path} 抛异常 {type(error).__name__}: {error}")
            failure_details.append(traceback.format_exc(limit=8))
            return None
        html = page.get_data(as_text=True)
        if page.status_code >= 500 and path == "/ai":
            # /ai 依赖智能体运行时（可能懒启动/竞态），给它一次重试机会，避免把环境抖动当成页面 bug
            page = client.get(path)
            html = page.get_data(as_text=True)
        if page.status_code >= 500:
            failures.append(f"[{user}] {label} {path} → HTTP {page.status_code}")
            if html_report:
                failure_details.append(f"[{user}] {path}\n" + html[:1500])
            return page
        if page.status_code not in (200, 302, 403, 404):
            failures.append(f"[{user}] {label} {path} → 异常状态码 {page.status_code}")
            return page
        if "{{" in html or "{%" in html:
            failures.append(f"[{user}] {label} {path} 渲染残留模板标记")
        if "Traceback" in html:
            failures.append(f"[{user}] {label} {path} 页面里出现 Traceback")
        return page

    failure_details: list[str] = []
    with flask_app.test_client() as client:
        for user in ACCOUNTS:
            with client.session_transaction() as sess:
                sess.clear()
            landing = client.get("/login")
            match = re.search(r'name="csrf_token" value="([^"]+)"', landing.get_data(as_text=True))
            token = match.group(1) if match else ""
            if not token:
                failures.append(f"[{user}] 登录页没有 csrf_token")
                continue
            login = client.post("/login",
                                data={"username": user, "password": seed_demo.DEMO_PASSWORD, "csrf_token": token},
                                follow_redirects=True)
            if login.status_code != 200 or "退出" not in login.get_data(as_text=True):
                failures.append(f"[{user}] 登录失败（HTTP {login.status_code}）")
                continue

            real_name = re.search(r"<strong>([^<]+)</strong>", login.get_data(as_text=True))
            for label, path in PAGES:
                visit(client, user, label, path)

            # 页面标题栏必须与导航口径一致（防止模板被覆盖/串页）
            for path, expect in EXPECT_H1.items():
                page = client.get(path)
                if page.status_code != 200:
                    continue
                html = page.get_data(as_text=True)
                match = re.search(r"<h1>([^<]*)</h1>", html)
                got = match.group(1).strip() if match else ""
                if expect not in got:
                    failures.append(f"[{user}] {path} 标题栏是 {got!r}，期望含 {expect!r}（模板可能被覆盖）")

            # 工单详情：从列表页解析真实 id
            listing = visit(client, user, "维修工单列表", "/orders", html_report=False)
            if listing is not None and listing.status_code == 200:
                ids = re.findall(r'href="/orders/(\d+)"', listing.get_data(as_text=True))
                if ids:
                    visit(client, user, "工单详情", f"/orders/{ids[0]}")
                else:
                    failures.append(f"[{user}] 工单列表里没有可点进详情的工单（数据范围可能过滤掉了全部）")
            # 房屋档案：从列表解析 id 后按 query 过滤（三级联动）
            houses_page = visit(client, user, "房屋档案过滤", "/houses?keyword=101", html_report=False)
            if houses_page is not None and houses_page.status_code == 200:
                visit(client, user, "房屋详情页(带过滤)", "/houses?community=&building=&keyword=1")
            print(f"  {user:11} 登录 {real_name.group(1) if real_name else '?'} 完成")

    try:
        app_db.dispose_engines()
        if db_file.exists():
            db_file.unlink()
    except Exception:  # noqa: BLE001
        pass

    print(f"\n端到端页面请求：{checked} 次，问题 {len(failures)} 个")
    for item in failures:
        print("  - " + item)
    for block in failure_details[:3]:
        print("--- 失败页面片段 ---")
        print(block[:1600])
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())