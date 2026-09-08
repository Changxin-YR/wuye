"""Read-only scoped business queries exposed to Agent; never accept SQL."""
from datetime import datetime, timedelta

from flask import abort
from sqlalchemy import func, or_, select

from agent_security import safe_record
from models import *
from permissions import Policy, effective
from property_service import snapshot


STAFF_QUERY_SPECS = {
    "staff.search": ("order.dispatch", "order.work"),
    "complaint_staff.search": ("complaint.handle", "complaint.handle"),
    "inspection_staff.search": ("inspection.assign", "inspection.write"),
}

QUERIES = {
    "house.search": "property.read",
    "building.search": "property.read",
    "unit.search": "property.read",
    "person.search": "person.read",
    "person.properties": "person.read",
    **{command: actor_permission for command, (actor_permission, _) in STAFF_QUERY_SPECS.items()},
    "order.search": "order.read",
    "order.pending": "order.read",
    "complaint.search": "complaint.read",
    "complaint.stats": "complaint.handle",
    "visitor.search": "visitor.read",
    "vehicle.search": "vehicle.read",
    "parking.search": "parking.read",
    "parking_use.search": "parking.read",
    "device.search": "device.read",
    "inspection.search": "inspection.read",
    "fee.search": "billing.read",
    "bill.search": "billing.read",
    "payment.search": "billing.read",
    "billing.unpaid": "billing.read",
    "notice.read": "notice.read",
    "whoami": "notice.read",
}


def _building_aliases(value):
    """Return equivalent spoken/persisted building labels without widening scope."""
    raw = str(value or "").strip()
    if not raw:
        return set()
    from agent_planner import building_name_variants
    variants = set(building_name_variants(raw))
    core = raw
    for suffix in ("号楼", "栋"):
        if core.endswith(suffix):
            core = core[:-len(suffix)].strip()
            break
    if core:
        variants.update({core, core + "栋", core + "号楼"})
    return {item for item in variants if item}


def _unit_aliases(value):
    """Treat `3` and `3单元` as equivalent business labels."""
    raw = str(value or "").strip()
    if not raw:
        return set()
    core = raw[:-2].strip() if raw.endswith("单元") else raw
    return {item for item in {raw, core, core + "单元" if core else ""} if item}


def _clean_args(command, args):
    if not isinstance(args, dict):
        abort(400, description="无效查询参数")
    args = dict(args)
    if command in {"person.search", "person.properties"} and "person_name" not in args:
        for alias in ("name", "q", "keywords"):
            if alias in args:
                args["person_name"] = args.pop(alias)
                break
    if command in STAFF_QUERY_SPECS and "staff_name" not in args:
        for alias in ("name", "q", "keywords"):
            if alias in args:
                args["staff_name"] = args.pop(alias)
                break
    if command == "person.properties" and "person_id" not in args and "id" in args:
        args["person_id"] = args.pop("id")
    if command.startswith("order.") and "order_no" not in args and "q" in args:
        args["order_no"] = args.pop("q")
    if command == "device.search" and "code" not in args and "q" in args:
        args["code"] = args.pop("q")
    if command == "vehicle.search" and "plate" not in args and "q" in args:
        args["plate"] = args.pop("q")
    if command in {"parking.search", "parking_use.search"} and "space_code" not in args and "q" in args:
        args["space_code"] = args.pop("q")
    allowed = {
        "community_id", "building_id", "building_name", "building", "unit_id", "unit", "unit_name",
        "room_no", "house_id", "id", "person_id", "person_name", "staff_name", "username", "phone", "month", "status", "q",
        "order_no", "plate", "space_code", "code", "name", "device_id", "assignee_id", "bill_id",
        "fee_item_id", "category",
    }
    if set(args) - allowed:
        abort(400, description="查询包含未知参数")
    if any(not isinstance(value, (str, int)) or isinstance(value, bool) for value in args.values()):
        abort(400, description="查询参数类型无效")
    return args


