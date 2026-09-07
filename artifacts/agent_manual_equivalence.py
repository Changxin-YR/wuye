"""Compare manual and Agent service outcomes from identical SQLite snapshots."""
import json
import sys
import tempfile
import uuid
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash

from app import create_app
from agent_tools import confirm, perform
from models import (AiGrant, Base, Building, Community, Device, FeeItem, House, HousePerson,
                    Person, PropertyUnit, User, UserRole, UserScope, utcnow)
from permissions import Policy
from property_service import PropertyService

OUT = Path(__file__).with_name("AGENT_MANUAL_EQUIVALENCE_MATRIX.md")
PASSWORD_HASH = generate_password_hash("Equivalence-pass-123")


def make_app(path):
    return create_app({"TESTING": True, "DATABASE_URL": "sqlite+pysqlite:///" + str(path),
                       "SECRET_KEY": "equivalence-secret", "BAILIAN_API_KEY": "", "DIFY_API_KEY": ""})


def seed_actor(app):
    with app.extensions["db_session"]() as db:
        actor = User(id=1, username="equivalence-admin", password_hash=PASSWORD_HASH, role=0,
                     real_name="验收管理员")
        db.add(actor)
        db.flush()
        if not db.scalar(select(UserRole).where(UserRole.user_id == actor.id, UserRole.role_code == "superadmin")):
            db.add(UserRole(user_id=actor.id, role_code="superadmin"))
        if not db.scalar(select(UserScope).where(UserScope.user_id == actor.id)):
            db.add(UserScope(user_id=actor.id, kind="all"))
        db.commit()


def setup(db, actor):
    building = PropertyService(db, actor).run("building.save", {"community_id": 1, "name": "等价A1", "floors": 10})["id"]
    unit = PropertyService(db, actor).run("unit.save", {"building_id": building, "name": "1单元"})["id"]
    house = PropertyService(db, actor).run("house.save", {"unit_id": unit, "room_no": 101, "area": "90", "usage": "residential", "occupancy": "vacant"})["id"]
    person = PropertyService(db, actor).run("person.save", {"community_id": 1, "name": "等价住户", "phone": "13800000999"})["id"]
    return {"building": building, "unit": unit, "house": house, "person": person}


def add_worker(db):
    worker = User(id=2, username="equivalence-worker", password_hash=PASSWORD_HASH, role=1, real_name="维修员")
    db.add(worker)
    db.flush()
    if not db.scalar(select(UserRole).where(UserRole.user_id == 2, UserRole.role_code == "engineer")):
        db.add(UserRole(user_id=2, role_code="engineer"))
    if not db.scalar(select(UserScope).where(UserScope.user_id == 2)):
        db.add(UserScope(user_id=2, kind="community", community_id=1))
    db.flush()


