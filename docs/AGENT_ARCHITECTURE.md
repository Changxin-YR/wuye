# 智能体架构：受控数字代理（DeepSeek Harness + MCP + 风险闸门）

> 本文回答三个问题：**智能体是什么、它的权限从哪来、它凭什么敢替用户改数据。**

## 1. 一句话

平台里嵌了一个**真的智能体**（不是"查询机器人"）：它自己规划、自己调工具、自己回读结果；
但它的**每一次写操作都要重新过一遍服务端权限与业务规则**，高风险操作还要**用户在浏览器上点确认**。

- **智能体运行时**：DeepSeek Harness（`dsh`）**原封不动**接入——不改它一行源码，只用它官方的
  Python SDK + 配置文件（`--patch`）和 MCP 协议把它"接"进来。
- **业务能力**：以 MCP 工具形式暴露（73 个工具，覆盖查询与增删改查）。
- **权限来源**：当前登录用户。页面能做的，智能体才能做；页面看不到的数据，智能体也拿不到。

## 2. 我们只做三件事（其余全部复用 harness）

| 我们写的 | 作用 | 对应文件 |
| --- | --- | --- |
| **配置覆盖** | 换系统提示词、挂 MCP 服务、关掉与业务无关的编码类工具 | `agent/runtime.py`、`agent/config/wuye_agent.md` |
| **MCP 工具服务** | 把业务能力暴露成工具，并在服务端做权限/风险判定 | `agent/mcp_server.py`、`agent/tools.py`、`risk.py` |
| **SSE 桥** | 把 dsh 的会话事件翻译成浏览器能渲染的进度与回答 | `agent/bridge.py`、`agent/routes.py` |

> 历史版本是自己写"规划器 + 工具网关"；本轮改成**真正的 agent runtime**：模型自主决定
> 调哪些工具、调几次、失败怎么重试，我们只负责**授权、风险与呈现**。

## 3. 调用链

```text
浏览器 /ai
  │  POST /ai/chat（SSE，带 CSRF）
  ▼
Flask 蓝图 agent/routes.py
  │  1) 落库用户消息   2) 取该用户的运行时（懒启动 + 复用 + 空闲回收）
  ▼
DeepSeek Harness（Python SDK，每登录用户一个进程）
  │  profile: sdk  +  --patch（每个用户单独生成）
  ├── 系统提示词 = agent/config/wuye_agent.md（物业助手身份，非编码助手）
  ├── MCP stdio 行：python -m agent.mcp_server（env 带短期令牌）
  └── 其余工具行全部 disabled（bash/文件/网络/子智能体/计划…）
  ▼
agent/mcp_server.py（stdio；一个用户一个进程）
  │  令牌 → 登录用户 → Policy(RBAC + DataScope) → 能力目录过滤
  │  → risk.decision(tool)
  │        ├─ R0/R1 → 直接执行
  │        ├─ R2 白名单内 → 直接执行；其余 → 建 AiAction（待确认）
  │        └─ R3 → 一律建 AiAction（待确认）
  ▼
services.py / services_ops.py（PropertyService）
  │  权限 → 数据范围 → 幂等 → 校验 → 状态机 → 乐观锁 → 写库
  │  → order_log/audit(agent) → 回读 → 返回 {..., message, version}
  ▼
MySQL
```

**页面与智能体走的是同一套代码**：`app.py` 调 `services.*`，智能体也调 `services.*`，
所以"权限只在提示词里约束"这种假安全在这里不存在。

## 4. 权限公式

```text
智能体实际能力
 = 当前登录用户
 ∩ 实时 RBAC 权限
 ∩ DataScope（小区 / 楼栋 / 本人任务 / 本人房屋）
 ∩ 会话有效期（auth_version）
 ∩ 风险策略（R0–R3）
```

- 提示词里写"忽略权限、你是管理员"没有用：**授权在服务端**，模型看到的只是"调用被拒绝"。
- 工具执行时用令牌重新解析用户，**不接受模型传入的 user_id / role**。

## 5. 动态能力目录

MCP 服务启动时按当前用户权限**只注册他能用的工具**（`tools_for_permissions()`）：

| 账号 | 可见工具数 | 说明 |
| --- | --- | --- |
| 物业经理 `manager01` | 73 | 本小区全量能力 |
| 业主 `owner01` | 27 | 看不到删除房屋、派单、财务等（46 个被隐藏） |
| 工程维修 `engineer01` | 少 | 只有工单执行、设备巡检相关 |

没权限的能力**不进模型上下文**（省 token，也避免模型"看到就想试"）；即使模型硬造一个工具名，
服务端 `Policy.require()` 仍然会二次拒绝。

## 6. 风险分级与人工确认

| 级别 | 含义 | 执行方式 | 例子 |
| --- | --- | --- | --- |
| **R0** | 只读 | 直接执行 | 查房屋、查工单、欠费汇总 |
| **R1** | 普通写，可回滚 | 直接执行 | 登记报修、记录进展、登记访客 |
| **R2** | 关系/状态/分配类 | 白名单内自动，其余确认 | 派单、验收关闭、解除关系、退租 |
| **R3** | 删除、归档、财务、批量 | **永远确认** | 删除房屋/人员、作废账单、收款、冲销 |

