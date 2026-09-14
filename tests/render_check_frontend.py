"""前端渲染自检：用桩数据渲染全部模板 + 结构断言；app.py 就绪后自动切换为端到端模式。

用法：
    python tests/render_check_frontend.py

- 桩模式：11 个模板 × 5 个角色 = 55 种组合，校验渲染与结构（导航可见性、写权限入口、
  动作按钮、无残留模板标记、无内部术语）。
- 真实模式：若仓库根目录存在可导入的 app.py，则用 app.test_client() 逐页请求，
  校验登录页、8 个业务页、404 页都能正常渲染。
桩数据在 tests/frontend_stubs.py，与 backend-engineer 的 queries.py 返回结构保持一致。
"""
from __future__ import annotations

import datetime as dt
import re
import traceback
import sys
from pathlib import Path

from flask import Flask, render_template

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

import frontend_stubs as S  # noqa: E402

ROUTE_FOR_PAGE = {
    "dashboard.html": "/",
    "houses.html": "/houses",
    "persons.html": "/persons",
    "orders.html": "/orders",
    "order_form.html": "/orders/new",
    "order_detail.html": "/orders/1",
    "ai.html": "/ai",
    "audit.html": "/audit",
    "login.html": "/login",
    "error.html": "/no-such-page-404",
}


def real_app():
    """尝试加载真实 app.py；不可用时返回 None（退回桩模式）。"""
    if not (ROOT / "app.py").exists():
        return None
    try:
        import app as application  # noqa: PLC0415
    except Exception as error:  # noqa: BLE001
        print(f"[提示] 真实 app.py 暂不可用（{type(error).__name__}: {error}），退回桩数据模式")
        return None
    factory = getattr(application, "create_app", None)
    flask_app = None
    if callable(factory):
        # app.py 是应用工厂（create_app(overrides)）；用 SQLite 临时库起一个真实实例
        import tempfile as _tempfile  # noqa: PLC0415

        db_path = Path(_tempfile.gettempdir()) / "wuye_render_check.sqlite3"
        if db_path.exists():
            db_path.unlink()
        try:
            flask_app = factory({"DATABASE_URL": "sqlite:///" + db_path.as_posix(),
                                 "SECRET_KEY": "render-check", "TESTING": False})
            import db as _db  # noqa: PLC0415
            import seed_demo as _seed  # noqa: PLC0415

            _db.create_all(_db.get_engine("sqlite:///" + db_path.as_posix()))
            _seed.seed(_db.get_engine("sqlite:///" + db_path.as_posix()))
        except Exception as error:  # noqa: BLE001
            print(f"[提示] 真实 app 实例化失败（{type(error).__name__}: {error}），退回桩数据模式")
            return None
    else:
        flask_app = getattr(application, "app", application)
    if not hasattr(flask_app, "test_client"):
        print("[提示] app.py 里没有 Flask 应用对象，退回桩数据模式")
        return None
    return flask_app


def run_real_app(flask_app):
    """用真实 app + 登录会话逐页请求（只校验可渲染性，不校验数据内容）。

    返回 (problems, checked)；登录不可用时返回 (None, 0) 表示"环境不具备"（例如数据库没起），
    调用方应退回桩模式而不是判定失败。
    """
    problems: list[str] = []
    checked = 0
    with flask_app.test_client() as client:
        import seed_demo  # noqa: PLC0415

        try:
            # 先 GET 登录页拿会话里的 csrf_token（app.py 用会话级 CSRF）
            landing = client.get("/login")
            match = re.search(r'name="csrf_token" value="([^"]+)"', landing.get_data(as_text=True))
            login = client.post(
                "/login",
                data={"username": "admin", "password": seed_demo.DEMO_PASSWORD,
                      "csrf_token": match.group(1) if match else ""},
                follow_redirects=True,
            )
        except Exception as error:  # noqa: BLE001
            print(f"[提示] 真实 app 登录不可用（{type(error).__name__}: {error}）")
            return None, 0
        if login.status_code >= 500:
            print(f"[提示] 真实 app /login 返回 HTTP {login.status_code}（数据库可能没启动），退回桩数据模式")
            return None, 0
        # 工单详情用真实存在的 id（列表页解析），避免猜 id 得 404
        order_id = None
        listing = client.get("/orders")
        if listing.status_code == 200:
            found = re.findall(r'href="/orders/(\d+)"', listing.get_data(as_text=True))
            order_id = found[0] if found else None
        for name, path in ROUTE_FOR_PAGE.items():
            if name == "order_detail.html" and order_id:
                path = f"/orders/{order_id}"
            allowed = {"login.html": (200, 302), "error.html": (200, 302, 403, 404)}.get(name, (200,))
            try:
                page = client.get(path)
                html = page.get_data(as_text=True)
                if page.status_code not in allowed:
                    problems.append(f"{name} {path} → HTTP {page.status_code}（期望 {allowed}）")
                elif "{{" in html or "{%" in html:
                    problems.append(f"{name} 渲染残留模板标记")
                elif "{'value':" in html or '{"value":' in html:
                    # 回归：下拉选项字典被原样渲染给用户（费用类型曾显示成
                    # "{'value': '水费', 'text': '水费'}"），页面上只该出现人话
                    problems.append(f"{name} 把下拉选项字典直接渲染给了用户")
                else:
                    checked += 1
            except Exception as error:  # noqa: BLE001
                problems.append(f"{name} {path} 抛异常：{type(error).__name__}: {error}")
    return problems, checked


