"""导航高亮验证：用真实路由注册，让 request.endpoint 生效（裸 test_request_context 里它是 None）。"""
import sys
sys.path.insert(0, "tests")
from pathlib import Path
from flask import Flask, render_template
import frontend_stubs as S

ROOT = Path(".").resolve()
app = Flask(__name__, template_folder=str(ROOT / "templates"), static_folder=str(ROOT / "static"))
holder = S.install_globals(app.jinja_env)

VIEW = lambda: "ok"
PATHS = {
    "dashboard": "/", "orders": "/orders", "order_new": "/orders/new",
    "order_detail": "/orders/<int:order_id>", "audit": "/audit",
    "ai_page": "/ai", "ai_session_create": "/ai/sessions",
}
for endpoint, rule in PATHS.items():
    app.add_url_rule(rule, endpoint, VIEW, methods=["GET", "POST"])
try:
    app.add_url_rule("/ai/chat", "ai_chat", VIEW, methods=["POST"])
except AssertionError:
    pass

AI_ENDPOINTS = {"ai_page", "ai_session_create", "ai_chat"}
NAV_VARIANTS = {
    "只给 key": [{"key": "ai", "label": "AI 助手", "href": "/ai"}],
    "给蓝图端点": [{"key": "ai", "endpoint": "ai_page", "label": "AI 助手", "href": "/ai"}],
}
failures = []
checked = 0
for variant, nav in NAV_VARIANTS.items():
    for endpoint, rule in PATHS.items():
        checked += 1
        with app.test_request_context(rule.replace("<int:order_id>", "7")):
            assert __import__("flask").request.endpoint == endpoint, endpoint
            ctx = S.case_dashboard("service")
            ctx["nav"] = nav
            holder.context = ctx
            html = render_template("base.html", **ctx)
        active = 'class="active"' in html
        expect = endpoint in AI_ENDPOINTS
        if active != expect:
            failures.append(f"{variant} endpoint={endpoint}: 高亮={active}，期望={expect}")
print(f"导航高亮验证：{checked} 组，问题 {len(failures)}")
for item in failures:
    print("  - " + item)
sys.exit(1 if failures else 0)