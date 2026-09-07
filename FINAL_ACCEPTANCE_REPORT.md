# 美家物业 V2 + AI Agent 第二轮最终验收报告

## 项目概况

- 项目：美家物业 V2（Python / Flask / Jinja2 / SQLAlchemy / MySQL / 百炼）
- 分支：`main`
- 验收基线 commit：`8ea12a999785dca2288c742891040475a3373938`
- 第一轮最终 commit：`457002dbf31712a8021cedc3c828f95226b706dd`
- 第二轮最终代码/证据 commit：`7d84815`

## 环境与配置

- Python 依赖按 `requirements.txt` 安装；`.venv` 独立环境可用。
- `python -m compileall -q .`：PASS。
- `.env` 已检查 `SECRET_KEY`、`DATABASE_URL`、`AI_PROVIDER`、百炼地址/模型、上传目录、HOST/PORT；密钥和数据库密码未写入证据。
- 生产诊断：`python manage.py diagnose` 返回 `database=ok`、`schema=ok`、`missing=[]`。
- 百炼诊断：`python manage.py diagnose --ai --infer` 返回 `status=ok`、`inference_checked=true`、模型 `qwen-plus`。

## 功能统计

- 管理页面模块：22
- 路由/API 装饰器：34
- SQLAlchemy 表：34
- `PropertyService` 命令：52
- 内置角色：11
- 功能地图：[artifacts/FUNCTION_MATRIX.md](artifacts/FUNCTION_MATRIX.md)

## 自动测试统计

### SQLite

第二轮新鲜执行 `python -m unittest discover -s tests -v`：

- Total：133
- PASS：133
- FAIL：0
- ERROR：0
- SKIP：0
- 用时：74.248s
- 日志：[artifacts/test-full-round-2.log](artifacts/test-full-round-2.log)

### 独立 MySQL

使用独立 Docker MySQL 8.0（`127.0.0.1:23306`）和专用 `property_test_runner` 账号。fixture 实际为每个用例创建并删除 `property_test_<uuid>` 数据库；结束后查询无残留数据库。

- Total：133
- PASS：133
- FAIL：0
- ERROR：0
- SKIP：0
- 用时：241.546s
- 状态：PASS
- 汇总：[artifacts/mysql-test-summary.json](artifacts/mysql-test-summary.json)
- 日志：[artifacts/test-mysql-final.log](artifacts/test-mysql-final.log)

## 管理系统人工/E2E

在隔离 SQLite 和真实 Playwright 浏览器中完成 22 个管理模块访问、数据加载、表单校验及主要新增写入，并回读数据库/详情页；结果见 [artifacts/e2e_acceptance_result.json](artifacts/e2e_acceptance_result.json)，截图见 [artifacts/e2e-work-orders.png](artifacts/e2e-work-orders.png)。

## 11 角色权限矩阵

真实登录后逐角色检查菜单、创建按钮、直接 URL、读 API、写 API 和跨社区查询：

- 角色：11
- 页面行：242（11 × 22）
- 失败：0
- `unauthorized_success`：0
- 结果：[artifacts/ROLE_BROWSER_PERMISSION_MATRIX.md](artifacts/ROLE_BROWSER_PERMISSION_MATRIX.md)
- 结构化证据：[artifacts/role_browser_permission_matrix.json](artifacts/role_browser_permission_matrix.json)

## Agent 能力与真实执行

- 11/11 角色 capability 的命令权限均为当前持久化权限子集，泄漏命令：0；证据：[artifacts/agent_acceptance_result.json](artifacts/agent_acceptance_result.json)。
- Agent Tool 走 `Policy`、`PropertyService`、实时 DataScope 和数据库回读；关系绑定返回 `executed` 且 `house_person` 关联存在，审计行可查。
- 高风险操作返回 `pending`，浏览器确认后再次校验权限、范围、版本；旧 `auth_version` 授权返回 403。
- 14 类人工/Agent 业务结果结构化比较：14/14 PASS；证据：[artifacts/AGENT_MANUAL_EQUIVALENCE_MATRIX.md](artifacts/AGENT_MANUAL_EQUIVALENCE_MATRIX.md)。
- 真实百炼请求、SSE 流式输出和 Tool Call：PASS；证据：[artifacts/bailian_acceptance_result.json](artifacts/bailian_acceptance_result.json)。

## 50 条自然语言意图集

- 固定数据集：50 条，字段包含意图、工具、风险、执行模式、权限结果、实体解析和数据库效果：[tests/fixtures/agent_intent_cases.json](tests/fixtures/agent_intent_cases.json)。
- 已通过真实 `/ai/chat` 连续执行 50 条并记录工具调用、动作、HTTP 状态和业务表差异：[artifacts/agent_intent_acceptance_result.json](artifacts/agent_intent_acceptance_result.json)。
- 危险数据库写入：0。
- 按项目实际 `*.search`/`*.properties` 命令归一化后的工具命中：34/50（68%）。多条请求因模型重复尝试不同参数触发 `tool_loop`/`bad_response`，后端均回滚且未发生未授权业务写入。
- 因语义/工具准确率尚未达到 50/50，本项不宣称 PASS。

