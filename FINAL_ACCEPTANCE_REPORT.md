# 美家物业 V2 + AI Agent 第三轮最终验收报告

## 项目概况

- 项目：美家物业 V2（Python / Flask / Jinja2 / SQLAlchemy / MySQL / 百炼）
- 分支：`main`
- `baseline_commit`：`8ea12a999785dca2288c742891040475a3373938`
- `first_round_commit`：`457002dbf31712a8021cedc3c828f95226b706dd`
- `second_round_commit`：`6355a9e10bcb43c54f4217955249726719d02207`
- `final_commit`：`82d5bbe`（第三轮代码、测试和验收证据最终提交）；报告文档提交另行列出。

第三轮只修复 Agent 自然语言可靠性缺口，保留既有 `PropertyService`、Policy、RBAC、DataScope、MySQL schema、AiGrant/AiAction、事务、幂等和高风险确认机制。

## 环境与配置

- 独立 Python 环境按 `requirements.txt` 安装。
- `python -m compileall -q .`：PASS。
- `.env` 已检查 `SECRET_KEY`、`DATABASE_URL`、`AI_PROVIDER`、`BAILIAN_BASE_URL`、`BAILIAN_API_KEY`、`BAILIAN_MODEL`、`UPLOAD_FOLDER`、`HOST`、`PORT`；证据中不输出密钥和数据库密码。
- `python manage.py diagnose`：PASS，数据库连接、schema 和必需表完整。
- `python manage.py diagnose --ai --infer`：PASS，真实百炼推理可用。

## 功能统计

- 管理页面：22
- 角色：11
- SQLAlchemy 表：34
- Agent/PropertyService 命令：52
- 11 角色权限矩阵行：242
- 固定 Agent 自然语言集：50
- Holdout 集：30
- 人工-Agent 等价业务：14
- Prompt/Tool Injection 红队：7
- 功能地图：[artifacts/FUNCTION_MATRIX.md](artifacts/FUNCTION_MATRIX.md)

## 自动测试统计

### SQLite

`python -m unittest discover -s tests -v`：

- Total：159
- PASS：159
- FAIL：0
- ERROR：0
- SKIP：0
- 日志：[artifacts/test-sqlite-final.log](artifacts/test-sqlite-final.log)

### 独立 MySQL

使用隔离 Docker MySQL 8.4（`127.0.0.1:33306`），fixture 实际创建并删除 `property_test_<uuid>` 临时数据库；测试结束后无残留。

- Total：159
- PASS：159
- FAIL：0
- ERROR：0
- SKIP：0
- 临时数据库创建：true
- 临时数据库删除：true
- 汇总：[artifacts/mysql-test-summary.json](artifacts/mysql-test-summary.json)
- 日志：[artifacts/test-mysql-final.log](artifacts/test-mysql-final.log)

## 管理系统人工/E2E

真实浏览器完成 22 个管理模块访问、加载、主要写入、表单 required 校验和数据库/详情回读；结果：[artifacts/e2e_acceptance_result.json](artifacts/e2e_acceptance_result.json)，截图：[artifacts/e2e-work-orders.png](artifacts/e2e-work-orders.png)。

## 11 角色权限矩阵

逐角色真实登录并验证菜单、按钮、直接 URL、读 API、写 API、跨社区/楼栋 DataScope：

- 角色：11
- 页面/操作矩阵：242/242 PASS
- `failed`：0
- `unauthorized_success`：0
- 证据表：[artifacts/ROLE_BROWSER_PERMISSION_MATRIX.md](artifacts/ROLE_BROWSER_PERMISSION_MATRIX.md)
- 结构化日志：[artifacts/role_browser_permission_final.log](artifacts/role_browser_permission_final.log)

## Agent 自然语言可靠性

固定集字段包含 `expected_action`（`TOOL`、`CLARIFY`、`DISAMBIGUATE`、`CONFIRM`、`DENY`、`ANSWER`），不把安全澄清或拒绝误算为 Tool miss。数据集：[tests/fixtures/agent_intent_cases.json](tests/fixtures/agent_intent_cases.json)。

### 原始 50 条

使用真实 `/ai/chat` 连续运行三轮，证据：[artifacts/agent_intent_acceptance_round-final-a.json](artifacts/agent_intent_acceptance_round-final-a.json)、[artifacts/agent_intent_acceptance_round-final-b.json](artifacts/agent_intent_acceptance_round-final-b.json)、[artifacts/agent_intent_acceptance_round-final-c.json](artifacts/agent_intent_acceptance_round-final-c.json)，canonical 结果：[artifacts/agent_intent_acceptance_result.json](artifacts/agent_intent_acceptance_result.json)。

- Decision Accuracy：50/50（三轮均一致）
- Tool-required cases：24
- Correct Tool：24/24，100%
- Clarification/Disambiguation/Confirmation/Denied：按 Fixture 预期处理
- Task completion：50/50
- Unsafe execution：0
- Wrong object mutation：0
- Unauthorized mutation：0
- False success：0
- Unresolved Tool loop：0

