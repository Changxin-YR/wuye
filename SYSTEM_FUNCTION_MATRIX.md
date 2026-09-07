# 美家物业 V2 — 全量功能检查矩阵

> 结论基于本次源码逐层检查（页面 → 路由/API → Service → Policy/DataScope → ORM/数据库）及仓库现有测试契约。当前沙箱缺少 Flask/Werkzeug/PyMySQL/Waitress 且无法联网安装，因此无法在本沙箱重新启动整站/MySQL；本次能独立执行的纯安全与百炼客户端测试均通过。完整运行回归请在依赖齐全环境执行 `python -m unittest discover -s tests -v`。

| 模块 | 主要能力 | 权限/数据范围 | 业务服务/持久化 | 测试契约 | 检查结论 |
|---|---|---|---|---|---|
| 登录与会话 | 登录、注册、登出、会话失效、密码修改、锁定 | 服务端 Session + auth_version | User | CSRF、外部 next、防暴力、改密踢旧会话 | PASS（源码） |
| 物业基础 | 小区、楼栋、单元、房屋、产权、归档 | property.read/write + Community/Building scope | PropertyService | 唯一房号、范围越权、历史兼容 | PASS（源码） |
| 人员档案 | 人员新增/编辑/归档、联系方式 | person.read/write + scope | PropertyService | 楼栋范围、同名处理 | PASS（源码） |
| 房屋关系 | 业主/家庭成员/联系人、关系终止 | relation.write/end + scope | PropertyService | 多产权人、有效关系、越权 | PASS（源码） |
| 租赁 | 入住、共同入住、退租、历史 | lease.write + scope | PropertyService | 入住/退租/有效关系 | PASS（源码） |
| 工单 | 创建、派单、改派、接单、进度、完工、返修、关闭、取消、评价 | order.* + owner/repairer/DataScope | PropertyService | 完整状态机、并发版本、失败回滚 | PASS（源码） |
| 投诉 | 登记、分配、处理、回访结案 | complaint.* + scope | PropertyService | CRUD/流程/统计 | PASS（源码） |
| 公告 | 发布、修改、撤下、范围读取 | notice.read/write + scope | Service + Policy | 读写、空值、旧页回归 | PASS（源码） |
| 访客 | 登记、进入、离开、取消 | visitor.* + scope | PropertyService | 生命周期 | PASS（源码） |
| 车辆 | 登记、编辑、归档 | vehicle.* + scope | PropertyService | 车辆/人员/房屋关系 | PASS（源码） |
| 车位 | 车位档案、分配、释放 | parking.* + scope | PropertyService | 唯一占用、释放 | PASS（源码） |
| 设备 | 设备档案、状态、归档 | device.* + scope | PropertyService | 设备/巡检联动 | PASS（源码） |
| 巡检 | 分配任务、完成、故障转报修 | inspection.* + assignee scope | PropertyService | 任务与故障场景 | PASS（源码） |
| 收费项目 | 固定/面积计费 | billing.read/manage + scope | PropertyService | 金额精度 | PASS（源码） |
| 账单 | 单户、批量、作废、部分支付 | billing.* + scope | PropertyService | 批量原子性、跨楼栋隔离 | PASS（源码） |
| 收款与冲销 | 已核验现金/银行流水、测试通道、冲销 | billing.collect/reverse + scope | PropertyService + PaymentAdapter | 精度、生产禁 mock、冲销 | PASS（源码）；不包含第三方真实扣款 |
| RBAC | 内置角色、自定义角色、角色分配 | rbac.manage/define | UserRole/RolePermission | 角色变更、会话失效 | PASS（源码） |
| DataScope | 小区、楼栋、assigned、self、all | Policy.condition/require_scope | UserScope | 楼栋隔离、住户本人、账单隔离 | PASS（源码） |
| 通知 | 工单/业务通知、已读 | 用户本人 | Notification | 归属校验 | PASS（源码） |
| 附件 | 工单图片、头像、类型/大小/归属 | can_access_order / Policy super | 私有上传目录 | 非图片拒绝、私有访问、重编码 | PASS（源码）；修复旧 `role==0` 头像越权判断 |
| 审计 | 手工/Agent 成功失败、before/after、trace | audit.read | AuditLog | 失败写审计、Agent 来源 | PASS（源码） |
| Agent 查询 | 房屋、人员、工单、欠费、投诉统计、whoami | 实时 Permission + DataScope | business_queries | 查询越权/歧义 | PASS（源码） |
| Agent 写操作 | 52 个领域命令中按当前权限动态暴露 | RBAC + DataScope + Risk | AgentToolGateway → PropertyService | 提案、执行、回读、幂等、回滚 | PASS（本轮强化） |
| Agent 高风险确认 | R2 策略确认、R3 强制确认 | 当前用户 + auth_version + 参数哈希 + 版本重检 | AiAction | 过期、异账号、目标变化 | PASS（本轮强化） |
| AI 上游 | 百炼默认、Dify 兼容 | 最小披露上下文、短期委托 | BailianClient/DifyClient | HTTP/SSE/tool callback | PASS（客户端）；真实云推理需 API Key |
| 数据库初始化/迁移 | schema、种子、升级、环境隔离 | 管理命令 | SQLAlchemy/MySQL | DDL/迁移测试存在 | PASS（源码）；真实 MySQL 需目标环境复验 |

## 关键边界

1. Agent 不直接执行 SQL，不接受模型传入的 `user_id/role` 作为身份。
2. 人工页面和 Agent 的领域写操作共用 `PropertyService`；旧兼容页面仍由 `BusinessService` 适配并受服务端权限约束。
3. 当前财务功能是“已核验收款记账/冲销”，不是支付宝/微信等真实支付发起器；这是产品范围边界，不伪装成已接第三方支付。
4. 源码包原有真实 `.env` 和 `uploads` 属于运行配置/数据，不应进入可分享源码交付包。
