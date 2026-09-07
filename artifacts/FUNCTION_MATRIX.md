# 美家物业功能矩阵

验收基线：`8ea12a999785dca2288c742891040475a3373938`。路由、Service、模型和权限来自当前源码；Agent 项为 `PropertyService` 领域命令或只读查询。`DataScope` 统一由服务端 `Policy` 复核。

| ID | 模块 | 页面 | 操作 | Route/API | Service | Table | Permission | DataScope | Agent Tool | Risk |
| -- | -- | -- | -- | --------- | ------- | ----- | ---------- | --------- | ---------- | ---- |
| F01 | 认证 | 登录/注册/资料 | 登录、注册、改密、登出 | `/auth/login`, `/auth/register`, `/profile`, `/auth/logout` | `BusinessService.user_action` | `sys_user`, `audit_log` | Session/CSRF | 当前会话、auth_version | 无 | R1 |
| F02 | 物业 | 小区 | 新增、编辑、归档 | `/manage/communities`, `/api/manage/communities`, `/operations/community.save` | `PropertyService.do_community` | `community` | `community.manage` | community scope | `community.save` | R1/R3 |
| F03 | 物业 | 楼栋 | 新增、编辑、归档 | `/manage/buildings`, `/operations/building.save` | `do_building`, `do_property` | `building` | `property.read/write` | community/building | `building.save`, `property.archive` | R1/R3 |
| F04 | 物业 | 单元 | 新增、编辑、归档 | `/manage/units`, `/operations/unit.save` | `do_unit`, `do_property` | `property_unit` | `property.read/write` | community/building | `unit.save`, `property.archive` | R1/R3 |
| F05 | 物业 | 房屋 | 新增、编辑、产权、归档 | `/manage/houses`, `/operations/house.save` | `do_house`, `do_property` | `house` | `property.read/write` | community/building/self | `house.save`, `house.ownership`, `property.archive` | R1/R2/R3 |
| F06 | 人员 | 人员档案 | 新增、编辑、归档 | `/manage/people`, `/operations/person.save` | `do_person` | `person` | `person.read/write` | community/building/self | `person.save`, `person.archive` | R1/R3 |
| F07 | 关系 | 房屋人员关系 | 绑定、解除、历史 | `/manage/relations`, `/operations/relation.bind` | `do_relation`, `bind` | `house_person` | `relation.write/end` | relation house scope | `relation.bind`, `relation.bind_by_name`, `relation.end` | R2 |
| F08 | 租赁 | 租户与租赁 | 入住、共同入住、退租 | `/manage/leases`, `/operations/lease.create` | `do_lease` | `lease` | `lease.write` | house scope | `lease.create`, `lease.checkout` | R2 |
| F09 | 工单 | 维修工单 | 报修、派单、改派、接单、进度、完工、验收、返修、取消、评价 | `/orders`, `/orders/<order_no>`, `/api/business/order.*` | `do_order`, `BusinessService.order_action`, `transition_status` | `work_order`, `order_log`, `notification`, `order_evaluate` | `order.*` | owner/repairer/assigned/community/building | `order.create`, `order.assign`, `order.reassign`, `order.accept`, `order.progress`, `order.finish`, `order.close`, `order.reopen`, `order.cancel`, `order.evaluate` | R1/R2 |
| F10 | 投诉 | 投诉建议 | 登记、分派、处理、结案 | `/manage/complaints`, `/api/reports/complaints` | `do_complaint` | `complaint` | `complaint.read/create/handle` | reporter/community/building | `complaint.create`, `complaint.assign`, `complaint.resolve`, `complaint.close` | R1/R2 |
| F11 | 公告 | 公告通知 | 发布、编辑、撤下、范围读取 | `/notices`, `/manage/notices` | `do_notice` | `notice` | `notice.read/write` | community/building/self | `notice.save`, `notice.archive` | R1/R2 |
| F12 | 访客 | 访客登记 | 登记、进入、离开、取消 | `/manage/visitors` | `do_visitor` | `visitor` | `visitor.read/write` | community/building/house | `visitor.create`, `visitor.checkin`, `visitor.checkout`, `visitor.cancel` | R1 |
| F13 | 车辆 | 车辆 | 登记、编辑、归档 | `/manage/vehicles` | `do_vehicle` | `vehicle` | `vehicle.read/write` | community/building/house/self | `vehicle.save`, `vehicle.archive` | R1/R2 |
| F14 | 车位 | 车位/使用记录 | 建档、分配、释放 | `/manage/parking`, `/manage/parking-uses` | `do_parking` | `parking_space`, `parking_use` | `parking.read/write` | community/building/house | `parking.save`, `parking.assign`, `parking.release` | R1/R2 |
| F15 | 设备 | 设备设施 | 建档、状态、归档 | `/manage/devices` | `do_device` | `device` | `device.read/write` | community/building | `device.save`, `device.archive` | R1/R2 |
| F16 | 巡检 | 巡检任务 | 分配、完成、故障报修 | `/manage/inspections` | `do_inspection` | `inspection` | `inspection.read/write/assign` | assignee/community/building | `inspection.create`, `inspection.complete` | R1 |
| F17 | 财务 | 收费项目 | 新增、编辑 | `/manage/fees` | `do_fee` | `fee_item` | `billing.read/manage` | community | `fee.save` | R2 |
| F18 | 财务 | 应收账单 | 单户、批量、作废 | `/manage/bills`, `/api/reports/finance` | `do_bill` | `bill` | `billing.read/manage` | house/community/building | `bill.create`, `bill.batch`, `bill.void` | R2/R3 |
| F19 | 财务 | 收款与冲销 | 登记收款、冲销 | `/manage/payments` | `do_payment` | `payment`, `bill` | `billing.collect/reverse` | bill house/community/building | `payment.record`, `payment.reverse` | R3 |
| F20 | RBAC | 账号与岗位 | 新增账号、岗位、启停 | `/users`, `/manage/staff` | `do_staff`, `set_roles` | `sys_user`, `user_role`, `user_scope` | `staff.manage`, `rbac.manage` | role/data scope | `staff.create`, `staff.roles`, `staff.state`（能力目录排除直接 Tool） | R3 |
| F21 | RBAC | 角色权限 | 自定义角色 | `/manage/roles` | `do_role` | `rbac_role`, `role_permission` | `rbac.define/manage` | permitted role subset | `role.save`（能力目录排除直接 Tool） | R3 |
| F22 | 审计 | 操作审计 | 查询操作、Agent Trace | `/audit`, `/manage/audit` | `audit`, `_agent_event` | `audit_log`, `ai_action` | `audit.read` | operator/scope | `context`, `lookup` | R0 |
| F23 | AI | AI 助手 | 状态、对话、流式、确认/取消 | `/ai`, `/ai/status`, `/ai/chat`, `/ai/actions/*`, `/api/agent/tools` | `BailianClient`, `DifyClient`, `AgentToolGateway` | `ai_conversation`, `ai_grant`, `ai_action` | 当前登录用户权限 | 实时 RBAC/DataScope/auth_version | `business_queries.QUERIES` + `property_service.COMMANDS` | R0-R3 |
| F24 | 附件 | 工单图片/头像 | 上传、私有读取 | `/uploads/<filename>` | `save_image`, `can_access_order` | `work_order`, `sys_user` | owner/admin/order scope | 归属工单或本人/管理员 | 无 | R1 |
| F25 | 部署 | 健康/迁移 | 初始化、升级、诊断 | `/health`, `manage.py init-db/upgrade-db/diagnose` | `initialize`, `upgrade`, `missing_schema` | 34 张 V2 表 | 部署账号/数据库权限 | runtime_environment 隔离 | 无 | R0 |

## Agent 能力清单

`available_commands(actor)` 按当前持久化 `RolePermission` 动态生成；`staff.create`、`role.save`、`relation.bind` 不直接暴露为 Agent Tool。所有写命令进入 `PropertyService`，高风险命令只能生成 `AiAction` 待确认记录，确认时重新校验用户、权限、数据范围、版本和状态。

## 数据库表

当前 `models.py` 声明 34 张表：认证与审计（`sys_user`, `audit_log`, `ai_conversation`, `ai_grant`, `ai_action`, `schema_migration`, `business_request`, `system_setting`）、兼容工单（`house`, `work_order`, `notice`, `order_evaluate`, `order_log`, `notification`）以及物业 V2 规范化表（`community`, `building`, `property_unit`, `person`, `lease`, `house_person`, `rbac_role`, `role_permission`, `user_role`, `user_scope`, `complaint`, `visitor`, `vehicle`, `parking_space`, `parking_use`, `device`, `inspection`, `fee_item`, `bill`, `payment`）。
