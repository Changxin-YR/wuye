# 云邻AI智脑 · 重写契约（v2，权威）

> v2 定稿口径：本文件描述的每个技术点都必须能在仓库里逐条定位与复现。
> 本文件是后端、前端、智能体集成三方的**唯一接口契约**；改动先改本文件并通知全员。
> （v1 旧稿保留为 `docs/REWRITE_CONTRACT.v1.md`，仅供追溯。）

## 0. 目标与验收口径

**一句话**：Flask + SQLAlchemy + MySQL 的物业业务平台，所有写操作走统一的
`PropertyService`（事务 / 乐观锁 / 幂等 / 回读 / 审计），权限由 `RBAC + DataScope`
实时决定；平台内嵌入 **DeepSeek Harness（dsh）** 作为受控 AI Agent，
它通过 MCP 工具在**当前登录用户权限范围内**完成查询与增删改查，
高风险操作走「动态能力目录 + R0–R3 风险分级 + 短期授权 + 人工确认 + 执行后回读 + 审计」。

**验收清单（必须能现场跑通的能力点）**

| # | 能力点 | 验证方式 |
| --- | --- | --- |
| 1 | Flask + Jinja2 + SQLAlchemy + MySQL，多业务模块 | 页面走一遍：房屋 / 人员关系 / 租赁 / 工单 / 投诉 / 访客 / 车辆车位 / 设备巡检 / 收费 |
| 2 | 社区—楼栋—单元—房屋 规范化数据模型 | 房屋页三级联动 + 全程展示房屋全称 |
| 3 | PropertyService：状态流转、账单、收款、冲销 | 工单全流程 + 建账单 + 收款 + 冲销，全部同一个服务层 |
| 4 | 事务 / 乐观锁 / 版本校验 / 幂等 / 回滚 | `tests/test_service_guards.py` + 现场重复提交（幂等键、旧版本号被拒） |
| 5 | RBAC + DataScope，页面与 Agent 同一套校验 | 同一句话，admin/manager/service/engineer/finance/owner 六种结果 |
| 6 | AI Agent 自然语言查询 + 办理业务 | 对话：查欠费、报修、派单、登记访客、催收 |
| 7 | 动态能力目录 | 无权限的工具**不出现在模型工具列表**；硬调仍被拒 |
| 8 | R0–R3 风险分级 + 人工确认 | 删除房屋 / 作废账单 → 确认卡片，点击后执行 |
| 9 | 短期授权 | 令牌绑定 user + auth_version，8 小时过期，改角色立即失效 |
| 10 | 操作审计 + 结果回读 | 审计页 `source=agent` 记录；写操作回读目标行再核对 |

## 1. 目录布局

```text
wuye/
├── app.py                  # Flask 装配 + 页面路由（薄）
├── config.py  db.py        # 配置 / 会话与事务
├── models.py               # 26 张表
├── permissions.py          # Policy：RBAC + DataScope（唯一授权来源）
├── risk.py                 # R0–R3 风险分级 + 能力目录 + 确认策略
├── services.py             # PropertyService 核心：空间/人员/租赁/工单/收费（写操作唯一入口）
├── queries.py              # 核心只读查询（全部带数据范围）
├── services_ops.py         # PropertyService 运营扩展：投诉/访客/车辆车位/设备巡检
├── queries_ops.py          # 运营模块只读查询
├── audit.py                # 审计（before/after、source=web|agent）
├── seed_demo.py            # 幂等演示数据
├── agent/                  # 智能体集成（dsh 原封不动，只做配置与桥接）
│   ├── runtime.py  bridge.py  mcp_server.py  tools.py  token.py  session_store.py  actions.py
│   └── config/wuye_agent.md
├── templates/  static/     # 页面
├── tests/
├── _legacy/                # 历史代码（不参与运行）
└── docs/
```

## 2. 数据模型（26 张表）

`Record` 基类：`id, created_at, updated_at, version, deleted`（`version` 用于乐观锁）。

