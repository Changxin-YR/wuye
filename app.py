"""Flask 装配与页面路由（薄视图层）。

职责边界：
- 只做「收参数 → 调 services / queries → flash → redirect / render」，不含业务规则。
- 授权一律交给 :class:`permissions.Policy`；``request.form`` 里的身份/范围一律不采信。
- 事务边界由 ``db.init_app`` 的请求级会话负责。
- ``/ai*`` 路由由 captain 的 ``agent.routes.ai_bp`` 蓝图提供（本模块只注册蓝图）。

模板上下文契约（与 templates/*.html 实际读取的一致）：
``app_name`` / ``current_user``(User 对象) / ``identity``(dict) / ``nav`` / ``csrf_token`` /
``can(perm)`` / ``cn_time``(filter) / 各页面的业务变量（见各视图）。
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime
from typing import Any, Optional

from flask import (
    Flask,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import select
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

import config as app_config
import db as app_db
import models
import permissions
import queries
import services
from models import User

log = logging.getLogger(__name__)

CSRF_FIELD = "csrf_token"
SESSION_USER_ID = "user_id"
SESSION_AUTH_VERSION = "auth_version"

#: 侧边导航：(active_key, endpoint, 标签, 需要的权限)
NAV_ITEMS = [
    {"key": "dashboard", "endpoint": "dashboard", "label": "工作台", "perm": None},
    {"key": "houses", "endpoint": "houses", "label": "房屋", "perm": "house.read"},
    {"key": "persons", "endpoint": "persons", "label": "人员", "perm": "person.read"},
    {"key": "orders", "endpoint": "orders", "label": "工单", "perm": "order.read"},
    {"key": "ai", "endpoint": ["ai.ai_page", "ai_page"], "label": "AI 助手", "perm": None},
    {"key": "audit", "endpoint": "audit", "label": "操作记录", "perm": "audit.read"},
]

ERROR_TITLES = {400: "请求有误", 401: "请先登录", 403: "没有权限", 404: "找不到页面", 500: "系统开小差了"}


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
class Row(dict):
    """模板里既能 ``item.key`` 也能 ``item['key']``（Jinja 对 dict 的 getattr 优先走方法名）。"""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def cn_time(value: Any) -> str:
    """模板过滤器：``YYYY-MM-DD HH:MM:SS`` → ``YYYY-MM-DD HH:MM``；空值 ``—``。"""
    if value in (None, ""):
        return "—"
    if isinstance(value, str):
        text = value.strip().replace("T", " ")
        if not text:
            return "—"
        return text[:16] if len(text) >= 16 else text
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


def _int_arg(name: str) -> Optional[int]:
    text = (request.args.get(name) or "").strip()
    if text.lstrip("-").isdigit():
        return int(text)
    return None


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _csrf_token() -> str:
    token = session.get(CSRF_FIELD)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_FIELD] = token
    return token


def check_csrf() -> None:
    """POST 一律校验 CSRF（表单隐藏域或 X-CSRF-Token 头）。"""
    sent = _text(request.form.get(CSRF_FIELD)) or _text(request.headers.get("X-CSRF-Token"))
    token = session.get(CSRF_FIELD) or ""
    if not sent or not token or not secrets.compare_digest(sent, token):
        abort(400, description="页面已过期，请刷新后重新提交")


def _db():
    return app_db.get_session()


def current_policy(source: str = "web") -> permissions.Policy:
    """当前请求的 Policy（未登录时是匿名 Policy）。"""
    policy = getattr(g, "policy", None)
    if policy is None:
        policy = permissions.Policy(_db(), session.get(SESSION_USER_ID), source=source)
        g.policy = policy
    return policy


def _login_required() -> None:
    if not session.get(SESSION_USER_ID):
        abort(401, description="请先登录后再操作")


def wants_json() -> bool:
    return request.path.startswith("/ai/") or request.accept_mimetypes.best == "application/json"


def _safe_next(target: Any, fallback: str) -> str:
    text = _text(target)
    if text.startswith("/") and not text.startswith("//"):
        script_root = (request.script_root or "").rstrip("/")
        if script_root and text != script_root and not text.startswith(script_root + "/"):
            return script_root + text
        return text
    return fallback


def build_nav(policy: permissions.Policy) -> list[Row]:
    """按权限过滤导航项：元素 ``{key, label, href}``（base.html 用 request.endpoint 判高亮）。"""
    items: list[Row] = []
    entries = list(NAV_ITEMS)
    # 运营模块（投诉/访客/车辆车位/设备巡检/收费）与租赁页的入口由 platform 的模块提供，
    # 未加载时不渲染（避免坏链接）。
    try:
        from ops_routes import OPS_NAV_ITEMS  # type: ignore

        entries.extend(OPS_NAV_ITEMS)
    except Exception:  # pragma: no cover
        pass
    entries.append({"key": "leases", "endpoint": "leases", "label": "租赁", "perm": "lease.write"})
    for item in entries:
        if item["perm"] and not policy.has(item["perm"]):
            continue
        # endpoint 可以是字符串或候选列表（如 AI 项：蓝图端点优先、app 级回退）
        candidates = item["endpoint"]
        if isinstance(candidates, str):
            candidates = [candidates]
        href = None
        for endpoint in candidates:
            try:
                href = url_for(endpoint)
                break
            except Exception:  # 蓝图/模块未加载时不渲染坏链接
                continue
        if href is None:
            continue
        items.append(Row({"key": item["key"], "label": item["label"], "href": href}))
    return items


def page_of(result: dict) -> dict:
    """分页信息：queries 给 ``pages``，模板也用 ``pages``。"""
    return {
        "page": result.get("page", 1),
        "page_size": result.get("page_size", 20),
        "pages": result.get("pages", 1),
        "total": result.get("total", 0),
        "has_prev": result.get("has_prev", False),
        "has_next": result.get("has_next", False),
    }


# --------------------------------------------------------------------------
# 视图
# --------------------------------------------------------------------------

#: 登录页「演示账号」区块的账号顺序（与 docs/DEMO_SCRIPT.md 的演示动线一致）
DEMO_LOGIN_USERNAMES = ("admin", "manager01", "service01", "engineer01", "finance01", "owner01")


def demo_login_accounts() -> list[dict[str, str]]:
    """登录页要展示的演示账号（账号 + 统一演示口令）。

    账号与口令只在 ``seed_demo`` 定义一次，这里只做「挑选 + 角色代码转中文名」，
    避免口令在模板里再抄一份、两处漂移；取不到就返回空列表，登录页自动隐藏该区块
    （登录页不能为一个演示提示而 500）。真实部署可用 ``DEMO_LOGIN_HINT=0`` 关掉。
    """
    if not current_app.config.get("DEMO_LOGIN_HINT", True):
        return []
    try:
        from seed_demo import ACCOUNT_SPECS, DEMO_PASSWORD
    except Exception:  # noqa: BLE001 - 演示提示缺失不应影响登录
        log.warning("演示账号提示不可用", exc_info=True)
        return []
    specs = {spec["username"]: spec for spec in ACCOUNT_SPECS}
    accounts: list[dict[str, str]] = []
    for username in DEMO_LOGIN_USERNAMES:
        spec = specs.get(username)
        if spec is None:
            continue
        accounts.append(
            {
                "username": username,
                "name": spec["real_name"],
                "role": permissions.ROLE_NAMES.get(spec["role"], spec["role"]),
                "password": DEMO_PASSWORD,
            }
        )
    return accounts


def view_login():
    """登录：哈希校验 + CSRF；成功后把 user_id / auth_version 写进 session。"""
    next_url = _text(request.values.get("next"))
    policy = current_policy()
    if request.method == "POST":
        check_csrf()
        username = _text(request.form.get("username"))
        password = request.form.get("password") or ""
        user = _db().execute(
            select(User).where(User.username == username, User.deleted.is_(False))
        ).scalars().first()
        if user is None or not user.active or not check_password_hash(user.password_hash or "", password):
            log.info("登录失败：%s", username)
            return render_template(
                "login.html",
                error="用户名或密码不正确",
                next=next_url,
                username=username,
                demo_accounts=demo_login_accounts(),
            )
        session.clear()
        session[SESSION_USER_ID] = user.id
        session[SESSION_AUTH_VERSION] = int(user.auth_version)
        session.permanent = True
        session[CSRF_FIELD] = secrets.token_urlsafe(32)
        flash(f"欢迎回来，{user.real_name or user.username}", "success")
        return redirect(_safe_next(next_url, url_for("dashboard")))
    if policy.user is not None:
        return redirect(_safe_next(next_url, url_for("dashboard")))
    return render_template(
        "login.html",
        error=None,
        next=next_url,
        username="",
        demo_accounts=demo_login_accounts(),
    )


def view_logout():
    """退出登录（POST + CSRF）。"""
    check_csrf()
    session.clear()
    return redirect(url_for("login"))


def view_health():
    """健康检查：数据库可连返回 200。"""
    ok = app_db.ping()
    return {"status": "ok" if ok else "degraded", "database": ok, "app_env": current_app.config.get("APP_ENV")}, (
        200 if ok else 503
    )


def view_dashboard():
    """工作台：6 个状态卡 + 最近工单 + 身份卡。"""
    _login_required()
    data = queries.dashboard(current_policy())
    return render_template(
        "dashboard.html",
        identity=data["identity"],
        status_counts=[Row({"status": c["status"], "text": c["text"], "count": c["count"], "class": c["class"]}) for c in data["status_counts"]],
        recent_orders=[Row(item) for item in data["recent_orders"]],
        my_stats=Row(data["my_stats"]),
    )


def view_houses():
    """房屋：小区 → 楼栋 → 房屋 三级联动。"""
    _login_required()
    policy = current_policy()
    policy.require("house.read")
    community_id = _int_arg("community")
    building_id = _int_arg("building")
    keyword = _text(request.args.get("keyword"))
    status_filter = _text(request.args.get("status"))
    page = _int_arg("page") or 1

    communities = queries.list_communities(policy, page_size=100)["items"]
    buildings = (
        queries.list_buildings(policy, community_id=community_id, page_size=200)["items"] if community_id else []
    )
    data = (
        queries.list_houses(
            policy, building=building_id, keyword=keyword or None, status=status_filter or None, page=page, page_size=50
        )
        if building_id
        else {"items": [], "total": 0, "page": 1, "pages": 1}
    )
    return render_template(
        "houses.html",
        communities=[Row(item) for item in communities],
        buildings=[Row(item) for item in buildings],
        houses=[Row(item) for item in data["items"]],
        selected_community_id=community_id,
        selected_building_id=building_id,
        keyword=keyword,
        status_filter=status_filter,
        house_status_options=queries.options_meta()["house_status"],
        can_edit=policy.has("house.write"),
        **page_of(data),
    )


def view_houses_post(entity: str, action: str):
    """``/houses/<entity>/<action>``：小区 / 楼栋 / 房屋 的增删改。"""
    check_csrf()
    policy = current_policy()
    form = request.form
    try:
        if entity == "community":
            policy.require("community.write")
            if action == "create":
                services.create_community(policy, name=form.get("name"), address=form.get("address"))
            elif action == "update":
                services.update_community(
                    policy, community_id=form.get("id"), name=form.get("name"), address=form.get("address")
                )
            elif action == "delete":
                services.delete_community(policy, community_id=form.get("id"))
            else:
                abort(404)
        elif entity == "building":
            policy.require("community.write")
            if action == "create":
                services.create_building(policy, community_id=form.get("community_id"), name=form.get("name"))
            elif action == "update":
                services.update_building(policy, building_id=form.get("id"), name=form.get("name"))
            elif action == "delete":
                services.delete_building(policy, building_id=form.get("id"))
            else:
                abort(404)
        elif entity == "house":
            policy.require("house.write")
            if action == "create":
                services.create_house(
                    policy,
                    building_id=form.get("building_id"),
                    unit=form.get("unit"),
                    room=form.get("room"),
                    area=form.get("area"),
                    status=form.get("status") or 0,
                )
            elif action == "update":
                services.update_house(
                    policy,
                    house_id=form.get("id"),
                    unit=form.get("unit"),
                    room=form.get("room"),
                    area=form.get("area"),
                    status=form.get("status"),
                )
            elif action == "delete":
                services.delete_house(policy, house_id=form.get("id"))
            else:
                abort(404)
        else:
            abort(404)
    except services.ServiceError as exc:
        _db().rollback()
        flash(exc.message, "error")
        return redirect(request.referrer or url_for("houses"))
    flash("操作成功", "success")
    return redirect(request.referrer or url_for("houses"))


def view_persons():
    """人员：档案列表 + 有效关系列表（关系登记/结束的表单在同一页）。"""
    _login_required()
    policy = current_policy()
    policy.require("person.read")
    keyword = _text(request.args.get("keyword"))
    page = _int_arg("page") or 1

    data = queries.list_persons(policy, keyword=keyword or None, page=page, page_size=20)
    relations = queries.list_relations(policy, page=1, page_size=50)["items"]
    houses = queries.list_houses(policy, page_size=200)["items"] if policy.has("relation.write") else []
    return render_template(
        "persons.html",
        persons=[Row(item) for item in data["items"]],
        relations=[Row(item) for item in relations],
        houses=[Row({"id": h["id"], "label": h["full_name"], "full_name": h["full_name"]}) for h in houses],
        relation_options=queries.options_meta()["relations"],
        keyword=keyword,
        **page_of(data),
    )


def view_persons_post(entity: str, action: str):
    """``/persons/<entity>/<action>``：人员档案与房屋关系。"""
    check_csrf()
    policy = current_policy()
    form = request.form
    try:
        if entity == "person":
            policy.require("person.write")
            if action == "create":
                services.create_person(policy, name=form.get("name"), phone=form.get("phone"))
            elif action == "update":
                services.update_person(policy, person_id=form.get("id"), name=form.get("name"), phone=form.get("phone"))
            elif action == "delete":
                services.delete_person(policy, person_id=form.get("id"))
            else:
                abort(404)
        elif entity == "relation":
            policy.require("relation.write")
            if action == "create":
                services.bind_relation(
                    policy, house_id=form.get("house_id"), person_id=form.get("person_id"), relation=form.get("relation")
                )
            elif action == "end":
                services.end_relation(policy, relation_id=form.get("relation_id") or form.get("id"), reason=form.get("reason"))
            else:
                abort(404)
        else:
            abort(404)
    except services.ServiceError as exc:
        _db().rollback()
        flash(exc.message, "error")
        return redirect(request.referrer or url_for("persons"))
    flash("操作成功", "success")
    return redirect(request.referrer or url_for("persons"))


def view_orders():
    """工单列表：状态页签 + 关键词 + 只看我的 + 分页。"""
    _login_required()
    policy = current_policy()
    policy.require("order.read")
    status_filter = _text(request.args.get("status"))
    keyword = _text(request.args.get("keyword"))
    mine = _text(request.args.get("mine"))
    page = _int_arg("page") or 1

    data = queries.list_work_orders(
        policy,
        status=status_filter or None,
        keyword=keyword or None,
        mine=bool(mine),
        page=page,
        page_size=20,
    )
    counts = queries.order_status_summary(policy)
    return render_template(
        "orders.html",
        orders=[Row(item) for item in data["items"]],
        status_counts=[Row({"status": c["status"], "text": c["text"], "count": c["count"], "class": c["class"]}) for c in queries.status_counts(policy, counts)],
        status_filter=status_filter,
        keyword=keyword,
        mine=mine,
        community=_text(request.args.get("community")),
        **page_of(data),
    )


def view_order_new():
    """报修表单。"""
    _login_required()
    policy = current_policy()
    policy.require("order.create")
    options = queries.options_meta()
    return render_template(
        "order_form.html",
        houses=[Row({"id": h["id"], "full_name": h["full_name"], "label": h["full_name"]}) for h in queries.list_houses(policy, page_size=200)["items"]],
        category_options=options["categories"],
        urgency_options=options["urgencies"],
        form=Row({}),
        error=None,
    )


def view_order_create():
    """提交报修；失败时把中文错误回填到表单。"""
    check_csrf()
    policy = current_policy()
    form = request.form
    try:
        result = services.create_work_order(
            policy,
            house_id=form.get("house_id"),
            contact_name=form.get("contact_name"),
            contact_phone=form.get("contact_phone"),
            category=form.get("category"),
            description=form.get("description"),
            urgency=form.get("urgency") or 0,
        )
    except services.ServiceError as exc:
        _db().rollback()
        options = queries.options_meta()
        return (
            render_template(
                "order_form.html",
                houses=[Row({"id": h["id"], "full_name": h["full_name"], "label": h["full_name"]}) for h in queries.list_houses(policy, page_size=200)["items"]],
                category_options=options["categories"],
                urgency_options=options["urgencies"],
                form=Row(dict(form)),
                error=exc.message,
            ),
            400,
        )
    flash(result.get("message") or "报修已登记", "success")
    order_id = result.get("id") or result.get("order_id")
    return redirect(url_for("order_detail", order_id=order_id) if order_id else url_for("orders"))


def view_order_detail(order_id: int):
    """工单详情：流转时间线 + 后端算好的可执行操作 + 派单候选人。"""
    _login_required()
    policy = current_policy()
    policy.require("order.read")
    order = queries.get_work_order(policy, order_id)
    actions = order.get("actions") or []
    staff = []
    if any(item["name"] == "assign" for item in actions) and policy.has("staff.read"):
        staff = queries.list_staff(policy, role="engineer", page_size=50)["items"]
    return render_template(
        "order_detail.html",
        order=Row(order),
        logs=[Row(item) for item in (order.get("logs") or [])],
        actions=[Row(item) for item in actions],
        staff=[Row(item) for item in staff],
    )


def view_order_action(order_id: int, action: str):
    """工单动作：状态名严格限定在契约清单内。"""
    check_csrf()
    policy = current_policy()
    form = request.form
    try:
        if action == "assign":
            services.assign_work_order(policy, order_id, repairer=form.get("repairer"), note=_text(form.get("note")))
        elif action == "accept":
            services.accept_work_order(policy, order_id, note=_text(form.get("note")))
        elif action == "progress":
            services.add_order_progress(policy, order_id, note=form.get("note"))
        elif action == "finish":
            services.finish_work_order(policy, order_id, note=_text(form.get("note")))
        elif action == "verify":
            services.verify_work_order(policy, order_id, note=_text(form.get("note")))
        elif action == "reopen":
            # 验收不通过 → 退回返修（待验收 → 维修中）。模板早就渲染了这个按钮，
            # 但这里一直没接分支，点下去就是 404。
            services.reopen_work_order(policy, order_id, reason=_text(form.get("note")))
        elif action == "cancel":
            services.cancel_work_order(policy, order_id, reason=form.get("reason"))
        elif action == "rate":
            services.rate_work_order(policy, order_id, rating=form.get("rating"), note=_text(form.get("note")))
        else:
            abort(404)
    except services.ServiceError as exc:
        _db().rollback()
        flash(exc.message, "error")
        return redirect(url_for("order_detail", order_id=order_id))
    flash("操作成功", "success")
    return redirect(url_for("order_detail", order_id=order_id))


def view_audit():
    """操作审计：页面操作与 AI 助手同表，可按来源筛选。"""
    _login_required()
    policy = current_policy()
    policy.require("audit.read")
    source_filter = _text(request.args.get("source"))
    keyword = _text(request.args.get("keyword"))
    page = _int_arg("page") or 1
    data = queries.list_audit_logs(
        policy, keyword=keyword or None, source=source_filter or None, page=page, page_size=20
    )
    return render_template(
        "audit.html",
        logs=[Row(item) for item in data["items"]],
        source_filter=source_filter,
        keyword=keyword,
        **page_of(data),
    )


# --------------------------------------------------------------------------
# 装配
# --------------------------------------------------------------------------
def register_jinja(app: Flask) -> None:
    """Jinja 过滤器 + 全局函数 + 每页上下文（模板实际读取的键）。"""
    app.jinja_env.filters["cn_time"] = cn_time

    @app.context_processor
    def inject_globals():  # pragma: no cover - 由 Flask 调用
        try:
            policy = current_policy()
            user = policy.user
            identity = policy.identity()
            return {
                "app_name": app.config.get("APP_NAME", "美家物业"),
                "current_user": user,
                "identity": Row(identity) if user else None,
                "nav": build_nav(policy) if user else [],
                "csrf_token": _csrf_token(),
                "can": policy.has,
                "now": datetime.now(),
            }
        except Exception:  # 数据库不可用时也要能渲染错误页
            log.warning("渲染上下文失败", exc_info=True)
            return {
                "app_name": app.config.get("APP_NAME", "美家物业"),
                "current_user": None,
                "identity": None,
                "nav": [],
                "csrf_token": session.get(CSRF_FIELD) or "",
                "can": lambda *_: False,
                "now": datetime.now(),
            }


def load_agent_settings(application: "Flask") -> None:
    """把智能体运行参数放进 ``app.config['AGENT_SETTINGS']``（``agent`` 蓝图需要）。

    缺少依赖或凭据时只记录告警并置 None，页面仍可打开、/ai 会给出人话提示，
    不影响其余业务模块启动。
    """
    try:
        from agent.runtime import AgentSettings  # type: ignore

        project_dir = os.path.dirname(os.path.abspath(__file__))
        application.config["AGENT_SETTINGS"] = AgentSettings.from_env(project_dir=project_dir)
        log.info("智能体运行时配置就绪：provider=%s model=%s",
                 application.config["AGENT_SETTINGS"].provider,
                 application.config["AGENT_SETTINGS"].model)
    except Exception:  # noqa: BLE001 - 智能体不可用不应阻断主站
        log.warning("agent.runtime 未就绪，/ai 将提示运行时不可用", exc_info=True)
        application.config["AGENT_SETTINGS"] = None


def shutdown_agent() -> None:
    """进程退出时回收 dsh 子进程。"""
    try:
        from agent.routes import shutdown_agent_runtimes  # type: ignore

        shutdown_agent_runtimes()
    except Exception:  # noqa: BLE001
        pass


def register_routes(app: Flask, with_ai: bool = True) -> None:
    """注册全部页面路由。

    端点名与模板 ``url_for`` 完全一致：``houses_command`` / ``persons_command`` /
    ``order_command`` / ``order_create``。``with_ai=False`` 时把 ``/ai*`` 交给 agent 蓝图。
    """
    app.add_url_rule("/login", "login", view_login, methods=["GET", "POST"])
    app.add_url_rule("/logout", "logout", view_logout, methods=["POST"])
    app.add_url_rule("/health", "health", view_health, methods=["GET"])
    app.add_url_rule("/ready", "ready", view_health, methods=["GET"])

    app.add_url_rule("/", "dashboard", view_dashboard, methods=["GET"])

    app.add_url_rule("/houses", "houses", view_houses, methods=["GET"])
    app.add_url_rule("/houses/<entity>/<action>", "houses_command", view_houses_post, methods=["POST"])

    app.add_url_rule("/persons", "persons", view_persons, methods=["GET"])
    app.add_url_rule("/persons/<entity>/<action>", "persons_command", view_persons_post, methods=["POST"])

    app.add_url_rule("/orders", "orders", view_orders, methods=["GET"])
    app.add_url_rule("/orders", "order_create", view_order_create, methods=["POST"])
    app.add_url_rule("/orders/new", "order_new", view_order_new, methods=["GET"])
    app.add_url_rule("/orders/<int:order_id>", "order_detail", view_order_detail, methods=["GET"])
    app.add_url_rule("/orders/<int:order_id>/<action>", "order_command", view_order_action, methods=["POST"])

    app.add_url_rule("/audit", "audit", view_audit, methods=["GET"])


def register_agent_routes(app: Flask) -> bool:
    """注册 captain 提供的 ``agent.routes.ai_bp``（/ai 页面与 SSE 都归它）。"""
    if not app.config.get("USE_AGENT_ROUTES", True):
        return False
    try:
        from agent.routes import ai_bp  # type: ignore
    except Exception:  # pragma: no cover - 取决于智能体模块是否就绪
        log.info("agent.routes 未就绪，本次不注册 /ai 蓝图", exc_info=True)
        return False
    app.register_blueprint(ai_bp)
    return True




def register_ai_fallbacks(app: Flask) -> bool:
    """在没有 ``ai`` 蓝图时，补一组 app 级 AI 端点（base.html 宏的回退分支用）。

    只做「不炸」：页面跳 `/ai`，接口返回可读 JSON 提示。有蓝图时什么都不做，
    避免出现两套同名路由。
    """
    if "ai" in app.blueprints:
        return False
    from flask import jsonify, request

    def _hint() -> tuple:
        return jsonify({"ok": False, "error": "AI 助手模块未加载，暂时无法使用"}), 503

    app.add_url_rule("/ai", "ai_page", lambda: _hint())
    app.add_url_rule("/ai/chat", "ai_chat", _hint, methods=["GET", "POST"])
    app.add_url_rule("/ai/actions", "ai_actions", _hint)
    app.add_url_rule(
        "/ai/actions/<int:action_id>/confirm", "ai_actions_confirm", _hint, methods=["POST"]
    )
    app.add_url_rule(
        "/ai/actions/<int:action_id>/cancel", "ai_actions_cancel", _hint, methods=["POST"]
    )
    app.add_url_rule(
        "/ai/sessions/<int:session_id>", "ai_session_messages", _hint
    )
    log.warning("未加载 ai 蓝图，已注册 %d 个 AI 回退端点", len(app.url_map._rules) and 6)
    return True


def register_leases_page(app: Flask) -> bool:
    """注册租赁页面（``ops_routes`` 之外的独立模块，端点名与模板一致）。"""
    try:
        from leases_routes import register_leases_routes  # type: ignore
    except Exception:  # pragma: no cover
        log.info("leases_routes 未就绪，本次不注册租赁页", exc_info=True)
        return False
    register_leases_routes(app)
    return True


def register_ops_module_routes(app: Flask) -> bool:
    """注册 ``ops_routes``（投诉/访客/车辆车位/设备巡检/收费页面）。

    兼容两种交付形态：优先注册蓝图 ``ops_bp``；模块若只提供 ``register_ops_routes(app)``
    就回退到函数式注册（当前实现走的是后者，端点名为 bills/complaints/visitors/… ）。
    """
    try:
        from ops_routes import ops_bp  # type: ignore

        app.register_blueprint(ops_bp)
        return True
    except Exception:
        log.info("ops_routes 未提供 ops_bp 蓝图，改用 register_ops_routes(app)")
    try:
        from ops_routes import register_ops_routes  # type: ignore
    except Exception:  # pragma: no cover - 运营模块未就绪时不影响主站
        log.info("ops_routes 未就绪，本次不注册运营模块页面", exc_info=True)
        return False
    register_ops_routes(app)
    return True


def register_error_handlers(app: Flask) -> None:
    """400/401/403/404/500 统一渲染 ``error.html``（未登录时渲染登录页外壳）。"""

    #: 错误页渲染失败时的最小兜底（避免「错误页自己也 500」，把原始错误彻底掩盖）
    _FALLBACK_HTML = (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>{code}</title></head><body style=\"font-family:system-ui;padding:40px\">"
        "<h1>{code} {title}</h1><p>{message}</p><p><a href=\"/\">返回工作台</a></p>"
        "</body></html>"
    )

    def render_error(code: int, message: str):
        if wants_json():
            return {"ok": False, "code": code, "message": message}, code
        title = ERROR_TITLES.get(code, "出错了")
        try:
            return (
                render_template(
                    "error.html",
                    code=code,
                    title=title,
                    message=message,
                    back_url=url_for("dashboard")
                    if session.get(SESSION_USER_ID)
                    else url_for("login"),
                ),
                code,
            )
        except Exception:  # pragma: no cover - 模板出问题时仍要给出可读错误页
            current_app.logger.exception("错误页模板渲染失败，退化为最小 HTML（code=%s）", code)
            return _FALLBACK_HTML.format(code=code, title=title, message=message), code

    @app.errorhandler(HTTPException)
    def handle_http_error(exc: HTTPException):  # pragma: no cover - 由 Flask 调用
        code = exc.code or 500
        message = exc.description or ERROR_TITLES.get(code, "请求无法完成")
        if code == 401 and not session.get(SESSION_USER_ID):
            return redirect(url_for("login", next=request.full_path if request.method == "GET" else "/"))
        return render_error(code, message)

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception):  # pragma: no cover - 由 Flask 调用
        if isinstance(exc, HTTPException):
            return handle_http_error(exc)
        if isinstance(exc, services.ServiceError):
            _db().rollback()
            flash(exc.message, "error")
            return redirect(request.referrer or url_for("dashboard"))
        app.logger.exception("未处理的异常：%s", exc)
        return render_error(500, "系统开小差了，请稍后重试")


def create_app(overrides: dict | None = None) -> Flask:
    """应用工厂（``waitress``/``gunicorn`` 用 ``app:create_app()``）。"""
    cfg = app_config.get_config(overrides)
    application = Flask(
        __name__, template_folder="templates", static_folder="static", static_url_path="/static"
    )
    application.config.update(cfg.to_flask())
    if overrides:
        application.config.update(overrides)
    application.json.ensure_ascii = False
    application.json.sort_keys = False
    application.secret_key = application.config["SECRET_KEY"]

    # 反向代理（nginx 子路径 /wuye/）支持：认 X-Forwarded-Prefix，把 SCRIPT_NAME 设成真实前缀，
    # url_for / request.script_root 才会生成 /wuye/... 的地址。
    # 直连（本地开发、/health 自检）没有这个头，行为完全不变。
    application.wsgi_app = ProxyFix(
        application.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1
    )

    app_db.init_app(application)
    register_jinja(application)
    agent_owns_ai = register_agent_routes(application)
    register_ai_fallbacks(application)

    # 把真实存在的 AI 端点写进 config，供模板宏判定（request.blueprints 是「当前请求」的
    # 蓝图集合，在 dashboard 等页面恒为空，用它判定会误判）。
    _eps = {rule.endpoint for rule in application.url_map.iter_rules()}
    application.config["AI_ENDPOINTS"] = sorted(e for e in _eps if e == "ai_page" or e.startswith("ai."))

    load_agent_settings(application)
    register_routes(application, with_ai=not agent_owns_ai)
    register_ops_module_routes(application)
    register_leases_page(application)
    register_error_handlers(application)
    return application


def main() -> None:
    """``python app.py``：本地 http 演示入口（回环地址上关闭 Secure Cookie 并打印地址）。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = app_config.get_config()
    run_app = create_app()
    if run_app.config.get("SESSION_COOKIE_SECURE"):
        run_app.config["SESSION_COOKIE_SECURE"] = False
        print("[提示] 本地入口已关闭 Secure Cookie（线上请用 Waitress 并保持 COOKIE_SECURE=1）")
    host = run_app.config.get("HOST", "127.0.0.1")
    port = int(run_app.config.get("PORT", 5000))
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"[启动] {cfg.APP_NAME} http://{shown}:{port}/   健康检查 http://{shown}:{port}/health")
    run_app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
