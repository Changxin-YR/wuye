"""Production WSGI entrypoint for Waitress."""
import os

from app import create_app


def _is_loopback(host: str) -> bool:
    """回环地址判定（本地演示时不能用 Secure Cookie，否则浏览器不回传会话）。"""
    return (host or "").strip().lower() in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "")


def create_production_app():
    """生产/演示入口。

    guard 口径：**production 且非回环**才强制 Secure Cookie。
    - 本机回环（127.0.0.1）跑 http 时放行：否则本地 `python serve.py` 会直接 RuntimeError，
      而浏览器在 http 下也不会回传 Secure Cookie，反而登录不上。
    - 对外地址（隧道/真机域名）仍强制 `COOKIE_SECURE=1`，与线上一致。
    """
    application = create_app()
    if (
        application.config.get("APP_ENV") == "production"
        and not application.config.get("SESSION_COOKIE_SECURE")
        and not _is_loopback(application.config.get("HOST", ""))
    ):
        raise RuntimeError("对外的生产环境必须启用 Secure Cookie（COOKIE_SECURE=1）")
    return application


class _LazyApplication:
    def __init__(self):
        self._application = None

    def __call__(self, environ, start_response):
        if self._application is None:
            self._application = create_production_app()
        return self._application(environ, start_response)


application = _LazyApplication()


if __name__ == '__main__':
    from waitress import serve
    serve(
        create_production_app(),
        host=os.getenv('HOST', '127.0.0.1'),
        port=int(os.getenv('PORT', '5000')),
        threads=max(2, min(int(os.getenv('WAITRESS_THREADS', '8')), 64)),
        channel_timeout=max(10, min(int(os.getenv('WAITRESS_CHANNEL_TIMEOUT', '120')), 600)),
        cleanup_interval=max(1, min(int(os.getenv('WAITRESS_CLEANUP_INTERVAL', '30')), 300)),
    )
