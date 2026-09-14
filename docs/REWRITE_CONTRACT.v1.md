# 美家物业 · 精简版重写契约（v1）

> 本文件是本轮重写的**唯一权威接口契约**。后端、前端、智能体集成三方都以本文件为准，
> 任何一方需要改动契约，先改本文件并在群里同步。

## 0. 本轮目标

1. **后端重写**：只保留最小闭环（空间主数据 → 人员与关系 → 维修工单闭环 → 审计），
   代码量大幅下降，删除历史补丁式文件。
2. **前端配合删减**：页面只保留闭环需要的 8 个页面，导航按权限渲染。
3. **智能体重写**：智能体换成 **DeepSeek Harness（dsh）原始运行时，不做任何改造**，
   通过它自带的 Python SDK 驱动；能力通过 **MCP 工具** 暴露；
   **权限与当前登录用户完全一致**（RBAC + 数据范围 + 会话）。

非目标：财务/账单、投诉、访客、车辆、车位、设备、巡检、公告、通知、附件、多租户、
R0–R3 风险卡片、自定义角色、手机号加密等，本轮全部不做。

## 1. 目录布局（重写后）


```text
wuye/
├── app.py                  # Flask 应用装配 + 全部 HTTP 路由（薄）
├── config.py               # 环境配置（读 .env）
├── db.py                   # SQLAlchemy engine/session/事务
├── models.py               # 精简模型（11 张表）
├── permissions.py          # Policy：RBAC + 数据范围（唯一授权来源）
├── services.py             # 领域服务：所有写操作的唯一入口（web 与 agent 共用）
├── queries.py              # 只读查询（列表/详情，全部带数据范围）
├── audit.py                # 审计写入
├── seed_demo.py            # 幂等演示数据（美家花园 + 5 个账号）
├── agent/
│   ├── __init__.py
│   ├── runtime.py          # dsh 运行时：发现/启动/复用/回收（每登录用户一个）
│   ├── bridge.py           # SSE 桥：dsh 通知 → 浏览器事件
│   ├── mcp_server.py       # MCP stdio server：把 services/queries 暴露成工具
│   ├── tools.py            # 工具清单（名称/参数/权限）——MCP 与文档的唯一来源
│   ├── session_store.py    # 会话与消息落库（供页面刷新后回看）
│   └── config/
│       ├── wuye_agent.md   # 智能体系统提示词
│       └── dsh.patch.yml.j2# dsh 配置覆盖模板（注入 MCP server 与系统提示词）
├── templates/              # 8 个页面模板
├── static/                 # css/js
├── tests/                  # 精简测试（见第 8 节）
└── docs/                   # 本契约 + 架构 + 演示脚本
```

历史文件（`agent_*_patch.py`、`agent_planner*`、`dify_client*`、`business*`、`management*`、
`property_service*` 等）统一移入 `_legacy/`，不再参与运行。

## 2. 数据模型（11 张表，`models.py`）

所有业务表带 `deleted`(bool, default False) 软删除；`Record` 基类提供 `id/created_at/updated_at/version`。

| 表 | 关键字段 | 说明 |
| --- | --- | --- |
| `sys_user` | username, password_hash, real_name, phone, active, auth_version | 登录账号 |
| `rbac_role` | code, name | 固定 5 个角色，种子写入 |
| `role_permission` | role_code, permission | 角色 → 权限点 |
| `user_role` | user_id, role_code | 账号 → 角色 |
| `user_scope` | user_id, kind(all/community/building/assigned/self), community_id, building_id | 数据范围 |
| `community` | name, address | 小区 |
| `building` | community_id, name | 楼栋 |
| `house` | community_id, building_id, unit, room, area, status(0空置/1自住/2出租) | 房屋 |
| `person` | name, phone, user_id(nullable) | 人员档案 |
| `house_person` | house_id, person_id, relation(owner/tenant/family), status(active/ended), start_at, end_at | 房屋人员关系 |
| `work_order` | no, community_id, building_id, house_id, requester_person_id, owner_id, contact_name, contact_phone, category, description, urgency, status, repairer_id, finished_at, closed_at, rating, rating_note | 维修工单 |
| `order_log` | order_id, action, from_status, to_status, operator_id, note | 工单流转日志 |
| `audit_log` | user_id, action, target_type, target_id, detail(JSON), source(web/agent) | 审计 |
| `agent_session` | user_id, title, dsh_session_id | AI 会话 |
| `agent_message` | session_id, role, content, created_at | AI 消息（渲染用） |

