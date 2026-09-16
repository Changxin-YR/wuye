"""端到端验收脚本（独立于 unittest discover，可重复执行）。

用途：用**真实 Flask 应用 + 真实模板 + 真实种子数据**跑一遍演示动线，抓出
「页面返回 200 但内容其实是空的 / 字段名对不上」这类只有联调才能发现的问题。

覆盖：
1. 页面渲染：5 个角色 × 全部页面，断言 200 / 越权被拒，并断言页面里出现**种子数据里的真实文本**
   （小区名、工单号、人员姓名），空渲染会被判为失败。
2. 登录/登出：正确口令、错误口令、CSRF 缺失被拒。
3. 工单闭环：报修 → 派单 → 接单 → 进展 → 完工 → 验收 → 评价 的真实 POST，并回查状态与审计页。
4. AI 页面：/ai 页面 200、/ai/chat 返回 SSE 帧（agent.bridge 未就绪时也要返回可读提示）。
5. 未登录被挡、404 渲染 error.html。

结果写入 ``artifacts/platform/acceptance_report.json``，控制台打印 [OK]/[FAIL] 明细。

用法::

    .venv\\Scripts\\python.exe tests\\acceptance_e2e.py

默认使用 ``artifacts/platform/acceptance.db``（SQLite，每次重建），不连 MySQL。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ARTIFACTS = ROOT / "artifacts" / "platform"
DB_PATH = ARTIFACTS / "acceptance.db"

ARTIFACTS.mkdir(parents=True, exist_ok=True)

#: 默认用 SQLite 临时库快速回归；设 WUYE_ACCEPT_DB=mysql（或直接给 DATABASE_URL）就跑真实 MySQL。
#: captain 口径：所有验收必须在 MySQL 上跑一遍，SQLite 全绿只作为快速回归。
_MODE = (os.environ.get("WUYE_ACCEPT_DB") or "").strip().lower()
if _MODE in ("mysql", "env") or (os.environ.get("DATABASE_URL") or "").startswith("mysql"):
    # 用 .env 里的真实连接串（不再覆盖 DATABASE_URL），只把它暴露出来供打印/报告使用
    os.environ.setdefault("APP_ENV", "development")
    os.environ["COOKIE_SECURE"] = "0"
    if not os.environ.get("DATABASE_URL"):
        sys.path.insert(0, str(ROOT))
        import config as _cfg

        os.environ["DATABASE_URL"] = _cfg.get_config().DATABASE_URL
    USING_MYSQL = True
else:
    for suffix in ("", "-wal", "-shm"):
        target = Path(str(DB_PATH) + suffix)
        if target.exists():
            target.unlink()
    os.environ["DATABASE_URL"] = "sqlite:///" + DB_PATH.as_posix()
    os.environ["APP_ENV"] = "testing"
    os.environ["COOKIE_SECURE"] = "0"
    USING_MYSQL = False
os.environ.pop("WUYE_LIVE_AGENT", None)

DEMO_PASSWORD = "Demo-only-292!"
ROLES = ["admin", "manager01", "service01", "engineer01", "owner01"]

results: list[dict] = []
console: list[str] = []


def record(ok: bool, section: str, name: str, detail: str = "") -> bool:
    results.append({"ok": bool(ok), "section": section, "name": name, "detail": str(detail)[:400]})
    line = f"{'[OK]  ' if ok else '[FAIL]'} {section} · {name}"
    if detail and not ok:
        line += f"\n         └─ {str(detail)[:300]}"
    console.append(line)
    print(line, flush=True)
    return ok


def body_text(response) -> str:
    return response.get_data(as_text=True)


def csrf_of(client, path: str = "/login") -> str:
    """从页面隐藏域里取 CSRF token。"""
    text = body_text(client.get(path))
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', text)
    return match.group(1) if match else ""


def login(client, username: str, password: str = DEMO_PASSWORD) -> bool:
    token = csrf_of(client)
    response = client.post(
        "/login", data={"username": username, "password": password, "csrf_token": token}, follow_redirects=False
    )
    return response.status_code in (302, 303)


class _Boom:
    """异常的替身：让后续断言按 500 处理，验收脚本不因单个页面炸掉。"""

    status_code = 500
    headers = {"Content-Type": "text/html"}

    def __init__(self, exc):
        self.exc = exc

    def get_data(self, as_text=False):
        return f"<exception>{type(self.exc).__name__}: {self.exc}</exception>"


def safe_get(client, path, **kwargs):
    """GET 一个页面：异常当成 500 结果返回。"""
    try:
        return client.get(path, **kwargs)
    except Exception as exc:  # noqa: BLE001
        return _Boom(exc)


def safe_post(client, path, data):
    try:
        return client.post(path, data=data, follow_redirects=False)
    except Exception as exc:  # noqa: BLE001
        return _Boom(exc)


def main() -> int:
    from sqlalchemy import select

    import app as app_module
    import db as app_db
    import models as models_module
    import permissions as perms_module
    import queries as q
    import seed_demo

    started = time.time()
    console.append(f"端到端验收开始 {datetime.now():%Y-%m-%d %H:%M:%S}")
    console.append(f"数据库：{os.environ['DATABASE_URL']}")

    print(f"\n=== 建表 + 写入演示数据 + 自检（数据库：{'MySQL' if USING_MYSQL else 'SQLite'}） ===")
    app_db.create_all()
    seed_demo.seed()   # 幂等：MySQL 上复用已有演示库，不会清数据
    record(bool(seed_demo.check()), "seed", "seed_demo.check() 全绿")

    application = app_module.create_app()
    application.config.update(TESTING=True)

    def policy_for(username: str):
        """在应用上下文里构造某账号的 Policy（仅验收脚本内部取样用）。"""
        with application.app_context():
            user = app_db.get_session().execute(
                select(models_module.User).where(models_module.User.username == username)
            ).scalars().first()
            return perms_module.Policy(app_db.get_session(), user)

    role_perms = {username: set(policy_for(username).permissions) for username in ROLES}

    print("\n=== 登录 / 登出 / CSRF ===")
    with application.test_client() as anon:
        page = safe_get(anon, "/login")
        record(page.status_code == 200, "auth", "/login 可访问", f"status={page.status_code}")
        record('name="csrf_token"' in body_text(page), "auth", "登录页含 CSRF 隐藏域")

    with application.test_client() as c:
        bad_token = csrf_of(c)
        bad = c.post("/login", data={"username": "admin", "password": "wrong", "csrf_token": bad_token}, follow_redirects=False)
        record(
            bad.status_code == 200 and ("不正确" in body_text(bad) or "密码" in body_text(bad)),
            "auth",
            "错误口令被拒并提示中文",
            f"status={bad.status_code}",
        )

    with application.test_client() as c:
        no_csrf = c.post("/login", data={"username": "admin", "password": DEMO_PASSWORD}, follow_redirects=False)
        record(
            no_csrf.status_code not in (302, 303),
            "auth",
            "缺 CSRF 的登录 POST 被拒",
            f"status={no_csrf.status_code}",
        )

    clients: dict[str, object] = {}
    for username in ROLES:
        c = application.test_client()
        clients[username] = c
        record(login(c, username), "auth", f"{username} 演示口令登录成功（302）")

    with application.test_client() as c:
        login(c, "admin")
        home = safe_get(c, "/")
        record(home.status_code == 200, "auth", "登录后工作台 200", f"status={home.status_code}")
        record(DEMO_PASSWORD not in body_text(home), "auth", "页面不回显口令")

    print("\n=== 页面渲染（角色 × 页面） ===")
    pages = {
        "/": (None, ["工作台", "待派单"]),
        "/houses": ("house.read", ["云邻花园"]),
        "/persons": ("person.read", ["人员"]),
        "/orders": ("order.read", ["WO"]),
        "/orders/new": ("order.create", ["报修"]),
        "/audit": ("audit.read", []),
        # /ai 必须渲染出聊天界面（不是空壳）：容器 + 输入框 + 隐藏 csrf_token
        "/ai": (None, ["ai-layout", "chat-input", "csrf_token"]),
    }
    for username in ROLES:
        c = clients[username]
        for path, (permission, needles) in pages.items():
            response = safe_get(c, path)
            allowed = permission is None or permission in role_perms[username]
            if not allowed:
                record(
                    response.status_code in (403, 302),
                    "render",
                    f"{username} GET {path} 越权被拒",
                    f"status={response.status_code}",
                )
                continue
            text = body_text(response)
            ok = response.status_code == 200
            detail = f"status={response.status_code}"
            if ok:
                missing = [needle for needle in needles if needle not in text]
                if missing:
                    ok = False
                    detail = f"页面 200 但缺少文本 {missing}（字段名对不上或空渲染）"
                elif "{{" in text or "{%" in text:
                    ok = False
                    detail = "页面残留未渲染的模板标记"
            record(ok, "render", f"{username} GET {path}", detail)

    print("\n=== 工单详情页（6 个状态各取一张） ===")
    orders = q.list_work_orders(policy_for("admin"), page_size=50)["items"]
    by_status: dict[int, dict] = {}
    for item in orders:
        by_status.setdefault(int(item["status"]), item)
    for status, order in sorted(by_status.items()):
        response = safe_get(clients["admin"], f"/orders/{order['id']}")
        text = body_text(response)
        house = order.get("house_full") or ""
        ok = response.status_code == 200 and order["no"] in text and (not house or house in text)
        record(
            ok,
            "render",
            f"工单详情（状态 {status}）显示工单号与房屋全称",
            f"status={response.status_code} no={order['no'] in text} house={house in text if house else 'n/a'}",
        )

    print("\n=== 工单闭环（真实 POST：报修→派单→接单→进展→完工→验收→评价） ===")
    house_id = q.list_houses(policy_for("admin"), page_size=1)["items"][0]["id"]
    admin = clients["admin"]
    created = safe_post(
        admin,
        "/orders",
        {
            "house_id": house_id,
            "contact_name": "验收用例",
            "contact_phone": "13900001234",
            "category": "water",
            "urgency": "1",
            "description": "验收脚本自动生成的报修：厨房水管漏水",
            "csrf_token": csrf_of(admin, "/orders/new"),
        },
    )
    record(created.status_code in (302, 303, 200), "flow", "报修 POST 成功", f"status={created.status_code}")

    rows = q.list_work_orders(policy_for("admin"), keyword="验收用例", page_size=5)["items"]
    if not rows:
        record(False, "flow", "新建工单能在列表里查到")
    else:
        order_id = rows[0]["id"]
        record(True, "flow", f"新建工单可查询（{rows[0]['no']}）")
        steps = [
            ("manager01", "assign", {"repairer": "黄磊"}, "派单"),
            ("engineer01", "accept", {}, "接单"),
            ("engineer01", "progress", {"note": "已上门检查，准备更换角阀"}, "登记进展"),
            ("engineer01", "finish", {"note": "更换角阀完成，已试水"}, "完工"),
            ("admin", "verify", {"note": "现场确认不漏水"}, "验收"),
            ("owner01", "rate", {"rating": "5", "note": "师傅上门很快"}, "评价"),
        ]
        for actor, action, extra, label in steps:
            client = clients[actor]
            data = dict(extra)
            data["csrf_token"] = csrf_of(client, f"/orders/{order_id}")
            response = safe_post(client, f"/orders/{order_id}/{action}", data)
            record(
                response.status_code in (302, 303, 200),
                "flow",
                f"{label}（{actor}）POST 成功",
                f"status={response.status_code}",
            )
        final = q.get_work_order(policy_for("admin"), order_id)
        record(int(final["status"]) == 4, "flow", f"闭环后状态=4 已关闭（实际 {final['status']}）")
        record(bool(final.get("rating")), "flow", f"评价已落库（rating={final.get('rating')}）")

    audit_page = safe_get(clients["admin"], "/audit")
    audit_text = body_text(audit_page)
    record(audit_page.status_code == 200, "flow", "审计页 200", f"status={audit_page.status_code}")
    record(("报修" in audit_text) or ("派单" in audit_text), "flow", "审计页出现工单动作中文名")

    print("\n=== AI 页面与 /ai/chat ===")
    ai_page = safe_get(clients["admin"], "/ai")
    ai_text = body_text(ai_page)
    record(
        ai_page.status_code == 200 and "ai-layout" in ai_text and "chat-input" in ai_text,
        "ai",
        "/ai 页面 200 且渲染出聊天界面",
        f"status={ai_page.status_code} layout={'ai-layout' in ai_text} input={'chat-input' in ai_text}",
    )
    chat = clients["admin"].post(
        "/ai/chat",
        data={"q": "你好", "session": "", "csrf_token": csrf_of(clients["admin"], "/ai")},
        follow_redirects=False,
    )
    content_type = chat.headers.get("Content-Type", "")
    chat_text = body_text(chat)
    record(
        chat.status_code == 200 and "event-stream" in content_type,
        "ai",
        "/ai/chat 返回 SSE 流",
        f"ctype={content_type}",
    )
    record("data:" in chat_text, "ai", "/ai/chat 帧是 data: 格式", chat_text[:160])

    print("\n=== 未登录与 404 ===")
    with application.test_client() as anon:
        response = safe_get(anon, "/orders", follow_redirects=False)
        record(
            response.status_code in (302, 303, 401),
            "guard",
            "未登录访问 /orders 被挡",
            f"status={response.status_code}",
        )
        response = safe_get(anon, "/no-such-page")
        record(response.status_code == 404, "guard", "不存在的路径 404", f"status={response.status_code}")

    passed = sum(1 for item in results if item["ok"])
    failed = [item for item in results if not item["ok"]]
    summary = {
        "started_at": console[0],
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_seconds": round(time.time() - started, 1),
        "database": os.environ["DATABASE_URL"],
        "dialect": "mysql" if USING_MYSQL else "sqlite",
        "total": len(results),
        "passed": passed,
        "failed": len(failed),
        "results": results,
    }
    report_path = ARTIFACTS / "acceptance_report.json"
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (ARTIFACTS / "acceptance_e2e.log").write_text("\n".join(console) + "\n", encoding="utf-8")

    print("\n" + "=" * 66)
    print(f"端到端验收：通过 {passed} / {len(results)}，失败 {len(failed)}")
    print(f"报告：{report_path.relative_to(ROOT)}")
    if failed:
        print("\n失败清单：")
        for item in failed:
            print(f"  - [{item['section']}] {item['name']}：{item['detail'][:150]}")
    print("=" * 66)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