| 分组 | 表 | 关键字段 |
| --- | --- | --- |
| 账号权限 | `sys_user` | username, password_hash, real_name, phone, active, auth_version |
| | `rbac_role` | code, name |
| | `role_permission` | role_code, permission |
| | `user_role` | user_id, role_code |
| | `user_scope` | user_id, kind(all/community/building/assigned/self), community_id, building_id |
| 空间 | `community` | name, address |
| | `building` | community_id, name |
| | `unit` | building_id, name |
| | `house` | community_id, building_id, unit_id, room, area, status(0 空置/1 自住/2 出租) |
| 人员 | `person` | name, phone, user_id(nullable) |
| | `house_person` | house_id, person_id, relation(owner/tenant/family), status(active/ended), start_at, end_at |
| | `lease` | house_id, person_id, rent, start_at, end_at, status(0 在租/1 已退租) |
| 工单 | `work_order` | no, community_id, building_id, house_id, requester_person_id, owner_id, contact_name, contact_phone, category, description, urgency, status, repairer_id, finished_at, closed_at, rating, rating_note |
| | `order_log` | order_id, action, from_status, to_status, operator_id, note |
| 投诉 | `complaint` | no, community_id, building_id, house_id, reporter_id, content, category, status(0 待处理/1 处理中/2 已结案/3 已取消), handler_id, result |
| 访客 | `visitor` | community_id, building_id, house_id, name, phone, visit_at, purpose, status(0 待进/1 已进/2 已离/3 已取消), operator_id |
| 车辆车位 | `vehicle` | community_id, house_id, plate, brand, owner_person_id, status(0 正常/1 已归档) |
| | `parking_space` | community_id, code, status(0 空闲/1 占用), house_id, vehicle_id |
| 设备巡检 | `device` | community_id, building_id, name, category, status(0 正常/1 维修中/2 已归档), location |
| | `inspection` | device_id, community_id, assignee_id, plan_at, status(0 待巡检/1 已完成/2 已转报修), result, order_id |
| 收费 | `bill` | no, community_id, house_id, person_id, fee_type, amount, paid_amount, status(0 待缴/1 部分/2 已缴/3 已作废), period, due_at |
| | `payment` | no, bill_id, amount, method(0 现金/1 银行/2 其他), reference, status(0 已入账/1 已冲销), operator_id, reversed_by |
| 审计与 AI | `audit_log` | user_id, action, target_type, target_id, detail(JSON), source(web/agent), ok |
| | `ai_action` | user_id, auth_version, session_id, tool_name, payload(JSON), payload_hash, preview, risk_level, status(0 待确认/1 已执行/2 已取消/3 已过期/4 执行失败), result(JSON), expires_at |
| | `agent_session` | user_id, title, dsh_session_id |
| | `agent_message` | session_id, role, content |

**状态机**

```text
工单：0 待派单 →(派单) 1 已派单 →(接单) 2 维修中 →(完工) 3 待验收 →(验收) 4 已关闭
      非终态 →(取消) 5 已取消            待验收 →(返修) 2 维修中
投诉：0 待处理 →(分配) 1 处理中 →(结案) 2 已结案；非终态 →(取消) 3 已取消
访客：0 待进 →(进入) 1 已进 →(离开) 2 已离；非终态 →(取消) 3 已取消
账单：0 待缴 →(全额收款) 2 已缴；0 →(部分收款) 1 部分；0/1 →(作废) 3 已作废
收款：0 已入账 →(冲销) 1 已冲销（账单回退到收款前状态）
巡检：0 待巡检 →(完成) 1 已完成 →(有故障转报修) 2 已转报修（生成工单）
```

## 3. 权限模型（`permissions.py`）

**权限点（35 个）**

```text
community.read community.write house.read house.write
person.read person.write relation.write lease.write
order.read order.create order.dispatch order.work order.verify order.cancel
complaint.read complaint.create complaint.handle
visitor.read visitor.write
vehicle.read vehicle.write parking.read parking.write
device.read device.write inspection.read inspection.write inspection.assign
billing.read billing.manage billing.collect billing.reverse
staff.read audit.read resident.self
```

**角色（6 个）**

| code | 名称 | 权限 | DataScope |
| --- | --- | --- | --- |
| `admin` | 系统管理员 | 全部 | `all` |
| `manager` | 物业经理 | 除 `resident.self` 外全部 | `community` |
| `service` | 客服 | community/house/person/relation/lease 读写、order.read/create/dispatch/verify/cancel、complaint 全部、visitor 全部、vehicle/parking 读写、staff.read | `community` |
| `engineer` | 工程维修 | order.read/work、device.read/write、inspection.read/write、community.read | `assigned` |
| `finance` | 财务 | billing.read/manage/collect/reverse、house.read、person.read、community.read | `community` |
| `owner` | 业主 | resident.self、house.read、person.read、order.read/create/verify/cancel、complaint.create/read、visitor.write/read、billing.read、vehicle.read | `self` |

`Policy` 接口：`has/require/within/require_scope/condition/query/get/identity`，
行级范围**必须落在 SQL 上**（`assigned` 只看本人被派单据，`self` 只看本人关系下数据）。

## 4. PropertyService（`services.py` + `queries.py`）

**统一签名**

```python
def cmd(actor: Policy, *, request_key: str | None = None,
        expected_version: int | None = None, source: str = "web", **params) -> dict
```

**固定流程**

```text
权限 → 数据范围 → 幂等键（命中则回放上次结果）→ 参数校验 → 业务规则
→ 乐观锁（expected_version 不符 → ServiceError 409）
→ 写库 + 关联行 + order_log/audit(before/after) → 回读校验 → 返回 {..., "message", "version"}
```

