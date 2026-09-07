# Agent 安全测试矩阵

| 场景 | 预期 | 对应实现/测试 |
|---|---|---|
| 无/过期 grant | 401 | `test_missing_or_expired_grant` |
| grant 属于其他用户 | 拒绝冒充 | `test_property_v2.py` foreign grant 场景 |
| 账号停用 | 旧会话/确认失效 | `test_disabled_session_cannot_confirm` |
| auth_version 变化 | 拒绝执行 | `test_user_disable_proposal_revalidates_target_version` 等 |
| Role/Permission/DataScope 直接变化 | 授权指纹失效 | `test_runtime_grant_detects_scope_change`（本轮新增） |
| 未开放命令/SQL | 400 | `test_unknown_commands_and_bad_types_rejected` |
| 参数注入 | 400 | Tool 参数白名单 + 类型/长度校验 |
| 楼栋越权 | 不可查询/修改 | `test_building_data_scope_and_legacy_route_no_bypass` |
| 跨楼栋账单泄漏 | 不返回 | `test_bill_rows_cannot_leak_across_building_scope` |
| 住户历史失效关系 | 不授权 | `test_expired_relation_does_not_authorize_resident` |
| 重名人员 | 不猜目标 | `test_agent_direct_bind_repeat_lookup_and_ambiguity` |
| R1 自动执行 | 执行 + 回读 + 审计 | `perform` + `_verification` |
| R2 非 allow-list | pending | `execution_mode` |
| R3 | 永远 pending，浏览器确认 | `test_high_risk_agent_pending_confirm_rechecks_version` |
| 确认参数篡改 | 无参数入口，使用服务端 payload | `AiAction.payload_hash` + confirm(action_id) |
| 确认过期 | 410 | `test_expired_confirmation` |
| 目标版本变化 | 409 | `test_stale_order_proposal_rejected` |
| 重复执行 | 返回同一 AiAction | grant + payload_hash unique |
| dry-run 留脏数据 | 全部回滚 | savepoint rollback + 现有 proposal 测试 |
| DB commit 失败 | 业务和状态回滚 | `test_failed_commit_rolls_back_execution_and_status` |
| 模型上下文 PII | 掩码/删除 | `test_model_record_uses_minimum_disclosure`（本轮新增） |
| 模型回显 grant | UI 前统一清除 | `test_provider_output_scrubs_current_grant`（本轮新增） |
| 百炼 Tool grant | 不进入模型正文 | `app.py` server-bound callback |
| Dify Tool grant | 短期、哈希、实时 IAM 复核 | `/api/agent/tools` + `grant_actor` |

## 当前实际执行

- `python tests/test_agent_security_static.py -v`：**6/6 OK**。
- `python -m unittest discover -s tests -v`：**131/131 OK**（超时测试的本地 mock server 会出现预期 BrokenPipe 日志，不影响测试结果）。
- `python -m compileall -q .`：**通过**。
- 测试目录静态统计：**131 个 test_* 用例**。
- 部署环境 `python manage.py diagnose --ai --infer`：**通过**。
