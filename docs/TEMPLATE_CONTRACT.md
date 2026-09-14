# 前端实现说明（非契约）

> **契约只有一份：`docs/REWRITE_CONTRACT.md`（v2）**——路由见其 §5，前端见其 §7，事件协议见其 §6。
> 本文件**不是契约**、不定义接口，只记录前端侧的落地事实与自检方法；与主契约冲突时以主契约为准。

## 1. 交付物（19 个模板 + 1 个宏库）

| 分组 | 模板 |
| --- | --- |
| 外框 / 匿名 | `base.html`、`login.html`、`error.html` |
| 主数据 | `dashboard.html`、`houses.html`、`persons.html`、`leases.html` |
| 工单 | `orders.html`、`order_form.html`、`order_detail.html` |
| 服务 | `complaints.html`、`complaint_detail.html`、`visitors.html` |
| 资产 | `vehicles.html`（车辆 + 车位）、`devices.html`（设备 + 巡检） |
| 收费 | `bills.html`、`bill_detail.html` |
| 智能体 | `ai.html` |
| 审计 | `audit.html` |

`templates/_macros.html`：全站共用宏（`csrf_field()`）。**单一来源**——`base.html` 与各子模板都`{% from '_macros.html' import csrf_field with context %}`，不再在 `base.html` 里定义，避免多人共写 `base.html` 时被覆盖（曾经发生过）。

静态资源：`static/css/style.css`（精简样式，含 1180/960 两档断点）、`static/js/ai.js`（SSE 客户端 + 确认卡片）、`static/js/app.js`（二次确认、提示淡出）。

旧模板与旧 css/js 已按要求先复制备份到 `_legacy/templates/`、`_legacy/static/`（与 git HEAD 逐字节一致），再由精简版替换。

## 2. 前端侧实现约定（主契约没写、但实现必须定下来的部分）

1. **SSE 客户端**：`/ai/chat` 后端 **GET + POST 都支持**（两种都要求 CSRF），前端统一用 `POST`
   （body 带 `q`/`session`/`csrf_token`），请求一定带 `X-CSRF-Token` 头（值取自 `<meta name="csrf-token">`）：
   用 `fetch` + `ReadableStream` 解析 `data:` 帧；事件 `status / tool / delta / title / action / done / error`，
   **未知类型一律忽略**（保证协议向后兼容）。
2. **确认卡片**（`action` 事件）：显示风险中文名（普通操作 / 重要变更 / 高风险操作，不出现 R0–R3）+ 后端给的
   `label`/`preview`、「确认执行」/「取消」按钮；点击 `POST /ai/actions/<id>/confirm|cancel`（带 CSRF），
   返回的 `message` 作为一条系统消息插入消息流，卡片标记为已完成/已取消/未执行；`expires_at` 到点则按钮置灰显示「已过期」。
   **卡片只提交 action_id**，真正的权限与状态校验全在后端。
   **刷新不丢卡片**：`/ai` 初始化时 `GET /ai/actions` 拉一次待确认列表，把未过期的卡片补渲染出来
   （已在渲染 + 跳过的状态会被忽略，并提示「刷新前的记录已恢复」）。
2.1 **CSRF 来源**：`base.html` 的 head 有 `<meta name="csrf-token">`（所有页面可用），`ai.html` 额外在自己的
   `head_extra` 块里也放了一份（主契约要求）；`ai.js` 取 token 顺序：表单隐藏域 → meta → `data-csrf`。
   `POST /ai/chat`、`POST /ai/sessions`、`POST /ai/actions/<id>/confirm|cancel` 都带 `X-CSRF-Token` 头
   （确认/取消另在 body 里再放一份 `csrf_token`）。
2.2 **接口返回形状**：`GET /ai/actions` → `{"ok":true,"actions":[{action_id,tool,risk,preview,expires_at,session_id}]}`
   （前端也容忍 `{items:[...]}` 与裸数组）；`POST /ai/actions/<id>/confirm|cancel` → `{"ok":true,"status":"executed|cancelled","message":"…"}`，
   **卡片最终状态以后端 `status` 为准**（不是"点了哪个按钮"），`message` 作为系统消息插入消息流。
3. **CSRF**：所有 POST 表单走 `base.html` 的 `csrf_field()` 宏，兼容 `csrf_token` 与 `csrf_token()` 两种写法。
4. **端点兼容**：`/ai` 与 `/ai/actions/*` 既可能由 `app.py` 提供，也可能由 agent 蓝图提供，统一走
   `ai_page_url()/ai_new_url()/ai_chat_url()/ai_messages_url()/ai_action_url()/ai_actions_url()` 宏。
