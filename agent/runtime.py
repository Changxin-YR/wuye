"""按登录用户管理 DeepSeek Harness（dsh）运行时。

要点：

* **一个登录用户一个运行时**：``dsh_home`` 隔离到 ``instances/dsh-home/u<id>/``，
  会话之间互不可见；同一用户复用同一个运行时（启动一次约 3 秒）。
* **配置覆盖（patch）而不是改代码**：系统提示词、MCP 工具、无关工具裁剪，
  全部通过 dsh 官方的 ``--patch`` 配置文件注入，harness 源码一个字节都不改。
* **身份靠令牌**：令牌随 MCP 子进程环境变量下发，不出现在提示词里；
  ``auth_version`` 变化（改角色/数据范围/登出）后旧运行时会话自动作废。

对外只有四个动作：``run_turn`` / ``warm_up`` / ``close_idle`` / ``shutdown_all``。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

import db

from .token import mint_token

#: MCP 服务在模型侧的名字空间：工具会显示为 ``mcp__wuye__<tool>``
MCP_SERVER_NAME = "wuye"

#: 与业务无关的行：只关掉"能力行"（工具/技能/网页/子智能体/目标），保留会话、
#: 持久化、模型、沙箱策略等基础设施，尽量减少启动风险。
#: 这些 id 必须存在于 profile 中，否则 dsh 会打印 "entry not found" 警告。
DISABLED_TOOL_ROWS: tuple[str, ...] = (
    # 命令行 / 终端
    "tool-bash",
    "tool-pwsh",
    # 文件系统与项目指令
    "tool-fs",
    "tool-fs-search",
    "agent-instructions",
    # 技能文件
    "skill",
    "skill-filesystem",
    "skill-badge",
    "tool-skill",
    # 网络
    "web",
    "web-search-deepseek",
    "web-fetch-http",
    "tool-web",
    # 计划 / 目标 / 子智能体 / 工作流 / 后台任务
    "plan-mode",
    "goal",
    "goal-round-driver",
    "command-goal",
    "tool-goal",
    "tool-ralph",
    "tool-todo",
    "subagent",
    "subagent-spawn-in-process",
    "subagent-fork-in-process",
    "tool-subagent",
    "tool-subagent-control",
    "tool-subagent-list-agents",
    "tool-subagent-fork",
    "workflow-worker-thread",
    "tool-workflow",
    "jobs",
    "tool-jobs",
    # 追问工具：SDK 场景下没有交互式问答面板，让模型直接用文字问用户
    "user-questions",
)


class AgentError(Exception):
    """智能体运行时不可用。"""


@dataclass
class AgentSettings:
    """智能体运行参数（全部可由环境变量覆盖）。"""

    project_dir: Path
    home_root: Path
    workspace_dir: Path
    python_exe: str
    provider: str = "deepseek-official"
    model: str = "deepseek-v4-flash"
    api_key: str | None = None
    base_url: str | None = None
    dsh_bin: str | None = None
    idle_seconds: int = 1800
    seed_key: str | None = None
    #: 显式传给 MCP 子进程的环境变量（数据库地址、密钥等）。dsh 会清洗环境变量，
    #: 不能依赖"父进程有子进程就有"，必须写进 patch。
    mcp_env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, *, project_dir: Path | None = None, **overrides: Any) -> "AgentSettings":
        project = Path(project_dir or Path(__file__).resolve().parent.parent).resolve()
        defaults: dict[str, Any] = {
            "project_dir": project,
            "home_root": Path(os.environ.get("WUYE_AGENT_HOME_ROOT") or project / "instances" / "dsh-home"),
            "workspace_dir": Path(os.environ.get("WUYE_AGENT_WORKSPACE") or project / "instances" / "dsh-workspace"),
            "python_exe": os.environ.get("WUYE_AGENT_PYTHON") or sys.executable,
            "provider": os.environ.get("WUYE_AGENT_PROVIDER", "deepseek-official"),
            "model": os.environ.get("WUYE_AGENT_MODEL") or os.environ.get("DSH_MODEL") or "deepseek-v4-flash",
            "api_key": os.environ.get("DEEPSEEK_API_KEY"),
            "base_url": os.environ.get("DEEPSEEK_BASE_URL"),
            "dsh_bin": os.environ.get("WUYE_DSH_BIN") or None,
            "idle_seconds": int(os.environ.get("WUYE_AGENT_IDLE_SECONDS", "1800")),
            "seed_key": os.environ.get("SECRET_KEY"),
            "mcp_env": {
                key: value
                for key, value in (
                    ("DATABASE_URL", os.environ.get("DATABASE_URL", "")),
                    ("SECRET_KEY", os.environ.get("SECRET_KEY", "")),
                    ("APP_ENV", os.environ.get("APP_ENV", "")),
                )
                if value
            },
        }
        defaults.update(overrides)
        return cls(**defaults)


def load_persona(project_dir: Path) -> str:
    """读取系统提示词（persona）。"""
    path = Path(project_dir) / "agent" / "config" / "wuye_agent.md"
    if not path.is_file():
        raise AgentError(f"缺少智能体系统提示词：{path}")
    return path.read_text(encoding="utf-8").strip()


def build_patch(settings: AgentSettings, *, token: str, persona: str) -> list[dict[str, Any]]:
    """生成 dsh 配置覆盖（patch）。

    覆盖三件事：① 系统提示词换成物业助手；② 用 ``insert`` 挂上我们的 MCP 工具服务；
    ③ 关掉与业务无关的工具行。返回的是可直接 ``yaml.safe_dump`` 的列表。

    注意 dsh 的 patch 语义（``vendor/include`` 的 ``applyEntryPatches``）：

    * 带 ``id`` 的补丁是**按 id 覆盖**（``config`` 整个字段被替换）；id 不存在只会警告；
    * 新增行必须用 ``{"insert": [...]}``（补丁自身**不能**带 ``id``）。
    """
    rows: list[dict[str, Any]] = [
        {
            "id": "system-prompt",
            "config": {
                # 业务场景：不要 "你是编码智能体" 那句身份说明，也不注入运行时上下文
                "includeHarnessIdentity": False,
                "includeRuntimeContext": False,
                # 注意：dsh 0.1.5-rc.1 的 system-prompt schema 只认 personaPrefix/personaSuffix，
                # 早期写法 "persona" 会被静默忽略（dump 出来但模型收不到）——所以这里必须用前缀字段。
                "personaPrefix": persona,
                "personaSuffix": "",
            },
        },
        {
            "insert": [
                {
                    "id": "mcp-wuye",
                    "name": "@deepseek-ai/dsh-mcp-client",
                    "config": {
                        "serverName": MCP_SERVER_NAME,
                        "transport": "stdio",
                        "command": settings.python_exe,
                        "args": ["-m", "agent.mcp_server"],
                        "cwd": str(settings.project_dir),
                        "env": {
                            "WUYE_AGENT_TOKEN": token,
                            "PYTHONIOENCODING": "utf-8",
                            "PYTHONUNBUFFERED": "1",
                            **settings.mcp_env,
                        },
                        "toolCallTimeoutMs": 60000,
                        # 连不上就明确失败，避免出现"看起来能聊天但没有工具"的假象
                        "failOnStartupError": True,
                    },
                }
            ]
        },
        # 会话标题用首条消息即可，不必多花一次模型调用
        {"id": "session-title-llm", "disabled": True},
    ]
    rows.extend({"id": row_id, "disabled": True} for row_id in DISABLED_TOOL_ROWS)
    return rows


class _BlockStr(str):
    """让多行文本（系统提示词）在 YAML 里用 ``|`` 块样式输出，便于人工核对。"""


def _represent_block_str(dumper: yaml.Dumper, data: _BlockStr) -> yaml.Node:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="|")


yaml.SafeDumper.add_representer(_BlockStr, _represent_block_str)
yaml.Dumper.add_representer(_BlockStr, _represent_block_str)


def dump_patch(rows: list[dict[str, Any]]) -> str:
    prepared: list[dict[str, Any]] = []
    for row in rows:
        config = row.get("config")
        if isinstance(config, dict) and isinstance(config.get("persona"), str):
            config = dict(config)
            config["persona"] = _BlockStr(config["persona"])
            row = {**row, "config": config}
        prepared.append(row)
    return yaml.safe_dump(prepared, allow_unicode=True, sort_keys=False, default_flow_style=False, width=200)


@dataclass
class RuntimeEntry:
    """一个登录用户的 dsh 运行时。"""

    user_id: int
    auth_version: int
    home: Path
    patch_path: Path
    harness: Any
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_used: float = field(default_factory=time.time)
    sessions: set[str] = field(default_factory=set)
    #: 本次运行时创建时被自动换新的会话 id（磁盘上已存在、新进程无法复用）
    rotated_sessions: list[int] = field(default_factory=list)

    @property
    def key(self) -> tuple[int, int]:
        return (self.user_id, self.auth_version)


class AgentRuntimeManager:
    """运行时池：懒启动、按用户复用、空闲回收。"""

    def __init__(self, settings: AgentSettings) -> None:
        self.settings = settings
        self._entries: dict[tuple[int, int], RuntimeEntry] = {}
        self._lock = threading.Lock()
        self._reaper: threading.Thread | None = None
        self._closed = False
        self.settings.home_root.mkdir(parents=True, exist_ok=True)
        self.settings.workspace_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 运行时
    def _new_harness(self, home: Path, patch_path: Path):
        from deepseek_harness import DeepSeekHarness  # 延迟导入，便于无依赖时给出清晰报错

        # dsh 规定 DEEPSEEK_BASE_URL 只能由启动环境注入：若子进程 cwd 下的 .env 也写了它，
        # dsh 会直接拒绝启动。这里显式检查一次，防止以后有人把工作目录指到项目根。
        stray_env = Path(self.settings.workspace_dir) / ".env"
        if stray_env.is_file():
            raise AgentError(
                f"智能体工作目录里不允许放 .env（{stray_env}）：它会让 dsh 拒绝启动，请删除或更换工作目录"
            )

        env: dict[str, str] = {}
        if self.settings.api_key:
            env["DEEPSEEK_API_KEY"] = self.settings.api_key
        if self.settings.base_url:
            env["DEEPSEEK_BASE_URL"] = self.settings.base_url
        env["DSH_HOME"] = str(home)
        # 注意：cwd 用 workspace（不是项目根）。dsh 会自动加载子进程 cwd 下的 .env，
        # 而 .env 里的 DEEPSEEK_BASE_URL 与"启动环境注入"冲突时 dsh 会拒绝启动；
        # 传 runtime_cwd=项目根也会踩同一个坑，所以这里只把参数经 env 显式下发。
        kwargs: dict[str, Any] = {
            "dsh_home": str(home),
            "cwd": str(self.settings.workspace_dir),
            "provider": self.settings.provider,
            "model": self.settings.model,
            "patches": (str(patch_path),),
            "env": env,
        }
        if self.settings.dsh_bin:
            kwargs["dsh_bin"] = self.settings.dsh_bin
        return DeepSeekHarness(**kwargs)

    def _create_entry(self, user_id: int, auth_version: int) -> RuntimeEntry:
        home = self.settings.home_root / f"u{user_id}"
        home.mkdir(parents=True, exist_ok=True)
        token = mint_token(user_id, auth_version, secret_key=self.settings.seed_key)
        persona = load_persona(self.settings.project_dir)
        patch_path = home / "wuye.patch.yml"
        patch_path.write_text(dump_patch(build_patch(self.settings, token=token, persona=persona)), encoding="utf-8")
        harness = self._new_harness(home, patch_path)
        entry = RuntimeEntry(
            user_id=user_id,
            auth_version=auth_version,
            home=home,
            patch_path=patch_path,
            harness=harness,
        )
        self._mask_patch_log(entry)
        # 新起的 dsh 进程无法复用磁盘上已存在的会话 id（会报 session already exists），
        # 所以这里把冲突的会话 id 换新；页面上的历史消息仍在本平台数据库里，不受影响。
        entry.rotated_sessions = self._rotate_colliding_sessions(user_id, home)
        return entry

    def _rotate_colliding_sessions(self, user_id: int, home: Path) -> list[int]:
        """把"磁盘上已存在、新进程无法复用"的 dsh 会话 id 换新，返回被换的会话 id 列表。"""
        sessions_root = home / "sessions"
        if not sessions_root.is_dir():
            return []
        rotated: list[int] = []
        try:
            from . import session_store as store

            with db.db_session() as session:
                for agent_session_id, dsh_session_id in store.list_dsh_session_ids(session, user_id):
                    if not dsh_session_id:
                        continue
                    if any(sessions_root.glob(f"*/{dsh_session_id}")):
                        store.rotate_dsh_session_id(session, user_id, agent_session_id)
                        # 在对话里留一条提示：历史消息还在，但助手不再记得更早的上下文
                        store.append_message(
                            session, agent_session_id, "system",
                            "助手已重新启动，本次对话的上下文已重置；上面的历史消息仍然保留，直接继续提问即可。",
                        )
                        rotated.append(agent_session_id)
        except Exception:  # noqa: BLE001 - 轮换失败不影响启动，下一轮会再试
            return rotated
        return rotated

    @staticmethod
    def _mask_patch_log(entry: RuntimeEntry) -> None:
        """落一份脱敏后的 patch 副本，便于排查配置问题。"""
        try:
            text = entry.patch_path.read_text(encoding="utf-8")
            safe = text
            for line in text.splitlines():
                if "WUYE_AGENT_TOKEN:" in line:
                    safe = safe.replace(line, "      WUYE_AGENT_TOKEN: '***'")
            (entry.home / "wuye.patch.masked.yml").write_text(safe, encoding="utf-8")
        except OSError:
            pass

    def _entry_for(self, user_id: int, auth_version: int) -> RuntimeEntry:
        key = (int(user_id), int(auth_version))
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                # 同一用户换角色/数据范围后旧运行时立即作废
                for stale_key in [k for k in self._entries if k[0] == key[0] and k != key]:
                    stale = self._entries.pop(stale_key)
                    self._close_entry(stale, reason="权限版本变化")
                entry = self._create_entry(*key)
                self._entries[key] = entry
            entry.last_used = time.time()
            self._ensure_reaper()
            return entry

    def warm_up(self, user_id: int, auth_version: int) -> None:
        """提前把运行时拉起来（首次对话就不用等启动）。"""
        self._entry_for(user_id, auth_version)

    def run_turn(
        self,
        *,
        user_id: int,
        auth_version: int,
        session_id: str,
        text: str,
        on_notification: Callable[[Any], None],
    ) -> Any:
        """跑一轮对话：返回 dsh 的 RunResult（其中含权威最终回答）。"""
        if self._closed:
            raise AgentError("智能体服务已关闭")
        entry = self._entry_for(user_id, auth_version)
        if not entry.lock.acquire(timeout=180):
            raise AgentError("上一轮对话还在进行，请稍候再试")
        try:
            entry.last_used = time.time()
            entry.sessions.add(session_id)
            result = entry.harness.run(text, session_id=session_id, on_notification=on_notification)
            entry.last_used = time.time()
            return result
        finally:
            entry.lock.release()

    def session_known(self, user_id: int, auth_version: int, session_id: str) -> bool:
        with self._lock:
            entry = self._entries.get((int(user_id), int(auth_version)))
            return bool(entry and session_id in entry.sessions)

    # ------------------------------------------------------------ 生命周期
    def _ensure_reaper(self) -> None:
        if self._reaper is not None and self._reaper.is_alive():
            return
        self._reaper = threading.Thread(target=self._reap_loop, name="agent-reaper", daemon=True)
        self._reaper.start()

    def _reap_loop(self) -> None:
        while not self._closed:
            time.sleep(30)
            try:
                self.close_idle()
            except Exception:  # noqa: BLE001 - 回收失败不影响主流程
                pass

    def close_idle(self, now: float | None = None) -> int:
        """关闭空闲运行时，返回关闭数量。"""
        now = now or time.time()
        with self._lock:
            stale = [
                key
                for key, entry in self._entries.items()
                if now - entry.last_used > self.settings.idle_seconds and not entry.lock.locked()
            ]
            entries = [self._entries.pop(key) for key in stale]
        for entry in entries:
            self._close_entry(entry, reason="空闲回收")
        return len(entries)

    @staticmethod
    def _close_entry(entry: RuntimeEntry, *, reason: str = "") -> None:
        try:
            entry.harness.close()
        except Exception:  # noqa: BLE001 - 关闭失败不阻塞
            pass

    def shutdown_all(self) -> None:
        self._closed = True
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            self._close_entry(entry)

    # ------------------------------------------------------------------ 自检
    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "runtimes": len(self._entries),
                "users": sorted({key[0] for key in self._entries}),
                "model": self.settings.model,
                "provider": self.settings.provider,
                "home_root": str(self.settings.home_root),
            }

    def patch_preview(self, user_id: int = 0, auth_version: int = 1) -> str:
        """返回一份脱敏的 patch 文本，供自检与文档使用（不启动运行时）。"""
        persona = load_persona(self.settings.project_dir)
        rows = build_patch(self.settings, token="<token>", persona=persona)
        return dump_patch(rows)


def dsh_runtime_path() -> str | None:
    """解析 dsh 可执行文件路径（供诊断接口展示）。"""
    env_bin = os.environ.get("WUYE_DSH_BIN")
    if env_bin:
        return env_bin
    try:
        from deepseek_harness_runtime import resolve_bundled_launch_args

        args = resolve_bundled_launch_args()
    except Exception:  # noqa: BLE001 - 未安装时返回 None
        return None
    return " ".join(args)


def describe_settings(settings: AgentSettings) -> str:
    """一行配置摘要（不包含任何密钥）。"""
    return json.dumps(
        {
            "provider": settings.provider,
            "model": settings.model,
            "dsh_bin": settings.dsh_bin or dsh_runtime_path(),
            "home_root": str(settings.home_root),
            "workspace": str(settings.workspace_dir),
            "python": settings.python_exe,
        },
        ensure_ascii=False,
    )