> 表数量以本节为准：**共 15 张** = 业务 11 张（community/building/house/person/house_person/work_order）
> 之外的 9 张中，权限 5 张（sys_user/rbac_role/role_permission/user_role/user_scope）、日志 2 张（order_log/audit_log）、
> AI 辅助 2 张（agent_session/agent_message）。

**工单状态机**（`status` 用整数，演示固定 6 态）：

```text
0 待派单 --assign--> 1 已派单 --accept--> 2 维修中 --finish--> 3 待验收 --verify--> 4 已关闭 --rate--> 4
任意非终态 --cancel--> 5 已取消          3 --reopen--> 2
```

## 3. 权限模型（`permissions.py`）

**权限点（精简后 15 个）**

```text
community.read community.write house.read house.write
person.read person.write relation.write
order.read order.create order.dispatch order.work order.verify order.cancel
staff.read audit.read resident.self
```

**角色 → 权限（种子固定，不可在界面上改）**

| 角色 code | 名称 | 权限 | 数据范围 kind |
| --- | --- | --- | --- |
| `admin` | 系统管理员 | 全部 | `all` |
| `manager` | 物业经理 | 除 `resident.self` 外全部 | `community` |
| `service` | 客服 | community.read, house.read, person.read/write, relation.write, order.read/create/dispatch/verify/cancel, staff.read | `community` |
| `engineer` | 工程维修 | order.read, order.work | `assigned` |
| `owner` | 业主 | resident.self, house.read, person.read, order.read/create/verify/cancel | `self` |

**Policy 接口（唯一授权入口，web / MCP 工具都走它）**

```python
Policy(db, user)
  .roles / .permissions / .super / .scopes
  .has(perm) -> bool
  .require(perm) -> 403 on deny
  .within(cid, bid=None, write=False) -> bool      # 小区/楼栋范围
  .require_scope(cid, bid=None) -> 403 on deny
  .condition(Model) -> SQLAlchemy 表达式           # 行级数据范围
  .query(Model) -> select(...)                     # 自动带 condition + deleted
  .get(Model, id) -> obj or 404
  .identity() -> {userId, username, roles, permissions, data_scope}
```

工程维修（`assigned`）只有本人被派到的工单可见；业主（`self`）只能看到本人关系下的房屋与工单。
**权限一律实时从数据库算，不从提示词/请求参数取身份。**

## 4. 服务层（`services.py` + `queries.py`）

服务的每个函数签名统一为 `fn(actor: Policy, **params) -> dict`，内部顺序固定：

```text
require(权限) → require_scope(数据范围) → 参数校验 → 业务规则 → 写库 → order_log/audit → 回读校验 → 返回
```

**写命令（web 表单与 MCP 工具共用同一函数，不重复实现）**

| 命令 | 权限 | 说明 |
| --- | --- | --- |
| `create_community / update_community / delete_community` | community.write | 删除为软删，需无楼栋 |
| `create_building / update_building / delete_building` | community.write | |
| `create_house / update_house / delete_house` | house.write | 房号在楼栋内唯一；有在住关系不可删 |
| `create_person / update_person / delete_person` | person.write | 有有效关系不可删 |
| `bind_relation / end_relation` | relation.write | 同一房屋同人员只允许一条 active |
| `create_work_order` | order.create | 联系人+电话必填，房屋必填 |
| `assign_work_order` | order.dispatch | 目标必须是 `engineer` 角色账号或有效人员 |
| `accept_work_order` | order.work | 仅被派人 |
| `add_order_progress` | order.work | 仅被派人，工单须为维修中 |
| `finish_work_order` | order.work | 仅被派人，维修中 → 待验收 |
| `verify_work_order` | order.verify | 待验收 → 已关闭 |
| `cancel_work_order` | order.cancel | 非终态 → 已取消，需原因 |
| `rate_work_order` | order.create(本人单) | 已关闭可评价 1–5 |

