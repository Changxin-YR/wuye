"""SQLAlchemy 2.0 引擎、会话与事务边界。

对外提供：
- :func:`get_engine` / :func:`get_session`：进程内按 URL 复用引擎；请求内复用同一个 session。
- :func:`db_session`：脚本/测试用的上下文管理器（正常提交、异常回滚、结束关闭）。
- :func:`create_all` / :func:`reset_tables`：建表与「先删后建」（MySQL 与 SQLite 都支持）。
- :func:`init_app`：注册 Flask 请求级会话与 teardown（正常结束提交、异常回滚）。

事务约定：写操作由 ``services`` 在自己的事务里提交；请求结束时的 teardown 只做兜底提交/回滚。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from flask import current_app, g, has_app_context
from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import config as app_config

log = logging.getLogger(__name__)

_engines: dict[str, Engine] = {}
_sessionmakers: dict[str, sessionmaker] = {}
_lock = threading.RLock()
_thread_local = threading.local()

_G_SESSIONS_KEY = "_wuye_db_sessions"


def normalize_url(url: str) -> str:
    """把 SQLite 相对路径规范化成绝对路径，避免不同工作目录建出多个库文件。"""
    if not url:
        url = app_config.get_config().DATABASE_URL
    if url.startswith("sqlite:///") and ":memory:" not in url:
        prefix = "sqlite:///"
        path = Path(url[len(prefix):])
        if not path.is_absolute():
            path = (app_config.BASE_DIR / path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return prefix + path.as_posix()
    return url


def default_url() -> str:
    """当前上下文使用的数据库地址：优先 Flask 配置，其次环境变量。"""
    if has_app_context():
        url = current_app.config.get("DATABASE_URL")
        if url:
            return normalize_url(url)
    return normalize_url(app_config.get_config().DATABASE_URL)


def get_engine(url: str | None = None, echo: bool = False) -> Engine:
    """按 URL 复用引擎（同一 URL 只建一个连接池）。"""
    url = normalize_url(url or default_url())
    engine = _engines.get(url)
    if engine is not None:
        return engine
    with _lock:
        engine = _engines.get(url)
        if engine is not None:
            return engine
        kwargs: dict = {"echo": echo, "future": True, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
            if ":memory:" in url:
                kwargs["poolclass"] = StaticPool
        else:
            kwargs.update(pool_recycle=280, pool_size=5, max_overflow=10)
        engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            # SQLite 默认不校验外键，测试里需要真实的外键行为
            @event.listens_for(engine, "connect")
            def _sqlite_pragma(dbapi_conn, _record):  # pragma: no cover - 驱动回调
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()
        _engines[url] = engine
        _sessionmakers[url] = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        engine._wuye_sessionmaker = _sessionmakers[url]
        log.debug("创建数据库引擎: %s", url)
        return engine


def get_sessionmaker(engine: Engine | None = None) -> sessionmaker:
    """取该引擎的 session 工厂。

    注意：**不能**用 ``str(engine.url)`` 回查缓存——SQLAlchemy 在字符串化时会把口令
    打码成 ``***``（``mysql+pymysql://user:***@host/db``），与缓存键不一致会直接 KeyError，
    表现为「/health 正常但任何页面都 500」。这里把工厂挂在 engine 上，彻底不依赖 URL 字符串。
    """
    engine = engine or get_engine()
    maker = getattr(engine, "_wuye_sessionmaker", None)
    if maker is None:
        maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        engine._wuye_sessionmaker = maker
    return maker


def dispose_engines() -> None:
    """关闭全部引擎（测试拆卸用）。"""
    with _lock:
        for engine in _engines.values():
            engine.dispose()
        _engines.clear()
        _sessionmakers.clear()


def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """脚本 / 智能体 MCP 子进程用的事务上下文（**不依赖 Flask 请求上下文**）。

    与 :func:`db_session` 同语义：正常提交、异常回滚、结束关闭。
    """
    return db_session(engine)


def get_session(engine: Engine | None = None) -> Session:
    """取当前上下文的会话：Flask 请求内共用，脚本内按线程复用。"""
    engine = engine or get_engine()
    if has_app_context():
        sessions = g.setdefault(_G_SESSIONS_KEY, {})
        session = sessions.get(engine)
        if session is None:
            session = get_sessionmaker(engine)()
            sessions[engine] = session
        return session
    session = getattr(_thread_local, "session", None)
    if session is None or session.get_bind() is not engine:
        if session is not None:
            session.close()
        session = get_sessionmaker(engine)()
        _thread_local.session = session
    return session


@contextmanager
def db_session(engine: Engine | None = None) -> Iterator[Session]:
    """脚本/测试用会话：正常提交、异常回滚、结束关闭。"""
    engine = engine or get_engine()
    session = get_sessionmaker(engine)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_app(app) -> None:
    """注册请求级会话、登录态有效性校验与 teardown。"""
    get_engine(app.config.get("DATABASE_URL") or None)

    @app.before_request
    def _validate_login_session():  # pragma: no cover - 由 Flask 调用
        """session 里的 ``auth_version`` 与数据库不一致（角色/数据范围被改、账号停用或删除）→ 强制登出。

        会话有效性属于数据层兜底：任何视图都拿不到过期的登录态。
        """
        from flask import session as flask_session

        from models import User as _User

        user_id = flask_session.get("user_id")
        if not user_id:
            return
        try:
            user = get_session().get(_User, int(user_id))
            stored_version = int(flask_session.get("auth_version") or -1)
        except Exception:  # pragma: no cover - 数据库不可用时不要连累请求
            log.warning("校验登录态失败", exc_info=True)
            return
        if user is None or user.deleted or not user.active or int(user.auth_version) != stored_version:
            flask_session.clear()

    @app.teardown_appcontext
    def _teardown(exc):  # pragma: no cover - 由 Flask 调用
        sessions = g.pop(_G_SESSIONS_KEY, None)
        if not sessions:
            return
        for session in sessions.values():
            try:
                if exc is None:
                    session.commit()
                else:
                    session.rollback()
            except Exception:  # 兜底：服务层已经提交过，这里失败只记录
                session.rollback()
                log.exception("请求结束提交事务失败")
            finally:
                session.close()


class _LazyEngine:
    """模块级 ``db.engine`` 的惰性代理：真正取连接时才建引擎。

    这样 ``import db`` 不会触发数据库连接（测试可以先覆盖 DATABASE_URL），
    而 ``db.engine`` 仍可像普通 Engine 一样直接使用（供 agent 侧调用）。
    """

    def __getattr__(self, name):
        return getattr(get_engine(), name)

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"<LazyEngine {default_url()}>"


#: 进程内共享引擎（惰性）；``agent/mcp_server.py`` 等独立进程直接用它
engine = _LazyEngine()


class _LazySessionLocal:
    """模块级 ``db.SessionLocal`` 的惰性代理：``SessionLocal()`` 等价 ``get_session()``。"""

    def __call__(self, *args, **kwargs):
        return get_sessionmaker()(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(get_sessionmaker(), name)


#: 会话工厂（惰性）
SessionLocal = _LazySessionLocal()


def create_all(engine: Engine | None = None) -> None:
    """按模型建表（MySQL / SQLite 通用）。"""
    import models  # 延迟导入，避免循环依赖

    models.Base.metadata.create_all(bind=engine or get_engine())


def drop_all(engine: Engine | None = None) -> None:
    import models

    models.Base.metadata.drop_all(bind=engine or get_engine())


def table_names(engine: Engine | None = None) -> list[str]:
    return list(inspect(engine or get_engine()).get_table_names())


def reset_tables(engine: Engine | None = None, names: list[str] | None = None) -> list[str]:
    """按名删表（``DROP TABLE IF EXISTS``），返回真正删掉的表名。

    用于 ``seed_demo.py reset``：旧库里同名表结构不同时，``create_all`` 不会改表，必须先删。
    MySQL 下会临时关闭外键检查，否则旧库里指向这些表的其它表会阻止删除。
    """
    import models

    engine = engine or get_engine()
    wanted = list(names or models.Base.metadata.tables.keys())
    existing = set(table_names(engine))
    dropped: list[str] = []
    is_mysql = engine.dialect.name.startswith("mysql")
    with engine.begin() as conn:
        if is_mysql:
            conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        else:
            conn.execute(text("PRAGMA foreign_keys=OFF"))
        for name in wanted:
            if name in existing:
                conn.execute(text(f"DROP TABLE IF EXISTS `{name}`"))
                dropped.append(name)
        if is_mysql:
            conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))
        else:
            conn.execute(text("PRAGMA foreign_keys=ON"))
    return dropped


def ping(engine: Engine | None = None) -> bool:
    """健康检查：数据库可连返回 True。"""
    try:
        with (engine or get_engine()).connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # pragma: no cover - 依赖外部数据库
        log.warning("数据库连接检查失败", exc_info=True)
        return False
