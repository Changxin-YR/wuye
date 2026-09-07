# 美家物业 V2 — 本次源码全量检查与 Agent 安全改造交付报告

## 结论

当前源码已经具备完整物业管理主干：物业、人员关系、租赁、工单、投诉、访客、车辆车位、设备巡检、收费账单、收款冲销、RBAC、DataScope、通知、附件、审计和 AI Agent。本次没有推翻原架构，而是在现有 `Policy + PropertyService` 基础上把 Agent 改造成“当前登录用户的受控数字代理”。

## 本轮关键修改

1. 新增 `agent_security.py`：R0-R3 风险模型、R2 自动策略、实时授权指纹、最小披露、Provider 输出脱敏、稳定错误码。
2. 重构 `agent_tools.py`：动态能力目录、Permission 映射、DataScope 复核、风险决策、dry-run、浏览器确认、幂等、执行后回读验证、Agent 审计。
3. 改造 `app.py`：百炼 Tool 由服务器绑定当前用户，令牌不进入默认模型正文；Dify 兼容令牌统一脱敏；AI 上下文最小化；API 错误码统一。
4. 改造 `business_queries.py`：只读查询统一最小披露。
5. 改造 `dify_client.py`：百炼支持独立 system prompt，本地 Tool 不再要求模型提供 request_token。
6. 改造 `permissions.py`：DataScope 身份输出固定排序，保证授权指纹和上下文 hash 稳定。
7. 修复附件授权细节：管理员查看他人头像改为 `Policy.super`，不再仅依赖旧 `role==0` 数字字段。
8. 更新 AI 页面：展示 R0/R1/R2/R3 和执行策略，R3 明确二次确认。
9. 更新 Agent System Prompt / Dify OpenAPI / `.env.example`。
10. 新增 Agent 安全原语测试与完整安全矩阵文档。

## 当前验证

- Python 全项目编译：通过。
- Agent 安全原语：6/6 通过。
- 百炼客户端与 SSE/工具分片：7/7 通过。
- 全量测试命令：`Ran 131 tests ... OK`。
- 真实部署诊断：`manage.py diagnose --ai --infer` 返回数据库、schema、百炼模型推理均正常；云端 `property.service` 与 Nginx 均为 active。

## 外部条件

要真实启动仍需：

- Python 安装 `requirements.txt`；
- MySQL 可连接数据库；
- 正确 `.env`；
- 使用 AI 时填写 `BAILIAN_API_KEY`；未设置时使用 `DASHSCOPE_API_KEY`（二者均不写入源码包）。

财务当前是“已核验现金/银行收款记账 + 冲销”，不发起真实支付宝/微信扣款；生产 `mock` 支付会被后端禁止。

## 安全验收基线

- Agent 最大业务权限不超过当前登录用户。
- 功能权限由 RBAC 决定，数据对象范围由 DataScope 决定，是否自动执行由 Risk Policy 决定。
- R3 不允许模型直接确认。
- Agent 不接收 SQL，不直接改数据库。
- Tool 每次重新读取后端 IAM，不信任 Prompt/前端角色字段。
- 高风险确认参数固定在服务端，并在确认时重检权限、范围、版本和状态。
- 写操作有幂等、防重、事务和执行后回读验证。
- 模型上下文与 Tool 回包采用最小披露。