### Holdout 30 条

最新代码重新运行结果：[artifacts/agent_intent_holdout_result.json](artifacts/agent_intent_holdout_result.json)。

- Decision Accuracy：30/30
- Tool-required cases：9，Correct Tool：9/9
- 安全澄清/拒绝：21/21
- Unsafe execution：0
- Wrong object mutation：0
- Unauthorized mutation：0
- False success：0
- Tool loop：0

失败根因、修复和期望修订：[artifacts/AGENT_FAILURE_ANALYSIS.md](artifacts/AGENT_FAILURE_ANALYSIS.md)。

## Agent 能力、真实百炼与业务等价

- 11/11 角色的 `available_commands` 均为真实登录权限子集，权限泄漏 0；证据：[artifacts/agent_acceptance_result.json](artifacts/agent_acceptance_result.json)。
- Tool 统一经过参数校验、实体解析、Policy/DataScope、PropertyService 和数据库回读；模型不能直接执行 SQL。
- 同一 Tool/参数成功后不会重复执行，最大 Tool round 和无进展循环均有终止状态。
- 高风险命令保留 Proposal → 真实用户确认 → 重新权限/范围/版本校验 → 执行流程。
- 14/14 人工-Agent 数据快照等价：[artifacts/AGENT_MANUAL_EQUIVALENCE_MATRIX.md](artifacts/AGENT_MANUAL_EQUIVALENCE_MATRIX.md)。
- 真实百炼 non-stream、SSE、Tool Call、Tool callback 和最终回答：PASS；[artifacts/bailian_acceptance_result.json](artifacts/bailian_acceptance_result.json)。

## 真实业务闭环

人工、全 Agent、人工+Agent 混合路径均复用同一 Service 和事务边界，覆盖住户报修、派单、接单、进度、完工、返修、验收、评价，以及房屋、人员、关系、租赁、投诉、访客、车辆/车位、设备、巡检、账单、收款和冲销；最终业务表、关联表、流水、通知、审计和版本字段通过等价矩阵比较。

## 安全与审计

SQLite/MySQL 和真实 `/ai/chat` 入口覆盖 RBAC、DataScope、IDOR、参数篡改、CSRF、Session/auth_version、SQL 注入、XSS、文件上传、并发版本、幂等、Prompt Injection、Tool Injection、高风险确认和旧授权失效。红队 7/7 PASS，未授权数据库变更 0；[artifacts/agent_injection_redteam_result.json](artifacts/agent_injection_redteam_result.json)。

Agent 审计包含操作人、角色、输入、命令、参数、风险、确认、目标对象、前后值、验证结果和 trace；未改变既有权限模型。

## Bug 列表

| ID | 严重度 | 模块 | 根因与修复 | 回归 | 状态 |
| --- | --- | --- | --- | --- | --- |
| P1-001 | P1 | Agent 写入 | 同回合渐进补参可能重复写入；增加命令签名去重并保留 AiAction 幂等。 | SQLite/MySQL 159/159；重复 Tool 回归通过。 | CLOSED |
| P1-002 | P1 | Agent 自然语言 | 通用别名抢先匹配、缺参猜测、实体不消歧、查询结果未消费和 Tool loop 导致 34/50。增加两阶段 Planner、统一实体/房号解析、结构化结果、参数前置校验、终止状态和 Loop Guard。 | 原始 50 条三轮 50/50；Holdout 30/30；危险执行和循环均 0。 | CLOSED |
| P1-003 | P1 | 只读查询 | 常用 `id`、`building_id`、`name/q`、`order_no` 参数未统一归一化。 | SQLite/MySQL 全量和 DataScope 回归通过。 | CLOSED |

## 未解决问题与外部阻塞

无阻塞验收项。未接入第三方支付宝/微信支付不属于当前现金/银行收款、冲销和 Agent 业务验收范围。

## 四道质量门禁

| 门禁 | 结果 | 证据 |
| --- | --- | --- |
| Gate 1 管理系统人工功能 | PASS | 22 模块浏览器 E2E + DB 回读 |
| Gate 2 Agent | PASS | 原始 50 三轮、Holdout 30、Tool/Service/DB/回读/确认 |
| Gate 3 权限安全 | PASS | 11 角色矩阵、DataScope、RBAC、红队、SQLite/MySQL |
| Gate 4 真实业务 | PASS | 14/14 等价、工单闭环、MySQL 核心业务回归 |

缺陷统计：P0=0，P1=0，P2=0，P3=0。

## Git 提交

第三轮按代码修复、回归测试/证据、验收文档分批提交：`269f658`（代码修复）、`82d5bbe`（测试与证据）。本报告的文档提交在该 `final_commit` 之后追加；推送后必须满足本地 `HEAD == origin/main` 且工作区干净。

## 最终结论

**A：通过，可以正式交付。**

四道质量门禁已全部通过，可以交付。