**命令清单**

| 模块 | 命令 | 权限 | 风险 |
| --- | --- | --- | --- |
| 空间 | `create_community / update_community / delete_community` | community.write | R1 / R1 / R3 |
| | `create_building / update_building / delete_building` | community.write | R1 / R1 / R3 |
| | `create_unit / update_unit / delete_unit` | community.write | R1 / R1 / R3 |
| | `create_house / update_house / delete_house` | house.write | R1 / R1 / R3 |
| 人员 | `create_person / update_person / delete_person` | person.write | R1 / R1 / R3 |
| | `bind_relation / end_relation` | relation.write | R1 / R2 |
| 租赁 | `check_in_lease / check_out_lease` | lease.write | R2 / R2 |
| 工单 | `create_work_order` | order.create | R1 |
| | `assign_work_order` | order.dispatch | R2 |
| | `accept_work_order / add_order_progress / finish_work_order` | order.work | R1 |
| | `verify_work_order / reopen_work_order` | order.verify | R2 / R2 |
| | `cancel_work_order` | order.cancel | R2 |
| | `rate_work_order` | order.create | R1 |
| 投诉 | `create_complaint` | complaint.create | R1 |
| | `assign_complaint / handle_complaint / close_complaint / cancel_complaint` | complaint.handle | R1 / R1 / R2 / R2 |
| 访客 | `register_visitor / enter_visitor / leave_visitor / cancel_visitor` | visitor.write | R1 |
| 车辆车位 | `create_vehicle / update_vehicle / archive_vehicle` | vehicle.write | R1 / R1 / R3 |
| | `assign_parking / release_parking` | parking.write | R2 / R2 |
| 设备巡检 | `create_device / update_device / archive_device` | device.write | R1 / R1 / R3 |
| | `create_inspection` | inspection.assign | R1 |
| | `complete_inspection` | inspection.write | R1 |
| 收费 | `create_bill / create_bills_batch` | billing.manage | R2 / R2 |
| | `void_bill` | billing.manage | R3 |
| | `collect_payment` | billing.collect | R3 |
| | `reverse_payment` | billing.reverse | R3 |

**只读查询**：`list_communities / list_buildings / list_units / list_houses / get_house /
list_persons / get_person / list_relations / list_leases / list_work_orders / get_work_order /
list_complaints / get_complaint / list_visitors / list_vehicles / list_parking_spaces /
list_devices / list_inspections / list_bills / get_bill / list_payments /
arrears_summary / work_order_stats / list_staff / whoami`
——先过 `Policy.query/get`，返回 `{"items": [...], "total": n}`。

## 5. 页面路由（`app.py`，前端契约）

| 方法 | 路径 | 权限 |
| --- | --- | --- |
| GET/POST | `/login`，`POST /logout`，`GET /health` | 匿名 / 登录 |
| GET | `/` 工作台 | 登录 |
| GET | `/houses`；POST `/houses/<entity>/<action>`（community/building/unit/house） | house.read / house.write / community.write |
| GET | `/persons`；POST `/persons/<entity>/<action>`（person/relation） | person.read / person.write / relation.write |
| GET | `/leases`；POST `/leases/<action>`（check-in/check-out） | lease.write |
| GET | `/orders`、`/orders/new`、`/orders/<id>`；POST `/orders`、`/orders/<id>/<action>` | order.* |
| GET | `/complaints`、`/complaints/<id>`；POST `/complaints`、`/complaints/<id>/<action>` | complaint.* |
| GET | `/visitors`；POST `/visitors`、`/visitors/<id>/<action>` | visitor.* |
| GET | `/vehicles`（含车位）；POST `/vehicles/...`、`/parking/...` | vehicle.* / parking.* |
| GET | `/devices`（含巡检）；POST 对应 action | device.* / inspection.* |
| GET | `/bills`、`/bills/<id>`；POST `/bills/<id>/<action>`（collect/void/reverse） | billing.* |
| GET | `/audit` | audit.read |
| GET | `/ai`；POST `/ai/sessions`；GET `/ai/sessions/<id>`；POST `/ai/chat`（SSE） | 登录 |
| GET | `/ai/actions`；POST `/ai/actions/<id>/confirm`、`/ai/actions/<id>/cancel` | 登录（本人） |

**模板上下文契约（冻结）**：全局 `current_user{id,username,name,role_names,scope_text}`、
`nav[{label,href,active_key}]`、`csrf_token()`、`can(perm)`、`status_text(int)`、`cn_time(dt)`；
列表页统一 `items/total/page/pages` + 筛选回填；详情页动作由后端算好（`actions`）。
每个业务对象行都是**下划线风格** dict（`house_text`、`status_text`、`repairer_name`、`plate`、`amount`…）。

## 6. 智能体集成（dsh 原封不动 + MCP + 风险分级）