def _items(rows):
    rows = list(rows)
    return {
        "items": [safe_record(snapshot(row)) for row in rows[:100]],
        "truncated": len(rows) > 100,
        "message": "仅返回当前账号授权范围内的最小必要数据",
    }


def _house_items(db, policy, rows, include_residents=False):
    """Serialize houses and optionally attach only scoped current residents.

    Broad house listings intentionally stay house-only. Resident identity is
    attached only after the caller has narrowed the query to a concrete room
    and the current actor also has person.read. Policy is applied independently
    to HousePerson and Person so this enrichment can never widen DataScope.
    """
    rows = list(rows)
    visible = rows[:100]
    items = [safe_record(snapshot(row)) for row in visible]
    if not include_residents or not policy.has("person.read") or not visible:
        return {
            "items": items,
            "truncated": len(rows) > 100,
            "message": "仅返回当前账号授权范围内的最小必要数据",
        }

    house_ids = [row.id for row in visible]
    relations = list(db.scalars(
        policy.query(HousePerson).where(
            HousePerson.house_id.in_(house_ids),
            HousePerson.is_resident.is_(True),
            effective(),
        ).order_by(HousePerson.id)
    ))
    person_ids = {row.person_id for row in relations}
    people = {
        person.id: person
        for person in db.scalars(policy.query(Person).where(Person.id.in_(person_ids)))
    } if person_ids else {}
    residents_by_house = {house_id: [] for house_id in house_ids}
    for relation in relations:
        person = people.get(relation.person_id)
        if not person:
            continue
        residents_by_house.setdefault(relation.house_id, []).append(safe_record({
            "id": person.id,
            "name": person.name,
            "phone": person.phone,
            "kind": relation.kind,
            "is_resident": True,
        }))
    for item in items:
        item["residents"] = residents_by_house.get(item["id"], [])
    return {
        "items": items,
        "truncated": len(rows) > 100,
        "message": "房屋住户信息仅返回当前账号授权范围内的有效居住关系",
    }