**只读查询（`queries.py`）**：`list_communities / list_buildings / list_houses / get_house /
list_persons / get_person / list_relations / list_work_orders / get_work_order / list_staff / whoami`
——全部先过 `Policy.query/get`。

所有函数返回**可 JSON 序列化的 dict**（页面与 MCP 工具共用），错误统一抛 `ServiceError(ok=False, code, message)`，
`abort` 只用于 web 层的 401/403/404 呈现。

## 5. HTTP 路由（`app.py`，前端契约）

| 方法 | 路径 | 权限 | 页面/作用 |
| --- | --- | --- | --- |
| GET/POST | `/login` | 匿名 | 登录 |
| POST | `/logout` | 登录 | 登出 |
| GET | `/health` | 匿名 | 健康检查 |
| GET | `/` | 登录 | 工作台（工单看板 + 最近工单 + 我的身份卡） |
| GET | `/houses` | house.read | 小区/楼栋/房屋三级列表 + 新增/编辑/删除表单 |
| POST | `/houses/<entity>/<action>` | house.write/community.write | entity ∈ community/building/house；action ∈ create/update/delete |
| GET | `/persons` | person.read | 人员档案 + 房屋关系 |
| POST | `/persons/<entity>/<action>` | person.write/relation.write | entity ∈ person/relation |
| GET | `/orders` | order.read | 工单列表（按状态/关键词过滤，分页 20） |
| GET | `/orders/new` | order.create | 报修表单 |
| POST | `/orders` | order.create | 提交报修 |
| GET | `/orders/<id>` | order.read | 工单详情 + 按当前状态与权限渲染的操作按钮 |
| POST | `/orders/<id>/<action>` | 对应命令权限 | action ∈ assign/accept/progress/finish/verify/cancel/rate |
| GET | `/ai` | 登录 | AI 助手页（会话列表 + 聊天窗口） |
| POST | `/ai/sessions` | 登录 | 新建会话 |
| GET | `/ai/sessions/<id>` | 登录 | 会话消息 JSON |
| POST | `/ai/chat` (body: session, q) | 登录 | **SSE** 流式回答（`text/event-stream`，见第 6 节） |
| GET | `/audit` | audit.read | 审计列表（含 source=agent 标记） |

导航只渲染当前用户有权限的入口；无权限直接访问返回 403 页面。

> `/ai/chat` 用 **POST + SSE 帧**（body: `session=<id>&q=<文本>`，带 CSRF，响应 `text/event-stream`）。
> 前端用 `fetch` + `ReadableStream` 解析 `data:` 帧，不用 `EventSource`
> （EventSource 只能 GET 且无法带 CSRF，等于允许第三方页面替登录用户下指令）。

**模板上下文契约（前后端唯一耦合点，已冻结；任何改动先改本表并通知双方）**

