"""services_ops / queries_ops 的独立验证（不属于 unittest discover 的三件套）。

覆盖：
- 19 个写命令的权限、状态机、幂等（request_key）、乐观锁（expected_version）、审计留痕；
- queries_ops 的列表分页、数据范围隔离、中文文案；
- 每个命令都回读并返回 message / version。

用法::

    .venv\\Scripts\\python.exe tests\\check_ops_modules.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB = ROOT / "artifacts" / "platform" / "ops_check.db"
DB.parent.mkdir(parents=True, exist_ok=True)
for suffix in ("", "-wal", "-shm"):
    target = Path(str(DB) + suffix)
    if target.exists():
        target.unlink()
os.environ["DATABASE_URL"] = "sqlite:///" + DB.as_posix()
os.environ["APP_ENV"] = "testing"
os.environ["COOKIE_SECURE"] = "0"

from sqlalchemy import select  # noqa: E402
from werkzeug.exceptions import HTTPException  # noqa: E402

import db as app_db  # noqa: E402
import models  # noqa: E402
import permissions  # noqa: E402
import queries_ops as qo  # noqa: E402
import seed_demo  # noqa: E402
import services  # noqa: E402
import services_ops as so  # noqa: E402

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((bool(ok), label))
    mark = "[OK]  " if ok else "[FAIL]"
    line = f"{mark} {label}"
    if detail and not ok:
        line += f" —— {detail[:200]}"
    print(line, flush=True)


def main() -> int:
    app_db.create_all()
    seed_demo.seed()
    session = app_db.get_session()

    def policy(username: str) -> permissions.Policy:
        user = session.execute(select(models.User).where(models.User.username == username)).scalars().first()
        return permissions.Policy(session, user)

    admin = policy("admin")
    service = policy("service01")
    engineer = policy("engineer01")
    owner = policy("owner01")

    house = admin.db.execute(select(models.House).where(models.House.deleted.is_(False))).scalars().first()
    person = admin.db.execute(select(models.Person).where(models.Person.name == "张伟")).scalars().first()
    admin_user = admin.db.execute(
        select(models.User).where(models.User.username == "engineer01")
    ).scalars().first()

    print("\n=== 投诉：登记 → 分配 → 处理 → 结案 ===")
    complaint = so.create_complaint(
        service, house_id=house.id, content="楼道里有人堆放杂物影响通行", category="公共设施"
    )
    check(complaint.get("id") and complaint.get("version"), "create_complaint 返回 id/version")
    check(complaint.get("status") == 0 and complaint.get("status_text") == "待处理", "新投诉状态=待处理")
    check(bool(complaint.get("message")), "返回 message 供智能体汇报")

    replay = so.create_complaint(
        service, house_id=house.id, content="楼道里有人堆放杂物影响通行", category="公共设施",
        request_key="idem-complaint-1",
    )
    again = so.create_complaint(
        service, house_id=house.id, content="楼道里有人堆放杂物影响通行", category="公共设施",
        request_key="idem-complaint-1",
    )
    check(
        again.get("id") == replay.get("id") and again.get("replayed"),
        "同一 request_key 只执行一次（幂等回放）",
        f"first={replay.get('id')} second={again.get('id')}",
    )

    assigned = so.assign_complaint(service, complaint_id=complaint["id"], handler=admin_user.id, note="转工程")
    check(assigned.get("status") == 1 and assigned.get("status_text") == "处理中", "assign_complaint 待处理→处理中")
    check(bool(assigned.get("handler_name")), "投诉带处理人姓名")

    stale = None
    try:
        so.handle_complaint(service, complaint_id=complaint["id"], result="已清理", expected_version=999)
    except services.ServiceError as exc:
        stale = exc
    check(stale is not None and stale.code == "conflict", "expected_version 过期被拒（409 语义）", str(stale))

    handled = so.handle_complaint(service, complaint_id=complaint["id"], result="已联系业主清理完毕")
    check(handled.get("status") == 1 and handled.get("result"), "handle_complaint 记录处理结果")
    closed = so.close_complaint(service, complaint_id=complaint["id"])
    check(closed.get("status") == 2 and closed.get("status_text") == "已结案", "close_complaint 处理中→已结案")

    bad_state = None
    try:
        so.cancel_complaint(service, complaint_id=complaint["id"], reason="不办了")
    except services.ServiceError as exc:
        bad_state = exc
    check(bad_state is not None and bad_state.code == "state", "已结案不能再取消（状态机保护）")

    print("\n=== 投诉：权限与数据范围 ===")
    denied = None
    try:
        so.create_complaint(engineer, house_id=house.id, content="工程师不该能投诉登记")
    except Exception as exc:  # noqa: BLE001 - 权限拒绝是 HTTPException
        denied = exc
    check(denied is not None, "engineer 无 complaint.create 被拒")

    # 业主只能给自己名下的房屋写数据，取他真正关联的那一套
    owner_house_id = services.my_house_ids(owner)
    check(bool(owner_house_id), f"owner01 关联到自己的房屋（{len(owner_house_id or [])} 套）")
    owner_complaint = so.create_complaint(
        owner, house_id=sorted(owner_house_id)[0], content="我家厨房下水道有异味"
    )

    # 投诉的 house 是「涉事房屋」（例：楼上装修噪音），所以业主可以指认本小区的邻居家；
    # 越权边界落在**小区**上，跨小区仍然必须被拒。
    owner_house = admin.db.get(models.House, sorted(owner_house_id)[0])
    neighbour = admin.db.execute(
        select(models.House).where(
            models.House.deleted.is_(False),
            models.House.id.notin_(owner_house_id),
            models.House.community_id == owner_house.community_id,
        )
    ).scalars().first()
    if neighbour is not None:
        accepted = False
        try:
            so.create_complaint(owner, house_id=neighbour.id, content="楼上装修噪音很大")
            accepted = True
        except (services.ServiceError, HTTPException):  # noqa: BLE001
            accepted = False
        check(accepted, "业主可以投诉本小区的邻居家（涉事房屋口径）")

    far_house = admin.db.execute(
        select(models.House).where(
            models.House.deleted.is_(False),
            models.House.community_id != owner_house.community_id,
        )
    ).scalars().first()
    if far_house is not None:
        outside = None
        try:
            so.create_complaint(owner, house_id=far_house.id, content="跨小区投诉应该被拒")
        except (services.ServiceError, HTTPException) as exc:  # noqa: BLE001
            outside = exc
        check(outside is not None, "业主跨小区投诉被拒（数据范围）")
    check(owner_complaint.get("status") == 0, "业主可以给自己的房屋登记投诉")

    print("\n=== 访客：登记 → 进门 → 离开 ===")
    visitor = so.register_visitor(service, house_id=house.id, name="王小明", phone="13811112222", purpose="送快递")
    check(visitor.get("status") == 0 and visitor.get("status_text") == "待进", "register_visitor 待进")
    entered = so.enter_visitor(service, visitor_id=visitor["id"])
    check(entered.get("status") == 1, "enter_visitor 待进→已进")
    left = so.leave_visitor(service, visitor_id=visitor["id"])
    check(left.get("status") == 2, "leave_visitor 已进→已离")

    cancel_me = so.register_visitor(service, house_id=house.id, name="刘芳", phone="13811113333", purpose="看望家人")
    cancelled = so.cancel_visitor(service, visitor_id=cancel_me["id"], reason="临时取消")
    check(cancelled.get("status") == 3, "cancel_visitor 待进→已取消")

    print("\n=== 车辆与车位 ===")
    vehicle = so.create_vehicle(service, plate="京D88888", house_id=house.id, brand="特斯拉", owner_person_id=person.id)
    check(vehicle.get("plate") == "京D88888", "create_vehicle 登记成功")
    dup = None
    try:
        so.create_vehicle(service, plate="京d88888", house_id=house.id)
    except services.ServiceError as exc:
        dup = exc
    check(dup is not None and dup.code == "conflict", "重复车牌被拒（含大小写归一）")

    updated = so.update_vehicle(service, vehicle_id=vehicle["id"], brand="特斯拉 Model Y")
    check("Model Y" in str(updated.get("brand")), "update_vehicle 修改品牌")

    free_space = admin.db.execute(
        select(models.ParkingSpace).where(models.ParkingSpace.status == 0, models.ParkingSpace.deleted.is_(False))
    ).scalars().first()
    if free_space is None:
        new_space = models.ParkingSpace(community_id=house.community_id, code="T-999", status=0)
        admin.db.add(new_space)
        admin.db.commit()
        free_space = new_space
    assigned_space = so.assign_parking(service, space_id=free_space.id, vehicle_id=vehicle["id"])
    check(assigned_space.get("status") == 1 and assigned_space.get("plate") == "京D88888", "assign_parking 分配车位")
    busy = None
    try:
        so.assign_parking(service, space_id=free_space.id, vehicle_id=vehicle["id"])
    except services.ServiceError as exc:
        busy = exc
    check(busy is not None, "重复分配同一车位被拒（车辆已占位）")

    released = so.release_parking(service, space_id=free_space.id)
    check(released.get("status") == 0, "release_parking 释放车位")

    archived = so.archive_vehicle(service, vehicle_id=vehicle["id"], reason="车辆已出售")
    check(archived.get("ok") and archived.get("version"), "archive_vehicle 归档并返回 version")

    print("\n=== 设备与巡检 ===")
    device = so.create_device(
        admin, name="3 号水泵", community_id=house.community_id, building_id=house.building_id,
        category="供水", location="地下一层",
    )
    check(device.get("status") == 0 and device.get("status_text") == "正常", "create_device 正常状态")
    inspection = so.create_inspection(admin, device_id=device["id"], assignee_id=admin_user.id)
    check(inspection.get("status") == 0 and inspection.get("status_text") == "待巡检", "create_inspection 待巡检")

    blocked = None
    try:
        so.archive_device(admin, device_id=device["id"], reason="报废")
    except services.ServiceError as exc:
        blocked = exc
    check(blocked is not None and blocked.code == "conflict", "有未完成巡检时禁止归档设备")

    done = so.complete_inspection(admin, inspection_id=inspection["id"], result="运行正常")
    check(done.get("status") == 1, "complete_inspection 待巡检→已完成")

    fault_task = so.create_inspection(admin, device_id=device["id"], assignee_id=admin_user.id)
    fault = so.complete_inspection(admin, inspection_id=fault_task["id"], result="发现异响", has_fault=True)
    check(fault.get("status") == 2 and fault.get("status_text") == "已转报修", "has_fault=True → 已转报修")

    archived_device = so.archive_device(admin, device_id=device["id"], reason="报废")
    check(archived_device.get("ok"), "无待巡检后可归档设备")

    print("\n=== queries_ops：列表 / 分页 / 数据范围 ===")
    for label, fn, kwargs in (
        ("投诉", qo.list_complaints, {"page_size": 5}),
        ("访客", qo.list_visitors, {"page_size": 5}),
        ("车辆", qo.list_vehicles, {"page_size": 5}),
        ("车位", qo.list_parking_spaces, {"page_size": 5}),
        ("设备", qo.list_devices, {"page_size": 5}),
        ("巡检", qo.list_inspections, {"page_size": 5}),
    ):
        data = fn(admin, **kwargs)
        check(
            set(("items", "total", "page", "page_size", "pages", "has_prev", "has_next")) <= set(data),
            f"{label}列表返回统一分页结构（total={data.get('total')}）",
        )

    complaint_list = qo.list_complaints(admin, status="已结案")
    check(
        all(item["status"] == 2 for item in complaint_list["items"]) and complaint_list["total"] >= 1,
        "投诉状态筛选=已结案 生效",
    )
    keyword_hit = qo.list_complaints(admin, keyword="杂物")
    check(keyword_hit["total"] >= 1, "投诉关键词搜索命中")

    owner_visitors = qo.list_visitors(owner, page_size=50)
    admin_visitors = qo.list_visitors(admin, page_size=50)
    check(
        len(owner_visitors["items"]) <= len(admin_visitors["items"]),
        f"业主可见访客不超过管理员（owner={len(owner_visitors['items'])} admin={len(admin_visitors['items'])}）",
    )

    detail = qo.get_complaint(admin, complaint["id"])
    check(detail.get("no", "").startswith("TS") and detail.get("house_full"), "get_complaint 带回房屋全称")

    summary = qo.complaint_status_summary(admin)
    check(sum(summary.values()) >= 1, f"投诉状态汇总可用（{summary}）")

    print("\n=== 审计留痕 ===")
    audits = admin.db.execute(
        select(models.AuditLog).where(models.AuditLog.source == "web")
    ).scalars().all()
    actions = {row.action for row in audits}
    need = {"complaint.create", "complaint.assign", "complaint.close", "visitor.register", "visitor.enter",
            "vehicle.create", "parking.assign", "parking.release", "device.create", "inspection.create",
            "inspection.complete", "vehicle.archive", "device.archive"}
    missing = sorted(need - actions)
    check(not missing, f"审计留痕覆盖 {len(need)} 个动作", f"缺少 {missing}")

    passed = sum(1 for ok, _ in results if ok)
    print("\n" + "=" * 62)
    print(f"运营模块检查：通过 {passed} / {len(results)}")
    failed = [label for ok, label in results if not ok]
    if failed:
        print("失败项：")
        for label in failed:
            print("  -", label)
    print("=" * 62)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
