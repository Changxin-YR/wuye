"""生成静态预览站点（桩数据渲染），用于在浏览器里核对视觉与交互。

用法：python tests/build_preview_site.py [输出目录，默认 artifacts/frontend-preview]
配合：node tests/check_layout_cdp.mjs 对它做真实浏览器布局体检。
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

from flask import Flask, render_template

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import frontend_stubs as S  # noqa: E402

PAGES = [
    ("login", "login.html", "owner"),
    ("dashboard", "dashboard.html", "service"),
    ("houses", "houses.html", "admin"),
    ("persons", "persons.html", "service"),
    ("orders", "orders.html", "service"),
    ("order-new", "order_form.html", "owner"),
    ("order-detail", "order_detail.html", "engineer"),
    ("ai", "ai.html", "service"),
    ("audit", "audit.html", "admin"),
    ("leases", "leases.html", "service"),
    ("complaints", "complaints.html", "service"),
    ("complaint-detail", "complaint_detail.html", "service"),
    ("visitors", "visitors.html", "service"),
    ("vehicles", "vehicles.html", "service"),
    ("devices", "devices.html", "engineer"),
    ("bills", "bills.html", "finance"),
    ("bill-detail", "bill_detail.html", "finance"),
    ("error", "error.html", "service"),
]

LINK_MAP = {
    "login": "login.html", "dashboard": "dashboard.html", "houses": "houses.html",
    "houses_command": "houses.html", "persons": "persons.html",
    "persons_command": "persons.html", "orders": "orders.html", "order_new": "order-new.html",
    "order_create": "order-detail.html", "order_detail": "order-detail.html",
    "order_command": "order-detail.html", "ai_page": "ai.html",
    "ai_session_create": "ai.html", "ai_chat": "ai.html", "audit": "audit.html",
    "logout": "login.html", "static": "__static__",
    "leases": "leases.html", "leases_command": "leases.html",
    "complaints": "complaints.html", "complaints_command": "complaint-detail.html",
    "complaint_detail": "complaint-detail.html",
    "visitors": "visitors.html", "visitors_command": "visitors.html",
    "vehicles": "vehicles.html", "vehicles_command": "vehicles.html",
    "parking_command": "vehicles.html", "devices": "devices.html",
    "devices_command": "devices.html", "inspections_command": "devices.html",
    "bills": "bills.html", "bills_command": "bill-detail.html", "bill_detail": "bill-detail.html",
    "ai_actions_confirm": "ai.html", "ai_actions_cancel": "ai.html",
    # 预览里"接口地址"必须保留可识别的路径，否则站点内的 fetch 桩匹配不上
    "ai_actions": "/ai/actions", "ai.ai_actions": "/ai/actions",
}


#: 这些端点保留真实路径形状（页面脚本要按路径识别接口），只补预览用的文件扩展
KEEP_PATH = {
    "ai_actions": "/ai/actions",
    "ai_action_confirm": "/ai/actions/{action_id}/confirm",
    "ai_action_cancel": "/ai/actions/{action_id}/cancel",
    "ai_actions_confirm": "/ai/actions/{action_id}/confirm",
    "ai_actions_cancel": "/ai/actions/{action_id}/cancel",
    "ai.ai_actions": "/ai/actions",
    "ai.ai_action_confirm": "/ai/actions/{action_id}/confirm",
    "ai.ai_action_cancel": "/ai/actions/{action_id}/cancel",
}


def preview_url_for(endpoint, **values):
    if endpoint in KEEP_PATH:
        template = KEEP_PATH[endpoint]
        try:
            return template.format(**values)
        except (KeyError, IndexError):
            return template
    target = LINK_MAP.get(endpoint)
    if target == "__static__":
        return "/" + str(values.get("filename", ""))
    if target is None:
        return "#"
    return target


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "artifacts" / "frontend-preview"
    out.mkdir(parents=True, exist_ok=True)

    app = Flask(__name__, template_folder=str(ROOT / "templates"), static_folder=str(ROOT / "static"))
    app.jinja_env.filters["cn_time"] = (
        lambda value: value.strftime("%Y-%m-%d %H:%M") if isinstance(value, dt.datetime) else (value or "—")
    )
    app.jinja_env.globals.update(
        can=lambda perm: True,
        status_text=lambda value: S.STATUS_TEXT.get(int(value), "—"),
        relation_text=lambda value: S.RELATION_TEXT.get(value, value),
        house_status_text=lambda value: S.HOUSE_STATUS_TEXT.get(int(value), "—"),
        urgency_text=lambda value: S.URGENCY_TEXT.get(int(value), "—"),
    )

    written = []
    for page, template, role in PAGES:
        with app.test_request_context("/"):
            context = S.CASES[template](role)
            context["url_for"] = preview_url_for
            app.jinja_env.globals["can"] = context.get("can", lambda perm: True)
            html = render_template(template, **context)
        html = html.replace('src="/js/', 'src="static/js/').replace('href="/css/', 'href="static/css/')
        target = out / f"{page}.html"
        target.write_text(html, encoding="utf-8")
        written.append(target)

    print(f"预览站点已生成：{out}")
    for path in written:
        print(f"  {path.name}  ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()