| 变量 | 作用域 | 类型/说明 |
| --- | --- | --- |
| `current_user` | 全局（context_processor） | `{id, username, name, role_names[], scope_text}`，顶栏显示 `name` |
| `nav` | 全局 | `[{label, href, active_key}]`，由 `Policy` 计算；模板只渲染，不判断权限 |
| `csrf_token` | 全局 | 函数：`{{ csrf_token() }}`，隐藏域 `name="csrf_token"` |
| flash | 全局 | 标准 `get_flashed_messages(with_categories=True)` |
| `status_counts` | dashboard | `{0:3,1:1,2:1,3:1,4:2,5:1}`，缺失键按 0 |
| `recent_orders` | dashboard | `[{id, no, house_text, description, status, status_text, repairer_name, created_at}]` |
| `identity` | dashboard | `{name, role_names[], scope_text, permission_count}` |
| `communities` | houses / order_form | `[{id, name, address}]` |
| `buildings` | houses / order_form | `[{id, community_id, name}]` |
| `houses` | houses | `[{id, house_text, community_name, building_name, unit, room, area, status, status_text}]` |
| `selected_community`, `selected_building` | houses | `int \| None`（回填筛选） |
| `persons` | persons | `[{id, name, phone, house_text, relation_text}]` |
| `selected_person` | persons | `{id, name, phone} \| None` |
| `relations` | persons | `[{id, house_text, person_name, relation, relation_text, status, status_text, start_at}]` |
| `orders` | orders | `[{id, no, house_text, description, status, status_text, repairer_name, created_at}]` |
| `total`, `page`, `pages` | orders / audit | 分页信息（`page_size = 20`） |
| `status_filter`, `keyword` | orders | 回填筛选项 |
| `order` | order_detail | `{id, no, house_text, community_name, building_name, unit, room, contact_name, contact_phone, category, description, urgency, urgency_text, status, status_text, requester_name, repairer_name, created_at, finished_at, closed_at, rating, rating_note}` |
| `logs` | order_detail | `[{action_text, operator_name, note, from_status_text, to_status_text, created_at}]` |
| `staff` | order_detail | `[{id, name, username}]`（派单下拉） |
| `actions` | order_detail | `[str]`，当前状态且当前权限**允许**的动作，取值 `assign/accept/progress/finish/verify/cancel/rate` |
| `sessions` | ai | `[{id, title, updated_at}]` |
| `current_session` | ai | `{id, title} \| None`；历史消息由 `GET /ai/sessions/<id>` 返回 JSON |
| `audit_logs` | audit | `[{id, created_at, user_name, action_text, target_text, source_text, source}]` |

## 6. 智能体集成契约（dsh）

**形态**：`deepseek-harness`（dsh）**原封不动**，作为 Python 依赖安装
（`deepseek-harness-sdk`，自带单文件运行时 `deepseek-harness-runtime-bin`）。
本项目只做三件事：**配置覆盖（patch）**、**MCP 工具服务**、**SSE 桥**。

```text
浏览器 /ai (SSE)
  → Flask /ai/chat
  → agent/bridge.py（本次消息 → dsh session/prompt）
  → DeepSeekHarness（Python SDK，一个登录用户一个运行时，懒启动 + 复用 + 空闲回收）
      profile: sdk + --patch <每用户生成的覆盖文件>
      ├─ 系统提示词 = agent/config/wuye_agent.md
      ├─ 工具只有 wuye MCP（stdio: python -m agent.mcp_server，env 带 WUYE_AGENT_TOKEN）
      └─ 关闭 bash/文件/网络等与业务无关的工具
  → agent/mcp_server.py 每次调用：token → 登录用户 → Policy → services/queries（与页面同一套代码）
  → 回传 tool/call、tool/result、assistant/message、session/turn 事件
```

**身份绑定**：
- 每次为一个登录用户启动运行时，生成**短期会话令牌**（`itsdangerous`，含 `uid` + `auth_version`，TTL 8h）。
- 令牌只放在 MCP 子进程**环境变量**里，**绝不出现在提示词或模型上下文**。
- 令牌失效条件：用户登出 / 角色或数据范围改变（`auth_version` 变化）/ 过期 → 工具调用返回 401，提示重新登录。

**工具清单（`agent/tools.py`，与 MCP 注册一一对应）**

