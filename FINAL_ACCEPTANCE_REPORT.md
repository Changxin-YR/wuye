# 美家物业 V2 + AI Agent 验收报告

## 当前基线

- 代码提交：`6011a51928e49bea3fd901c3fef6983f91e0dbc4`
- 远端：`git@github.com:Changxin-YR/wuye.git`
- 本地验证：`python -m unittest discover -s tests -q`，`407 tests OK`
- 编译：`python -m compileall -q .`，通过
- CI run：[#266](https://github.com/Changxin-YR/wuye/actions/runs/34423953603)，SQLite/MySQL 均成功；前序 run [#265](https://github.com/Changxin-YR/wuye/actions/runs/34423776151) 亦成功

## P1

| 项目 | 状态 | 证据 |
| --- | --- | --- |
| Agent 持久化、多实例状态 | PASS | DB ConversationState/Message，406 项测试覆盖重启/跨实例路径 |
| 跨请求幂等 | PASS | BusinessRequest + request_key，重复 payload 回放、冲突 409 |
| Schema Contract / revision | PASS | 类型、长度、nullable、FK、索引、唯一/检查约束及 revision 检查 |
| 生产 WSGI | PASS | Waitress `wsgi.py`、ProxyFix、`/health`、`/ready` smoke |
| 远端 CI | PASS | run #266 的 SQLite/MySQL job 均成功 |
| GitHub Branch Protection | BLOCKED | 配置文件存在；查询远端需要 `GITHUB_TOKEN` |

## P2

| 项目 | 状态 |
| --- | --- |
| Agent 状态 retention/cleanup | PASS |
| 上下文按需披露 | NOT_TESTABLE | 当前版本仍保留有限预加载，后续应改为按 Tool 查询 |
| 登录 username + IP 限流 | FAIL |
| R3 step-up password/token | PASS | 生产环境 R3 确认要求当前密码，成功后 5 分钟 session step-up |
| Secure Cookie/HSTS | PASS |
| 依赖安全 | PASS (`pip-audit -r requirements.lock.txt`: no known vulnerabilities) |
| 备份/恢复 | PARTIAL | `manage.py backup` + `restore-test` 已提供并完成归档校验；真实 MySQL restore drill 需目标环境 |

## Provider

- DeepSeek：PASS（普通推理、原生 Tool Call、`diagnose --ai --infer`）
- Bailian：NOT_TESTABLE（未配置凭据）

## 仍未解决

远端 Branch Protection 未能由当前权限确认；登录 IP 限流、按需上下文和真实 MySQL 恢复演练尚未完成。故当前结论仍为：暂不建议正式生产交付。