def query(db, actor, command, args):
    if command not in QUERIES:
        abort(400, description="无效查询")
    args = _clean_args(command, args)
    policy = Policy(db, actor)
    policy.require(QUERIES[command])

    if command == "whoami":
        return policy.identity()

    if command == "notice.read":
        community_id = args.get("community_id")
        building_id = args.get("building_id")
        building_name = args.get("building_name") or args.get("building")
        target_building = None
        if building_id:
            target_building = policy.get(Building, int(building_id))
            if community_id and target_building.community_id != int(community_id):
                abort(400, description="楼栋不属于指定小区")
        elif building_name:
            buildings = policy.query(Building)
            if community_id:
                buildings = buildings.where(Building.community_id == int(community_id))
            buildings = buildings.where(Building.name.in_(_building_aliases(building_name)))
            matches = list(db.scalars(buildings.order_by(Building.id).limit(2)))
            if not matches:
                abort(404, description="未找到该楼栋或该楼栋不在当前账号数据范围内")
            if len(matches) > 1:
                abort(409, description="多个小区存在同名楼栋，请补充小区后再查询")
            target_building = matches[0]
        q = policy.query(Notice).order_by(Notice.id.desc())
        if target_building:
            q = q.where(
                Notice.community_id == target_building.community_id,
                or_(Notice.building_id.is_(None), Notice.building_id == target_building.id),
            )
        elif community_id:
            q = q.where(Notice.community_id == int(community_id))
        return _items(db.scalars(q.limit(101)))

    if command in {"house.search", "person.properties", "billing.unpaid"}:
        houses = policy.query(House)
        if args.get("id"):
            houses = houses.where(House.id == args["id"])
        if args.get("house_id"):
            houses = houses.where(House.id == args["house_id"])
        if args.get("building_id"):
            houses = houses.where(House.building_id == args["building_id"])
        if args.get("community_id"):
            houses = houses.where(House.community_id == args["community_id"])
        if args.get("building_name"):
            houses = houses.where(House.building_name.in_(_building_aliases(args["building_name"])))
        if args.get("unit"):
            houses = houses.where(House.unit.in_(_unit_aliases(args["unit"])))
        if args.get("room_no"):
            houses = houses.where(House.room_no == args["room_no"])
        if command == "person.properties":
            person_id = args.get("person_id")
            name = str(args.get("person_name", "")).strip()
            pq = policy.query(Person)
            if person_id:
                pq = pq.where(Person.id == person_id)
            else:
                if not name:
                    abort(400, description="请提供人员姓名")
                pq = pq.where(Person.name == name)
            if args.get("phone"):
                pq = pq.where(Person.phone == args["phone"])
            people = list(db.scalars(pq.limit(2)))
            if not people:
                abort(404, description="未找到该人员")
            if len(people) > 1:
                abort(409, description="存在同名人员，请提供联系电话")
            houses = houses.where(House.id.in_(select(HousePerson.house_id).where(
                HousePerson.person_id == people[0].id,
                HousePerson.kind == "owner",
                effective(),
            )))
        if command == "billing.unpaid":
            person_targeted = any(args.get(key) not in (None, "") for key in ("person_id", "person_name", "phone"))
            if person_targeted:
                policy.require("person.read")
                pq = policy.query(Person)
                if args.get("person_id"):
                    pq = pq.where(Person.id == args["person_id"])
                if args.get("person_name"):
                    pq = pq.where(Person.name == str(args["person_name"]).strip())
                if args.get("phone"):
                    pq = pq.where(Person.phone == str(args["phone"]).strip())
                people = list(db.scalars(pq.order_by(Person.id).limit(2)))
                if not people:
                    abort(404, description="未找到该人员或该人员不在当前账号数据范围内")
                if len(people) > 1:
                    abort(409, description="存在同名或重复联系方式人员，请补充联系电话或更具体身份信息")
                linked_houses = select(HousePerson.house_id).where(
                    HousePerson.person_id == people[0].id,
                    HousePerson.is_resident.is_(True),
                    effective(),
                )
                houses = houses.where(House.id.in_(linked_houses))
            q = policy.query(Bill).where(
                Bill.house_id.in_(houses.with_only_columns(House.id)),
                Bill.status.in_(["unpaid", "partial"]),
            )
            if args.get("bill_id"):
                q = q.where(Bill.id == args["bill_id"])
            if args.get("month"):
                q = q.where(Bill.period == args["month"])
            return _items(db.scalars(q.limit(101)))
        if command == "house.search":
            targeted = bool(
                args.get("id")
                or args.get("house_id")
                or (
                    args.get("room_no") is not None
                    and (args.get("building_id") or args.get("building_name"))
                )
            )
            return _house_items(db, policy, db.scalars(houses.limit(101)), include_residents=targeted)
        return _items(db.scalars(houses.limit(101)))

    if command == "building.search":
        q = policy.query(Building)
        if args.get("id"):
            q = q.where(Building.id == args["id"])
        if args.get("community_id"):
            q = q.where(Building.community_id == args["community_id"])
        name = args.get("name") or args.get("building_name") or args.get("building")
        if name:
            q = q.where(Building.name.in_(_building_aliases(name)))
        return _items(db.scalars(q.limit(101)))

    if command == "unit.search":
        q = policy.query(PropertyUnit)
        if args.get("id") or args.get("unit_id"):
            q = q.where(PropertyUnit.id == (args.get("id") or args.get("unit_id")))
        if args.get("building_id"):
            q = q.where(PropertyUnit.building_id == args["building_id"])
        name = args.get("name") or args.get("unit") or args.get("unit_name")
        if name:
            q = q.where(PropertyUnit.name.in_(_unit_aliases(name)))
        return _items(db.scalars(q.limit(101)))

    if command == "person.search":
        q = policy.query(Person)
        if args.get("id"):
            q = q.where(Person.id == args["id"])
        if args.get("person_name"):
            q = q.where(Person.name == args["person_name"])
        if args.get("phone"):
            q = q.where(Person.phone == args["phone"])
        return _items(db.scalars(q.limit(101)))

    if command in STAFF_QUERY_SPECS:
        _, required_worker_permission = STAFF_QUERY_SPECS[command]
        cid = args.get("community_id")
        bid = args.get("building_id")
        if bid:
            building = policy.get(Building, bid)
            if cid and building.community_id != cid:
                abort(400, description="楼栋不属于指定小区")
            cid = building.community_id
        if cid:
            policy.get(Community, cid)
            policy.require_scope(cid, bid)
        eligible = select(UserRole.user_id).join(
            RolePermission, RolePermission.role_code == UserRole.role_code
        ).where(RolePermission.permission == required_worker_permission)
        q = policy.query(User).where(User.active.is_(True), User.id.in_(eligible))
        if args.get("id"):
            q = q.where(User.id == args["id"])
        if args.get("staff_name"):
            name = str(args["staff_name"]).strip()
            q = q.where(or_(User.real_name == name, User.username == name))
        if args.get("username"):
            q = q.where(User.username == args["username"])
        if args.get("phone"):
            q = q.where(User.phone == args["phone"])
        rows = list(db.scalars(q.order_by(User.id).limit(201)))
        if cid:
            rows = [user for user in rows if Policy(db, user).within(cid, bid)]
        return _items(rows)

    if command.startswith("order."):
        q = policy.query(WorkOrder)
        if args.get("id"):
            q = q.where(WorkOrder.id == args["id"])
        if args.get("order_no"):
            q = q.where(WorkOrder.order_no == args["order_no"])
        if command == "order.pending":
            q = q.where(WorkOrder.status.in_([0, 1, 2, 3]))
        if args.get("status"):
            q = q.where(WorkOrder.status == int(args["status"]))
        if args.get("q"):
            q = q.where(WorkOrder.title.contains(str(args["q"])[:100], autoescape=True))
        return _items(db.scalars(q.order_by(WorkOrder.updated_at.desc()).limit(101)))

    if command == "complaint.search":
        q = policy.query(Complaint)
        if args.get("id"):
            q = q.where(Complaint.id == args["id"])
        for key in ("community_id", "building_id", "house_id", "status"):
            if args.get(key):
                q = q.where(getattr(Complaint, key) == args[key])
        if args.get("q"):
            term = str(args["q"])[:100]
            q = q.where(or_(Complaint.title.contains(term, autoescape=True), Complaint.content.contains(term, autoescape=True)))
        return _items(db.scalars(q.order_by(Complaint.updated_at.desc()).limit(101)))

    if command == "visitor.search":
        q = policy.query(Visitor)
        if args.get("id"):
            q = q.where(Visitor.id == args["id"])
        for key in ("community_id", "building_id", "house_id", "status", "phone"):
            if args.get(key):
                q = q.where(getattr(Visitor, key) == args[key])
        if args.get("name"):
            q = q.where(Visitor.name == args["name"])
        return _items(db.scalars(q.order_by(Visitor.created_at.desc()).limit(101)))

    if command == "vehicle.search":
        q = policy.query(Vehicle)
        if args.get("id"):
            q = q.where(Vehicle.id == args["id"])
        for key in ("community_id", "building_id", "house_id", "person_id", "status"):
            if args.get(key):
                q = q.where(getattr(Vehicle, key) == args[key])
        if args.get("plate"):
            q = q.where(Vehicle.plate == str(args["plate"]).upper().replace(" ", ""))
        return _items(db.scalars(q.limit(101)))

    if command == "parking.search":
        spaces = policy.query(ParkingSpace)
        if args.get("id"):
            spaces = spaces.where(ParkingSpace.id == args["id"])
        for key in ("community_id", "building_id", "status"):
            if args.get(key):
                spaces = spaces.where(getattr(ParkingSpace, key) == args[key])
        if args.get("space_code"):
            spaces = spaces.where(ParkingSpace.code == args["space_code"])
        if args.get("plate"):
            vehicle_ids = select(Vehicle.id).where(Vehicle.plate == str(args["plate"]).upper().replace(" ", ""))
            active_space_ids = select(ParkingUse.space_id).where(ParkingUse.vehicle_id.in_(vehicle_ids), ParkingUse.status == "active")
            spaces = spaces.where(ParkingSpace.id.in_(active_space_ids))
        return _items(db.scalars(spaces.limit(101)))

    if command == "parking_use.search":
        uses = policy.query(ParkingUse)
        if args.get("id"):
            uses = uses.where(ParkingUse.id == args["id"])
        for key in ("community_id", "building_id", "status"):
            if args.get(key):
                uses = uses.where(getattr(ParkingUse, key) == args[key])
        if args.get("space_code"):
            space_ids = select(ParkingSpace.id).where(ParkingSpace.code == args["space_code"])
            uses = uses.where(ParkingUse.space_id.in_(space_ids))
        if args.get("plate"):
            vehicle_ids = select(Vehicle.id).where(Vehicle.plate == str(args["plate"]).upper().replace(" ", ""))
            uses = uses.where(ParkingUse.vehicle_id.in_(vehicle_ids))
        return _items(db.scalars(uses.order_by(ParkingUse.id.desc()).limit(101)))

    if command == "device.search":
        q = policy.query(Device)
        if args.get("id"):
            q = q.where(Device.id == args["id"])
        for key in ("community_id", "building_id", "status", "category"):
            if args.get(key):
                q = q.where(getattr(Device, key) == args[key])
        if args.get("code"):
            q = q.where(Device.code == args["code"])
        if args.get("name"):
            q = q.where(Device.name.contains(str(args["name"])[:100], autoescape=True))
        return _items(db.scalars(q.limit(101)))

    if command == "inspection.search":
        q = policy.query(Inspection)
        if args.get("id"):
            q = q.where(Inspection.id == args["id"])
        for key in ("community_id", "building_id", "device_id", "assignee_id", "status"):
            if args.get(key):
                q = q.where(getattr(Inspection, key) == args[key])
        return _items(db.scalars(q.order_by(Inspection.due_at.desc()).limit(101)))

    if command == "fee.search":
        q = policy.query(FeeItem)
        if args.get("id") or args.get("fee_item_id"):
            q = q.where(FeeItem.id == (args.get("id") or args.get("fee_item_id")))
        if args.get("community_id"):
            q = q.where(FeeItem.community_id == args["community_id"])
        if args.get("name"):
            q = q.where(FeeItem.name.contains(str(args["name"])[:80], autoescape=True))
        return _items(db.scalars(q.limit(101)))

    if command == "bill.search":
        q = policy.query(Bill)
        bill_id = args.get("bill_id") or args.get("id")
        if bill_id:
            q = q.where(Bill.id == bill_id)
        for key in ("community_id", "building_id", "house_id", "fee_item_id", "status"):
            if args.get(key):
                q = q.where(getattr(Bill, key) == args[key])
        if args.get("month"):
            q = q.where(Bill.period == args["month"])
        return _items(db.scalars(q.order_by(Bill.id.desc()).limit(101)))

    if command == "payment.search":
        q = policy.query(Payment)
        if args.get("id"):
            q = q.where(Payment.id == args["id"])
        if args.get("bill_id"):
            q = q.where(Payment.bill_id == args["bill_id"])
        if args.get("status"):
            q = q.where(Payment.status == args["status"])
        return _items(db.scalars(q.order_by(Payment.created_at.desc()).limit(101)))

    if command == "complaint.stats":
        month = str(args.get("month") or (utcnow() + timedelta(hours=8)).strftime("%Y-%m"))
        try:
            start = datetime.strptime(month, "%Y-%m")
            end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        except ValueError:
            abort(400, description="账期应为年-月")
        sub = policy.query(Complaint).where(
            Complaint.created_at >= start - timedelta(hours=8),
            Complaint.created_at < end - timedelta(hours=8),
        ).subquery()
        return {
            "month": month,
            "items": [
                {"building_id": building_id, "count": count}
                for building_id, count in db.execute(
                    select(sub.c.building_id, func.count()).group_by(sub.c.building_id).order_by(func.count().desc())
                )
            ],
        }

    abort(400, description="未实现的查询")
