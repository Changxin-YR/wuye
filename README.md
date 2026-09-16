# 云邻AI智脑

> **English summary** — A Flask + SQLAlchemy + MySQL property-management platform covering
> housing, residents, leases, work orders, complaints, visitors, vehicles/parking, equipment
> inspection and billing. Every write goes through one service layer (`services.py` /
> `services_ops.py`) that centralises transactions, optimistic locking, idempotency,
> post-write read-back and auditing. Authorisation is decided at runtime by a single
> `RBAC + row-level DataScope` policy object (`permissions.Policy`), shared by the web pages
> and by the AI assistant tools. The platform embeds **DeepSeek Harness (dsh)** as a
> controlled AI assistant that reaches business logic only through **MCP tools**, so it can
> never see or touch data outside the logged-in user's permissions. Risky operations pass
> through a dynamic capability catalogue, an R0-R3 risk classification, short-lived tokens,
> human confirmation, post-execution read-back and audit logging. Server-rendered Jinja2
> front end with no JS build step; 26 tables, 6 RBAC roles, 35 permissions, 73 MCP tools.

**关键词（GitHub topics）**：`flask` `sqlalchemy` `mysql` `property-management` `rbac`
`row-level-security` `audit-log` `llm-agent` `mcp` `deepseek-harness` `function-calling`
`human-in-the-loop`

Flask + SQLAlchemy + MySQL 的物业业务平台：房屋 / 人员 / 租赁 / 报修工单 / 投诉 / 访客 /
车辆车位 / 设备巡检 / 收费，全部写操作走同一个服务层 `services.py` + `services_ops.py`
（事务 / 乐观锁 / 幂等 / 回读 / 审计），权限由 `RBAC + DataScope` 实时决定；平台内嵌入
**DeepSeek Harness（dsh）** 作为受控 AI 助手，它通过 **MCP 工具**在**当前登录用户的
权限范围内**完成查询与增删改查，高风险操作走「动态能力目录 + R0–R3 风险分级 + 短期授权 +
人工确认 + 执行后回读 + 审计」。

## 1. 这个项目想证明什么

| # | 能力 | 怎么看 |
| --- | --- | --- |
| 1 | Flask + Jinja2 + SQLAlchemy + MySQL，10 个业务域 | 页面走一遍：房屋 / 人员关系 / 租赁 / 工单 / 投诉 / 访客 / 车辆车位 / 设备巡检 / 收费 / 操作记录 |
| 2 | 社区—楼栋—单元—房屋 规范化建模 | 房屋页三级联动，全站显示房屋全称 |
| 3 | 统一服务层（`services.py` / `services_ops.py`） | 工单全流程 + 建账单 + 收款 + 冲销，走同一套写命令函数 |
| 4 | 事务 / 乐观锁 / 版本校验 / 幂等 / 回滚 | `tests/test_service_guards.py`；现场重复提交（同一幂等键只执行一次） |
| 5 | RBAC + DataScope，页面与 AI 同一套校验 | 同一句话问 6 个角色，答案与可见范围各不同 |
| 6 | AI 自然语言查询与办理 | 对话：「我报修的漏水到哪一步了」「1 栋 101 厨房漏水，帮我报修」 |
| 7 | 动态能力目录 | 无权限的工具**不出现在模型工具列表**；硬调仍被服务端拒绝 |
| 8 | R0–R3 风险分级 + 人工确认 | 删除房屋 / 作废账单 → 确认卡片，点击后才执行 |
| 9 | 短期授权 | 令牌绑定 `user + auth_version`，默认 8 小时过期，改角色立即失效 |
| 10 | 操作审计 + 结果回读 | 审计页区分「页面操作 / AI 助手」，写操作回读目标行再核对 |

## 2. 技术栈

