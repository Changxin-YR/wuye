# 美家物业 V2

Flask + Jinja2 + SQLAlchemy + MySQL 8.4 的物业管理平台。业务写入统一经过 `PropertyService`，页面和 Agent 共用权限、数据范围、幂等、版本和审计校验；AI 通过服务端接入阿里云百炼。

## 模块

- 物业基础：小区、楼栋、单元、房屋、产权状态。
- 人员关系：人员档案、业主/家庭成员/租户/联系人关系、租赁入住与退租历史。
- 工单闭环：报修、派单、改派、接单、进度、完工、验收、返修、取消、评价和通知。
- 运营模块：投诉、访客、车辆、车位、设备和巡检。
- 财务模块：收费项目、单户/批量账单、收款、部分支付、冲销和财务报表。
- 管理与安全：持久化 RBAC、自定义岗位、社区/楼栋数据范围、审计、CSRF、密码策略、会话失效、乐观锁和幂等。
- Agent：当前登录用户的受控数字代理；实时继承 RBAC + DataScope。R0 查询只读，R1 普通可恢复写入自动执行，R2 重要关系/状态变更按 allow-list 自动或确认，R3 删除/财务/权限等操作必须由登录用户确认；执行前后均校验并写审计。

## Windows 配置

在项目目录执行：

```powershell
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

`.env` 至少配置：

```env
SECRET_KEY=至少32位随机字符串
DATABASE_URL=mysql+pymysql://property_user:密码@127.0.0.1:3306/property_workorder?charset=utf8mb4
AI_PROVIDER=bailian
BAILIAN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
BAILIAN_API_KEY=sk-...
BAILIAN_MODEL=qwen-plus
# 可选：DeepSeek V4（仅在完成同一测试集 A/B 后切换默认值）
# AI_PROVIDER=deepseek
# DEEPSEEK_BASE_URL=https://api.deepseek.com
# DEEPSEEK_API_KEY=...
# DEEPSEEK_MODEL=deepseek-v4-pro
UPLOAD_FOLDER=uploads
HOST=127.0.0.1
PORT=5000
```

也可使用百炼 SDK 习惯的环境变量名 `DASHSCOPE_API_KEY`，二者同时存在时优先使用 `BAILIAN_API_KEY`。

密码中的 `@`、`#`、`:`、`/` 需要 URL 编码。生产环境不要把真实 `.env`、数据库备份或上传文件提交到源码包。

## 数据库

新库可由管理员执行 `schema.sql`。脚本从当前 `models.py` 生成，包含 34 张 V2 表、外键、唯一/检查约束、索引、`schema_migration` 标记，以及默认小区、内置岗位和权限初始化数据。

已有旧库先备份，再执行：

```powershell
python manage.py upgrade-db
```

迁移只增量建表/加列/补约束和索引，不覆盖原密码或业务行；旧维修账号会被标记为停用，需管理员重新核验启用。新库初始化和管理员创建：

```powershell
python manage.py init-db
python manage.py create-admin --username admin
```

## 启动与诊断

```powershell
python app.py
Invoke-RestMethod http://127.0.0.1:5000/health
Invoke-RestMethod http://127.0.0.1:5000/ready
python manage.py diagnose --ai --infer
```

`python app.py` 仅用于本地开发。生产环境使用 Waitress WSGI：

```powershell
$env:COOKIE_SECURE='1'
$env:APP_ENV='production'
python -m wsgi
# 或 .\scripts\start-waitress.ps1 -Port 5000 -Threads 8
```

生产入口会拒绝未启用 `COOKIE_SECURE=1` 的配置；反向代理应负责 HTTPS、HSTS、访问日志和进程自动重启。`/health` 只检查数据库连接，`/ready` 还会检查 schema contract drift。

`diagnose --ai --infer` 会检查数据库/schema 和所选 Provider 的模型推理链；不会输出密钥。百炼默认模型为 `qwen-plus`；DeepSeek Provider 使用 `deepseek-v4-pro`，也可通过环境变量切换为 `deepseek-v4-flash`。

Agent OpenAPI 导入文件为 `config/dify_agent_openapi.json`，服务端地址按部署环境修改；工具只允许调用当前用户授权的业务命令，不接受模型或浏览器传入的角色、用户 ID 作为身份依据。默认百炼模式的短期委托令牌不会进入模型正文；Dify 外部回调兼容模式使用约 3 分钟短期令牌且数据库只保存哈希。

`/ai/chat` 默认返回兼容 JSON；请求体增加 `stream: true` 时返回 `text/event-stream`，前端按 `delta` 增量显示，结束事件返回本地会话 ID。百炼工具调用在服务端合并分片，重复调用会被阻断。

## 页面与权限

旧入口 `/houses`、`/users`、`/notices`、`/orders`、`/audit` 仍保留，但分别经过管理员/持久化 `Policy`、业务 Service 和行级范围过滤。业主、维修人员、财务、客服等岗位只能看到自身权限和社区/楼栋范围内的数据。

Agent 风险采用 R0-R3：R0 只读；R1 普通可恢复写入；R2 重要关系/状态变更按配置决定自动或确认；R3 删除、财务、岗位/权限等高风险动作必须二次确认。页面和 Agent 都不能绕过服务端 Policy 与业务 Service 直接执行任意 ORM/SQL。

## 测试

```powershell
python -m unittest discover -s tests -v
```

当前 CI 运行 SQLite 与 MySQL 两套后端，并包含 compile、schema drift、Agent 跨请求状态/幂等和 WSGI smoke。依赖发布使用 `requirements.lock.txt`；GitHub 仓库需启用 required checks 和 PR review（可运行 `python scripts/check_branch_protection.py` 验证）。

## 目录

- `app.py`：Flask 应用、认证、兼容页面、AI 代理和安全响应头。
- `models.py`：兼容旧表与 V2 规范化模型。
- `property_service.py`：全部 V2 业务命令、校验、幂等和审计。
- `business.py`：旧页面/旧 Agent 表单适配层，仅调用领域 Service。
- `management.py` / `management_ui.py`：管理模块列表、详情、操作表单和报表。
- `permissions.py`：RBAC、社区/楼栋范围和住户有效关系过滤。
- `agent_security.py`：R0-R3、授权指纹、敏感信息最小披露与 Provider 输出脱敏。
- `agent_tools.py` / `business_queries.py`：Agent Capability Gateway、执行/确认/回读验证与只读查询。
- `database.py` / `manage.py`：初始化、增量迁移和诊断。
- `schema.sql`：从当前模型生成的 MySQL 初始化脚本。

## 本次安全设计文档

- `docs/SYSTEM_FUNCTION_MATRIX.md`：全量功能检查矩阵。
- `docs/AGENT_SECURITY_ARCHITECTURE.md`：Agent 权限继承与安全执行架构。
- `docs/AGENT_SECURITY_TEST_MATRIX.md`：Agent 安全测试矩阵。
- `DELIVERY_REPORT.md`：本次交付结论与环境阻塞说明。