## Agent 注入红队

真实 `/ai/chat` 入口测试 7 条身份伪造、隐藏工具、SQL 诱导、绕过确认和跨楼栋读取指令：

- 请求：7/7
- 未授权业务表变更：0
- 状态：PASS
- 证据：[artifacts/agent_injection_redteam_result.json](artifacts/agent_injection_redteam_result.json)

## 工单与真实业务

既有回归覆盖住户报修、派单、接单、进度、完工、返修、验收和评价；人工/Agent 共用同一领域 Service。14 类等价矩阵覆盖房屋、人员、关系、租赁、工单、投诉、访客、车辆/车位、设备、巡检、账单、收款和冲销。独立 MySQL 全量回归也覆盖这些核心持久化路径。

## 安全与审计

SQLite/MySQL 回归和真实 Agent 入口覆盖 RBAC、DataScope、IDOR、building/community/house/user 参数篡改、CSRF、Session/auth_version、SQL 注入搜索、XSS 输出、文件上传、并发版本、幂等、Prompt/Tool Injection 和高风险确认。Agent 不直接执行 SQL，不信任模型身份字段，前端不能决定最终权限；业务成功均要求 Service 执行并回读数据库。

Agent 审计记录包含操作人、角色、来源、命令、目标、风险/确认状态、前后数据和 trace；本轮未新增统一 Trace 页面。

## Bug 列表

本轮复现 3 个 Agent 缺陷，其中 2 个已修复、1 个开放；另修正 3 个验收夹具问题。未复现 P0。

| ID | 严重度 | 模块 | 复现与原因 | 修复与验证 | 状态 |
| --- | --- | --- | --- | --- | --- |
| P1-001 | P1 | `/ai/chat` Agent 写入 | 同一回合对 `order.create` 渐进补参会提交多个不同 payload，原有 payload 幂等键无法阻止重复业务行和通知。 | 回合内对已成功 `execute/propose` 的命令去重；新增 `test_ai_blocks_repeated_mutation_command_in_one_turn`，SQLite/MySQL 全量通过；commit `d65fddf`。 | 已修复 |
| P1-002 | P1 | Agent 自然语言路由 | 50 条真实百炼语料中部分命令循环或未完成，归一化工具命中 34/50（68%）。 | 已加强 function schema 和只读参数归一化；再次真实运行仍为 34/50，需后续提示/语料优化。当前无修复 commit。 | 开放，阻止 A |
| P1-003 | P1 | Agent 只读查询 | 模型常用 `id`、`building_id`、`name/q`、`order_no` 参数原先被白名单拒绝，导致合法查询 400 和工具循环。 | 在 `business_queries.query` 做只读别名归一化，继续使用原有 Policy/DataScope；新增 `test_agent_query_common_aliases_are_scoped`，SQLite/MySQL 全量通过；commit `d65fddf`。 | 已修复 |

验收夹具问题：红队脚本根路径、`Person.user_id` 重复插入、外键提交顺序均已修正并重新运行。上述产品修复保持现有技术栈、权限模型和数据库约束不变。

## 未解决问题与外部阻塞

- 50 条自然语言真实模型测试的安全性通过，但工具/意图准确率为 33/50，存在模型工具循环和部分请求无法完成；这不是后端越权，但使该质量门禁不能标记为完全通过。
- P1-002（开放）：50 条自然语言真实模型测试工具命中 34/50，仍有模型工具循环/部分意图未完成；需继续优化模型提示与业务语料并回归，当前不能升级 A。
- 第一轮生产 MySQL 账号无建库权限的阻塞已通过独立 Docker 测试实例解除；未扩大生产账号权限。
- 未连接第三方支付宝/微信支付；当前验收范围为现金/银行收款记账和冲销。

缺陷分级汇总：P0=0，P1=1（开放的自然语言准确率缺口），P2=0，P3=0。

## 四道质量门禁

| 门禁 | 结果 | 依据 |
| --- | --- | --- |
| Gate 1 管理系统人工功能 | PASS | 22 模块 Playwright + 数据库回读 |
| Gate 2 Agent | 有条件 | Tool/Service/DB/回读/确认通过；50 条意图准确率未达 50/50 |
| Gate 3 权限安全 | PASS | 11 角色矩阵、DataScope、红队、133 SQLite + 133 MySQL |
| Gate 4 真实业务 | 有条件 | 14/14 等价和 MySQL 通过；自然语言完整准确率仍有缺口 |

## Git 提交

本轮新增验收脚本、固定意图集、权限矩阵、等价矩阵、红队结果和 MySQL 汇总，并修复 Agent 查询兼容与同回合重复写入。代码修复 commit：`d65fddf`；验收证据 commit：`7d84815`；本报告随后以 docs commit 固化。

## 最终结论

**B：有条件交付**

编译、SQLite/MySQL 自动回归、浏览器权限矩阵、Agent 安全红队和 14 类人工/Agent 数据等价均通过；要升级为 A，还需将固定 50 条自然语言集的真实工具/意图准确率提升到 50/50 并完成对应回归。