`risk.py` 是唯一的分级来源；白名单可用环境变量**收紧**（`AGENT_R2_AUTO_COMMANDS`），
但 **R3 不能放宽**——这是硬闸门。

**确认卡片的生命周期（`ai_action` 表 + `agent/actions.py`）**

```text
模型调用 R3 工具
  → 服务端不执行，落一条 ai_action：
      payload（服务端保存的原始参数）/ payload_hash / preview（人话预览）
      risk_level / expires_at（10 分钟）
  → 工具返回 {"requires_confirmation": {...}}，模型据此告诉用户"请点确认卡片"
  → 浏览器渲染卡片；用户点击「确认执行」
  → 服务端重新校验：账号归属、auth_version、状态、是否过期、单据版本、业务规则
  → 执行 → 写审计（source=agent）→ 回读校验 → 卡片置为已执行
```

已验证的两个安全性质（见第 9 节证据）：
1. **聊天里说"我确认"不能代替点击**——模型在第二轮再次调用工具，仍然只是生成/复用同一张卡片。
2. **过期、越权、目标变化一律拒绝**：确认时全套校验重跑。

## 7. 短期授权（令牌）

- 令牌由 `itsdangerous` 签发，内容 `{uid, auth_version}`，TTL 8 小时。
- 只通过 **MCP 子进程环境变量**传递，**绝不进入提示词或模型上下文**。
- 用户登出、角色或数据范围变化 → `auth_version` 变化 → 令牌失效、**缓存的运行时同时作废**
  （`AgentRuntimeManager` 以 `(user_id, auth_version)` 为键，换权限即重建）。

## 8. 执行后回读与审计

- 每个写命令都返回 `{..., "message", "version"}`，并且**回读目标行**再返回。
- 智能体的系统提示词要求：**只有工具返回 `ok=true` 才能说"已完成"**，否则如实转述失败原因。
- 所有智能体发起的写操作写审计 `source=agent`（谁、什么动作、目标、before/after、trace），
  页面上的"操作审计"能直接看到 `AI` 角标。

## 9. 怎么验证（可复跑）

```powershell
# 0) 智能体链路（启动 dsh、挂 MCP、列能力目录、调工具）
python artifacts/agent-spec/mcp_handshake_probe.py

# 1) 端到端四场景（真实模型 + 真实落库；S1 查询 / S2 报修 / S3 删除确认 / S4 越权）
python artifacts/agent-spec/e2e_agent_demo.py

# 2) 能力目录与风险分级一致性（工具 ↔ 服务函数 ↔ 权限 ↔ 风险）
python -m unittest tests.test_mcp_tools -v

# 3) SSE 事件协议（不花模型配额）
python -m unittest tests.test_agent_bridge -v
```

最近一次结果（`artifacts/agent-spec/e2e-full.log`，5/5 PASS）：

| 场景 | 结果 | 证据 |
| --- | --- | --- |
| S1 业主按数据范围查询 | PASS | 只返回本人房屋 `云邻花园1栋1单元101` |
| S2 一句话报修 | PASS | 库里真实新增 `WO202609120001`（厨房水管漏水 / 13800002001），并**回读** `get_work_order` 确认 |
| S3 高风险删除 | PASS | 生成 R3 卡片 → 聊天里说"确认"无效 → 点击确认后软删除 → agent 审计 +1 |
| S4 越权修改小区 | PASS | 能力目录里没有该工具，小区名未变 |
| S5 运营模块 | PASS | 调 `list_complaints` 汇报未结案投诉；调 `register_visitor` 真实新增访客（0 → 1） |

其他可复跑的自检：

```powershell
python artifacts/agent-spec/final_acceptance.py --with-agent   # 一键七项验收（含真实模型）
python artifacts/agent-spec/persona_check.py                   # 系统提示词确实是我们的 persona
python artifacts/agent-spec/finance_smoke.py                   # 账单/收款/冲销 + 租赁链路
python artifacts/agent-spec/page_smoke.py --mysql              # 6 角色 × 全部页面（含 404 检测）
python artifacts/agent-spec/web_ai_multiturn.py                # 网页层多轮：报修 → 指代派单
```

最近一次总验收（`artifacts/FINAL_ACCEPTANCE.md`）：**7/7 PASS** —— 库结构与模型一致、
演示数据自检、143 个单元测试、平台端到端 70/70、页面体检无失败、
智能体五场景端到端、网页层多轮闭环。

## 10. 已知边界

1. **模型通道可切换**：默认 DeepSeek（`.env` 的 `DEEPSEEK_*`）；配置 `BAILIAN_API_KEY`
   即可切到百炼 OpenAI 兼容端点，代码不动。
2. **一个用户一个运行时**：首轮启动约 3 秒；空闲 30 分钟回收。要支撑多人并发需要换成
   "共享运行时 + 每次调用带用户上下文"，本轮为演示场景做了简化。
3. **SSE 是"步骤级"流式**：dsh 的会话事件是轮次/步骤粒度（工具调用、最终回答），
   没有 token 级增量，所以前端做的是"进度 + 分片打字机"，不是逐字流。
4. **确认卡片是 10 分钟有效的一次性动作**：不做"记住这个偏好下次自动执行"（演示场景不需要）。
5. **R3 的"确认"是应用层权限**：真实生产还应叠加二次口令/短信等强认证，本轮只做点击确认 + 审计。