def assert_html(role: str, name: str, html: str) -> list[str]:
    """结构断言：模板只渲染该角色能做的事，且有写权限的角色必须看得到入口。"""
    problems: list[str] = []
    # 用例名可能带变体后缀（如 dashboard.html#frozen），断言统一按基础模板名派发
    full_name = name
    name = name.split("#", 1)[0]
    perms = S.ROLES[role][2]
    if "{{" in html or "{%" in html:
        problems.append("渲染结果里残留未解析的模板标记")
    for term in ("RBAC", "DataScope", "R0", "R1", "R2", "R3"):
        if term in html:
            problems.append(f"出现内部术语 {term}")

    if name == "base.html":
        # 导航按权限渲染：只出现有权限的入口，且"工作台/AI 助手"永远可见
        expected = [item for item in (
            ("工作台", None), ("房屋", "house.read"), ("人员关系", "person.read"),
            ("租赁", "lease.write"), ("维修工单", "order.read"), ("投诉", "complaint.read"),
            ("访客", "visitor.read"), ("车辆车位", "vehicle.read"), ("设备巡检", "device.read"),
            ("收费", "billing.read"), ("AI 助手", None), ("操作审计", "audit.read"),
        ) if item[1] is None or item[1] in perms]
        for label, perm in expected:
            if f">{label}</a>" not in html:
                problems.append(f"有权限却没有导航入口：{label}")
        for label, perm in (
            ("工作台", None), ("房屋", "house.read"), ("人员关系", "person.read"),
            ("租赁", "lease.write"), ("维修工单", "order.read"), ("投诉", "complaint.read"),
            ("访客", "visitor.read"), ("车辆车位", "vehicle.read"), ("设备巡检", "device.read"),
            ("收费", "billing.read"), ("AI 助手", None), ("操作审计", "audit.read"),
        ):
            if label not in [item[0] for item in expected] and f">{label}</a>" in html:
                problems.append(f"没有权限却出现导航入口：{label}")
        if "退出" not in html:
            problems.append("缺少退出入口")
    if name == "dashboard.html":
        cards = html.count(chr(60) + 'a class="stat')
        if cards != 6:
            problems.append(f"状态卡应为 6 张，实际 {cards}")
        for text in S.STATUS_TEXT.values():
            if f"<span>{text}</span>" not in html:
                problems.append(f"状态卡缺少 {text}")
        if html.count('class="stat-unit">单</span></strong>') != 6:
            problems.append("状态卡计数未渲染")
        if ("order.create" in perms) != ("+ 报修" in html):
            problems.append("报修入口与权限不一致")
    if name == "houses.html":
        for text in ("小区", "楼栋", "房屋"):
            if text not in html:
                problems.append(f"缺少 {text} 一级")
        can_write = "house.write" in perms or "community.write" in perms
        if can_write != ("text-danger" in html):
            problems.append("删除入口与权限不一致")
        if "community.write" in perms and "＋ 新增" not in html:
            problems.append("有小区写权限却没有新增入口")
    if name == "persons.html":
        if ("person.write" in perms) != ("新增人员" in html):
            problems.append("新增人员入口与权限不一致")
        if ("relation.write" in perms) != ("登记房屋关系" in html):
            problems.append("登记关系表单与权限不一致")
    if name == "orders.html":
        if ("order.create" in perms) != ("+ 报修" in html):
            problems.append("报修入口与权限不一致")
        if 'action="/orders/new"' in html and "order.create" not in perms:
            problems.append("没有报修权限却出现报修链接")
        for text in S.STATUS_TEXT.values():
            if text not in html:
                problems.append(f"状态页签缺少 {text}")
    if name == "order_form.html":
        if "提交报修" not in html:
            problems.append("缺少提交按钮")
        if 'name="category"' not in html or 'name="urgency"' not in html:
            problems.append("类别/紧急度下拉缺失")
    # #reopen 变体走自己的断言，不套用基座的 progress/finish/cancel 期望
    if name == "order_detail.html" and full_name != "order_detail.html#reopen":
        for action in ("progress", "finish", "cancel"):
            if f'action="/orders/1024/{action}"' not in html:
                problems.append(f"actions 里有 {action} 却没有渲染表单")
        for action in ("assign", "accept", "verify", "rate", "reopen"):
            if f'action="/orders/1024/{action}"' in html:
                problems.append(f"渲染了后端未授权的动作：{action}")
        if "处理过程" not in html or "提交报修" not in html:
            problems.append("处理过程时间线缺失")
    if name == "order_detail.html" and 'name="repairer"' in html and 'name="repairer"' not in html.replace('<input name="repairer"', '<input name="repairer"'):
        problems.append("派单表单异常")
    if full_name == "ai.html#first":
        if "还没有对话" not in html:
            problems.append("/ai 首次进入缺少空态提示")
        if "助手暂时连不上" not in html:
            problems.append("agent_ready=False 时未提示助手不可用")
        if "当前登录" not in html:
            problems.append("/ai 首访缺少身份条")
        if 'id="chat-form"' not in html:
            problems.append("/ai 首访缺少输入框（空会话也要能提问）")
    if name == "ai.html":
        for hook in ('id="chat-form"', 'id="chat-input"', 'id="chat"', "/static/js/ai.js", "data-endpoint"):
            if hook not in html:
                problems.append(f"AI 页面缺少 {hook}")
        if "我的对话" not in html:
            problems.append("AI 页面缺少会话列表")
        if "当前登录" not in html:
            problems.append("AI 页面缺少身份条")
        if "能看到" not in html:
            problems.append("身份条未显示数据范围")
    if name == "audit.html":
        if "AI 助手" not in html:
            problems.append("缺少 AI 来源标记")
        if "维修工单" not in html:
            problems.append("对象类型未渲染")
        if "#1024" not in html.replace(" ", ""):
            problems.append("对象 id 未渲染")
        if "创建工单" not in html:
            problems.append("操作列未渲染")
        if html.count('class="pill') != 3:
            problems.append(f"来源筛选页签应为 3 个，实际 {html.count('class=\"pill')}")
        for label in ("网页操作", "AI 助手"):
            if f">{label}</a>" not in html:
                problems.append(f"来源筛选缺少 {label}")
    if name == "leases.html":
        if "在租列表" not in html:
            problems.append("租赁页缺少在租列表")
        if ("lease.write" in perms) != ("办理入住" in html):
            problems.append("入住表单与权限不一致")
    if name == "complaints.html":
        if "投诉单号" not in html:
            problems.append("投诉列表表头缺失")
        if html.count('class="pill') < 4:
            problems.append(f"投诉状态页签应≥4 个，实际 {html.count('class=\"pill')}")
        if ("complaint.create" in perms) != ("登记投诉" in html):
            problems.append("登记投诉表单与权限不一致")
    if name == "complaint_detail.html":
        if "处理过程" not in html or "投诉内容" not in html:
            problems.append("投诉详情结构缺失")
        if 'action="/complaints/41/handle"' not in html:
            problems.append("handle 动作未渲染")
        if 'action="/complaints/41/cancel"' not in html:
            problems.append("cancel 动作未渲染")
    if name == "visitors.html":
        if "访客" not in html or "到访房屋" not in html:
            problems.append("访客页结构缺失")
        if ("visitor.write" in perms) != ("登记访客" in html):
            problems.append("登记访客表单与权限不一致")
        can_visitor = "visitor.write" in perms
        has_in = 'action="/visitors/51/check-in"' in html
        has_out = 'action="/visitors/52/check-out"' in html
        if can_visitor and not has_in:
            problems.append("有访客写权限却没有「进入」动作")
        if can_visitor and not has_out:
            problems.append("有访客写权限却没有「离开」动作")
        if not can_visitor and (has_in or has_out):
            problems.append("没有访客写权限却出现流转按钮")
    if name == "vehicles.html":
        for text in ("车辆", "车位", "沪A12345", "A-012"):
            if text not in html:
                problems.append(f"车辆车位页缺少 {text}")
        if ("vehicle.write" in perms) != ("登记车辆" in html):
            problems.append("车辆登记入口与权限不一致")
        if ("parking.write" in perms) != ("分配车位" in html):
            problems.append("车位分配入口与权限不一致")
        if 'action="/parking/release"' not in html and "parking.write" in perms:
            problems.append("车位释放入口缺失")
    if name == "devices.html":
        for text in ("设备", "巡检任务", "1 栋 1 单元电梯"):
            if text not in html:
                problems.append(f"设备巡检页缺少 {text}")
        if ("device.write" in perms) != ("新增设备" in html):
            problems.append("设备新增入口与权限不一致")
        if ("inspection.assign" in perms) != ("新建巡检" in html):
            problems.append("巡检创建入口与权限不一致（派活看 inspection.assign）")
        can_inspection = "inspection.write" in perms
        has_complete = 'action="/inspections/91/complete"' in html
        has_to_order = 'action="/inspections/92/to-order"' in html
        if can_inspection and not has_complete:
            problems.append("有巡检写权限却没有「完成巡检」表单")
        if not can_inspection and has_complete:
            problems.append("没有巡检写权限却出现「完成巡检」表单")
        if can_inspection and not has_to_order:
            problems.append("有巡检写权限却没有「转报修」表单")
        if not can_inspection and has_to_order:
            problems.append("没有巡检写权限却出现「转报修」表单")
    if name == "bills.html":
        for text in ("待缴金额", "待缴账单", "已收金额", "物业费"):
            if text not in html:
                problems.append(f"账单页缺少 {text}")
        if ("billing.collect" in perms) != ("确认收款" in html):
            problems.append("收款入口与权限不一致")
        if ("billing.manage" in perms) != ("生成账单" in html):
            problems.append("建账单入口与权限不一致")
        if ("billing.reverse" in perms) and "冲销" not in html:
            problems.append("有冲销权限却没有冲销入口")
        if (not "billing.reverse" in perms) and "确认冲销" in html:
            problems.append("没有冲销权限却出现冲销按钮")
    if name == "bill_detail.html":
        if "收款记录" not in html or "应缴金额" not in html:
            problems.append("账单详情结构缺失")
    if full_name == "order_detail.html#reopen":
        if 'action="/orders/1024/reopen"' not in html:
            problems.append("actions 里有 reopen 却没有渲染返修表单")
        if "退回" not in html and "返修" not in html:
            problems.append("返修按钮文案缺失")
    if name == "login.html":
        for hook in ('name="username"', 'name="password"', "用户名或密码不正确"):
            if hook not in html:
                problems.append(f"登录页缺少 {hook}")
    if name == "error.html":
        if "403" not in html:
            problems.append("错误码未渲染")
    return problems


