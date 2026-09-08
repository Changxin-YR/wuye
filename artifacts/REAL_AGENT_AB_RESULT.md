# Real Agent Acceptance

Dataset: `tests/fixtures/real_agent_tasks.json` (100 tasks, 20 notice tasks).

| Metric | Qwen / Bailian | DeepSeek V4 Pro |
|---|---:|---:|
| Task completion rate | 66% | not run: API key not configured |
| Unsafe execution | 0 | not run |
| Average tool calls | 0.98 | not run |

公告专项：20/20 PASS（包含原始复现句、单/多小区范围、批量确认、指定楼栋、缺失楼栋不造数据和公告修改/撤下澄清）。

Real-world Agent set：66/100 PASS，Unsafe Execution：0。Bailian run 记录在 `artifacts/real_agent_bailian_result.json`；每条记录包含 planner hint、provider tool calls、参数、PropertyService/action 结果、业务表 DB 变化和最终回答。任务成功率按真实业务执行/确认/只读结果与回读验证统计，不按模型文字命中统计。

`DEEPSEEK_API_KEY` 和 `DEEPSEEK_KEY` 均未配置，因此没有发送 DeepSeek 请求，也没有根据不完整证据切换 Provider。默认仍为 Bailian。SQLite 与 MySQL 真实部署、11 角色人工 Agent 等价测试和 DeepSeek 真实 A/B 尚未在本机完成，不能宣称最终状态 A。
