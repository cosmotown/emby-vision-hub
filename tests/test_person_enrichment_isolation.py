"""P0: optional Person refusal must not become a media-ingest failure."""
import copy
import ast
import logging
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from typing import List, Optional
from unittest.mock import MagicMock, patch

import psycopg2
import core_processor as core
from services.person_enrichment import (
    OPTIONAL_PERSON_DENIALS, PersonEnrichmentHold, optional_person_gate,
    persist_person_hold,
)


def webhook_functions():
    # Compile the real queue functions without importing optional 115 SDKs;
    # some SDK versions perform network access during import, even in tests.
    source = Path(core.__file__).parent / 'routes/webhook.py'
    nodes = [node for node in ast.parse(source.read_text()).body
             if isinstance(node, ast.FunctionDef) and node.name in {
                 '_handle_full_processing_flow', '_execute_persisted_webhook_event'}]
    namespace = dict(Optional=Optional, List=List, emby=core.emby,
                     logger=logging.getLogger('webhook-isolation-fixture'),
                     telegram=SimpleNamespace(send_media_notification=MagicMock()),
                     webhook_event_db=SimpleNamespace(mark_completed=MagicMock(), mark_failed=MagicMock()),
                     _schedule_persistent_webhook_drain=MagicMock(), spawn=MagicMock())
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
    return SimpleNamespace(**namespace)


class PersonEnrichmentIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.env = patch.dict(os.environ, APP_DATA_DIR=self.temporary.name)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.processor = core.MediaProcessor.__new__(core.MediaProcessor)
        p = self.processor
        p.config = {}
        p.emby_url, p.emby_api_key, p.emby_user_id = 'http://fixture', 'secret', 'u'
        p.tmdb_api_key = 'fixture'
        p.local_data_path = self.temporary.name
        p.ai_translator = None
        p.processed_items_cache = {}
        p.actor_db_manager = MagicMock()
        p.log_db_manager = MagicMock()
        p.is_stop_requested = lambda: False
        p.get_stop_event = lambda: None
        p._get_valid_local_data_path = lambda *args: self.temporary.name
        p._get_douban_data_with_local_cache = MagicMock(return_value=([], None))
        p._process_cast_list = MagicMock(return_value=[])
        p._sync_douban_only_cast_to_emby = MagicMock()
        p._refresh_emby_after_metadata_sync = MagicMock(return_value=True)
        p.sync_item_images = MagicMock(return_value=True)
        p._upsert_media_metadata = MagicMock()
        self.person = {'Id': 'p1', 'Type': 'Person', 'Name': '张三',
                       'ProviderIds': {'Tmdb': '100'}}
        self.source = dict(Id='media', Type='Movie', Name='Fixture',
                           _SourceLibraryId='fixture-library',
                           ProviderIds={'Tmdb': '123'},
                           People=[dict(self.person, Type='Actor')],
                           MediaSources=[{'MediaStreams': [
                               {'Type': 'Video', 'Width': 1280, 'Height': 720, 'Codec': 'h264'}]}])
        self.db = MagicMock()
        self.db.__enter__.return_value = self.db
        self.db.cursor.return_value.fetchall.return_value = []
        for mocker in (
            patch('core_processor.get_central_db_connection', return_value=self.db),
            patch.object(core.emby, 'get_emby_item_details',
                         side_effect=lambda i, *a, **kw: copy.deepcopy(self.source if i == 'media' else self.person)),
            patch.object(core.emby, 'read_person_identity_batch', return_value={'p1': self.person}),
            patch.object(core.emby, 'get_library_root_for_item', return_value={'Id': 'fixture-library'}),
            patch.object(core.emby, 'resolve_exact_person_identity', return_value=[self.person]),
            patch.object(core.emby, 'resolve_frozen_person_target', return_value=[self.person]),
            patch.object(core.emby, 'is_person_name_write_safe', return_value=True),
            patch.object(core.tmdb, 'get_movie_details', return_value={
                'id': 123, 'title': 'Fixture', 'credits': {'cast': [], 'crew': []}}),
            patch.object(core.tmdb, 'aggregate_full_series_data_from_tmdb', return_value={
                'series_details': {'id': 123, 'name': 'Fixture', 'credits': {'cast': []}},
                'episodes_details': {}}),
            patch.object(core.tmdb, 'get_tv_details', return_value={'credits': {'cast': []}}),
            patch.object(core.logger, 'trace', create=True),
            patch.object(logging.Logger, 'trace', logging.Logger.debug, create=True),
            patch.object(core.settings_db, 'get_setting', return_value=None),
            patch('actor_utils.evaluate_cast_processing_quality', return_value=10),
        ):
            mocker.start()
            self.addCleanup(mocker.stop)

    def run_core(self, episodes=None):
        return self.processor._process_item_core_logic(
            self.source, force_full_update=True, specific_episode_ids=episodes)

    def warnings(self):
        return list((Path(self.temporary.name) / 'person-governance' / 'media-holds').glob('*.json'))

    def test_exact_identity_normal_success(self):
        self.assertTrue(self.run_core())
        self.processor._refresh_emby_after_metadata_sync.assert_called_once()
        self.processor._upsert_media_metadata.assert_called_once()
        self.assertFalse(self.warnings())

    def test_real_name_collision_gate_holds_but_media_commits_and_keeps_people(self):
        before = copy.deepcopy(self.source)
        with patch.object(core.emby, 'is_person_name_write_safe', return_value=False), \
             patch.object(core.emby.emby_client, 'post') as post:
            self.assertTrue(self.run_core())
        self.processor._upsert_media_metadata.assert_called_once()
        self.processor._refresh_emby_after_metadata_sync.assert_not_called()
        self.processor._sync_douban_only_cast_to_emby.assert_not_called()
        self.processor.log_db_manager.mark_assets_as_synced.assert_not_called()
        self.assertFalse(list(Path(self.temporary.name).rglob('all.json')))
        self.assertEqual(self.source, before)
        post.assert_not_called()
        record = json.loads(self.warnings()[0].read_text())
        self.assertEqual(record['person_id'], 'p1')
        self.assertEqual(record['reason'], 'person_name_collision_or_unknown')
        self.assertEqual(record['person_enrichment'], 'HOLD')
        self.assertNotIn('secret', self.warnings()[0].read_text())

    def test_missing_person_provider_holds_without_clearing_native_people(self):
        self.person['ProviderIds'] = {}
        before = copy.deepcopy(self.source['People'])
        self.assertTrue(self.run_core())
        self.assertEqual(self.source['People'], before)
        self.assertEqual(json.loads(self.warnings()[0].read_text())['reason'], 'identity_missing')
        self.processor._upsert_media_metadata.assert_called_once()

    def test_same_name_different_tmdb_no_cross_write_and_media_success(self):
        second = dict(self.person, Id='p2', ProviderIds={'Tmdb': '200'})
        self.source['People'].append(dict(second, Type='Actor'))
        objects = {'media': self.source, 'p1': self.person, 'p2': second}
        before = copy.deepcopy(objects)
        with patch.object(core.emby, 'get_emby_item_details',
                          side_effect=lambda i, *a, **k: copy.deepcopy(objects[i])), \
             patch.object(core.emby, 'read_person_identity_batch', return_value=objects), \
             patch.object(core.emby.emby_client, 'post') as post, \
             patch.object(core.emby.emby_client, 'post_once') as post_once:
            self.assertTrue(self.run_core())
        self.assertEqual(objects, before)
        post.assert_not_called()
        post_once.assert_not_called()
        self.assertEqual(json.loads(self.warnings()[0].read_text())['reason'], 'metadata_name_collision')

    def test_known_denials_do_not_fail_media_or_refresh_stale_people(self):
        for reason in OPTIONAL_PERSON_DENIALS:
            with self.subTest(reason=reason), patch.object(
                self.processor, 'sync_item_metadata', side_effect=PersonEnrichmentHold(reason)
            ):
                self.assertTrue(self.run_core())
        self.processor._refresh_emby_after_metadata_sync.assert_not_called()

    def test_unknown_value_error_is_not_optional(self):
        with patch.object(self.processor, 'sync_item_metadata', side_effect=ValueError('broken_media')):
            self.assertFalse(self.run_core())
        self.processor._upsert_media_metadata.assert_not_called()

    def test_value_error_string_outside_person_gate_is_not_optional(self):
        with patch.object(self.processor, 'sync_item_metadata', side_effect=ValueError('identity_missing')):
            self.assertFalse(self.run_core())

    def test_media_sql_failure_after_person_hold_is_failed(self):
        with patch.object(core.emby, 'is_person_name_write_safe', return_value=False):
            self.processor._upsert_media_metadata.side_effect = psycopg2.DatabaseError('fixture-db-failure')
            self.assertFalse(self.run_core())

    def test_commit_failure_is_failed_not_success_with_warning(self):
        with patch.object(core.emby, 'is_person_name_write_safe', return_value=False):
            self.db.commit.side_effect = psycopg2.DatabaseError('fixture-commit-failure')
            self.assertFalse(self.run_core())
        self.assertNotIn('media', self.processor.processed_items_cache)

    def test_hold_persistence_failure_is_not_swallowed(self):
        with patch.object(core.emby, 'is_person_name_write_safe', return_value=False), \
             patch('core_processor.persist_person_hold', side_effect=OSError('disk-full')):
            self.assertFalse(self.run_core())
        self.processor._upsert_media_metadata.assert_not_called()

    def test_media_missing_is_failed(self):
        with patch.object(core.emby, 'get_emby_item_details', return_value=None):
            self.assertFalse(self.processor.process_single_item('missing'))

    def test_six_episode_update_continues_after_series_person_hold(self):
        self.source['Type'] = 'Series'
        episodes = [f'e{i}' for i in range(1, 7)]
        with patch.object(core.emby, 'is_person_name_write_safe', return_value=False):
            self.assertTrue(self.run_core(episodes))
        self.assertEqual(self.processor._upsert_media_metadata.call_args.kwargs['item_type'], 'Series')
        self.processor._refresh_emby_after_metadata_sync.assert_not_called()

    def test_batch_identity_unavailable_is_person_hold_not_media_failure(self):
        with patch.object(core.emby, 'read_person_identity_batch', return_value=None):
            self.assertTrue(self.run_core())
        self.processor._process_cast_list.assert_not_called()
        self.processor._upsert_media_metadata.assert_called_once()

    def test_warning_replay_uses_same_file_no_growth(self):
        with patch.object(core.emby, 'is_person_name_write_safe', return_value=False):
            self.assertTrue(self.run_core())
            self.assertTrue(self.run_core())
        self.assertEqual(len(self.warnings()), 1)
        self.assertEqual(self.processor._upsert_media_metadata.call_count, 2)

    def test_scope_boundary_does_not_catch_database_or_unknown_error(self):
        for error in [ValueError('unknown'), psycopg2.DatabaseError('identity_missing'),
                      OSError('identity_missing')]:
            def fail():
                raise error
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                optional_person_gate(fail)()

    def test_media_source_missing_or_type_changed_not_held(self):
        for error in ['source_item_unavailable', 'source_item_type_changed',
                      'operation_state_invalid', 'source_item_missing']:
            def fail():
                raise ValueError(error)
            with self.assertRaises(ValueError) as caught:
                optional_person_gate(fail)()
            self.assertNotIsInstance(caught.exception, PersonEnrichmentHold)

    def test_absent_people_on_present_media_is_optional_but_missing_media_is_not(self):
        p = self.processor
        with patch.object(core.emby, 'get_emby_item_details', return_value={'Id': 'media', 'Type': 'Movie'}):
            with self.assertRaises(PersonEnrichmentHold):
                p._prepare_person_metadata_cast([], self.source)
        with patch.object(core.emby, 'get_emby_item_details', return_value=None):
            with self.assertRaises(ValueError) as caught:
                p._prepare_person_metadata_cast([], self.source)
        self.assertNotIsInstance(caught.exception, PersonEnrichmentHold)
        with patch.object(core.emby, 'get_emby_item_details', return_value={'Id': 'media', 'Type': 'Person'}):
            with self.assertRaisesRegex(ValueError, 'source_item_type_changed'):
                p._prepare_person_metadata_cast([], self.source)

    def test_persisted_webhook_completes_after_core_hold_not_before(self):
        webhook = webhook_functions()
        with patch.object(core.emby, 'is_person_name_write_safe', return_value=False), \
             patch.object(webhook, '_schedule_persistent_webhook_drain'), \
             patch.object(webhook.telegram, 'send_media_notification'), \
             patch.object(webhook.webhook_event_db, 'mark_completed') as complete, \
             patch.object(webhook.webhook_event_db, 'mark_failed') as fail, \
             patch.object(webhook.logger, 'trace', create=True):
            complete.side_effect = lambda event: self.processor._upsert_media_metadata.assert_called_once()
            webhook._execute_persisted_webhook_event(self.processor, 1319, 'media_process', {
                'item_id': 'media', 'force_full_update': True, 'is_new_item': False})
        complete.assert_called_once_with(1319)
        fail.assert_not_called()
        self.assertEqual(len(self.warnings()), 1)

    def test_persisted_webhook_core_error_is_failed(self):
        webhook = webhook_functions()
        for error in [ValueError('missing_media'), psycopg2.DatabaseError('transaction_failed')]:
            with self.subTest(error=type(error).__name__), \
                 patch.object(self.processor, '_upsert_media_metadata', side_effect=error), \
                 patch.object(core.emby, 'is_person_name_write_safe', return_value=False), \
                 patch.object(webhook, '_schedule_persistent_webhook_drain'), \
                 patch.object(webhook.webhook_event_db, 'mark_completed') as complete, \
                 patch.object(webhook.webhook_event_db, 'mark_failed') as fail, \
                 patch.object(webhook.logger, 'trace', create=True), \
                 self.assertRaises(RuntimeError):
                webhook._execute_persisted_webhook_event(self.processor, 1319, 'media_process', {
                    'item_id': 'media', 'force_full_update': True, 'is_new_item': False})
            complete.assert_not_called()
            fail.assert_called_once()

    def test_metadata_update_all_media_types_still_sync_after_person_hold(self):
        from tasks.media import task_sync_all_metadata
        for kind in ('Movie', 'Series', 'Season', 'Episode'):
            self.source['Type'] = kind
            with self.subTest(kind=kind), patch.object(
                self.processor, 'sync_emby_updates_to_override_files',
            ) as override, patch.object(self.processor, 'sync_single_item_to_metadata_cache') as sync:
                task_sync_all_metadata(self.processor, 'media', 'Fixture')
            override.assert_called_once()
            sync.assert_called_once()

    def test_metadata_file_hold_preserves_existing_bytes_and_native_people(self):
        folder = Path(self.temporary.name) / 'override/tmdb-movies2/123'
        folder.mkdir(parents=True)
        path = folder / 'all.json'
        original = b'{"title":"Original", "credits":{"cast":[{"id":100,"name":"Old"}]}}'
        path.write_bytes(original)
        self.source['Overview'] = 'Core metadata is independently mirrored'
        with patch.object(core.emby, 'is_person_name_write_safe', return_value=False):
            self.processor.sync_emby_updates_to_override_files(self.source)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(self.warnings()), 1)

    def test_metadata_cache_db_failure_propagates(self):
        self.db.cursor.return_value.__enter__.return_value.execute.side_effect = psycopg2.DatabaseError('broken')
        with self.assertRaises(psycopg2.DatabaseError):
            self.processor.sync_single_item_to_metadata_cache('media')

    def test_failed_refresh_cannot_report_media_success(self):
        self.processor._refresh_emby_after_metadata_sync.return_value = False
        self.assertFalse(self.run_core())

    @unittest.skipUnless(os.environ.get('EVH_TEST_POSTGRES_HOST'), 'isolated PostgreSQL required')
    def test_real_pg_six_episodes_failed_event_replay_is_idempotent(self):
        import config_manager
        import constants
        from database import webhook_event_db, queries_db
        from database.connection import get_db_connection, init_db
        config = {
            constants.CONFIG_OPTION_DB_HOST: os.environ['EVH_TEST_POSTGRES_HOST'],
            constants.CONFIG_OPTION_DB_PORT: int(os.environ.get('EVH_TEST_POSTGRES_PORT', '5432')),
            constants.CONFIG_OPTION_DB_USER: os.environ['EVH_TEST_POSTGRES_USER'],
            constants.CONFIG_OPTION_DB_PASSWORD: os.environ['EVH_TEST_POSTGRES_PASSWORD'],
            constants.CONFIG_OPTION_DB_NAME: os.environ['EVH_TEST_POSTGRES_DB'],
        }
        with patch.dict(config_manager.APP_CONFIG, config):
            init_db()
            self.source.update(Type='Series', Path='/test/P0', DateCreated='2026-10-01T00:00:00Z')
            versions = [dict(Id=f'e{i}', Type='Episode', IndexNumber=i, ParentIndexNumber=1,
                             SeriesId='media', Path=f'/test/P0/E{i}.mp4',
                             DateCreated='2026-10-18T09:00:00Z',
                             MediaStreams=[{'Type': 'Video', 'Width': 1280, 'Height': 720, 'Codec': 'h264'}])
                        for i in range(1, 7)]
            aggregate = dict(series_details={'id': 123, 'name': 'Fixture', 'credits': {'cast': []},
                                             'first_air_date': '2026-10-01', 'last_air_date': '2026-10-18'},
                             seasons_details=[{'id': 1231, 'season_number': 1, 'name': 'Season 1'}],
                             episodes_details={f'S1E{i}': {'id': 12300+i, 'season_number': 1,
                                 'episode_number': i, 'name': f'E{i}'} for i in range(1, 7)})
            self.processor._get_realtime_ancestor_context = MagicMock(return_value=({}, None))
            payload = dict(item_id='media', force_full_update=True, is_new_item=False,
                           new_episode_ids=[ep['Id'] for ep in versions])
            event, _ = webhook_event_db.enqueue_event(dedupe_key='fixture:p0:media',
                task_kind='media_process', task_name='P0 six episodes', payload=payload,
                item_id='media', max_attempts=1)
            self.assertEqual(webhook_event_db.claim_next_event()['id'], event)
            webhook_event_db.mark_failed(event, 'person_metadata_gate_denied')
            self.assertTrue(webhook_event_db.retry_event(event))
            self.assertFalse(webhook_event_db.retry_event(event))
            webhook = webhook_functions()
            webhook._execute_persisted_webhook_event.__globals__['webhook_event_db'] = webhook_event_db
            with get_db_connection() as conn, conn.cursor() as cursor:
                cursor.execute("""INSERT INTO media_metadata (tmdb_id,item_type,actors_json,directors_json)
                    VALUES ('123','Series','[{"tmdb_id":888}]','[{"id":999}]')
                    ON CONFLICT (tmdb_id,item_type) DO UPDATE
                    SET actors_json=EXCLUDED.actors_json, directors_json=EXCLUDED.directors_json""")
            with patch('core_processor.get_central_db_connection', get_db_connection), \
                 patch.object(core.emby, 'is_person_name_write_safe', return_value=False), \
                 patch.object(core.emby, 'get_series_seasons', return_value=[
                     {'Id': 's1', 'Type': 'Season', 'IndexNumber': 1}]), \
                 patch.object(core.emby, 'get_all_library_versions', return_value=versions), \
                 patch.object(core.tmdb, 'aggregate_full_series_data_from_tmdb', return_value=aggregate), \
                 patch.object(self.processor, '_upsert_media_metadata',
                     core.MediaProcessor._upsert_media_metadata.__get__(self.processor)):
                for _ in range(2):
                    webhook._execute_persisted_webhook_event(self.processor, event, 'media_process', payload)
            with get_db_connection() as conn, conn.cursor() as cursor:
                cursor.execute("SELECT item_type, count(*) AS n FROM media_metadata WHERE tmdb_id IN ('123','1231','12301','12302','12303','12304','12305','12306') GROUP BY item_type")
                counts = {row['item_type']: row['n'] for row in cursor.fetchall()}
                self.assertEqual(counts, {'Series': 1, 'Season': 1, 'Episode': 6})
                cursor.execute('SELECT status FROM webhook_event_queue WHERE id=%s', (event,))
                self.assertEqual(cursor.fetchone()['status'], 'completed')
                cursor.execute("SELECT actors_json,directors_json FROM media_metadata WHERE tmdb_id='123' AND item_type='Series'")
                cached_people = cursor.fetchone()
                self.assertEqual(cached_people['actors_json'], [{'tmdb_id': 888}])
                self.assertEqual(cached_people['directors_json'], [{'id': 999}])
                cursor.execute("SELECT emby_item_ids_json FROM media_metadata WHERE parent_series_tmdb_id='123' AND item_type='Episode'")
                self.assertEqual({row['emby_item_ids_json'][0] for row in cursor.fetchall()}, {e['Id'] for e in versions})
            items, _ = queries_db.query_virtual_library_items(
                rules=[], logic='AND', user_id=None, limit=50, offset=0,
                sort_by='DateLastContentAdded', sort_order='Descending',
                item_types=['Movie', 'Series'], use_effective_recent_at=True)
            self.assertTrue(any(str(item.get('Id')) == 'media' for item in items))


if __name__ == '__main__':
    unittest.main()
