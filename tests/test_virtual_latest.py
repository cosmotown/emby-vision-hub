import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import config_manager  # Initialize settings before importing the query module.
from database import queries_db
import reverse_proxy


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params):
        self.executed.append((sql, params))

    def fetchone(self):
        return {'count': len(self.rows)}

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor


class VirtualLatestQueryTests(unittest.TestCase):
    def test_series_latest_sort_uses_newest_episode_before_limit(self):
        newest = datetime(2026, 8, 16, tzinfo=timezone.utc)
        cursor = _Cursor([{'emby_id': 'old-series', 'tmdb_id': '100', 'latest_sort_at': newest}])
        with patch.object(queries_db, 'get_db_connection', return_value=_Connection(cursor)):
            items, total = queries_db.query_virtual_library_items(
                rules=[],
                logic='AND',
                user_id=None,
                limit=20,
                item_types=['Series'],
                sort_by='DateLastContentAdded',
                sort_order='Descending',
            )

        query_sql = cursor.executed[-1][0]
        self.assertIn('SELECT MAX(ep.date_added)', query_sql)
        self.assertIn("ep.parent_series_tmdb_id = m.tmdb_id", query_sql)
        self.assertLess(query_sql.index('ORDER BY'), query_sql.index('LIMIT'))
        self.assertEqual(items[0]['Id'], 'old-series')
        self.assertEqual(items[0]['latest_sort_at'], newest)
        self.assertEqual(total, 1)


class VirtualLatestRouteTests(unittest.TestCase):
    def setUp(self):
        self.config = patch.dict(
            reverse_proxy.config_manager.APP_CONFIG,
            {'emby_server_url': 'http://isolated-emby:8096', 'emby_api_key': 'redacted'},
            clear=False,
        )
        self.config.start()

    def tearDown(self):
        self.config.stop()

    def test_single_virtual_series_preserves_database_latest_order(self):
        collection = {
            'type': 'filter',
            'definition_json': {'show_in_latest': True, 'item_type': ['Series'], 'rules': []},
        }
        ranked = [
            {'Id': 'old-parent-new-episode', 'tmdb_id': '1', 'latest_sort_at': '2026-08-16T10:00:00Z'},
            {'Id': 'new-parent-old-episode', 'tmdb_id': '2', 'latest_sort_at': '2026-08-15T10:00:00Z'},
        ]
        details = [
            {'Id': 'new-parent-old-episode', 'Name': 'second'},
            {'Id': 'old-parent-new-episode', 'Name': 'first'},
        ]
        with patch.object(reverse_proxy.custom_collection_db, 'get_custom_collection_by_id', return_value=collection), \
             patch.object(reverse_proxy.queries_db, 'query_virtual_library_items', return_value=(ranked, 2)) as query, \
             patch.object(reverse_proxy, '_fetch_items_in_chunks', return_value=details):
            response = reverse_proxy.handle_get_latest_items(
                'user-1',
                {'ParentId': reverse_proxy.to_mimicked_id(7), 'Limit': '2'},
            )

        self.assertEqual([item['Id'] for item in response.get_json()], [
            'old-parent-new-episode',
            'new-parent-old-episode',
        ])
        self.assertEqual(query.call_args.kwargs['sort_by'], 'DateLastContentAdded')

    def test_global_latest_deduplicates_and_uses_latest_content_timestamp(self):
        collections = {
            1: {'type': 'filter', 'definition_json': {'item_type': ['Series'], 'rules': []}},
            2: {'type': 'filter', 'definition_json': {'item_type': ['Movie', 'Series'], 'rules': []}},
        }
        query_results = [
            ([{'Id': 'series-a', 'latest_sort_at': '2026-08-16T10:00:00Z'}], 1),
            ([
                {'Id': 'series-a', 'latest_sort_at': '2026-08-15T10:00:00Z'},
                {'Id': 'movie-b', 'latest_sort_at': '2026-08-16T09:00:00Z'},
            ], 2),
        ]
        with patch.object(reverse_proxy.custom_collection_db, 'get_active_collection_ids_for_latest_view', return_value=[1, 2]), \
             patch.object(reverse_proxy.custom_collection_db, 'get_custom_collection_by_id', side_effect=lambda value: collections[value]), \
             patch.object(reverse_proxy.queries_db, 'query_virtual_library_items', side_effect=query_results) as query, \
             patch.object(reverse_proxy, '_fetch_items_in_chunks', return_value=[
                 {'Id': 'movie-b'}, {'Id': 'series-a'},
             ]):
            response = reverse_proxy.handle_get_latest_items('user-1', {'Limit': '2'})

        self.assertEqual([item['Id'] for item in response.get_json()], ['series-a', 'movie-b'])
        self.assertTrue(all(call.kwargs['sort_by'] == 'DateLastContentAdded' for call in query.call_args_list))

    def test_global_latest_applies_ascending_order_before_pagination(self):
        collection = {'type': 'filter', 'definition_json': {'item_type': ['Movie', 'Series'], 'rules': []}}
        ranked = [
            {'Id': 'oldest', 'latest_sort_at': '2026-08-14T10:00:00Z'},
            {'Id': 'middle', 'latest_sort_at': '2026-08-15T10:00:00Z'},
            {'Id': 'newest', 'latest_sort_at': '2026-08-16T10:00:00Z'},
        ]
        with patch.object(reverse_proxy.custom_collection_db, 'get_active_collection_ids_for_latest_view', return_value=[1]), \
             patch.object(reverse_proxy.custom_collection_db, 'get_custom_collection_by_id', return_value=collection), \
             patch.object(reverse_proxy.queries_db, 'query_virtual_library_items', return_value=(ranked, 3)) as query, \
             patch.object(reverse_proxy, '_fetch_items_in_chunks', return_value=[{'Id': 'middle'}]):
            response = reverse_proxy.handle_get_latest_items(
                'user-1',
                {'Limit': '1', 'StartIndex': '1', 'SortOrder': 'Ascending'},
            )
        self.assertEqual([item['Id'] for item in response.get_json()], ['middle'])
        self.assertEqual(query.call_args.kwargs['limit'], 2)
        self.assertEqual(query.call_args.kwargs['sort_order'], 'Ascending')


if __name__ == '__main__':
    unittest.main()
