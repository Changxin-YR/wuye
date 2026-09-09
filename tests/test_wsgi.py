import unittest
from types import SimpleNamespace
from unittest.mock import patch

import wsgi


class WsgiTests(unittest.TestCase):
    def test_production_requires_secure_cookie(self):
        insecure = SimpleNamespace(config={'APP_ENV': 'production', 'SESSION_COOKIE_SECURE': False})
        with patch.object(wsgi, 'create_app', return_value=insecure):
            with self.assertRaisesRegex(RuntimeError, 'Secure Cookie'):
                wsgi.create_production_app()

    def test_waitress_entry_is_callable(self):
        self.assertTrue(callable(wsgi.application))