5. **缺失变量兜底**：每个业务模板顶部 `{% set x = x|default(...) %}`，后端漏传变量时页面仍可渲染。
6. **状态样式**：跟随后端 `status_class`；v2 新增 `status-complaint/visitor/inspection/bill/payment`，
   状态**文案**一律用后端给的 `*_text`（投诉/访客/账单/收款/巡检的中文都由后端算）。
   运营模块直接消费 `queries_ops.py` 的真实输出：`house_full`/`house_label`、`complaint.category_text`、
   `reporter_phone`、`visitor.operator_name`、`vehicle.space_code`、`inspection.order_no`、
   `device.last_inspection_at`/`inspection_count`；列表页统一 `items/page/pages/total`。
7. **导航**：12 项（工作台/房屋/人员关系/租赁/维修工单/投诉/访客/车辆车位/设备巡检/收费/AI 助手/操作审计），
   由后端 `nav` 按权限过滤后提供；高亮用 `active_key` 映射（兼容 `ai` 与 `ai.ai_page` 两种端点写法）。
8. **返修**：`order_detail.html` 在 `actions` 出现 `reopen` 时渲染「退回原因 + 返修」表单（3 待验收 → 2 维修中）。
9. **派单下拉**：优先用后端给的 `staff` 渲染下拉（显示在手单数）；`staff` 缺失/为空时降级为手填维修师傅姓名。
10. **表单 POST 的 CSRF**：`app.js` 在提交前保证表单里有 `csrf_token` 字段（缺失则从 meta 补一个），这样后端无论读表单字段还是读头都能命中；
11. **AI 端点名**：宏里用的是蓝图真实端点 `ai.ai_page / ai.ai_create_session / ai.ai_session_messages / ai.ai_chat / ai.ai_list_actions / ai.ai_confirm_action / ai.ai_cancel_action`，蓝图未注册时退回 app.py 自带端点名（`ai_page / ai_session_create / ai_session_messages / ai_chat / ai_actions / ai_actions_confirm / ai_actions_cancel`）。

## 3. 自检（10 条）

```bash
# 全部不需要 MySQL；后三条用 SQLite 临时库跑真实 app
python tests/render_check_frontend.py    # 桩模式 120 组合（20 用例 × 6 角色）+ 真实 app 的 10 个页面
python tests/check_app_e2e.py            # 真实 create_app() + 演示数据 + 6 账号逐页请求
python tests/check_order_actions.py      # 7 个动作 × staff 有无
python tests/check_nav_active.py         # 导航高亮 14 组
python tests/check_ai_chat_wiring.py     # POST /ai/chat 接线（会真跑一次模型）+ CSRF 拒绝
node   tests/render_check_ai_client.mjs  # ai.js：事件流 / 确认卡片（确认·取消·拒绝·过期）/ 异常
node   tests/check_layout_cdp.mjs        # headless Chrome：1440/1024/390 × 18 页布局体检（54 组）
node   tests/check_ai_live_cdp.mjs       # 浏览器内 ai.js 端到端 + 刷新后恢复待确认卡片（CDP 网络拦截）
node   tests/check_form_csrf_cdp.mjs     # 普通表单 POST 是否带上 csrf_token（CDP 拦截 POST 断言）
python tests/check_template_macros.py     # 模板宏：无未定义宏 / 无死宏 / csrf_field 单一来源
```

`tests/frontend_stubs.py` 是桩数据（渲染自检与静态预览共用，逐字对齐后端上下文）；
`artifacts/frontend-preview/` 是静态预览站点（`scripts/build-frontend-preview.ps1` 一键重建）。

## 4. 与后端的对接事实（实测）

- **v2 六个运营页面已由 `ops_routes.register_ops_routes` 注册并端到端跑通**（`/leases` `/complaints`
  `/complaints/<id>` `/visitors` `/vehicles` `/parking/<action>` `/devices` `/inspections/<action>`
  `/bills` `/bills/<id>`），端点名与我模板里的 `url_for` 一致（`leases` / `leases_command` /
  `complaints` / `complaints_command` / `complaint_detail` / `visitors` / `visitors_command` /
  `vehicles` / `vehicles_command` / `parking_command` / `devices` / `devices_command` /
  `inspections_command` / `bills` / `bills_command` / `bill_detail`）。
  实测：6 个账号 × 逐页 **112 次请求全部 200/403 符合预期，0 问题**。
- 注意 `app.py` 用 `try/except` 注册这些模块（`register_ops_module_routes` / `register_leases_page`），
  模块导入失败时**只打 info 日志**、页面就会全 404。排查 404 时先看日志里有没有
  「ops_routes 未就绪」。
- `ops_routes` 传的权限布尔是 `can_edit` / `can_parking` / `can_device` / `can_inspection` /
  `can_manage` / `can_collect` / `can_reverse`；模板用 `x|default(can('…'))` 同时兼容"给布尔"和"只给 can()"两种。


- `POST /ai/chat` → `200 text/event-stream`；实测事件序列：`status → title → tool(running) → tool(done) → status → delta×N → done`。
- 缺 CSRF（POST 与 GET）→ `400`；前端展示中文提示并恢复输入框。
- ⚠️ **登录成功会轮换会话里的 `csrf_token`**：任何脚本化调用必须在登录后重新从页面读 token，否则 POST 一律 400。