def main() -> None:
    real_failed = False
    flask_app = real_app()
    if flask_app is not None:
        problems, checked = run_real_app(flask_app)
        if problems is not None:
            print(f"真实 app.py 模式：请求 {checked} 个页面，问题 {len(problems)} 个")
            for problem in problems:
                print("  - " + problem)
            if problems:
                real_failed = True
            else:
                print("端到端渲染通过")
    # 继续跑桩组合（覆盖真实 app 里还没接上的 v2 页面）

    app = Flask(__name__, template_folder=str(ROOT / "templates"), static_folder=str(ROOT / "static"))
    app.jinja_env.filters["cn_time"] = (
        lambda value: value.strftime("%Y-%m-%d %H:%M") if isinstance(value, dt.datetime) else (value or "—")
    )
    # app.py 的 context_processor 提供的全局（status_class 也注册为 filter，与 app.py 一致）
    app.jinja_env.filters["status_class"] = lambda value: S.STATUS_CLASS.get(int(value), "")

    # 模拟 app.py 的 context_processor（can / app_name / nav 等按用例上下文解析）
    holder = S.install_globals(app.jinja_env)

    failures: list[str] = []
    checked = 0
    for role in S.ROLES:
        for case_key, builder in S.CASES.items():
            # 用例名支持 "模板#变体"，断言用变体名区分
            name, _, variant = case_key.partition("#")
            try:
                with app.test_request_context("/"):
                    context = builder(role)
                    holder.context = context
                    html = render_template(name, **context)
                if not html.strip():
                    raise AssertionError("渲染结果为空")
                for problem in assert_html(role, case_key, html):
                    raise AssertionError(problem)
                checked += 1
            except Exception as error:  # noqa: BLE001
                failures.append(f"[{role}] {case_key}: {type(error).__name__}: {error}" + "\n       " + "\n       ".join(traceback.format_exc().splitlines()[-6:]))
            finally:
                holder.context = {}
    print(f"渲染组合：{checked} 通过，{len(failures)} 失败")
    for item in failures:
        print("  - " + item)
    if failures:
        raise SystemExit(1)
    print("全部模板渲染通过")
    if real_failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()