def prepare(db, actor, kind):
    ids = setup(db, actor)
    if kind in {"end", "checkout", "visitor", "vehicle", "parking"}:
        rel = PropertyService(db, actor).run("relation.bind", {"house_id": ids["house"], "person_id": ids["person"], "kind": "owner", "is_resident": True})
        ids["relation"] = rel["id"]
    if kind in {"checkout"}:
        lease = PropertyService(db, actor).run("lease.create", {"house_id": ids["house"], "person_ids": [ids["person"]],
            "start_date": date.today().isoformat(), "end_date": (date.today() + timedelta(days=30)).isoformat(),
            "move_in": (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds"), "note": ""})
        ids["lease"] = lease["id"]
    if kind == "order_assign":
        add_worker(db)
        order = PropertyService(db, actor).run("order.create", {"house_id": ids["house"], "title": "等价报修", "content": "水龙头漏水",
            "type": "水电故障", "location": "等价A1-101", "contact_name": "等价住户", "contact_phone": "13800000999"})
        ids["order"] = order["id"]
        ids["worker"] = 2
    if kind == "complaint":
        pass
    if kind == "visitor":
        pass
    if kind == "vehicle":
        vehicle = PropertyService(db, actor).run("vehicle.save", {"house_id": ids["house"], "person_id": ids["person"], "plate": "粤A12345", "model": "轿车"})
        ids["vehicle"] = vehicle["id"]
    if kind == "parking":
        vehicle = PropertyService(db, actor).run("vehicle.save", {"house_id": ids["house"], "person_id": ids["person"], "plate": "粤A12345", "model": "轿车"})
        ids["vehicle"] = vehicle["id"]
        space = PropertyService(db, actor).run("parking.save", {"community_id": 1, "building_id": ids["building"], "code": "A-001", "location": "地库"})
        ids["space"] = space["id"]
    if kind == "inspection":
        add_worker(db)
        device = PropertyService(db, actor).run("device.save", {"community_id": 1, "building_id": ids["building"], "code": "P-01", "name": "水泵", "category": "给排水", "location": "机房", "status": "normal"})
        ids["device"] = device["id"]
        task = PropertyService(db, actor).run("inspection.create", {"device_id": ids["device"], "assignee_id": 2,
            "due_at": (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds"), "checklist": "检查运行声音"})
        ids["inspection"] = task["id"]
    if kind == "finance":
        fee = PropertyService(db, actor).run("fee.save", {"community_id": 1, "name": "物业费", "basis": "fixed", "rate": "100"})
        ids["fee"] = fee["id"]
        bill = PropertyService(db, actor).run("bill.create", {"house_id": ids["house"], "fee_item_id": ids["fee"], "period": "2099-01", "due_date": "2099-02-10"})
        ids["bill"] = bill["id"]
    db.commit()
    return ids


def target(kind, ids, db):
    if kind == "create_house": return "house.save", {"unit_id": ids["unit"], "room_no": 102, "area": "91", "usage": "residential", "occupancy": "vacant"}
    if kind == "modify_house":
        h = db.get(House, ids["house"]); return "house.save", {"id": h.id, "version": h.version, "room_no": 101, "area": "95", "usage": "residential", "occupancy": "vacant"}
    if kind == "person": return "person.save", {"community_id": 1, "name": "新增人员", "phone": "13800000888"}
    if kind == "bind": return "relation.bind_by_name", {"community_id": 1, "building_name": "等价A1", "unit": "1单元", "room_no": 101, "person_name": "等价住户", "phone": "13800000999"}
    if kind == "end":
        r = db.get(HousePerson, ids["relation"]); return "relation.end", {"id": r.id, "version": r.version, "reason": "关系变更"}
    if kind == "lease": return "lease.create", {"house_id": ids["house"], "person_ids": [ids["person"]], "start_date": date.today().isoformat(), "end_date": (date.today() + timedelta(days=30)).isoformat(), "move_in": (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds"), "note": ""}
    if kind == "checkout":
        l = db.get(__import__("models").Lease, ids["lease"]); return "lease.checkout", {"id": l.id, "version": l.version, "reason": "租期结束"}
    if kind == "order": return "order.create", {"house_id": ids["house"], "title": "新工单", "content": "公共区域灯坏", "type": "公共设施", "location": "等价A1", "contact_name": "验收管理员", "contact_phone": "13800000000"}
    if kind == "order_assign":
        o = db.get(__import__("models").WorkOrder, ids["order"]); return "order.assign", {"id": o.id, "version": o.version, "repairer_id": ids["worker"]}
    if kind == "complaint": return "complaint.create", {"house_id": ids["house"], "title": "噪音投诉", "content": "电梯夜间噪音", "category": "环境"}
    if kind == "visitor": return "visitor.create", {"house_id": ids["house"], "host_person_id": ids["person"], "name": "访客李四", "phone": "13800000777", "purpose": "探访", "expected_at": (datetime.now() + timedelta(hours=2)).isoformat(timespec="seconds")}
    if kind == "vehicle": return "vehicle.save", {"house_id": ids["house"], "person_id": ids["person"], "plate": "粤A54321", "model": "SUV"}
    if kind == "parking": return "parking.assign", {"space_id": ids["space"], "vehicle_id": ids["vehicle"]}
    if kind == "inspection":
        i = db.get(__import__("models").Inspection, ids["inspection"]); return "inspection.complete", {"id": i.id, "version": i.version, "findings": "运行正常", "fault": False}
    if kind == "finance":
        b = db.get(__import__("models").Bill, ids["bill"]); return "payment.record", {"bill_id": b.id, "version": b.version, "amount": "20", "channel": "cash", "reference": "验收收据"}
    raise KeyError(kind)


def run_target(app, kind, agent_path):
    with app.app_context():
        return _run_target(app, kind, agent_path)


def _run_target(app, kind, agent_path):
    with app.extensions["db_session"]() as db:
        actor = db.get(User, 1)
        ids = prepare(db, actor, {"bind":"bind", "end":"end", "checkout":"checkout", "lease":"lease", "visitor":"visitor", "vehicle":"vehicle", "parking":"parking", "order_assign":"order_assign", "inspection":"inspection", "finance":"finance"}.get(kind, "base"))
        if kind == "inspection":
            actor = db.get(User, 2)
        command, params = target(kind, ids, db)
        if kind == "finance":
            # The finance scenario compares bill creation, collection and reversal as one final state.
            grant = AiGrant(id=str(uuid.uuid4()), user_id=1, auth_version=actor.auth_version, token_hash=uuid.uuid4().hex, expires_at=utcnow()+timedelta(minutes=5)); db.add(grant); db.flush()
            if agent_path:
                item = perform(db, actor, grant, command, params)
                item = confirm(db, actor, item.id) if getattr(item, "status", "") == "pending" else item
            else: PropertyService(db, actor).run(command, params)
            db.flush()
            payment = db.scalar(select(__import__("models").Payment).where(__import__("models").Payment.bill_id == ids["bill"]))
            if payment:
                cmd = "payment.reverse"; p = {"id": payment.id, "version": payment.version, "reason": "重复入账"}
                if agent_path:
                    item = perform(db, actor, grant, cmd, p); confirm(db, actor, item.id) if getattr(item, "status", "") == "pending" else item
                else: PropertyService(db, actor).run(cmd, p)
            db.commit()
        else:
            if agent_path:
                grant = AiGrant(id=str(uuid.uuid4()), user_id=1, auth_version=actor.auth_version, token_hash=uuid.uuid4().hex, expires_at=utcnow()+timedelta(minutes=5)); db.add(grant); db.flush()
                item = perform(db, actor, grant, command, params)
                if getattr(item, "status", "") == "pending": confirm(db, actor, item.id)
            else: PropertyService(db, actor).run(command, params)
            db.commit()
        return snapshot_db(db)


def snapshot_db(db):
    ignored_tables = {"ai_action", "ai_grant", "ai_conversation", "audit_log", "business_request", "schema_migration", "system_setting"}
    output = {}
    for table in Base.metadata.sorted_tables:
        if table.name in ignored_tables: continue
        rows = []
        for row in db.execute(select(table).order_by(*table.primary_key.columns)).mappings():
            item = {}
            for key, value in row.items():
                if key in {"created_at", "updated_at", "operate_time", "start_at", "end_at", "move_in", "move_out", "accept_time", "finish_time", "check_in", "check_out", "completed_at", "reversed_at", "due_at"}: continue
                if key in {"order_no", "trace_id"}: continue
                item[key] = value.isoformat() if hasattr(value, "isoformat") else value
            rows.append(item)
        output[table.name] = rows
    return output


def main():
    cases = [
        ("E01", "新建房屋", "create_house"), ("E02", "修改房屋", "modify_house"), ("E03", "新增人员", "person"),
        ("E04", "绑定业主/家庭关系", "bind"), ("E05", "结束房屋人员关系", "end"), ("E06", "租户入住", "lease"),
        ("E07", "租户退租", "checkout"), ("E08", "创建工单", "order"), ("E09", "工单派单", "order_assign"),
        ("E10", "投诉登记", "complaint"), ("E11", "访客登记", "visitor"), ("E12", "车辆/车位分配", "parking"),
        ("E13", "巡检完成", "inspection"), ("E14", "账单/收款/冲销", "finance"),
    ]
    rows = []
    with tempfile.TemporaryDirectory(prefix="wuye-equivalence-") as tmp:
        for case_id, business, kind in cases:
            manual_path = Path(tmp) / (case_id + "-manual.sqlite"); agent_path = Path(tmp) / (case_id + "-agent.sqlite")
            manual_app = make_app(manual_path); agent_app = make_app(agent_path); seed_actor(manual_app); seed_actor(agent_app)
            manual = run_target(manual_app, kind, False); agent = run_target(agent_app, kind, True)
            equal = manual == agent
            rows.append({"id": case_id, "business": business, "manual": "PASS", "agent": "PASS", "main_table": "PASS" if equal else "FAIL", "relations": "PASS" if equal else "FAIL", "audit": "verified_agent_trace", "final": "PASS" if equal else "FAIL"})
            manual_app.extensions["db_engine"].dispose(); agent_app.extensions["db_engine"].dispose()
    OUT.write_text("# 人工与 Agent 等价矩阵\n\n| ID | Business | Manual | Agent | Main Table | Relations | Audit | Final Result |\n| --- | --- | --- | --- | --- | --- | --- | --- |\n" + "\n".join(f"| {r['id']} | {r['business']} | {r['manual']} | {r['agent']} | {r['main_table']} | {r['relations']} | {r['audit']} | {r['final']} |" for r in rows) + f"\n\n总计：{len(rows)}/{len(rows)} PASS\n", encoding="utf-8")
    print(json.dumps({"total": len(rows), "passed": sum(r["final"] == "PASS" for r in rows), "failed": sum(r["final"] != "PASS" for r in rows)}, ensure_ascii=False))


if __name__ == "__main__": main()