- **后端**：Python 3.12+（本项目在 3.14 上开发验证，CI 在 3.12 上跑）、Flask 3、SQLAlchemy 2.0（ORM + 乐观锁 `version_id_col`）、PyMySQL / MySQL 8+（测试用 SQLite）
- **前端**：Jinja2 服务端渲染 + 原生 CSS/JS（无前端框架 / 无构建步骤）
- **鉴权**：`werkzeug.security` 口令哈希 + Flask session（`user_id` + `auth_version`）+ CSRF 隐藏域
- **智能体**：DeepSeek Harness（dsh）Python SDK，MCP stdio 工具服务，SSE 流式回传
- **测试**：`unittest`（`python -m unittest discover -s tests -q`），SQLite 临时库，不依赖 MySQL

## 3. 目录结构

wuye/
├── app.py                       # Flask 装配 + 页面路由（薄视图层：收参数 → services/queries → flash → render）
├── config.py  db.py             # 配置 / 引擎、请求级会话与事务边界
├── models.py                    # 26 张表的数据模型（枚举中文 / 状态机）
├── permissions.py               # Policy：RBAC + 行级 DataScope（全系统唯一授权来源）
├── risk.py                      # R0–R3 风险分级 + R2 自动白名单 + AUTO/CONFIRM 决策
├── services.py                  # 写命令：空间 / 人员 / 租赁 / 工单 / 收费
├── services_ops.py              # 写命令：投诉 / 访客 / 车辆车位 / 设备巡检
├── queries.py  queries_ops.py   # 只读查询（数据范围下推到 SQL）
├── leases_routes.py  ops_routes.py  # 租赁与运营模块的页面路由（注册进 app）
├── audit.py                     # 审计写入（source=web|agent）
├── seed_demo.py                 # 幂等演示数据（seed / check / reset）
├── serve.py  wsgi.py            # 生产入口（Waitress + Secure Cookie 校验）
├── agent/                       # 智能体集成（dsh 原封不动，只做配置与桥接）
│   ├── tools.py                 # 能力目录：73 个 MCP 工具（权限 + 目标函数 + 参数）
│   ├── mcp_server.py            # MCP stdio 服务端（按权限动态注册工具）
│   ├── runtime.py               # 每用户一个 dsh 运行时（--patch 注入 + 空闲回收）
│   ├── bridge.py  actions.py    # SSE 事件翻译 / AiAction 人工确认卡片
│   ├── token.py                 # 短期令牌（itsdangerous，绑定 user + auth_version）
│   ├── session_store.py         # 会话与消息持久化
│   └── config/wuye_agent.md
├── templates/  static/          # 20 个 Jinja2 模板 + 一份 CSS + 少量原生 JS（无构建步骤）
├── tests/                       # 单元测试 + 端到端验收脚本（acceptance_e2e.py）
├── scripts/                     # 启动与备份脚本（start-demo.ps1 / backup-before-reset.ps1）
├── deploy/                      # nginx 站点配置（对外路径入口）
├── .github/workflows/ci.yml     # CI：SQLite 与 MySQL 两套回归
└── docs/                        # 契约、演示脚本、开发规格
```

## 4. 快速开始

```powershell
# 1) 依赖
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2) 配置：复制模板后填 DATABASE_URL / SECRET_KEY / DEEPSEEK_API_KEY
copy .env.example .env

# 3) 建表 + 写入演示数据（幂等，可重复执行）
.venv\Scripts\python.exe seed_demo.py
.venv\Scripts\python.exe seed_demo.py check     # 自检，逐条打印 [OK]（全绿才算就绪）

# 3b) 首次迁移（或库里还是旧版本表结构时）：create_all 不会改表，必须先删旧表再重建，
#     否则会报 Unknown column 'xxx.id'。
#     ⚠️ 强制前置：reset 会 DROP 旧表，务必先备份（备份成功后才允许删）
.\scripts\backup-before-reset.ps1                                    # 一键备份（见下方说明）
.venv\Scripts\python.exe seed_demo.py reset --seed                  # 备份成功后再执行

