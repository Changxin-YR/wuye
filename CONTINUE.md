# 当前交付状态

本检查点已完成物业 V2 主干源码审计与 Agent 权限安全强化。Agent 采用当前登录用户实时 RBAC + DataScope + R0-R3 Risk Policy，不直接访问数据库；高风险操作由浏览器二次确认。

当前交付已完成，根目录仅保留这一套源码；AI 默认使用百炼并支持 SSE 流式输出。下一次在目标机器上只需：

1. `python -m pip install -r requirements.txt`
2. 配置本机 `.env`
3. `python manage.py upgrade-db`（已有库）或 `python manage.py init-db`（新库）
4. `python -m unittest discover -s tests -v`
5. `python manage.py diagnose --ai --infer`

当前 Windows 环境全量测试结果：`Ran 131 tests ... OK`。百炼 Key 加载顺序为 `BAILIAN_API_KEY` → `DASHSCOPE_API_KEY`。

本源码包不应提交真实 `.env`、上传业务图片或数据库备份。
