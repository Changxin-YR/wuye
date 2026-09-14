"""美家物业 · 智能体集成包。

本包只做三件事，不改动 DeepSeek Harness（dsh）源码：

1. ``runtime``：按登录用户启动/复用 dsh 运行时（Python SDK + 配置覆盖）。
2. ``mcp_server``：把业务能力以 MCP 工具暴露给 dsh，权限与当前登录用户一致。
3. ``bridge``：把 dsh 的会话事件翻译成浏览器 SSE 事件。
"""

__all__ = ["tools", "token", "runtime", "bridge", "mcp_server"]