## 5. 双名兼容层（captain 已裁决：保留）

主契约 §7 冻结的变量名与后端落地时微调的名字有 7 处不同。模板对**两种命名都接受**，
后端叫什么都能正确渲染（不新建契约、不改后端）。**演示结束后如需统一口径，删掉左列的回退即可。**

| 用途 | 主契约 §7 冻结名 | 后端实际落地名 | 模板写法 |
| --- | --- | --- | --- |
| 用户显示名 | `current_user.name` | `current_user.real_name` | `real_name or name or username` |
| 数据范围文案 | `current_user.scope_text` | `current_user.data_scope_text` | `scope_text or data_scope_text` |
| 当前会话 | `current_session` | `active_session` | `current_session\|default(active_session)` |
| 房屋选中小区 | `selected_community` | `selected_community_id` | `selected_community_id\|default(selected_community)` |
| 房屋选中楼栋 | `selected_building` | `selected_building_id` | `selected_building_id\|default(selected_building)` |
| 审计列表 | `audit_logs` | `logs` | `logs\|default(audit_logs)` |
| 房屋全称（工单/列表） | `house_text` | `house_full` | 只用 `house_full`（运营模块统一） |
| 权限布尔（运营页） | —（用 `can('…')`） | `can_manage/can_collect/can_reverse/can_edit/can_parking/can_device/can_inspection` | `can_x\|default(can('…'))` |

对应测试：`tests/render_check_frontend.py` 的 6 角色 × 20 用例会同时覆盖"给冻结名"和"给落地名"两种上下文。

## 6. 变量口径权威表（**以模板代码为准**，platform-engineer 的 app.py 已按此实现）

| 用途 | 模板实际使用 | 不要写成 |
| --- | --- | --- |
| 导航 | `nav` = `[{key,label,href}]`，已按权限过滤 | `nav_items` / `{url,active}` |
| 当前用户 | 侧边栏 `current_user`（`real_name`/`role_names`）；身份卡 `identity`（`real_name`/`role_names`/`data_scope_text`） | camelCase：`displayName`/`roleNames`/`dataScope` |
| 工作台状态卡 | `status_counts` = `[{status,text,count,class}]` | `status_cards` = `[{value,label,count}]` |
| 列表筛选页签 | 后端给 `status_counts`（工单/投诉/访客）或 `status_options[{value,text}]`（访客/账单）；当前筛选值 `status_filter` | `status_tabs` / `source_tabs` |
| 审计来源筛选 | `source_filter`（当前值） | `source_tabs` |
| 分页 | `page` / `pages` / `total`（houses、orders、persons、audit 都用 `pages`） | `total_pages` |
| 工单动作 | `actions` = **list[dict]**：`{name,label,style,need_note,target}` | 字符串列表 |
| 派单下拉 | `staff`（`display_name` / `name` / `open_orders`） | `repairers` |
| 报修表单选项 | `category_options` / `urgency_options` / `error`（单个字符串） | `form.categories` / `errors` 字典 |
| 评价入口 | 由 `actions` 里的 `rate` 决定 | `can_rate` |
| 工单时间线 | `logs`（`action_text`/`from_status_text`/`to_status_text`/`operator_name`） | — |
| 时间格式化 | `cn_time` 是 **filter**（`x` 后接 `|cn_time`，空值给 `—`） | global 函数 |
| 文案函数 | `can/status_text/relation_text/house_status_text/urgency_text` 是 **global** | — |

> 这份表就是"下一个接 app.py 的人该照哪份写"的答案：**以模板为准**。文档与模板不一致时，改文档。

## 7. `base.html` 是共享热点：改动约定

`base.html` 同时承载 4 件事，被并发改坏过 3 次（AI 端点宏 2 次、`vehicles.html` 内容被覆盖 1 次）：

1. 导航渲染（`{% for item in nav %}`）+ 高亮 `active_key`
2. 模板宏 import（`csrf_field` / `status_cls`，来自 `_macros.html`）
3. AI 端点宏（`ai_page_url` 等 8 个）——**判据**：`config['AI_ENDPOINTS']`（app.py 写入的已注册端点名单）为主、`'ai' in request.blueprints` 为备，两者任一成立就走蓝图端点名；
4. `{% block head_extra %}`（`ai.html` 用来放 `<meta name="csrf-token">`）。

**约定**：改 `base.html` 前先看本文件；只改自己那一段；改完跑 `python tests/check_app_e2e.py`。

**为什么端点名不能写错**：`url_for` 到不存在的端点会在**渲染期**抛 BuildError → 整页 500。
`check_app_e2e.py` 现在会**静态扫描所有模板的 `url_for('…')`** 并与真实 `view_functions` 比对，
端点名写错会直接报「url_for 引用了不存在的端点」。
