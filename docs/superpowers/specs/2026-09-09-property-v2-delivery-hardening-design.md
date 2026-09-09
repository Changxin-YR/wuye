# 美家物业 V2 交付加固设计

## 目标

把报告指出的上线阻塞项收敛到可验证的工程基线：测试恢复双绿，Agent 状态和幂等可跨进程恢复，数据库迁移能识别关键 schema drift，并提供正式 WSGI 启动与生产配置门禁。

## 架构

`AiConversation` 保存跨请求的结构化 planner 状态和消息摘要；状态只存业务选择器，不存旧版本号。Agent 每次写操作生成服务端 `agent_request_id`，传给 `PropertyService.run(request_key=...)`，由现有持久化请求表负责回放/冲突检测。Provider 的内存历史作为短期缓存，数据库消息作为真相来源。

数据库迁移增加 schema contract 检查，比较 SQLAlchemy metadata 中的列、约束、索引和外键；缺失或不匹配时返回明确 drift。生产入口使用 Waitress，并在 `APP_ENV=production` 时强制 HTTPS cookie 配置。CI 测试通过 JSON 解析断言，增加 WSGI smoke 与 drift 回归。

## 范围

- P1-06：修复两处 JSON 中文断言并保持 SQLite/MySQL 测试兼容。
- P1-01：新增 `AiConversation.state_json`、`AiConversation.messages_json`，统一读写上下文；Provider chat 从持久化消息恢复。
- P1-02：新增 `agent_request_id` 到 Agent action/request 链路，并传入领域幂等键。
- P1-03：扩展迁移契约检查，至少覆盖列类型/长度/nullability、唯一/检查约束、索引和外键。
- P1-04：新增 `wsgi.py` Waitress 入口、健康/就绪检查和生产配置 fail-closed。
- P1-05：提供 CI required-check 配置文档和可执行校验脚本；仓库权限需在 GitHub 管理侧启用。
- 更新 README、FINAL_ACCEPTANCE_REPORT，明确未覆盖的 P2 风险。

## 非目标

本阶段不实现 MFA、IP 限流、备份恢复编排、依赖锁定或完整上下文按需检索；这些保留为后续独立迭代。

## 验收

`python -m unittest discover -s tests -v`、`python -m compileall -q .`、WSGI startup smoke、schema drift regression 全部通过；报告绑定实际 commit 和未解决风险。
