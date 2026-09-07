import hashlib
import json
import os
import secrets
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash

from agent_tools import available_commands
from app import create_app
from database import make_engine
from models import AiGrant, AuditLog, Building, HousePerson, User, UserRole, UserScope, utcnow
from permissions import Policy, ROLES
from property_service import PropertyService


def main():
    with tempfile.TemporaryDirectory(prefix="wuye-agent-") as tmp:
        db_path = Path(tmp) / "agent.sqlite"
        url = "sqlite+pysqlite:///" + str(db_path)
        app = create_app({"TESTING": True, "DATABASE_URL": url, "SECRET_KEY": "agent-fixture-secret"})
        factory = app.extensions["db_session"]
        with factory() as db:
            admin = User(username="agent-admin", password_hash=generate_password_hash("Agent-pass-123"), role=0, real_name="验收管理员")
            db.add(admin)
            db.flush()
            service = PropertyService(db, admin)
            building = service.run("building.save", {"community_id": 1, "name": "23栋", "floors": 25})["id"]
            unit = service.run("unit.save", {"building_id": building, "name": "3单元"})["id"]
            house = service.run("house.save", {"unit_id": unit, "room_no": 311, "area": "100", "usage": "residential", "occupancy": "vacant"})["id"]
            person = service.run("person.save", {"community_id": 1, "name": "王五", "phone": "13800000123"})["id"]
            empty_building = service.run("building.save", {"community_id": 1, "name": "空楼栋", "floors": 1})["id"]
            db.commit()

            role_rows = {}
            for code in ROLES:
                user = db.scalar(select(User).where(User.username == "agent-" + code))
                if not user:
                    user = User(username="agent-" + code, password_hash=generate_password_hash("Agent-pass-123"), role=1, real_name=code)
                    db.add(user)
                    db.flush()
                    if not db.scalar(select(UserRole).where(UserRole.user_id == user.id, UserRole.role_code == code)):
                        db.add(UserRole(user_id=user.id, role_code=code))
                    db.add(UserScope(user_id=user.id, kind="community", community_id=1))
            db.commit()
            for code in ROLES:
                user = db.scalar(select(User).where(User.username == "agent-" + code))
                policy = Policy(db, user)
                commands = available_commands(user)
                leaked = [item["command"] for item in commands if item["permission"] not in policy.permissions]
                role_rows[code] = {"permissions": len(policy.permissions), "commands": len(commands), "leaked": leaked}

            token = secrets.token_urlsafe(32)
            grant = AiGrant(id=str(uuid.uuid4()), user_id=admin.id, auth_version=admin.auth_version,
                            token_hash=hashlib.sha256(token.encode()).hexdigest(),
                            expires_at=utcnow() + timedelta(minutes=3))
            db.add(grant)
            db.commit()

        client = app.test_client()
        def tool(operation, command, arguments):
            return client.post("/api/agent/tools", json={"request_token": token, "operation": operation,
                                                          "command": command, "arguments_json": json.dumps(arguments, ensure_ascii=False)})

        executed = tool("execute", "relation.bind_by_name", {"community_id": 1, "building_name": "23栋", "unit": "3单元", "room_no": 311, "person_name": "王五"})
        pending = tool("execute", "property.archive", {"kind": "building", "id": empty_building, "version": 1, "reason": "高风险验收"})
        unknown = tool("execute", "sql.execute", {"sql": "DROP TABLE sys_user"})
        injected = tool("execute", "person.save", {"community_id": 1, "name": "注入", "phone": "13800000000", "role": "superadmin"})
        db = factory()
        try:
            relationship_count = db.scalar(select(func.count(HousePerson.id)).where(HousePerson.house_id == house, HousePerson.person_id == person))
            agent_audit_count = db.scalar(select(func.count(AuditLog.id)).where(AuditLog.source == "agent"))
            building_deleted = db.get(Building, empty_building).deleted
            admin = db.get(User, db.scalar(select(AiGrant.user_id).where(AiGrant.id == grant.id)))
            admin.auth_version += 1
            db.commit()
        finally:
            db.close()
        replay = tool("context", "", {})
        result = {
            "role_capability_diff": role_rows,
            "all_roles_no_leak": all(not row["leaked"] for row in role_rows.values()),
            "direct_execute_status": executed.status_code,
            "direct_execute_payload_status": executed.json.get("status") if executed.is_json else None,
            "relationship_rows": relationship_count,
            "agent_audit_rows": agent_audit_count,
            "high_risk_status": pending.status_code,
            "high_risk_payload_status": pending.json.get("status") if pending.is_json else None,
            "high_risk_building_unchanged": not building_deleted,
            "unknown_tool_status": unknown.status_code,
            "injected_extra_field_status": injected.status_code,
            "revoked_token_status": replay.status_code,
        }
        app.extensions["db_engine"].dispose()
        Path(__file__).with_name("agent_acceptance_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
