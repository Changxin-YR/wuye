import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

import property_service
from property_service import PropertyService


class NoticeScopeWrapperTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

    def service(self, scopes, community_write=False):
        svc = object.__new__(PropertyService)
        svc.data = {'community_id': 1, 'title': '测试公告', 'content': '正文'}
        svc.policy = SimpleNamespace(
            scopes=scopes,
            within=lambda cid, bid=None, write=False: community_write,
        )
        return svc

    def test_single_building_scope_defaults_notice_to_that_building(self):
        svc = self.service([SimpleNamespace(kind='building', community_id=1, building_id=7)])

        def fake_core(instance, action):
            self.assertEqual(action, 'save')
            return dict(instance.data), 'ok'

        with patch.object(property_service._CorePropertyService, 'do_notice', new=fake_core):
            result, _ = svc.do_notice('save')
        self.assertEqual(result['community_id'], 1)
        self.assertEqual(result['building_id'], 7)
        self.assertNotIn('building_id', svc.data)

    def test_multiple_building_scopes_require_explicit_building(self):
        svc = self.service([
            SimpleNamespace(kind='building', community_id=1, building_id=7),
            SimpleNamespace(kind='building', community_id=1, building_id=8),
        ])
        with self.app.app_context():
            with self.assertRaises(Exception):
                svc.do_notice('save')

    def test_community_write_scope_keeps_community_wide_notice(self):
        svc = self.service([SimpleNamespace(kind='community', community_id=1, building_id=None)], community_write=True)

        def fake_core(instance, action):
            return dict(instance.data), 'ok'

        with patch.object(property_service._CorePropertyService, 'do_notice', new=fake_core):
            result, _ = svc.do_notice('save')
        self.assertNotIn('building_id', result)

    def test_community_batch_accepts_more_than_twenty_but_stays_bounded(self):
        svc = object.__new__(PropertyService)
        svc.data = {'community_ids': list(range(1, 26))}
        self.assertEqual(len(svc.ids('community_ids')), 25)
        svc.data = {'community_ids': list(range(1, 102))}
        with self.app.app_context():
            with self.assertRaises(Exception):
                svc.ids('community_ids')


if __name__ == '__main__':
    unittest.main()
