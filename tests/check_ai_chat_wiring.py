"""AI 对话接口接线验证：真实 app + SQLite，验证 POST /ai/chat 的 CSRF/参数/SSE 响应。

用法：python tests/check_ai_chat_wiring.py
不依赖模型：运行时不可用时后端也会回 {"type":"error",...} + {"type":"done"} 的合法 SSE 帧，
本脚本只验证"接线正确"（不是 400/404/405、是 text/event-stream、帧是 SSE 格式、错误是中文）。
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    db_file = Path(tempfile.gettempdir()) / "wuye_chat.sqlite3"
    if db_file.exists():
        db_file.unlink()
    url = "sqlite:///" + db_file.as_posix()

    import app as application
    import db as app_db
    import seed_demo

    flask_app = application.create_app({
        "DATABASE_URL": url, "SECRET_KEY": "chat-wiring-check", "PROPAGATE_EXCEPTIONS": False,
    })
    app_db.create_all(app_db.get_engine(url))
    seed_demo.seed(app_db.get_engine(url))

    failures: list[str] = []
    notes: list[str] = []
    with flask_app.test_client() as client:
        page = client.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True))
        token = token.group(1) if token else ""
        if not token:
            print("登录页没有 csrf_token，无法继续")
            return 2
        client.post("/login", data={"username": "service01", "password": seed_demo.DEMO_PASSWORD,
                                    "csrf_token": token}, follow_redirects=True)
        ai_page = client.get("/ai")
        if ai_page.status_code != 200:
            failures.append(f"GET /ai → HTTP {ai_page.status_code}")
        html = ai_page.get_data(as_text=True)
        endpoint = re.search(r'data-endpoint="([^"]*)"', html)
        data_csrf = re.search(r'data-csrf="([^"]*)"', html)
        # 重要：登录成功后 app.py 会 **轮换** 会话里的 csrf_token，
        # 所以之后必须用页面上渲染出来的新 token（真实浏览器也是这样，页面刷新即拿到新值）
        page_token = re.search(r'name="csrf_token" value="([^"]+)"', html)
        if page_token:
            token = page_token.group(1)
            notes.append(f"登录后从页面取到新 csrf_token（前 8 位 {token[:8]}…）")
        if data_csrf and data_csrf.group(1) and data_csrf.group(1) != token:
            failures.append("ai.html 的 data-csrf 与表单里的 csrf_token 不一致")
        notes.append(f"ai.html data-endpoint={endpoint.group(1) if endpoint else '缺失'} "
                     f"data-csrf={'有' if data_csrf and data_csrf.group(1) else '无'}")

        # 1) POST + 头 + body（前端现在的实际用法）
        response = client.post("/ai/chat",
                               data={"q": "现在有哪些待派单的工单？", "csrf_token": token},
                               headers={"X-CSRF-Token": token, "Accept": "text/event-stream"})
        notes.append(f"POST 带 CSRF → HTTP {response.status_code} "
                     f"content-type={response.headers.get('Content-Type', '')}")
        if response.status_code in (400, 401, 403, 404, 405):
            failures.append(f"POST /ai/chat 被拒：HTTP {response.status_code} "
                            f"{response.get_data(as_text=True)[:200]}")
        elif "text/event-stream" not in (response.headers.get("Content-Type") or ""):
            failures.append(f"POST /ai/chat 未返回 text/event-stream：{response.headers.get('Content-Type')}")
        else:
            body = response.get_data(as_text=True)
            frames = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
            types = []
            for frame in frames:
                try:
                    types.append(json.loads(frame).get("type"))
                except Exception:  # noqa: BLE001
                    failures.append(f"SSE 帧不是合法 JSON：{frame[:120]}")
            notes.append(f"SSE 帧类型={types}")
            if not frames:
                failures.append("SSE 响应里没有任何 data: 帧")
            elif "done" not in types and "error" not in types:
                failures.append(f"SSE 没有 done/error 收尾帧：{types}")
            # 只检查面向用户的字段（text/summary/message/label/preview）与边界词，
            # 不看 call_id / args：模型生成的调用 ID 可能碰巧包含 R0 之类的子串（曾误报）
            for frame in frames:
                try:
                    payload = json.loads(frame)
                except Exception:  # noqa: BLE001
                    continue
                if "Traceback" in frame:
                    failures.append(f"SSE 帧出现 Traceback：{frame[:120]}")
                for field in ("text", "summary", "message", "label", "preview"):
                    value = str(payload.get(field) or "")
                    if not value:
                        continue
                    if any(term in value for term in ("RBAC", "DataScope")):
                        failures.append(f"SSE 的 {field} 泄露内部术语：{value[:80]}")
                    if re.search(r"(^|[^A-Za-z0-9])R[0-3]([^A-Za-z0-9]|$)", value):
                        failures.append(f"SSE 的 {field} 出现风险档术语：{value[:80]}")
            # 带 session 参数也要能用
            ids = re.findall(r'/ai\?session=(\d+)', html)
            if ids:
                second = client.post("/ai/chat",
                                     data={"q": "再看看维修中的", "session": ids[0], "csrf_token": token},
                                     headers={"X-CSRF-Token": token})
                notes.append(f"POST 带 session={ids[0]} → HTTP {second.status_code}")
                if second.status_code in (400, 401, 403, 404, 405):
                    failures.append(f"POST /ai/chat 带 session 被拒：HTTP {second.status_code}")

        # 4) 确认卡片端到端：地址模板里的 {id} 必须真的被换成动作号
        #    回归：base.html 曾把地址渲染成 /ai/actions/0/confirm，ai.js 的 replace('{id}') 落空，
        #    点「确认执行」永远打到 action_id=0，后端查不到这条动作 →
        #    用户只看到「这条待确认动作不属于当前账号」，派单发不出去。
        confirm_url = re.search(r'data-confirm-url="([^"]*)"', html)
        cancel_url = re.search(r'data-cancel-url="([^"]*)"', html)
        notes.append(f"ai.html data-confirm-url={confirm_url.group(1) if confirm_url else '缺失'} "
                     f"data-cancel-url={cancel_url.group(1) if cancel_url else '缺失'}")
        for label, match, verb in (("确认", confirm_url, "confirm"), ("取消", cancel_url, "cancel")):
            if match is None:
                failures.append(f"ai.html 缺少 data-{verb}-url")
            elif "{id}" not in match.group(1):
                failures.append(f"{label}接口地址没有 {{id}} 占位符：{match.group(1)}"
                                f"（前端会把请求打到错误的动作号上）")

        import queries
        import services
        from agent import actions as agent_actions
        from agent.tools import TOOLS_BY_NAME
        from models import User as UserModel, WorkOrder
        from permissions import Policy
        from sqlalchemy import select

        engine = app_db.get_engine(url)
        with app_db.db_session(engine) as setup:
            service_user = setup.scalars(
                select(UserModel).where(UserModel.username == "service01")).one()
            service_policy = Policy(setup, service_user, source="agent")
            listed = queries.list_work_orders(service_policy, status=0, page_size=5)
            items = listed.get("items") or []
            if not items:
                notes.append("没有待派单工单，跳过派单端到端（地址模板校验仍然生效）")
            else:
                first = items[0]
                order_id = int(first["id"] if isinstance(first, dict) else first.id)
                confirmation = agent_actions.create_action(
                    setup, service_policy, TOOLS_BY_NAME["assign_work_order"],
                    {"order_id": order_id, "repairer": "黄磊"},
                )
                action_id = confirmation.action_id
                notes.append(f"已生成待确认动作 #{action_id}：{confirmation.preview}")

        if items:
            pending = client.get("/ai/actions").get_json() or {}
            item = next((row for row in pending.get("actions", [])
                         if row.get("action_id") == action_id), None)
            if item is None:
                failures.append("待确认动作没有出现在 GET /ai/actions 里（刷新后卡片会丢）")
            elif not item.get("label"):
                failures.append("待确认动作缺少人话名称 label（卡片标题会显示内部工具名）")
            else:
                notes.append(f"GET /ai/actions 返回 label={item['label']}")

            # 完全照 ai.js 的做法：拿页面渲染出来的模板，把 {id} 换成真实动作号
            target = (confirm_url.group(1) if confirm_url else "").replace("{id}", str(action_id))
            done = client.post(target, data={"csrf_token": token},
                               headers={"X-CSRF-Token": token})
            body = done.get_data(as_text=True)
            notes.append(f"POST {target} → HTTP {done.status_code} {body[:180]}")
            if "不属于当前账号" in body:
                failures.append(f"确认执行被判为「不属于当前账号」：HTTP {done.status_code} {body[:180]}")
            payload = done.get_json() or {}
            if done.status_code != 200 or payload.get("ok") is not True:
                failures.append(f"确认执行没有成功：HTTP {done.status_code} {body[:180]}")
            with app_db.db_session(engine) as check:
                row = check.get(WorkOrder, order_id)
                if int(row.status) != 1 or not row.repairer_id:
                    failures.append(f"派单结果没有落库：status={row.status} repairer_id={row.repairer_id}")
                else:
                    notes.append(f"工单 {row.no} 已派单给 user_id={row.repairer_id}（status=1）")
                acted = check.get(agent_actions.AiAction, action_id)
                if int(acted.status) != 1:
                    failures.append(f"待确认动作状态没有变成已执行：status={acted.status}")

        # 5) 「查不到」与「不是你的」必须分开报，否则前端地址拼错时会被误判成权限问题
        with app_db.db_session(engine) as setup:
            from models import AiAction as AiActionModel
            missing = setup.scalars(select(AiActionModel.id)).first()
        probe = client.post(f"/ai/actions/{(missing or 1) + 900000}/confirm",
                            data={"csrf_token": token}, headers={"X-CSRF-Token": token})
        probe_body = probe.get_data(as_text=True)
        notes.append(f"POST 不存在的动作号 → HTTP {probe.status_code} {probe_body[:120]}")
        if probe.status_code != 403:
            failures.append(f"不存在的动作号没有按 403 处理：HTTP {probe.status_code}")
        if "不属于当前账号" in probe_body:
            failures.append("动作号不存在时仍报「不属于当前账号」（排查方向会被带偏）")

        # 2) 无 CSRF 必须被拒（安全回归）
        bad = client.post("/ai/chat", data={"q": "没有令牌"})
        notes.append(f"POST 缺 CSRF → HTTP {bad.status_code}（应 400）")
        if bad.status_code != 400:
            failures.append(f"缺少 CSRF 时未拒绝：HTTP {bad.status_code}")

        # 3) GET 也要求令牌
        bad_get = client.get("/ai/chat?q=没有令牌")
        notes.append(f"GET 缺 CSRF → HTTP {bad_get.status_code}（应 400）")
        if bad_get.status_code != 400:
            failures.append(f"GET 缺 CSRF 时未拒绝：HTTP {bad_get.status_code}")

    app_db.dispose_engines()
    if db_file.exists():
        db_file.unlink()

    for note in notes:
        print("  " + note)
    print(f"\nAI 对话接线验证：问题 {len(failures)} 个")
    for item in failures:
        print("  - " + item)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())