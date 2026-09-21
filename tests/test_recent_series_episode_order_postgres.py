import json
import logging
import os
import unittest
from datetime import datetime, timedelta, timezone

import config_manager
import constants
from database import queries_db
from database.connection import (
    _EPISODE_DATE_ADDED_BACKFILL_KEY,
    backfill_episode_date_added,
    get_db_connection,
    init_db,
)


POSTGRES_HOST = os.environ.get('EVH_TEST_POSTGRES_HOST')
if not hasattr(logging.Logger, 'trace'):
    logging.Logger.trace = logging.Logger.debug


@unittest.skipUnless(POSTGRES_HOST, 'isolated PostgreSQL is not configured')
class RecentSeriesEpisodeOrderPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config_manager.APP_CONFIG.update({
            constants.CONFIG_OPTION_DB_HOST: POSTGRES_HOST,
            constants.CONFIG_OPTION_DB_PORT: int(os.environ.get('EVH_TEST_POSTGRES_PORT', '5432')),
            constants.CONFIG_OPTION_DB_USER: os.environ.get('EVH_TEST_POSTGRES_USER', 'evh_test'),
            constants.CONFIG_OPTION_DB_PASSWORD: os.environ.get('EVH_TEST_POSTGRES_PASSWORD', 'evh_test'),
            constants.CONFIG_OPTION_DB_NAME: os.environ.get('EVH_TEST_POSTGRES_DB', 'evh_test'),
        })
        init_db()

    def setUp(self):
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute('TRUNCATE TABLE media_metadata CASCADE')

    def _insert_item(
        self,
        cursor,
        tmdb_id,
        item_type,
        emby_id,
        date_added,
        parent_series_tmdb_id=None,
        in_library=True,
        asset_details=None,
    ):
        cursor.execute("""
            INSERT INTO media_metadata (
                tmdb_id, item_type, title, in_library, emby_item_ids_json,
                date_added, parent_series_tmdb_id, asset_details_json
            ) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb)
        """, (
            str(tmdb_id), item_type, f'{item_type}-{tmdb_id}', in_library,
            json.dumps([str(emby_id)]), date_added, parent_series_tmdb_id,
            json.dumps(asset_details or []),
        ))

    def _query_recent(self, limit=50, offset=0, days=30):
        return queries_db.query_virtual_library_items(
            rules=[{'field': 'date_added', 'operator': 'in_last_days', 'value': days}],
            logic='AND',
            user_id=None,
            limit=limit,
            offset=offset,
            sort_by='DateLastContentAdded',
            sort_order='Descending',
            item_types=['Movie', 'Series'],
            use_effective_recent_at=True,
        )

    def test_new_episode_lifts_old_series_and_keeps_one_parent_row(self):
        now = datetime.now(timezone.utc)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                self._insert_item(cursor, 'series-a', 'Series', 'series-a-emby', now - timedelta(days=20))
                self._insert_item(cursor, 'movie-b', 'Movie', 'movie-b-emby', now - timedelta(days=2))
                self._insert_item(cursor, 'ep-a1', 'Episode', 'episode-a1', now - timedelta(days=3), 'series-a')
                self._insert_item(cursor, 'ep-a2', 'Episode', 'episode-a2', now - timedelta(hours=1), 'series-a')

        items, total = self._query_recent()
        self.assertEqual(total, 2)
        self.assertEqual([item['Id'] for item in items], ['series-a-emby', 'movie-b-emby'])
        self.assertEqual(items[0]['recent_time_source'], 'episode:episode-a2')
        self.assertEqual(sum(item['Id'] == 'series-a-emby' for item in items), 1)

    def test_new_season_episode_includes_old_series_inside_existing_window(self):
        now = datetime.now(timezone.utc)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                self._insert_item(cursor, 'series-old', 'Series', 'series-old-emby', now - timedelta(days=90))
                self._insert_item(cursor, 'season-new', 'Season', 'season-new-emby', now - timedelta(hours=2), 'series-old')
                self._insert_item(cursor, 'episode-new', 'Episode', 'episode-new-emby', now - timedelta(hours=1), 'series-old')

        items, total = self._query_recent(days=30)
        self.assertEqual(total, 1)
        self.assertEqual([item['Id'] for item in items], ['series-old-emby'])

    def test_metadata_refresh_does_not_bump_series(self):
        now = datetime.now(timezone.utc)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                self._insert_item(cursor, 'series-a', 'Series', 'series-a-emby', now - timedelta(days=10))
                self._insert_item(cursor, 'ep-a1', 'Episode', 'episode-a1', now - timedelta(days=9), 'series-a')
                self._insert_item(cursor, 'movie-b', 'Movie', 'movie-b-emby', now - timedelta(days=2))
                cursor.execute("""
                    UPDATE media_metadata
                    SET last_updated_at = NOW(), title = 'metadata refreshed'
                    WHERE tmdb_id IN ('series-a', 'ep-a1')
                """)

        items, _ = self._query_recent()
        self.assertEqual([item['Id'] for item in items], ['movie-b-emby', 'series-a-emby'])

    def test_deleted_latest_episode_falls_back_to_remaining_episode_then_series(self):
        now = datetime.now(timezone.utc)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                self._insert_item(cursor, 'series-a', 'Series', 'series-a-emby', now - timedelta(days=20))
                self._insert_item(cursor, 'ep-a1', 'Episode', 'episode-a1', now - timedelta(days=5), 'series-a')
                self._insert_item(cursor, 'ep-a2', 'Episode', 'episode-a2', now - timedelta(hours=1), 'series-a')
                self._insert_item(cursor, 'movie-b', 'Movie', 'movie-b-emby', now - timedelta(days=2))
                cursor.execute("""
                    UPDATE media_metadata SET in_library = FALSE
                    WHERE tmdb_id = 'ep-a2' AND item_type = 'Episode'
                """)

        items, _ = self._query_recent()
        self.assertEqual([item['Id'] for item in items], ['movie-b-emby', 'series-a-emby'])
        series = next(item for item in items if item['Id'] == 'series-a-emby')
        self.assertEqual(series['recent_time_source'], 'episode:episode-a1')

        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    UPDATE media_metadata SET in_library = FALSE
                    WHERE tmdb_id = 'ep-a1' AND item_type = 'Episode'
                """)
        items, _ = self._query_recent(days=15)
        self.assertEqual([item['Id'] for item in items], ['movie-b-emby'])

    def test_pagination_is_stable_and_movie_semantics_are_unchanged(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                self._insert_item(cursor, 'movie-a', 'Movie', 'movie-a-emby', now - timedelta(days=1))
                self._insert_item(cursor, 'movie-b', 'Movie', 'movie-b-emby', now - timedelta(days=2))
                self._insert_item(cursor, 'series-c', 'Series', 'series-c-emby', now - timedelta(days=3))

        page_one, total_one = self._query_recent(limit=2, offset=0)
        page_two, total_two = self._query_recent(limit=2, offset=2)
        self.assertEqual(total_one, 3)
        self.assertEqual(total_two, 3)
        self.assertEqual(
            [item['Id'] for item in page_one + page_two],
            ['movie-a-emby', 'movie-b-emby', 'series-c-emby'],
        )
        self.assertTrue(all(item['recent_time_source'] == item['Id'].split('-')[0] for item in page_one))

    def test_legacy_episode_date_backfill_is_additive_and_idempotent(self):
        date_one = '2026-09-19T01:02:03.0000000Z'
        date_two = '2026-09-20T01:02:03Z'
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    'DELETE FROM app_settings WHERE setting_key = %s',
                    (_EPISODE_DATE_ADDED_BACKFILL_KEY,),
                )
                self._insert_item(
                    cursor, 'ep-legacy', 'Episode', 'episode-legacy', None, 'series-a',
                    asset_details=[
                        {'date_added_to_library': date_two},
                        {'date_added_to_library': date_one},
                    ],
                )
                self._insert_item(
                    cursor, 'ep-invalid', 'Episode', 'episode-invalid', None, 'series-a',
                    asset_details=[{'date_added_to_library': 'not-a-date'}],
                )
                self.assertEqual(backfill_episode_date_added(cursor), 1)
                self.assertEqual(backfill_episode_date_added(cursor), 0)
                cursor.execute("""
                    SELECT tmdb_id, date_added
                    FROM media_metadata
                    WHERE tmdb_id IN ('ep-legacy', 'ep-invalid')
                    ORDER BY tmdb_id
                """)
                rows = {row['tmdb_id']: row['date_added'] for row in cursor.fetchall()}

        self.assertIsNone(rows['ep-invalid'])
        self.assertEqual(rows['ep-legacy'], datetime(2026, 9, 19, 1, 2, 3, tzinfo=timezone.utc))


if __name__ == '__main__':
    unittest.main()