#     备份产物：artifacts\platform\legacy_dump_before_reset.sql
#     从备份恢复（危险：会先删同名表再建）：
#     .venv\Scripts\python.exe scripts\backup_db.py --restore artifacts\platform\legacy_dump_before_reset.sql

# 4) 本地开发启动（自动关闭 Secure Cookie，并在终端打印访问地址）
.venv\Scripts\python.exe app.py                 # http://127.0.0.1:5000/
```

生产 / 演示用 Waitress（保持 Secure Cookie，适合 https 隧道）：

```powershell
.venv\Scripts\python.exe serve.py               # Waitress，读 HOST/PORT
# 或一键起本地服务 + cloudflared 临时隧道
.\scripts\start-demo.ps1 -NoTunnel
.\scripts\start-demo.ps1                        # 打印公网 https 链接
```

> `python app.py` 是本地 http 开发入口，会显式把 `SESSION_COOKIE_SECURE` 置为 `False`；
> `serve.py` 走生产配置（`.env` 里 `APP_ENV=production` 时保持 Secure Cookie）。
> 走公网隧道时请使用 https 链接；本地 http 直连拿不到会话 Cookie。

### 备份 / 重置（演示库）

```powershell
.\scripts\backup-before-reset.ps1      # 备份到 artifacts\platform\legacy_dump_before_reset.sql
.venv\Scripts\python.exe seed_demo.py reset --seed
```

脚本会优先用 `mysqldump`；**但演示库账号 `property_user` 没有 `RELOAD` 权限**，
`mysqldump` 的快照需要 `FLUSH TABLES`，因此在本机会自动降级为 `scripts\backup_db.py`
（纯 `SELECT` 导出，输出同样可回放的 SQL，支持 `--restore`）。两者产物等价，脚本会打印实际用了哪条路径。

环境变量（`.env`，已被 `.gitignore` 排除）：

| 变量 | 说明 |
| --- | --- |
| `SECRET_KEY` | Flask 会话密钥，生产必须改 |
| `DATABASE_URL` | MySQL 连接串；缺省回退 SQLite `instances/wuye.db`（便于本地与测试） |
| `APP_ENV` | `development` / `production`（生产用 `python -m wsgi` / `serve.py`） |
| `HOST` / `PORT` | 监听地址与端口，默认 `127.0.0.1:5000` |
| `COOKIE_SECURE` | 是否要求 Secure Cookie，默认跟随 `APP_ENV`；纯 http 本地必须为 `0` |
| `UPLOAD_FOLDER` / `MAX_CONTENT_MB` / `PAGE_SIZE` | 上传目录、单请求体积上限、分页条数 |
| `AI_PROVIDER` | `bailian` 或 `deepseek`（OpenAI 兼容通道） |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | AI 助手模型通道 |
| `BAILIAN_API_KEY` / `BAILIAN_BASE_URL` / `BAILIAN_MODEL` | 可选备用通道（OpenAI 兼容端点） |
| `AGENT_R2_AUTO_COMMANDS` | 收紧 R2 自动执行白名单（只能减少，不能放宽 R3） |

> AI 助手是可选依赖：`pip install -r requirements.txt` 只装 Web 主站依赖；
> 未安装 `deepseek-harness` / `mcp` / `PyYAML` 时主站照常运行，`/ai` 会返回可读提示
> （见 `app.py` 的 `load_agent_settings` / `register_agent_routes`）。

## 5. 演示账号与数据范围

统一口令：`Demo-only-292!`（演示库专用口令，切勿用于真实环境）

| 账号 | 姓名 | 角色 | 数据范围 |
| --- | --- | --- | --- |
| `admin` | 系统管理员 | 系统管理员 | 全部数据 |
| `manager01` | 王经理 | 物业经理 | 本小区（云邻花园） |
| `service01` | 陈客服 | 客服 | 本小区（云邻花园） |
| `engineer01` | 黄磊 | 工程维修 | 只看到被派给自己的工单 |
| `finance01` | 周会计 | 财务 | 本小区（云邻花园）的账单与收款 |
| `owner01` | 张伟 | 业主 | 只看到本人与本人房屋相关数据 |

`seed_demo.py` 实际建 **8 个账号**：除上表 6 个角色外，还有同角色的维修师傅
`engineer02`（陈志强）与 `engineer03`（吴海涛），用于演示「多候选人消歧 / 派单范围」。

> 数据范围是**行级**的：`community` 范围绑定到「云邻花园」这一个小区，跨小区（云邻花园二期）
> 的数据对该角色不可见，这是演示 RBAC + DataScope 的关键一幕。
> 共 5 种范围：`all` / `community` / `building` / `assigned` / `self`。

演示数据（`seed_demo.py`，重复执行不会翻倍；条数取自 `seed_demo.py` 的 `*_SPECS` 清单）：

- 小区 2 个：**云邻花园**（1 栋、2 栋）+ **云邻花园二期**（3 栋，用于演示跨小区隔离）
- 房屋 **18** 套（云邻花园 16 套 + 二期 2 套；单元 / 房号 / 面积 / 空置-自住-出租状态）
- 人员档案 **11** 人（含两个「李娜」同名消歧）、有效房屋关系 **10** 条（含 3 条租赁关系）
- 工单 **9** 张，**覆盖全部 6 个状态**：待派单 / 已派单 / 维修中 / 待验收 / 已关闭 / 已取消
- 投诉 **4** 条（待处理 / 处理中 / 已结案 / 已取消）、访客 **3** 条（待进 / 已进 / 已离）
- 车辆 **3** 辆 + 车位 **3** 个（含已占用与空闲）、设备 **3** 台 + 巡检 **3** 条（待巡检 / 已完成）
- 账单 **4** 张（待缴 / 部分缴 / 已缴 / 已作废）+ 收款 **2** 笔（含部分收款）
- 每个工单带完整流转日志与审计留痕（≥15 条流转日志、≥9 条审计）；另有 1 条含 4 条消息的 AI 演示会话

`seed_demo.py check` 会逐条打印 `[OK]`，包含上面的模块条数与四种数据范围的隔离断言
（管理员看到全部工单；经理看不到二期；业主只看本人的；维修师傅只看派给自己的）。
自检全绿说明演示数据完整。
自检全绿说明演示数据完整。

其他维护命令：

```powershell
.venv\Scripts\python.exe seed_demo.py reset            # 删表重建（旧表结构不兼容时用）
.venv\Scripts\python.exe seed_demo.py reset --seed     # 重建后立即写入演示数据
```

## 6. 演示闭环（约 5 分钟）

1. **报修**：用 `owner01` 登录 → 工单页「+ 报修」→ 选房屋、填联系人、描述漏水 → 提交，拿到工单号。
2. **派单**：`manager01` 打开该工单 → 派给「黄磊」（下拉是按权限过滤的员工列表）。
3. **维修**：`engineer01` 接单 → 登记进展 → 报完工（状态 2 维修中 → 3 待验收）。
4. **验收**：`manager01` 验收通过 → 状态 4 已关闭；`owner01` 提交 1–5 星评价。
5. **审计**：`admin` 打开「操作记录」，看到上述每一步的页面操作留痕。
6. **AI 助手**：同一句话分别用 `admin` 与 `owner01` 问「帮我查一下我报修的工单」，可见范围不同；
   再让助手办理「1 栋 101 厨房漏水，帮我报修」，工具调用过程实时显示在对话里。
7. **高风险确认**：让助手删除房屋 / 作废账单，返回**确认卡片**（预览 + 过期时间），点击后才真正执行。

## 7. 智能体是怎么接进来的

```text
浏览器 /ai
  → POST /ai/chat（SSE）
  → agent/bridge.py → DeepSeekHarness（Python SDK；每个登录用户一个运行时，懒启动 + 空闲回收）
       profile sdk + --patch：换系统提示词 + 挂 MCP stdio 工具服务 + 关掉无关工具
  → agent/mcp_server.py（stdio，环境变量里带短期令牌）
       令牌 → 登录用户 → Policy → 能力目录过滤 → risk.py 决策
          ├ R0/R1    → 直接调 services.py / services_ops.py 的写命令
          ├ R2 白名单 → 直接调，其余建 AiAction（待人工确认）
          └ R3       → 一律建 AiAction（待人工确认）
  → SSE 事件：status / tool / delta / title / action / done / error
