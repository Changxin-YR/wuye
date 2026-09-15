"""环境配置（读取 .env）。

约定：
- 唯一配置入口是 :class:`Config` 与 :func:`get_config`，其它模块不要直接读业务环境变量。
- 缺少 ``DATABASE_URL`` 时回退到 SQLite（``instances/wuye.db``），方便本地离线跑与测试。
- ``AI_*`` / ``DEEPSEEK_*`` / ``DSH_*`` / ``BAILIAN_*`` / ``DASHSCOPE_*`` 前缀的环境变量原样透传，
  供智能体（dsh）运行时使用；本模块只汇总，不修改。
"""
from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
INSTANCE_DIR = BASE_DIR / "instances"

#: 数据库未配置时的回退地址（测试与本地开发用）
FALLBACK_DATABASE_URL = "sqlite:///" + (INSTANCE_DIR / "wuye.db").as_posix()

#: 需要原样透传给智能体运行时的环境变量前缀
PASSTHROUGH_PREFIXES = ("AI_", "DEEPSEEK_", "DSH_", "BAILIAN_", "DASHSCOPE_")

APP_NAME = "美家物业"


def load_env(path: str | os.PathLike | None = None, override: bool = False) -> None:
    """加载 .env（默认不覆盖已存在的真实环境变量，便于测试与部署覆盖）。"""
    load_dotenv(path or ENV_FILE, override=override)


def _raw(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _as_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _is_loopback(host: str) -> bool:
    """判断是否本机回环地址（回环上跑 http 时不能用 Secure Cookie）。"""
    host = (host or "").strip().lower()
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def _ensure_sqlite_dir(url: str) -> str:
    """SQLite 文件库：自动创建父目录。"""
    prefix = "sqlite:///"
    if url.startswith(prefix) and ":memory:" not in url:
        path = Path(url[len(prefix):])
        if not path.is_absolute():
            path = (BASE_DIR / path).resolve()
            url = prefix + path.as_posix()
        path.parent.mkdir(parents=True, exist_ok=True)
    return url


class Config:
    """运行期配置对象（属性访问，可直接喂给 Flask）。"""

    def __init__(self, overrides: dict | None = None) -> None:
        load_env()

        self.BASE_DIR = BASE_DIR
        self.APP_NAME = APP_NAME
        self.APP_ENV = _raw("APP_ENV", "development")
        self.IS_PRODUCTION = self.APP_ENV.lower() in ("production", "prod")
        self.DEBUG = not self.IS_PRODUCTION

        self.SECRET_KEY = _raw("SECRET_KEY", "dev-only-insecure-secret-key-please-set-in-env")
        self.DATABASE_URL = _ensure_sqlite_dir(_raw("DATABASE_URL", FALLBACK_DATABASE_URL))
        self.USING_SQLITE = self.DATABASE_URL.startswith("sqlite")

        self.HOST = _raw("HOST", "127.0.0.1")
        self.PORT = int(_raw("PORT", "5000") or 5000)
        self.LOOPBACK = _is_loopback(self.HOST)

        # Secure Cookie：生产默认开启；回环地址上跑 http 时强制关闭，否则浏览器不回传 cookie
        # 以 .env 的 COOKIE_SECURE 为准（生产默认开）：
        #   serve.py / Waitress + https 隧道 → 保持 True；
        #   python app.py（本地 http 开发入口）会自己把 SESSION_COOKIE_SECURE 置为 False。
        # 这里不再按回环地址自动关闭，否则生产入口会因为「生产必须 Secure」校验而启动失败。
        self.COOKIE_SECURE = _as_bool(os.getenv("COOKIE_SECURE"), default=self.IS_PRODUCTION)
        self.SESSION_COOKIE_SECURE = self.COOKIE_SECURE
        self.SESSION_LIFETIME = timedelta(hours=int(_raw("SESSION_HOURS", "8") or 8))

        self.UPLOAD_FOLDER = _raw("UPLOAD_FOLDER", "uploads")
        self.MAX_CONTENT_LENGTH = int(_raw("MAX_CONTENT_MB", "8") or 8) * 1024 * 1024
        self.PAGE_SIZE = int(_raw("PAGE_SIZE", "20") or 20)

        # 登录页是否展示「演示账号」区块（账号 + 统一演示口令）。
        # 演示站默认开；真实部署设 DEMO_LOGIN_HINT=0，登录页就回到只有账号/密码输入框的形态。
        self.DEMO_LOGIN_HINT = _as_bool(os.getenv("DEMO_LOGIN_HINT"), default=True)

        if overrides:
            for key, value in overrides.items():
                if key == "DATABASE_URL" and value:
                    value = _ensure_sqlite_dir(str(value))
                setattr(self, key, value)

    # -- 便捷属性 ---------------------------------------------------------
    @property
    def is_testing(self) -> bool:
        return bool(getattr(self, "TESTING", False)) or self.APP_ENV.lower() == "testing"

    def ai_env(self) -> dict:
        """汇总需要透传给智能体运行时的环境变量。"""
        return {
            key: value
            for key, value in os.environ.items()
            if key.startswith(PASSTHROUGH_PREFIXES)
        }

    def to_flask(self) -> dict:
        """转换成 Flask ``app.config`` 字典。"""
        return {
            "APP_NAME": self.APP_NAME,
            "APP_ENV": self.APP_ENV,
            "SECRET_KEY": self.SECRET_KEY,
            "DATABASE_URL": self.DATABASE_URL,
            "HOST": self.HOST,
            "PORT": self.PORT,
            "USING_SQLITE": self.USING_SQLITE,
            "SESSION_COOKIE_SECURE": self.SESSION_COOKIE_SECURE,
            "SESSION_COOKIE_HTTPONLY": True,
            "SESSION_COOKIE_SAMESITE": "Lax",
            "PERMANENT_SESSION_LIFETIME": self.SESSION_LIFETIME,
            "MAX_CONTENT_LENGTH": self.MAX_CONTENT_LENGTH,
            "PAGE_SIZE": self.PAGE_SIZE,
            "UPLOAD_FOLDER": self.UPLOAD_FOLDER,
            "DEMO_LOGIN_HINT": self.DEMO_LOGIN_HINT,
            "AI_ENV": self.ai_env(),
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"<Config env={self.APP_ENV} db={self.DATABASE_URL}>"


def get_config(overrides: dict | None = None) -> Config:
    """每次调用都重新读取环境变量，方便测试覆盖。"""
    return Config(overrides)
