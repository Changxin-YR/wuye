# Property V2 Delivery Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复上线前 P1 阻塞项并建立可验证的交付门禁。

**Architecture:** 持久化 Agent 会话状态和消息摘要，跨请求生成并传递领域幂等键；迁移以 SQLAlchemy metadata 契约校验结构；生产通过 Waitress WSGI 入口启动并在生产模式拒绝不安全 cookie 配置。

**Tech Stack:** Flask 3, SQLAlchemy 2, SQLite/MySQL, Waitress, unittest, GitHub Actions。

**Spec:** `docs/superpowers/specs/2026-09-09-property-v2-delivery-hardening-design.md`

## Global Constraints

- 保持现有 `Policy`、`PropertyService` 和 R0-R3 风险确认链不变。
- 不把旧业务对象 version 写入持久化上下文；执行前重新 Resolver/Policy/DataScope。
- 默认 ASCII 编辑；保留现有中文文档内容。
- 所有新增行为先写失败测试，再实现并运行全量测试。

---

### Task 1: 修复 CI JSON 断言

**Files:**
- Modify: `tests/test_app.py`
- Test: `tests/test_app.py`

- [ ] 将车辆和访客错误断言改为 `response.get_json()['error']`，先运行对应测试确认原断言在 JSON 转义下失败。
- [ ] 运行完整 SQLite 测试并确认无回归。

### Task 2: 持久化 Agent 会话状态

**Files:**
- Modify: `models.py`, `database.py`, `app.py`, `dify_client.py`
- Create: `agent_state.py`
- Test: `tests/test_agent_persistence.py`

- [ ] 新增跨请求状态 schema 和加载/保存 helper，写测试覆盖新 session、重启后读取、user/auth_version 绑定和仅保存 selector。
- [ ] 让 `/ai/chat` 使用 helper 替代 `app.extensions['agent_contexts']`，Provider history 从消息摘要恢复并落库。
- [ ] 为旧库 migration 增加列，并让 `missing_schema` 识别缺失。

### Task 3: Agent 持久化幂等

**Files:**
- Modify: `models.py`, `agent_tools.py`, `app.py`, `property_service.py`
- Test: `tests/test_agent_idempotency.py`

- [ ] 写失败测试：相同 `agent_request_id` 重放返回同一结果，参数变化返回冲突，跨 grant/重启仍生效。
- [ ] 新增请求键字段/表和 deterministic key helper；从 HTTP 请求贯穿到 `PropertyService.run(request_key=...)`。
- [ ] 保留 R3 confirm 的二次 Policy/version 校验。

### Task 4: Schema contract drift

**Files:**
- Modify: `database.py`, `manage.py`
- Test: `tests/test_migration.py`

- [ ] 写失败测试覆盖删除约束、索引、外键和类型不匹配。
- [ ] 使用 Inspector + metadata 规范化比较，返回稳定 drift code；升级按 revision 链记录。
- [ ] 诊断输出 drift 详情且不泄露凭据。

### Task 5: Waitress 生产入口和门禁

**Files:**
- Create: `wsgi.py`, `scripts/start-waitress.ps1`
- Modify: `app.py`, `.env.example`, `README.md`, `requirements.txt`
- Test: `tests/test_wsgi.py`

- [ ] 写失败测试验证 `APP_ENV=production` 且 `COOKIE_SECURE != 1` 时拒绝启动，Waitress callable 可导入。
- [ ] 实现 WSGI 工厂、线程/连接参数、健康与就绪响应头；保留 `app.py` 仅供本地开发。
- [ ] 更新 Windows 启动说明和 CI smoke 命令。

### Task 6: CI 与验收报告更新

**Files:**
- Create: `.github/workflows/ci.yml`, `scripts/check_branch_protection.py`
- Modify: `FINAL_ACCEPTANCE_REPORT.md`, `README.md`

- [ ] CI 包含 compile、SQLite、MySQL（可用 service 条件）、WSGI smoke、secret/dependency scan 占位门禁。
- [ ] 报告绑定实际 HEAD、实际测试计数，并明确 P2 未完成项。
- [ ] 本地运行全量验证并复核 git diff。
