# 美家物业 V2 当前验收报告

## 验收基线

- 验收日期：2026-09-09
- 代码基线：以最终合并提交的 exact SHA 为准（本工作树当前基线 `8728a26`，合并后需重新生成）
- 测试命令：`python -m unittest discover -s tests -v`
- 当前本地结果：`Ran 185 tests ... OK`

## 已修复上线阻塞项

1. Agent 会话状态和最多 24 条 Provider 消息持久化到数据库，按 `user_id + auth_version + conversation_id` 绑定；状态只保留业务 selector，不缓存旧 version。
2. Agent 每次动作返回 `agent_request_id`，贯穿 Tool、`AiAction.request_key` 和 `PropertyService` 的 `BusinessRequest` 幂等回放；参数变化返回 409。
3. schema 检查比较列类型/长度/nullability、唯一约束、检查约束、索引和外键，并记录 `property_v2_001` contract revision。
4. 新增 `/ready` readiness、Waitress `wsgi.py` 入口和 Windows 启动脚本；生产 Secure Cookie 配置缺失时拒绝启动。
5. CI 工作流覆盖 compile、SQLite、MySQL、WSGI smoke；依赖提供精确锁定文件。

## 保留的安全基线

RBAC/DataScope、R0-R3 确认链、财务状态机、文件上传校验和 Provider 敏感信息最小披露均保持现有实现，并由原有测试覆盖。

## 未完成风险（P2）

MFA/step-up、IP/设备限流、备份恢复演练、上下文按需检索、AI 状态 retention、完整浏览器 E2E、live Provider 验收和 GitHub Branch Protection 需要在部署环境完成，不能由本地单元测试替代。正式发布前应在目标 MySQL、当前 AI Provider 和反向代理链路重新执行验收，并把实际 HEAD SHA 写回本文件。
