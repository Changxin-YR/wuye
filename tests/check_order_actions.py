"""7 个动作（assign/accept/progress/finish/verify/cancel/rate）逐个渲染验证，含 staff 缺失降级。"""
import sys, datetime as dt
from pathlib import Path
sys.path.insert(0, "tests")
from flask import Flask, render_template
import frontend_stubs as S

ROOT = Path(".").resolve()
app = Flask(__name__, template_folder=str(ROOT / "templates"), static_folder=str(ROOT / "static"))
holder = S.install_globals(app.jinja_env)

ACTIONS = ["assign", "accept", "progress", "finish", "verify", "cancel", "rate"]
FIELD = {
    "assign": 'name="repairer"', "accept": None, "progress": 'name="note"',
    "finish": 'name="note"', "verify": 'name="note"', "cancel": 'name="reason"',
    "rate": 'name="rating"',
}
failures = []
for name in ACTIONS:
    for staff in ([{"id": 9, "display_name": "李师傅", "open_orders": 2}], []):
        with app.test_request_context("/"):
            ctx = S.case_order_detail("service")
            ctx["actions"] = [S.action_item(name)]
            ctx["staff"] = staff
            holder.context = ctx
            html = render_template("order_detail.html", **ctx)
        target = f'/orders/1024/{name}'
        if target not in html:
            failures.append(f"{name} staff={len(staff)}: 表单 target 未渲染")
        field = FIELD[name]
        if field and field not in html:
            failures.append(f"{name} staff={len(staff)}: 缺少字段 {field}")
        if name == "assign" and not staff and "维修师傅姓名" not in html:
            failures.append("assign 缺 staff 时未降级为手填姓名")
        if name == "assign" and staff and "请选择师傅" not in html:
            failures.append("assign 有 staff 时未渲染下拉")
        if name == "cancel" and 'data-confirm' not in html:
            failures.append("cancel 缺少二次确认")
        # 其他动作不应串场
        for other in set(ACTIONS) - {name}:
            if f'/orders/1024/{other}' in html:
                failures.append(f"{name}: 误渲染了 {other}")
print(f"动作渲染验证：7 个动作 × 2 种 staff 情况，问题 {len(failures)} 个")
for item in failures:
    print("  - " + item)
sys.exit(1 if failures else 0)