```text
浏览器 /ai
  → POST /ai/chat (SSE)
  → agent/bridge.py → DeepSeekHarness（Python SDK；每登录用户一个运行时，懒启动 + 复用 + 空闲回收）
      profile sdk + --patch（每用户生成）：系统提示词 + MCP stdio 行 + 关闭无关工具
  → agent/mcp_server.py（stdio，环境变量带短期令牌）
      → 令牌 → 登录用户 → Policy → 能力目录过滤 → risk.py 决策
          ├─ R0/R1        → 直接调 PropertyService
          ├─ R2 白名单内  → 直接调；其余 → 建 AiAction（待确认）
          └─ R3           → 一律建 AiAction（待确认）
  → SSE：status / tool / delta / title / action / done / error
```

- **短期授权**：`itsdangerous` 令牌含 `uid + auth_version`，TTL 8 小时；只放 MCP 子进程环境变量；
  登出、改角色、改数据范围 → `auth_version` 变化 → 令牌与缓存运行时同时失效。
- **动态能力目录**：MCP 握手时按当前用户权限过滤 `tools/list`；无权限能力不进模型上下文；
  即使模型硬调，服务端 `Policy.require` 仍二次拒绝。
- **风险分级（`risk.py`）**：R0 只读 / R1 普通写自动 / R2 白名单自动其余确认 /
  R3 删除、财务、归档一律确认。R2 白名单默认 `bind_relation`、`check_in_lease`、`assign_parking`，
  可用环境变量**收紧**（不能把 R3 放宽）。
- **人工确认**：`AiAction` 保存服务端 payload、`payload_hash`、预览、风险级别、过期时间（10 分钟）；
  浏览器只提交 `action_id`；确认时重新校验权限、数据范围、目标版本与业务状态；过期/越权/目标变化一律拒绝。
- **执行后回读**：写命令返回 `version` 与回读结果；智能体只有 `ok=true` 才能说"已完成"。

**MCP 工具清单**：与 §4 命令/查询一一对应（当前共 **73** 个：25 个查询 + 48 个写命令，
唯一真相源是 `agent/tools.py`），工具名 = 命令名的下划线形式
（模型侧形如 `mcp__wuye__create_work_order`）。工具描述写明"先查后做"的引导；
破坏性工具注明"需用户确认"。

**SSE 事件协议**

```json
{"type":"status","text":"正在思考…"}
{"type":"tool","call_id":"…","name":"create_work_order","label":"创建报修工单","state":"running","args":{}}
{"type":"tool","call_id":"…","name":"create_work_order","state":"done","ok":true,"summary":"工单 WO… 已创建"}
{"type":"action","action_id":12,"tool":"delete_house","risk":"R3","preview":"删除房屋 云邻花园1栋1单元101","expires_at":"…"}
{"type":"delta","text":"已经为张伟报修："}
{"type":"title","title":"1栋1单元101 水管漏水"}
{"type":"done","text":"<权威全文>","message_id":123}
{"type":"error","text":"…"}
```

**模型通道**：默认 DeepSeek（`.env` 的 `DEEPSEEK_*`）；配置 `BAILIAN_API_KEY` 后可切到
百炼 OpenAI 兼容端点，只改 `.env`，代码不变。

## 7. 前端

页面：`login / dashboard / houses / persons / leases / orders / order_form / order_detail /
complaints / visitors / vehicles（含车位） / devices（含巡检） / bills / ai / audit / error`。
文案通俗中文，不出现 `RBAC`、`DataScope`、`R0–R3`、`TOOL`、`MISSING_PARAMETER` 等内部术语；
工单状态一律「待派单 / 已派单 / 维修中 / 待验收 / 已关闭 / 已取消」。

## 8. 测试

1. `test_permissions.py`：6 角色权限矩阵 + 数据范围（越权 403、跨小区隔离）
2. `test_services.py`：工单/投诉/访客/账单状态机、非法流转被拒、软删约束、回读
3. `test_service_guards.py`：幂等键重复只执行一次、`expected_version` 过期被拒、异常回滚
4. `test_risk.py`：R0–R3 分级、R2 白名单、R3 生成待确认动作、确认后执行并审计
5. `test_routes.py`：登录、导航可见性、页面渲染、未授权 403
6. `test_mcp_tools.py`：工具清单与权限/风险一致性
7. `test_agent_bridge.py`：SSE 事件协议（假运行时，不花模型配额）
8. `test_agent_live.py`：`WUYE_LIVE_AGENT=1` 时才跑的真实模型端到端

验收：`python -m unittest discover -s tests -q` 全绿；`seed_demo.py check` 通过；
演示闭环（报修 → 派单 → 接单 → 完工 → 验收 → 审计，AI 自然语言查询与办理，高风险确认卡片）可复现。
