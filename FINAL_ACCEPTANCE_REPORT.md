# 美家物业最终验收报告

## 项目概况

- 项目：美家物业 V2（Flask + Jinja2 + SQLAlchemy + MySQL + 百炼适配）
- 当前分支：`main`
- 当前 commit：`8ea12a999785dca2288c742891040475a3373938`
- 工作区基线：验收开始时干净；本轮仅新增 `artifacts/` 验收证据和本报告。

## 环境

- 独立 Python 环境：`.venv`
- `requirements.txt`：安装成功
- `python -m compileall -q .`：PASS
- `.env` 已存在并检查键名；输出和报告不包含密钥或数据库密码。
- 主 `DATABASE_URL`：MySQL `127.0.0.1:3306`，数据库名已掩码处理。
- `python manage.py diagnose`：`database=ok`, `schema=ok`, `missing=[]`
- 实际 MySQL：34 张表，`SELECT 1` PASS，`runtime_environment=production`
- 百炼：通过安全回退变量 `DASHSCOPE_API_KEY` 配置（不输出凭据），真实云推理可用。

## 功能统计

- 管理页面模块：22 个
- 路由/API 装饰器：34 个
- SQLAlchemy 表：34 张
- `PropertyService` 业务命令：52 个
- 内置角色：11 个
- 功能矩阵：见 [artifacts/FUNCTION_MATRIX.md](C:/Users/27363/Desktop/tangli_miao/artifacts/FUNCTION_MATRIX.md)

## 自动测试

首轮完整回归命令：`python -m unittest discover -s tests -v`

- 总测试：131
- PASS：131
- FAIL：0
- ERROR：0
- SKIP：0
- 用时：33.194s
- 日志：[artifacts/test-full-round-1.log](C:/Users/27363/Desktop/tangli_miao/artifacts/test-full-round-1.log)

测试中的 `ConnectionAbortedError/WinError 10053` 来自测试 mock HTTP 服务在客户端提前结束连接；相关用例均 PASS，未发现业务失败。

## MySQL 测试

已执行真实 MySQL fixture 回归。fixture 为每个用例创建 `property_test_<uuid>` 独立数据库并在结束后删除，但当前 `property_user@127.0.0.1` 只有 `property_workorder.*` 权限，没有 `CREATE DATABASE`，服务器返回 `1044 Access denied`，所以 28 个依赖 fixture 的用例在 setUp 阶段 ERROR。未修改 fixture、未向生产库写入测试数据。

- MySQL 回归状态：`BLOCKED_EXTERNAL`
- 日志：[artifacts/test-mysql-full-round-2.log](C:/Users/27363/Desktop/tangli_miao/artifacts/test-mysql-full-round-2.log)
- 需要：提供仅用于测试的 MySQL 账号，具备创建/删除 `property_test_*` 数据库的权限，或配置已有安全测试实例的 `MYSQL_TEST_SERVER_URL`。

## 人工页面/E2E

在独立 SQLite 临时实例 `http://127.0.0.1:5003` 使用 Playwright 浏览器真实操作：

- 登录成功，CSRF 表单存在。
- 22 个管理模块页面访问、数据加载和表格渲染均 HTTP 200。
- 通过真实表单新增楼栋、单元、房屋、人员、工单，均成功重定向到实际详情页。
- 必填字段浏览器校验存在。
- 截图：[artifacts/e2e-work-orders.png](C:/Users/27363/Desktop/tangli_miao/artifacts/e2e-work-orders.png)
- 结果：[artifacts/e2e_acceptance_result.json](C:/Users/27363/Desktop/tangli_miao/artifacts/e2e_acceptance_result.json)

## Agent 验收

隔离数据库中创建 11 个内置角色并运行真实 Agent Tool HTTP 入口：

- 11/11 角色能力差异无泄漏：每个暴露命令的 permission 都属于当前角色。
- `relation.bind_by_name` 实际执行返回 `executed`，`house_person` 关联行=1。
- Agent 审计行=5，包含执行/提案相关记录。
- 空楼栋归档返回 `pending`，数据库 `deleted` 未改变，证明高风险 Tool 不直接执行。
- `sql.execute` 等未知 Tool 返回 400。
- 注入额外 `role=superadmin` 字段返回 400。
- `auth_version` 变化后旧授权 token 返回 403。

## 权限与安全

现有 131 项回归已覆盖 RBAC、DataScope、IDOR、building/community/house/user 参数篡改、CSRF、Session/auth_version、文件上传、SQL 注入搜索、XSS 输出编码、并发版本、幂等、Prompt/Tool 注入和高风险确认。源码链路确认 Agent 不直接执行 SQL、不接受模型身份字段、不让前端决定最终权限；页面和 Agent 共用 `Policy + PropertyService`。

## Agent 等价与真实云链路

现有测试覆盖人工页面与 Agent 共用领域 Service、工单闭环、财务精度/冲销、关系/租赁/设备/巡检等持久化结果；本轮实测补充了浏览器人工写入与 Agent 数据库回读。真实百炼诊断、非流式推理、流式输出和 Tool Call 均通过：流式事件含 `done`，受控回调触发 1 次且参数仅含 `operation`。结果见 [artifacts/bailian_acceptance_result.json](C:/Users/27363/Desktop/tangli_miao/artifacts/bailian_acceptance_result.json)。

## Bug 列表

本轮没有复现产品 P0/P1/P2 Bug，因此没有业务代码修改或修复 commit。当前 MySQL 账号缺少 `CREATE DATABASE`，服务器返回 1044；需要外部测试权限，不能由应用代码安全修复。

## 外部阻塞与未解决问题

- `BLOCKED_EXTERNAL`: MySQL 测试账号不能创建/删除临时测试库。
- 本轮没有生产写操作；人工 E2E 使用隔离 SQLite，不能替代受权限隔离的 MySQL 回归。
- 未连接第三方支付宝/微信支付；当前产品范围是已核验现金/银行收款记账与冲销。

## Git 状态

验收基线时工作区干净；本轮证据文件未提交。由于没有产品代码 Bug，不创建 `fix/security/agent/db` 业务 commit，也不执行 push，避免把临时验收夹具或环境文件推入仓库。

## 四道门禁

| 门禁 | 结果 | 依据 |
| --- | --- | --- |
| Gate 1 管理系统人工功能 | PASS（隔离实例） | 22 页面 + 5 项真实表单写入 + 截图/回读 |
| Gate 2 Agent | PASS（隔离实例） | 能力、Tool、执行、DB、回读、审计、高风险待确认 |
| Gate 3 权限安全 | PASS（现有回归 + Agent 差异） | 131 项安全契约、11 角色无能力泄漏、注入/旧授权拒绝 |
| Gate 4 真实业务 | 有条件 | SQLite 人工/Agent/工单链路和百炼真实云链路通过；仅 MySQL 临时库权限外部阻塞 |

## 最终结论

**B 有条件交付**

应用代码在当前可用环境通过编译、完整自动回归、数据库诊断、隔离浏览器业务、真实百炼和 Agent/RBAC 安全验收；交付前必须补齐具备临时库权限的 MySQL 测试账号并完成真实 MySQL 回归，才能升级为 A。
