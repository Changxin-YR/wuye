"""租赁页面路由（``/leases``）。

设计说明：页面上的"租赁"以**房屋人员关系（租户）**为载体（入住 = 绑定 tenant
关系，退租 = 结束该关系），并且**同一笔操作会写租赁合同记录**——月租金在合同里。
列表的「月租金」列按 房屋+承租人 去匹配「在租」的合同，没有合同的旧关系显示「—」。

端点名用裸名（``leases`` / ``leases_command``），与模板里的 ``url_for`` 一致。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from flask import Flask, abort, flash, redirect, render_template, request, url_for
from sqlalchemy import select

import db
import models
import queries
import services
from models import HousePerson, Person, User
from permissions import Policy

PAGE_SIZE = 20
TENANT = "tenant"


def _db():
    return db.get_session()


def _current_user(session) -> Optional[User]:
    from flask import session as flask_session

    user_id = flask_session.get("user_id")
    if not user_id:
        return None
    user = session.get(User, int(user_id))
    if user is None or not user.active:
        return None
    if int(flask_session.get("auth_version", -1)) != int(user.auth_version):
        return None
    return user


def _policy(source: str = "web") -> Policy:
    session = _db()
    user = _current_user(session)
    if user is None:
        abort(401)
    return Policy(session, user, source=source)


def _csrf_ok() -> bool:
    from flask import session as flask_session

    sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
    expected = flask_session.get("csrf_token") or flask_session.get("_csrf_token")
    return bool(expected and sent and str(expected) == str(sent))


def _form(name: str, default: Any = None) -> Any:
    value = request.form.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _money_text(value) -> str:
    """金额显示：4500 → 4500，4500.5 → 4500.5；没有金额返回空串。"""
    if value in (None, ""):
        return ""
    amount = float(value)
    if amount == 0:
        return ""
    return f"{amount:.2f}".rstrip("0").rstrip(".") or "0"


def _relation_rows(policy: Policy, keyword: str, page: int) -> dict:
    """在租/已退租的租户关系列表（分页）。"""
    session = policy.db
    # 数据范围直接下推到 SQL（Policy.query 已经处理了 deleted 与行级范围）
    stmt = (
        policy.query(HousePerson)
        .where(HousePerson.relation == TENANT)
        .order_by(HousePerson.status.asc(), HousePerson.id.desc())
    )
    houses = {row.id: row for row in session.execute(select(models.House)).scalars().all()}
    people = {row.id: row for row in session.execute(select(Person)).scalars().all()}
    # 月租金来自「在租」的租赁合同：列表主体是关系记录，按 房屋+承租人 对上合同
    rents = {
        (row.house_id, row.person_id): row.rent
        for row in session.execute(
            select(models.Lease).where(models.Lease.status == 0, models.Lease.deleted.is_(False))
        ).scalars().all()
    }
    rows = list(session.execute(stmt).scalars().all())
    if keyword:
        text = keyword.strip()
        def match(row):
            house = houses.get(row.house_id)
            person = people.get(row.person_id)
            haystack = " ".join([
                house.house_text if house else "", person.name if person else "",
                person.phone if person else "",
            ])
            return text in haystack
        rows = [row for row in rows if match(row)]

    total = len(rows)
    page = max(1, page)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)
    window = rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
    items = []
    for row in window:
        house = houses.get(row.house_id)
        person = people.get(row.person_id)
        active = row.status == "active"
        items.append({
            "id": row.id,
            "house_text": house.house_text if house else f"房屋#{row.house_id}",
            "person_name": person.name if person else f"人员#{row.person_id}",
            "phone": person.phone if person else "",
            "relation_text": "租户",
            "rent": float(rents.get((row.house_id, row.person_id)) or 0),
            "rent_text": _money_text(rents.get((row.house_id, row.person_id))),
            "start_at": row.start_at,
            "status": row.status,
            "status_text": "在租" if active else "已退租",
            "status_class": "working" if active else "closed",
        })
    return {"items": items, "total": total, "page": page, "pages": pages}


def leases():
    """租赁页：租户关系列表 + 入住/退租表单。"""
    policy = _policy()
    policy.require("lease.write")
    keyword = (request.args.get("keyword") or "").strip()
    page = request.args.get("page", type=int) or 1
    data = _relation_rows(policy, keyword, page)
    houses = queries.list_houses(policy, page=1, page_size=200)["items"]
    persons = queries.list_persons(policy, page=1, page_size=200)["items"]
    return render_template(
        "leases.html",
        items=data["items"], total=data["total"], page=data["page"], pages=data["pages"],
        keyword=keyword, houses=houses, persons=persons,
    )


def leases_command(action: str):
    """入住 / 退租（写操作统一走 PropertyService）。"""
    if not _csrf_ok():
        flash("页面校验已过期，请刷新后重试", "error")
        return redirect(url_for("leases"))
    policy = _policy(source="web")
    try:
        if action == "check-in":
            house_id = _form("house_id")
            person_id = _form("person_id")
            if not person_id:
                name = _form("person_name")
                phone = _form("phone") or "13800000000"
                if not name:
                    raise services.ServiceError("invalid", "请选择或填写承租人")
                created = services.create_person(policy, name=name, phone=phone)
                person_id = created["id"]
            # 入住要同时做三件事：写租赁合同（月租金）、建立租户关系、房屋转「出租」。
            # check_in_lease 一次做完——以前这里只调 bind_relation，表单上的「月租金」
            # 被直接丢掉，所以在租列表的月租金永远是「—」。
            services.check_in_lease(
                policy,
                house_id=house_id,
                person_id=person_id,
                rent=_form("rent") or 0,
                start_at=_form("start_at") or date.today().isoformat(),
                end_at=_form("end_at"),
            )
            flash("已办理入住（租赁合同与租户关系已登记）", "success")
        elif action == "check-out":
            relation_id = _form("relation_id")
            reason = _form("reason") or "页面办理退租"
            relation = policy.db.get(HousePerson, int(relation_id)) if relation_id else None
            lease = None
            if relation is not None:
                lease = policy.db.execute(
                    select(models.Lease).where(
                        models.Lease.house_id == relation.house_id,
                        models.Lease.person_id == relation.person_id,
                        models.Lease.status == 0,
                        models.Lease.deleted.is_(False),
                    )
                ).scalars().first()
            if lease is not None:
                # 有合同时由 check_out_lease 一并结束合同 + 关系 + 房屋状态；
                # 否则合同会一直挂在「在租」，下次入住会被判「已有在租记录」
                services.check_out_lease(policy, lease_id=lease.id, reason=reason)
            else:
                services.end_relation(policy, relation_id=relation_id, reason=reason)
            flash("已办理退租", "success")
        else:
            raise services.ServiceError("invalid", "不支持的操作")
    except services.ServiceError as exc:
        flash(exc.message, "error")
    return redirect(url_for("leases"))


def register_leases_routes(app: Flask) -> None:
    """端点名与 ``templates/leases.html`` 里的 ``url_for`` 完全一致。"""
    app.add_url_rule("/leases", "leases", leases, methods=["GET"])
    app.add_url_rule("/leases/<action>", "leases_command", leases_command, methods=["POST"])
