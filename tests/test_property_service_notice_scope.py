import unittest

from flask import Flask

from property_service import PropertyService


class NoticeScopeWrapperTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

    def test_community_batch_accepts_more_than_twenty_but_stays_bounded(self):
        svc = object.__new__(PropertyService)
        svc.data = {'community_ids': list(range(1, 26))}
        self.assertEqual(len(svc.ids('community_ids')), 25)
        svc.data = {'community_ids': list(range(1, 102))}
        with self.app.app_context():
            with self.assertRaises(Exception):
                svc.ids('community_ids')

    def test_non_community_id_lists_keep_original_twenty_item_limit(self):
        svc = object.__new__(PropertyService)
        svc.data = {'person_ids': list(range(1, 22))}
        with self.app.app_context():
            with self.assertRaises(Exception):
                svc.ids('person_ids')


if __name__ == '__main__':
    unittest.main()
