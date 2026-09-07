"""Browser-level RBAC, button, URL, API and row-scope acceptance matrix."""
import json
import sys
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright
from sqlalchemy import select
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import create_app
from property_service import COMMANDS
from management_ui import ACTIONS, MODULE_CONFIG
from models import Building, Community, User, UserRole, UserScope
from permissions import ROLES, Policy

OUT_JSON = Path(__file__).with_name("role_browser_permission_matrix.json")
OUT_MD = Path(__file__).with_name("ROLE_BROWSER_PERMISSION_MATRIX.md")
PASSWORD = "Matrix-pass-123"


def seed(app):
    factory = app.extensions["db_session"]
    with factory() as db:
        if not db.get(Community, 2):
            db.add(Community(id=2, name="验收社区B", address="", phone=""))
        if not db.scalar(select(Building).where(Building.community_id == 1)):
            db.add(Building(community_id=1, name="验收A1", floors=10))
        if not db.scalar(select(Building).where(Building.community_id == 2)):
            db.add(Building(community_id=2, name="验收B1", floors=10))
        db.flush()
        for code in ROLES:
            username = "matrix-" + code
            user = db.scalar(select(User).where(User.username == username))
            if not user:
                user = User(username=username, password_hash=generate_password_hash(PASSWORD),
                            role=2 if code == "resident" else (0 if code == "superadmin" else 1),
                            real_name=code)
                db.add(user)
                db.flush()
            if not db.scalar(select(UserRole).where(UserRole.user_id == user.id, UserRole.role_code == code)):
                db.add(UserRole(user_id=user.id, role_code=code))
            if not db.scalar(select(UserScope).where(UserScope.user_id == user.id)):
                db.add(UserScope(user_id=user.id, kind="all" if code == "superadmin" else "community",
                                 community_id=None if code == "superadmin" else 1))
        db.commit()


def login(page, base, code):
    page.goto(base + "/auth/login", wait_until="domcontentloaded")
    page.locator("input[name='username']").fill("matrix-" + code)
    page.locator("input[name='password']").fill(PASSWORD)
    page.locator("form button").click()
    page.wait_for_url("**/dashboard")


def main():
    with tempfile.TemporaryDirectory(prefix="wuye-role-matrix-") as tmp:
        app = create_app({"TESTING": True, "DATABASE_URL": "sqlite+pysqlite:///" + str(Path(tmp) / "matrix.sqlite"),
                          "SECRET_KEY": "role-matrix-secret", "UPLOAD_FOLDER": str(Path(tmp) / "uploads"),
                          "BAILIAN_API_KEY": "", "DIFY_API_KEY": ""})
        seed(app)
        server = make_server("127.0.0.1", 5017, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:5017"
        rows = []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                for code in ROLES:
                    page = browser.new_page(viewport={"width": 1440, "height": 1000})
                    login(page, base, code)
                    csrf = page.locator("input[name='csrf_token']").first.input_value()
                    with app.extensions["db_session"]() as db:
                        actor = db.scalar(select(User).where(User.username == "matrix-" + code))
                        policy = Policy(db, actor)
                        superuser = policy.super
                    for module, spec in MODULE_CONFIG.items():
                        permission = spec[2]
                        allowed = policy.has(permission)
                        create = spec[4]
                        create_allowed = bool(create and policy.has(COMMANDS[create][0]))
                        response = page.goto(base + "/manage/" + module, wait_until="domcontentloaded")
                        page_status = response.status if response else 0
                        menu = page.locator(f"a[href='/manage/{module}']").count() > 0
                        button = page.locator(f"a[href*='/operations/{create}']").count() > 0 if create else False
                        detail = page.goto(base + "/manage/" + module + "/999999", wait_until="domcontentloaded")
                        direct_status = detail.status if detail else 0
                        api = page.evaluate("""async ({url, csrf}) => {
                            const r = await fetch(url, {headers: {'X-CSRF-Token': csrf}});
                            return {status: r.status, text: await r.text()};
                        }""", {"url": base + "/api/manage/" + module, "csrf": csrf})
                        api_status = api["status"]
                        mutation_status = None
                        if create:
                            mutation = page.evaluate("""async ({url, csrf}) => {
                                const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: JSON.stringify({data: {}})});
                                return r.status;
                            }""", {"url": base + "/api/business/" + create, "csrf": csrf})
                            mutation_status = int(mutation)
                        scope = page.evaluate("""async ({url, csrf}) => {
                            const r = await fetch(url, {headers: {'X-CSRF-Token': csrf}});
                            return {status: r.status, body: await r.json().catch(() => ({}))};
                        }""", {"url": base + "/api/manage/buildings?community_id=2", "csrf": csrf})
                        scope_total = scope["body"].get("total") if scope["status"] == 200 else None
                        expected_page = 200 if allowed else 403
                        expected_direct = 404 if allowed else 403
                        expected_api = 200 if allowed else 403
                        expected_mutation = None if not create else (range(400, 500) if create_allowed else (403,))
                        ok = (menu == allowed and page_status == expected_page and direct_status == expected_direct and
                              api_status == expected_api and (button == create_allowed) and
                              (mutation_status is None or mutation_status in expected_mutation) and
                              (scope_total is None or superuser or scope_total == 0))
                        rows.append({"role": code, "page": module, "menu": menu, "button": button,
                                     "direct_url": direct_status, "api": api_status, "mutation_api": mutation_status,
                                     "datascope_community_b_total": scope_total, "result": "PASS" if ok else "FAIL"})
                    page.close()
                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            app.extensions["db_engine"].dispose()
        payload = {"rows": rows, "total": len(rows), "failed": sum(r["result"] != "PASS" for r in rows),
                   "unauthorized_success": sum(r["result"] == "FAIL" for r in rows)}
        OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = ["# 角色浏览器权限矩阵", "", "| Role | Page | Menu | Button | Direct URL | API | DataScope | Result |",
                 "| --- | --- | --- | --- | ---: | ---: | ---: | --- |"]
        for r in rows:
            lines.append(f"| {r['role']} | {r['page']} | {'PASS' if r['menu'] else 'HIDDEN'} | {'PASS' if r['button'] else 'HIDDEN'} | {r['direct_url']} | {r['api']} | {r['datascope_community_b_total']} | {r['result']} |")
        lines += ["", f"总行数：{len(rows)}", f"失败：{payload['failed']}", f"unauthorized_success：{payload['unauthorized_success']}"]
        OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