```

关键设计：

- **短期授权**：`itsdangerous` 令牌绑定 `user_id + auth_version`，默认 8 小时过期；登出、改角色、
  改数据范围都会让 `auth_version` 变化，令牌与缓存运行时同时失效。
- **动态能力目录**：MCP 握手时按当前用户权限过滤 `tools/list`，没权限的能力不进入模型上下文；
  即使模型硬调，服务端 `Policy.require` 仍会二次拒绝。
- **人工确认**：`AiAction` 保存服务端 payload、`payload_hash`、预览、风险级别与过期时间；
  浏览器只提交 `action_id`，确认时重新校验权限、数据范围、目标版本与业务状态。
- **执行后回读**：写命令返回 `version` 与回读结果，智能体只有拿到 `ok=true` 才可以说「已完成」。

dsh 本体不做任何修改，全部定制通过我们自己的配置覆盖文件完成。

## 8. 测试

```powershell
# 单元测试（SQLite 临时库，不连 MySQL）
.venv\Scripts\python.exe -m unittest discover -s tests -q

# 演示数据自检（逐条打印 [OK]，失败返回非 0）
.venv\Scripts\python.exe seed_demo.py check

# 端到端验收：真实 app + 真实模板 + 真实数据，跑完整个演示动线
.venv\Scripts\python.exe tests\acceptance_e2e.py
```

> 口径：**所有验收都必须在 MySQL 上跑一遍**（`.env` 里的 `property_workorder`），
> SQLite 全绿只作为快速回归。MySQL 模式：
> ```powershell
> $env:WUYE_ACCEPT_DB='mysql'; .venv\Scripts\python.exe tests\acceptance_e2e.py
> ```

`tests/acceptance_e2e.py` 覆盖：页面渲染（多角色 × 全页面，并校验页面里出现**真实数据文本**，
空渲染会被判失败）、登录 / 登出 / CSRF、工单闭环真实 POST、`/ai/chat` 的 SSE 帧、
未登录拦截与 404；结果写入 `artifacts/platform/acceptance_report.json`。

测试清单（`docs/REWRITE_CONTRACT.md` 第 8 节）：权限矩阵与数据范围、服务层状态机、
幂等 / 乐观锁 / 回滚、R0–R3 风险分级、路由与导航可见性、MCP 工具清单一致性、SSE 事件协议。
`tests/` 下的 10 个 `unittest` 用例全部离线运行（SQLite 临时库），不消耗模型配额。

## 9. 文档

| 文件 | 内容 |
| --- | --- |
| `docs/REWRITE_CONTRACT.md` | 唯一权威契约：数据模型、权限矩阵、服务层签名、页面路由、智能体集成、测试口径 |
| `docs/TEMPLATE_CONTRACT.md` | 模板变量契约（每页读哪些变量、表单字段、查询参数） |
| `docs/agent/DSH_INTEGRATION_SPEC.md` | dsh 配置覆盖与 MCP 接入的实测规格 |
| `docs/AGENT_ARCHITECTURE.md`、`docs/DEMO_SCRIPT.md` | 智能体分层架构说明 / 演示动线逐条脚本 |
