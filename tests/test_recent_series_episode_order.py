import unittest
from unittest.mock import patch

import config_manager  # Initialize settings before importing application modules.
from tasks.helpers import get_logical_episode_date_created
import reverse_proxy


class EpisodeDateCreatedTests(unittest.TestCase):
    def test_logical_episode_uses_earliest_version_date_created(self):
        self.assertEqual(
            get_logical_episode_date_created([
                {'Id': 'new-version', 'DateCreated': '2026-09-21T12:00:00Z'},
                {'Id': 'original', 'DateCreated': '2026-09-20T08:00:00Z'},
            ]),
            '2026-09-20T08:00:00Z',
        )

    def test_metadata_modified_timestamp_is_not_an_add_time(self):
        self.assertIsNone(get_logical_episode_date_created([
            {'Id': 'episode', 'DateModified': '2026-09-21T12:00:00Z'},
        ]))


class RecentRouteContractTests(unittest.TestCase):
    def setUp(self):
        self.config = patch.dict(
            reverse_proxy.config_manager.APP_CONFIG,
            {'emby_server_url': 'http://isolated-emby:8096', 'emby_api_key': 'redacted'},
            clear=False,
        )
        self.config.start()

    def tearDown(self):
        self.config.stop()

    def test_latest_recent_enables_effective_date_filter(self):
        collection = {
            'type': 'filter',
            'definition_json': {
                'show_in_latest': True,
                'item_type': ['Movie', 'Series'],
                'rules': [
                    {'field': 'date_added', 'operator': 'in_last_days', 'value': 30},
                ],
            },
        }
        with patch.object(
            reverse_proxy.custom_collection_db,
            'get_custom_collection_by_id',
            return_value=collection,
        ), patch.object(
            reverse_proxy.queries_db,
            'query_virtual_library_items',
            return_value=([], 0),
        ) as query:
            response = reverse_proxy.handle_get_latest_items(
                'user-1',
                {'ParentId': reverse_proxy.to_mimicked_id(7), 'Limit': '20'},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(query.call_args.kwargs['use_effective_recent_at'])

    def test_latest_non_recent_does_not_change_date_filter_semantics(self):
        collection = {
            'type': 'filter',
            'definition_json': {
                'show_in_latest': True,
                'item_type': ['Series'],
                'rules': [],
            },
        }
        with patch.object(
            reverse_proxy.custom_collection_db,
            'get_custom_collection_by_id',
            return_value=collection,
        ), patch.object(
            reverse_proxy.queries_db,
            'query_virtual_library_items',
            return_value=([], 0),
        ) as query:
            reverse_proxy.handle_get_latest_items(
                'user-1',
                {'ParentId': reverse_proxy.to_mimicked_id(8), 'Limit': '20'},
            )

        self.assertFalse(query.call_args.kwargs['use_effective_recent_at'])


if __name__ == '__main__':
    unittest.main()
