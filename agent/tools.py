"""智能体能力目录：**唯一来源**。

每条 ``ToolSpec`` 描述一个 MCP 工具：名称、给人看的标签、给模型看的说明、
所需权限、以及真正执行的 ``services`` / ``queries`` 函数与参数类型。

三处消费它：

* ``agent.mcp_server`` 按当前登录用户的权限**动态注册**工具——没权限的能力
  根本不会出现在模型上下文里（动态能力目录）。
* ``agent.bridge`` 把工具名翻译成人话进度。
* ``tests/test_mcp_tools.py`` 核对"工具 ↔ 服务函数 ↔ 权限 ↔ 风险级别"的一致性。

风险级别（R0–R3）由 ``risk.py`` 单独维护，本文件不重复定义，避免两份真相打架。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class Param:
    """一个工具参数。"""

    name: str
    type: str  # 'int' | 'str' | 'float' | 'bool'
    description: str
    required: bool = True

    @property
    def py_type(self) -> Any:
        return {"int": int, "str": str, "float": float, "bool": bool}[self.type]

    def annotation(self) -> Any:
        return self.py_type if self.required else self.py_type | None

    def default(self) -> Any:
        return ... if self.required else None


@dataclass(frozen=True)
class ToolSpec:
    """一个 MCP 工具。"""

    name: str
    label: str
    description: str
    permission: str | None
    target: str  # 'services:create_work_order' / 'queries:list_houses'
    params: tuple[Param, ...] = field(default_factory=tuple)
    kind: str = "command"  # 'command' | 'query'

    @property
    def module_name(self) -> str:
        return self.target.split(":", 1)[0]

    @property
    def function_name(self) -> str:
        return self.target.split(":", 1)[1]

    @property
    def is_query(self) -> bool:
        return self.kind == "query"

    def resolve(self) -> Callable[..., Any]:
        """导入并返回执行函数 ``fn(policy, **params)``。"""
        return getattr(importlib.import_module(self.module_name), self.function_name)

    def param_names(self) -> tuple[str, ...]:
        """业务参数名（不含幂等/版本/审计来源等公共控制参数）。"""
        control = {"request_key", "expected_version", "source"}
        return tuple(p.name for p in self.params if p.name not in control)


def P(name: str, type_: str, description: str) -> Param:
    return Param(name=name, type=type_, description=description)


def O(name: str, type_: str, description: str) -> Param:
    """可选参数。"""
    return Param(name=name, type=type_, description=description, required=False)


def _q(name: str, label: str, description: str, permission: str | None, target: str, *params: Param) -> ToolSpec:
    return ToolSpec(name=name, label=label, description=description, permission=permission,
                    target=target, params=tuple(params), kind="query")


def _c(name: str, label: str, description: str, permission: str, target: str, *params: Param) -> ToolSpec:
    return ToolSpec(name=name, label=label, description=description, permission=permission,
                    target=target, params=tuple(params), kind="command")


TOOLS: tuple[ToolSpec, ...] = (
    # ======================================================== 身份 / 空间（查）
    _q("whoami", "查看当前身份",
       "查看当前登录账号的姓名、角色、权限和数据范围。不确定自己能做什么时先调用它。",
       None, "queries:whoami"),
    _q("list_communities", "查询小区",
       "按关键词查询可见小区，返回 community_id。",
       "community.read", "queries:list_communities", O("keyword", "str", "小区名关键词")),
    _q("list_buildings", "查询楼栋",
       "查询某小区下的楼栋，返回 building_id。",
       "community.read", "queries:list_buildings", P("community_id", "int", "小区编号")),
    _q("list_units", "查询单元",
       "查询某楼栋下的单元，返回 unit_id。",
       "community.read", "queries:list_units", P("building_id", "int", "楼栋编号")),
    _q("list_houses", "查询房屋",
       "查询房屋列表，返回 house_id 与房屋全称。支持按小区、楼栋、关键词"
       "（楼栋名/单元/房号/业主姓名）、状态过滤。"
       "报修、绑定人员、建账单之前先用它确认 house_id。",
       "house.read", "queries:list_houses",
       O("community_id", "int", "小区编号"), O("building_id", "int", "楼栋编号"),
       O("keyword", "str", "关键词：楼栋/单元/房号/业主姓名"), O("status", "int", "0 空置 / 1 自住 / 2 出租")),
    _q("get_house", "查看房屋详情",
       "查看一套房屋的详情（含当前有效人员关系）。",
       "house.read", "queries:get_house", P("house_id", "int", "房屋编号")),
    # ============================================================ 人员与关系（查）
    _q("list_persons", "查询人员",
       "按姓名或手机号查询人员档案，返回 person_id。同名人员会有多条，请让用户确认。",
       "person.read", "queries:list_persons", O("keyword", "str", "姓名或手机号关键词")),
    _q("get_person", "查看人员详情",
       "查看人员详情及其名下的房屋关系。",
       "person.read", "queries:get_person", P("person_id", "int", "人员编号")),
    _q("list_relations", "查询房屋关系",
       "查询房屋与人员的关系（业主/租户/家庭成员）。按房屋或人员过滤。",
       "person.read", "queries:list_relations",
       O("house_id", "int", "房屋编号"), O("person_id", "int", "人员编号")),
    _q("list_leases", "查询租赁",
       "查询租赁记录（在租/已退租）。",
       "lease.write", "queries:list_leases",
       O("house_id", "int", "房屋编号"), O("status", "int", "0 在租 / 1 已退租")),
    _q("list_staff", "查询工作人员",
       "查询可派单的工作人员（维修工/工程维修），返回账号编号与姓名。派单/派巡检前用它确认人选。",
       "staff.read", "queries:list_staff", O("keyword", "str", "姓名或账号关键词")),
    # ================================================================== 工单（查）
    _q("list_work_orders", "查询工单",
       "查询工单。status：0 待派单 / 1 已派单 / 2 维修中 / 3 待验收 / 4 已关闭 / 5 已取消；"
       "keyword 支持单号、房号、报修内容、联系人；mine=true 只看与自己相关的工单。",
       "order.read", "queries:list_work_orders",
       O("status", "int", "工单状态"), O("keyword", "str", "关键词"),
       O("community", "str", "小区名称或编号"), O("mine", "bool", "只看与自己相关的")),
    _q("get_work_order", "查看工单详情",
       "查看工单完整信息与流转记录。改状态前先调用它确认当前状态。",
       "order.read", "queries:get_work_order", P("order_id", "int", "工单编号")),
    _q("work_order_stats", "工单统计",
       "按状态统计工单数量（待派单/已派单/维修中/待验收/已关闭/已取消）。",
       "order.read", "queries:work_order_stats", O("community_id", "int", "小区编号")),
    # ================================================================== 投诉（查）
    _q("list_complaints", "查询投诉",
       "查询投诉记录。status：0 待处理 / 1 处理中 / 2 已结案 / 3 已取消。",
       "complaint.read", "queries_ops:list_complaints",
       O("status", "int", "投诉状态"), O("keyword", "str", "关键词")),
    _q("get_complaint", "查看投诉详情",
       "查看一条投诉的完整信息。",
       "complaint.read", "queries_ops:get_complaint", P("complaint_id", "int", "投诉编号")),
    # ================================================================== 访客（查）
    _q("list_visitors", "查询访客",
       "查询访客登记记录。status：0 待进 / 1 已进 / 2 已离 / 3 已取消。",
       "visitor.read", "queries_ops:list_visitors",
       O("status", "int", "访客状态"), O("keyword", "str", "姓名/电话/房号关键词")),
    # ================================================== 车辆 / 车位 / 设备（查）
    _q("list_vehicles", "查询车辆",
       "查询车辆登记记录（含业主与车位）。",
       "vehicle.read", "queries_ops:list_vehicles", O("keyword", "str", "车牌/房号关键词")),
    _q("list_parking_spaces", "查询车位",
       "查询车位列表与占用情况。",
       "parking.read", "queries_ops:list_parking_spaces", O("status", "int", "0 空闲 / 1 占用")),
    _q("list_devices", "查询设备",
       "查询设备台账（含状态与位置）。",
       "device.read", "queries_ops:list_devices",
       O("keyword", "str", "设备名/位置关键词"), O("status", "int", "0 正常 / 1 维修中 / 2 已归档")),
    _q("list_inspections", "查询巡检任务",
       "查询巡检任务。status：0 待巡检 / 1 已完成 / 2 已转报修。",
       "inspection.read", "queries_ops:list_inspections", O("status", "int", "巡检状态")),
    # ================================================================== 收费（查）
    _q("list_bills", "查询账单",
       "查询账单。status：0 待缴 / 1 部分缴纳 / 2 已缴 / 3 已作废；overdue=true 只看已逾期。",
       "billing.read", "queries:list_bills",
       O("status", "int", "账单状态"), O("keyword", "str", "单号/房号/业主关键词"),
       O("house_id", "int", "房屋编号"), O("overdue", "bool", "只看逾期")),
    _q("get_bill", "查看账单详情",
       "查看一张账单的详情与收款记录。",
       "billing.read", "queries:get_bill", P("bill_id", "int", "账单编号")),
    _q("list_payments", "查询收款记录",
       "查询收款与冲销记录。",
       "billing.read", "queries:list_payments",
       O("bill_id", "int", "账单编号"), O("status", "int", "0 已入账 / 1 已冲销")),
    _q("arrears_summary", "欠费汇总",
       "按小区汇总欠费户数与金额，回答\"这个小区欠费多少\"用这个工具。",
       "billing.read", "queries:arrears_summary", O("community_id", "int", "小区编号")),

    # ============================================================== 空间（写）
    _c("create_community", "新增小区", "新增小区。",
       "community.write", "services:create_community",
       P("name", "str", "小区名称"), O("address", "str", "地址")),
    _c("delete_community", "删除小区", "删除小区（软删除）。有楼栋时会被拒绝。需用户确认。",
       "community.write", "services:delete_community", P("community_id", "int", "小区编号")),
    _c("create_building", "新增楼栋", "在指定小区下新增楼栋。",
       "community.write", "services:create_building",
       P("community_id", "int", "小区编号"), P("name", "str", "楼栋名称，如 3栋")),
    _c("delete_building", "删除楼栋", "删除楼栋（软删除）。有房屋时会被拒绝。需用户确认。",
       "community.write", "services:delete_building", P("building_id", "int", "楼栋编号")),
    _c("create_unit", "新增单元", "在指定楼栋下新增单元。",
       "community.write", "services:create_unit",
       P("building_id", "int", "楼栋编号"), P("name", "str", "单元名称，如 1单元")),
    _c("create_house", "新增房屋", "新增房屋：unit 传单元名（如 1单元），room 传房号。",
       "house.write", "services:create_house",
       P("community_id", "int", "小区编号"), P("building_id", "int", "楼栋编号"),
       P("unit", "str", "单元名，如 1单元"), P("room", "str", "房号"),
       O("area", "float", "建筑面积"), O("status", "int", "0 空置 / 1 自住 / 2 出租")),
    _c("update_house", "修改房屋", "修改房屋面积或状态。expected_version 传 get_house 返回的版本号。",
       "house.write", "services:update_house",
       P("house_id", "int", "房屋编号"), O("area", "float", "建筑面积"),
       O("status", "int", "0 空置 / 1 自住 / 2 出租"), O("expected_version", "int", "乐观锁版本号")),
    _c("delete_house", "删除房屋", "删除房屋（软删除）。有在住人员关系时会被拒绝。需用户确认。",
       "house.write", "services:delete_house", P("house_id", "int", "房屋编号")),

    # ============================================================== 人员（写）
    _c("create_person", "新增人员", "新增人员档案，姓名与手机号必填。",
       "person.write", "services:create_person",
       P("name", "str", "姓名"), P("phone", "str", "手机号")),
    _c("update_person", "修改人员", "修改人员姓名或手机号。",
       "person.write", "services:update_person",
       P("person_id", "int", "人员编号"), O("name", "str", "姓名"), O("phone", "str", "手机号"),
       O("expected_version", "int", "乐观锁版本号")),
    _c("delete_person", "删除人员", "删除人员档案。有有效房屋关系时会被拒绝。需用户确认。",
       "person.write", "services:delete_person", P("person_id", "int", "人员编号")),
    _c("bind_relation", "绑定房屋关系",
       "把人员绑定到房屋。relation 取 owner（业主）/ tenant（租户）/ family（家庭成员）。",
       "relation.write", "services:bind_relation",
       P("house_id", "int", "房屋编号"), P("person_id", "int", "人员编号"),
       P("relation", "str", "owner / tenant / family")),
    _c("end_relation", "解除房屋关系", "解除房屋关系（退租、过户等）。需用户确认。",
       "relation.write", "services:end_relation",
       P("relation_id", "int", "关系编号"), O("reason", "str", "原因")),

    # ============================================================== 租赁（写）
    _c("check_in_lease", "办理入住", "登记租赁入住：房屋 + 租户 + 租金 + 起租日期。",
       "lease.write", "services:check_in_lease",
       P("house_id", "int", "房屋编号"), P("person_id", "int", "租户人员编号"),
       P("rent", "float", "月租金"), P("start_at", "str", "起租日期 YYYY-MM-DD"),
       O("end_at", "str", "到期日期 YYYY-MM-DD")),
    _c("check_out_lease", "办理退租", "办理退租并结束房屋的租户关系。需用户确认。",
       "lease.write", "services:check_out_lease",
       P("lease_id", "int", "租赁记录编号"), O("end_at", "str", "退租日期 YYYY-MM-DD"),
       O("reason", "str", "原因")),

    # ============================================================== 工单（写）
    _c("create_work_order", "创建报修工单",
       "登记报修：房屋、联系人、联系电话、类别、问题描述必填。不要编造电话，用户没说就先问。",
       "order.create", "services:create_work_order",
       P("house_id", "int", "房屋编号"), P("contact_name", "str", "联系人"),
       P("contact_phone", "str", "联系电话"), P("category", "str", "报修类别：水暖/电路/门窗/电梯/公共设施/其他（也接受 water/electric/door/elevator/public/other）"),
       P("description", "str", "问题描述"), O("urgency", "str", "普通 / 紧急")),
    _c("assign_work_order", "派单",
       "把待派单工单指派给维修工。repairer 传维修工姓名或账号（先 list_staff）。需用户确认。",
       "order.dispatch", "services:assign_work_order",
       P("order_id", "int", "工单编号"), P("repairer", "str", "维修工姓名或账号"), O("note", "str", "备注")),
    _c("accept_work_order", "接单", "维修工接单：已派单 → 维修中。只有被派人可以接单。",
       "order.work", "services:accept_work_order", P("order_id", "int", "工单编号")),
    _c("add_order_progress", "记录维修进展", "维修工记录处理进展（工单须为维修中）。",
       "order.work", "services:add_order_progress",
       P("order_id", "int", "工单编号"), P("note", "str", "进展说明")),
    _c("finish_work_order", "提交完工", "维修工提交完工：维修中 → 待验收。",
       "order.work", "services:finish_work_order",
       P("order_id", "int", "工单编号"), O("note", "str", "完工说明")),
    _c("verify_work_order", "验收关闭", "验收通过并关闭工单：待验收 → 已关闭。需用户确认。",
       "order.verify", "services:verify_work_order",
       P("order_id", "int", "工单编号"), O("note", "str", "验收说明")),
    _c("reopen_work_order", "返修", "验收不通过，退回维修中（待验收 → 维修中）。需用户确认。",
       "order.verify", "services:reopen_work_order",
       P("order_id", "int", "工单编号"), O("reason", "str", "返修原因")),
    _c("cancel_work_order", "取消工单", "取消未关闭的工单，必须给出原因。需用户确认。",
       "order.cancel", "services:cancel_work_order",
       P("order_id", "int", "工单编号"), P("reason", "str", "取消原因")),
    _c("rate_work_order", "评价工单", "对已关闭工单打分（1–5 星）。",
       "order.create", "services:rate_work_order",
       P("order_id", "int", "工单编号"), P("rating", "int", "1–5 分"), O("note", "str", "评价内容")),

    # ============================================================== 投诉（写）
    _c("create_complaint", "登记投诉", "登记一条投诉：房屋 + 内容（类别可选）。",
       "complaint.create", "services_ops:create_complaint",
       P("house_id", "int", "房屋编号"), P("content", "str", "投诉内容"), O("category", "str", "类别")),
    _c("assign_complaint", "分配投诉", "把投诉分配给处理人（先 list_staff 确认名字）。",
       "complaint.handle", "services_ops:assign_complaint",
       P("complaint_id", "int", "投诉编号"), P("handler", "str", "处理人姓名或账号")),
    _c("handle_complaint", "记录投诉处理进展", "记录投诉处理进展（投诉须为处理中）。",
       "complaint.handle", "services_ops:handle_complaint",
       P("complaint_id", "int", "投诉编号"), P("note", "str", "处理说明")),
    _c("close_complaint", "投诉结案", "回访后结案：处理中 → 已结案。需用户确认。",
       "complaint.handle", "services_ops:close_complaint",
       P("complaint_id", "int", "投诉编号"), O("result", "str", "处理结果")),
    _c("cancel_complaint", "取消投诉", "取消投诉，必须给原因。需用户确认。",
       "complaint.handle", "services_ops:cancel_complaint",
       P("complaint_id", "int", "投诉编号"), P("reason", "str", "取消原因")),

    # ============================================================== 访客（写）
    _c("register_visitor", "登记访客", "登记访客：房屋 + 访客姓名 + 电话（来访时间/事由可选）。",
       "visitor.write", "services_ops:register_visitor",
       P("house_id", "int", "房屋编号"), P("name", "str", "访客姓名"), P("phone", "str", "访客电话"),
       O("visit_at", "str", "来访时间 YYYY-MM-DD HH:MM"), O("purpose", "str", "来访事由")),
    _c("enter_visitor", "访客进入", "访客进入小区：待进 → 已进。",
       "visitor.write", "services_ops:enter_visitor", P("visitor_id", "int", "访客编号")),
    _c("leave_visitor", "访客离开", "访客离开：已进 → 已离。",
       "visitor.write", "services_ops:leave_visitor", P("visitor_id", "int", "访客编号")),
    _c("cancel_visitor", "取消访客登记", "取消访客登记。",
       "visitor.write", "services_ops:cancel_visitor",
       P("visitor_id", "int", "访客编号"), O("reason", "str", "原因")),

    # ========================================================== 车辆车位（写）
    _c("create_vehicle", "登记车辆", "登记车辆：房屋 + 车牌（品牌、车主可选）。",
       "vehicle.write", "services_ops:create_vehicle",
       P("house_id", "int", "房屋编号"), P("plate", "str", "车牌号"),
       O("brand", "str", "品牌车型"), O("owner_person_id", "int", "车主人员编号")),
    _c("update_vehicle", "修改车辆", "修改车辆品牌或车牌。",
       "vehicle.write", "services_ops:update_vehicle",
       P("vehicle_id", "int", "车辆编号"), O("plate", "str", "车牌号"), O("brand", "str", "品牌车型"),
       O("expected_version", "int", "乐观锁版本号")),
    _c("archive_vehicle", "归档车辆", "归档（删除）车辆记录。需用户确认。",
       "vehicle.write", "services_ops:archive_vehicle",
       P("vehicle_id", "int", "车辆编号"), O("reason", "str", "原因")),
    _c("assign_parking", "分配车位", "把车位分配给房屋/车辆（车位编号先用 list_parking_spaces 查）。需用户确认。",
       "parking.write", "services_ops:assign_parking",
       P("space_id", "int", "车位编号"), P("house_id", "int", "房屋编号"),
       O("vehicle_id", "int", "车辆编号")),
    _c("release_parking", "释放车位", "释放车位占用。需用户确认。",
       "parking.write", "services_ops:release_parking",
       P("space_id", "int", "车位编号")),

    # ========================================================== 设备巡检（写）
    _c("create_device", "新增设备", "新增设备台账：小区 + 设备名 + 类别（位置、楼栋可选）。",
       "device.write", "services_ops:create_device",
       P("community_id", "int", "小区编号"), P("name", "str", "设备名称"),
       P("category", "str", "设备类别"), O("building_id", "int", "楼栋编号"), O("location", "str", "位置")),
    _c("update_device", "修改设备", "修改设备信息或状态（0 正常 / 1 维修中）。",
       "device.write", "services_ops:update_device",
       P("device_id", "int", "设备编号"), O("name", "str", "设备名称"), O("category", "str", "类别"),
       O("location", "str", "位置"), O("status", "int", "0 正常 / 1 维修中"),
       O("expected_version", "int", "乐观锁版本号")),
    _c("archive_device", "归档设备", "归档（删除）设备台账。需用户确认。",
       "device.write", "services_ops:archive_device",
       P("device_id", "int", "设备编号"), O("reason", "str", "原因")),
    _c("create_inspection", "创建巡检任务", "给设备派一条巡检任务（assignee_id 是工作人员账号编号，先用 list_staff 查）。",
       "inspection.assign", "services_ops:create_inspection",
       P("device_id", "int", "设备编号"), P("assignee_id", "int", "巡检人账号编号"),
       O("plan_at", "str", "计划时间 YYYY-MM-DD")),
    _c("complete_inspection", "完成巡检", "完成巡检；has_fault=true 时自动转成报修工单。",
       "inspection.write", "services_ops:complete_inspection",
       P("inspection_id", "int", "巡检编号"), P("result", "str", "巡检结果"),
       O("has_fault", "bool", "是否有故障（转报修）")),

    # ============================================================== 收费（写）
    _c("create_bill", "创建账单", "给单户创建账单（费用类型、金额、账期必填）。需用户确认。",
       "billing.manage", "services:create_bill",
       P("house_id", "int", "房屋编号"), P("fee_type", "str", "费用类型，如 物业费/水费"),
       P("amount", "float", "应收金额"), P("period", "str", "账期，如 2026-09"),
       O("due_at", "str", "缴费截止日 YYYY-MM-DD")),
    _c("create_bills_batch", "批量创建账单",
       "给整个小区批量创建账单（按房屋逐个生成，自动跳过已有同账期同类型账单）。需用户确认。",
       "billing.manage", "services:create_bills_batch",
       P("community_id", "int", "小区编号"), P("fee_type", "str", "费用类型"),
       P("amount", "float", "每户应收金额"), P("period", "str", "账期"),
       O("due_at", "str", "缴费截止日 YYYY-MM-DD")),
    _c("void_bill", "作废账单", "作废账单（有收款时会被拒绝）。需用户确认。",
       "billing.manage", "services:void_bill",
       P("bill_id", "int", "账单编号"), P("reason", "str", "作废原因")),
    _c("collect_payment", "登记收款", "登记一笔收款（现金/银行流水），支持部分收款。需用户确认（财务操作）。",
       "billing.collect", "services:collect_payment",
       P("bill_id", "int", "账单编号"), P("amount", "float", "收款金额"),
       O("method", "str", "现金 / 银行 / 其他"), O("reference", "str", "流水号/凭据号")),
    _c("reverse_payment", "冲销收款", "冲销一笔已入账收款，账单回退到收款前状态。需用户确认（财务操作）。",
       "billing.reverse", "services:reverse_payment",
       P("payment_id", "int", "收款记录编号"), P("reason", "str", "冲销原因")),
)

TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOLS}

_TOOL_NAME_OK = set("abcdefghijklmnopqrstuvwxyz0123456789_")


def tool_label(name: str) -> str:
    """把工具名翻译成人话（进度展示用）。"""
    spec = TOOLS_BY_NAME.get(name)
    return spec.label if spec else name


def tools_for_permissions(permissions: set[str] | None, super_user: bool = False) -> list[ToolSpec]:
    """按权限过滤出这台运行时该看到的工具（动态能力目录）。

    ``permissions=None``（取不到权限）时只返回无需权限的工具。
    """
    if super_user:
        return list(TOOLS)
    granted = set(permissions or ())
    return [spec for spec in TOOLS if spec.permission is None or spec.permission in granted]


def all_permissions_used() -> set[str]:
    return {spec.permission for spec in TOOLS if spec.permission}


def validate_catalog() -> list[str]:
    """自检：名称合法、无重复、目标函数存在且可调用。返回问题列表（空 = 通过）。"""
    problems: list[str] = []
    seen: set[str] = set()
    for spec in TOOLS:
        if spec.name in seen:
            problems.append(f"工具名重复：{spec.name}")
        seen.add(spec.name)
        if not set(spec.name) <= _TOOL_NAME_OK:
            problems.append(f"工具名含非法字符：{spec.name}")
        try:
            handler = spec.resolve()
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{spec.name} 找不到执行函数 {spec.target}：{exc}")
            continue
        if not callable(handler):
            problems.append(f"{spec.name} 目标不可调用：{spec.target}")
    return problems