| 工具 | 参数 | 对应服务 | 权限 |
| --- | --- | --- | --- |
| `whoami` | – | queries.whoami | 登录 |
| `list_communities` | keyword? | queries | community.read |
| `list_buildings` | community | queries | community.read |
| `list_houses` | community?, building?, keyword?, status? | queries | house.read |
| `get_house` | house_id | queries | house.read |
| `create_house` | community, building, unit, room, area? | services | house.write |
| `update_house` | house_id, area?, status? | services | house.write |
| `delete_house` | house_id | services | house.write |
| `list_persons` | keyword? | queries | person.read |
| `get_person` | person_id | queries | person.read |
| `create_person` | name, phone | services | person.write |
| `update_person` | person_id, name?, phone? | services | person.write |
| `delete_person` | person_id | services | person.write |
| `list_relations` | house_id?, person_id? | queries | person.read |
| `bind_relation` | house_id, person_id, relation | services | relation.write |
| `end_relation` | relation_id, reason? | services | relation.write |
| `list_work_orders` | status?, keyword?, community?, mine? | queries | order.read |
| `get_work_order` | order_id | queries | order.read |
| `create_work_order` | house_id, contact_name, contact_phone, category, description, urgency? | services | order.create |
| `assign_work_order` | order_id, repairer | services | order.dispatch |
| `accept_work_order` | order_id | services | order.work |
| `add_order_progress` | order_id, note | services | order.work |
| `finish_work_order` | order_id, note? | services | order.work |
| `verify_work_order` | order_id, note? | services | order.verify |
| `cancel_work_order` | order_id, reason | services | order.cancel |
| `rate_work_order` | order_id, rating, note? | services | order.create |
| `list_staff` | keyword? | queries | staff.read |

> 工具被拒时返回**通俗中文**（例："当前账号没有派单权限"），不暴露内部术语（R0–R3、RBAC 等）。

**SSE 事件协议（前端 `static/js/ai.js` 按此渲染）**

```json
{"type":"status","text":"正在思考…"}
{"type":"tool","name":"create_work_order","label":"创建报修工单","args":{"house_id":12},"state":"running"}
{"type":"tool","name":"create_work_order","state":"done","summary":"工单 #1024 已创建"}
{"type":"delta","text":"已为张伟…"}            // 最终回答分片（打字机效果）
{"type":"done","message_id":123}
{"type":"error","text":"..."}
```

**模型/凭据**：沿用 `.env` 的 `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL`；
provider `deepseek-official`，模型默认 `deepseek-v4-flash`（`DSH_MODEL` 可覆盖）。

## 7. 前端契约

- 模板：`base.html`（导航按权限）、`login.html`、`dashboard.html`、`houses.html`、
  `persons.html`、`orders.html`、`order_form.html`、`order_detail.html`、`ai.html`、`audit.html`、`error.html`。
- 只用服务端渲染 + 少量原生 JS，不引入前端框架；样式沿用现有 `static/css/style.css` 并删掉用不到的规则。
- 文案：面向用户，通俗中文，不出现 `RBAC`、`DataScope`、`R0–R3`、`TOOL` 等内部术语。
- `/ai` 页面：左侧会话列表、右侧消息流、底部输入框；工具调用以"正在执行：创建报修工单"样式展示。

## 8. 测试与验收（`tests/`）

1. `test_permissions.py`：5 个角色的权限矩阵与数据范围（越权必须 403）。
2. `test_services.py`：工单状态机全链路（含非法流转被拒）、房屋/人员唯一性、软删除约束。
3. `test_routes.py`：登录、导航可见性、页面渲染、未授权 403。
4. `test_mcp_tools.py`：工具清单与权限一致性（每个工具的权限点都能被 Policy 校验）。
5. `test_agent_bridge.py`：SSE 事件协议（用假运行时，不花模型配额）。
6. `test_agent_live.py`（默认 skip，`WUYE_LIVE_AGENT=1` 时跑）：真实模型跑一句增删改查。

验收口径：`python -m unittest discover -s tests -q` 全绿；`python seed_demo.py reset && python seed_demo.py check` 通过；
演示闭环（登录 → 报修 → 派单 → 接单 → 完工 → 验收 → 审计留痕，且 AI 助手能用自然语言完成同样动作）可复现。
