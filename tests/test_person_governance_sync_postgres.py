"""Isolated PG integration. Only connection-local temporary fixture tables."""
import logging
import os
import unittest
from unittest.mock import patch

import psycopg2
from psycopg2.extras import RealDictCursor

# Initialize the application's configuration entry point before database modules.
# Full discovery already does this; standalone PG runs must do so as well.
import config_manager
from database.actor_db import ActorDBManager
from handler.actor_sync import UnifiedSyncHandler
from services.person_sync_safety import PersonResultScope
from tasks.maintenance import _merge_person_identity_map_data


@unittest.skipUnless(os.environ.get('EVH_PERSON_GOVERNANCE_TEST_HOST') or os.environ.get('EVH_TEST_POSTGRES_HOST'),
                     'isolated PostgreSQL fixture not configured')
class GovernancePostgresTests(unittest.TestCase):
    def setUp(self):
        dedicated = bool(os.environ.get('EVH_PERSON_GOVERNANCE_TEST_HOST'))
        self.conn = psycopg2.connect(
            host=os.environ.get('EVH_PERSON_GOVERNANCE_TEST_HOST') or os.environ['EVH_TEST_POSTGRES_HOST'],
            port=os.environ.get('EVH_TEST_POSTGRES_PORT', '5432'),
            user='evh_governance_test' if dedicated else os.environ['EVH_TEST_POSTGRES_USER'],
            password='isolated_fixture_only' if dedicated else os.environ['EVH_TEST_POSTGRES_PASSWORD'],
            dbname='evh_governance_test' if dedicated else os.environ['EVH_TEST_POSTGRES_DB'],
            cursor_factory=RealDictCursor)
        self.addCleanup(self.conn.close)
        self.cursor = self.conn.cursor()
        self.cursor.execute('''CREATE TEMP TABLE person_identity_map (
            map_id SERIAL PRIMARY KEY, primary_name TEXT, emby_person_id TEXT UNIQUE,
            tmdb_person_id INTEGER UNIQUE, imdb_id TEXT UNIQUE, douban_celebrity_id TEXT UNIQUE,
            last_updated_at TIMESTAMPTZ, last_synced_at TIMESTAMPTZ)''')
        self.cursor.execute('''CREATE TEMP TABLE actor_metadata (
            tmdb_id INTEGER PRIMARY KEY REFERENCES person_identity_map(tmdb_person_id) ON DELETE CASCADE)''')
        self.cursor.execute('''INSERT INTO person_identity_map
            (primary_name,emby_person_id,tmdb_person_id,douban_celebrity_id) VALUES ('existing','old',123,'456')''')
        self.cursor.execute('INSERT INTO actor_metadata (tmdb_id) VALUES (123)')
        self.conn.commit()
        self.trace = patch.object(logging.Logger, 'trace', logging.Logger.debug, create=True)
        self.trace.start()
        self.addCleanup(self.trace.stop)
        self.manager = ActorDBManager()

    def rows(self):
        self.cursor.execute('SELECT * FROM person_identity_map ORDER BY map_id')
        return [dict(r) for r in self.cursor.fetchall()]

    def upsert(self, **identity):
        return self.manager.upsert_person(self.cursor, {'name': 'incoming', **identity}, {})

    def test_conflicting_tmdb_douban_does_not_overwrite(self):
        before = self.rows()
        self.assertEqual(self.upsert(tmdb_id=123, douban_id='999')[1], 'CONFLICT_ERROR')
        self.assertEqual(self.rows(), before)

    def test_same_identity_different_emby_id_does_not_replace(self):
        before = self.rows()
        self.assertEqual(self.upsert(tmdb_id=123, emby_id='new')[1], 'CONFLICT_ERROR')
        self.assertEqual(self.rows(), before)

    def test_exact_identity_can_enrich_empty_id(self):
        self.assertEqual(self.upsert(tmdb_id=123, douban_id='456', emby_id='old', imdb_id='nm123')[1], 'UPDATED')
        row = self.rows()[0]
        self.assertEqual((row['emby_person_id'], row['douban_celebrity_id'], row['imdb_id']), ('old', '456', 'nm123'))

    def test_import_cannot_merge_two_rows_or_cascade_metadata(self):
        self.cursor.execute("INSERT INTO person_identity_map (primary_name,tmdb_person_id,douban_celebrity_id) VALUES ('second',789,'999')")
        before = self.rows()
        stats = _merge_person_identity_map_data(self.cursor, 'person_identity_map',
            ['tmdb_person_id', 'douban_celebrity_id'], [(123, '999')])
        self.assertEqual(stats['conflicts'], 1)
        self.assertEqual(stats['merged_and_deleted'], 0)
        self.assertEqual(self.rows(), before)
        self.cursor.execute('SELECT COUNT(*) AS n FROM actor_metadata')
        self.assertEqual(self.cursor.fetchone()['n'], 1)

    def test_partial_sync_preserves_existing_mapping(self):
        handler = UnifiedSyncHandler.__new__(UnifiedSyncHandler)
        handler.actor_db_manager = self.manager
        handler.emby_url, handler.emby_api_key, handler.emby_user_id = 'http://fixture.invalid', 'test', 'u'
        def pages(*args, **kwargs):
            state = kwargs['enumeration']
            state.scope = PersonResultScope.PARTIAL_PAGE
            yield [{'Id': 'new', 'Name': 'new', 'ProviderIds': {'Tmdb': '888'}}]
            state.fail('synthetic_second_page_timeout')
        with patch('handler.actor_sync.connection.get_db_connection', return_value=self.conn), \
             patch('handler.actor_sync.emby.get_all_persons_from_emby', side_effect=pages):
            handler.sync_emby_person_map_to_db()
        self.assertEqual({r['emby_person_id'] for r in self.rows()}, {'old', 'new'})


if __name__ == '__main__':
    unittest.main()
