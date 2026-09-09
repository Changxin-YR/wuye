"""Production WSGI entrypoint for Waitress."""
import os

from app import create_app


def create_production_app():
    application = create_app()
    if application.config.get('APP_ENV') == 'production' and not application.config.get('SESSION_COOKIE_SECURE'):
        raise RuntimeError('生产环境必须启用 Secure Cookie（COOKIE_SECURE=1）')
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
