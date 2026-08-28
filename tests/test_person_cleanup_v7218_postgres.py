import ast
import json
import logging
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import config_manager
import constants
import extensions
from handler import emby
from database import person_cleanup_db
from database.connection import get_db_connection, init_db
from services.person_cleanup_safety import (
    build_protected_library_root_contract,
    classify_reference_check,
)
from tasks import actors


POSTGRES_HOST = os.environ.get('EVH_TEST_POSTGRES_HOST')
if not hasattr(logging.Logger, 'trace'):
    logging.Logger.trace = logging.Logger.debug


@unittest.skipUnless(POSTGRES_HOST, 'isolated PostgreSQL is not configured')
class PersonCleanupV7218PostgresTests(unittest.TestCase):
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

    def _reset_database(self):
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    TRUNCATE TABLE
                        person_cleanup_job_items,
                        person_cleanup_jobs,
                        person_cleanup_delete_attempts,
                        person_cleanup_candidates,
                        person_cleanup_protected_aliases,
                        person_cleanup_protected_identities,
                        person_cleanup_protected_names,
                        person_cleanup_protected_people,
                        person_cleanup_protected_libraries,
                        person_cleanup_protection_state
                    CASCADE
                """)
                cursor.execute("""
                    INSERT INTO person_cleanup_protection_state (singleton)
                    VALUES (TRUE)
                """)

    def setUp(self):
        self._reset_database()

    def ready(self, library_ids=('lib-a',)):
        person_cleanup_db.replace_protected_libraries([
            {'library_id': library_id, 'library_name': library_id}
            for library_id in library_ids
        ])
        generation = person_cleanup_db.begin_protection_snapshot()
        person_cleanup_db.complete_protection_snapshot(generation)
        return generation

    @staticmethod
    def candidate(name='1田中', provider_ids=None):
        return {
            'person_id': '1020094',
            'person_name': name,
            'provider_ids_json': provider_ids or {},
        }

    def insert_candidate(self, name='1田中', provider_ids=None):
        person_cleanup_db.replace_candidates([{
            'Id': '1020094',
            'Name': name,
            'ProviderIds': provider_ids or {},
        }])

    def test_alias_persistence_restart_and_fingerprint_drift(self):
        self.ready()
        self.insert_candidate()
        candidate = person_cleanup_db.list_candidates_raw()[0]
        self.assertTrue(person_cleanup_db.persist_protected_alias_and_remove_candidate(
            candidate, 'lib-a', 'protected_library_alias', 'media-a',
        ))
        self.assertEqual(person_cleanup_db.list_candidates_raw(), [])
        self.assertEqual(len(person_cleanup_db.list_protected_aliases()), 1)

        init_db()
        contract = person_cleanup_db.get_protection_contract()
        self.assertEqual(
            person_cleanup_db.candidate_protection_reason(
                self.candidate('新名字', {'Tmdb': '999'}), contract,
            ),
            'protected_library_alias',
        )
        self.insert_candidate('新名字', {'Tmdb': '999'})
        self.assertEqual(person_cleanup_db.list_candidates_raw(), [])

    def test_alias_persist_and_candidate_removal_are_atomic(self):
        self.ready()
        self.insert_candidate()
        candidate = person_cleanup_db.list_candidates_raw()[0]
        with self.assertRaises(Exception):
            person_cleanup_db.persist_protected_alias_and_remove_candidate(
                candidate, 'missing-library',
                'protected_library_alias', 'media-a',
            )
        self.assertEqual(
            [row['person_id'] for row in person_cleanup_db.list_candidates_raw()],
            ['1020094'],
        )
        self.assertEqual(person_cleanup_db.list_protected_aliases(), [])

    def test_multi_library_alias_survives_one_library_removal_then_cascades(self):
        self.ready(('lib-a', 'lib-b'))
        self.insert_candidate()
        candidate = person_cleanup_db.list_candidates_raw()[0]
        person_cleanup_db.persist_protected_alias_and_remove_candidate(
            candidate, 'lib-a', 'protected_library_alias', 'media-a',
        )
        person_cleanup_db.persist_protected_alias_and_remove_candidate(
            candidate, 'lib-b', 'protected_library_unverifiable', 'media-b',
        )
        self.assertEqual(len(person_cleanup_db.list_protected_aliases()), 2)

        person_cleanup_db.replace_protected_libraries([
            {'library_id': 'lib-b', 'library_name': 'B'},
        ])
        aliases = person_cleanup_db.list_protected_aliases()
        self.assertEqual([(row['library_id'], row['person_id']) for row in aliases], [
            ('lib-b', '1020094'),
        ])
        self.insert_candidate('漂移姓名')
        self.assertEqual(person_cleanup_db.list_candidates_raw(), [])

        person_cleanup_db.replace_protected_libraries([])
        self.assertEqual(person_cleanup_db.list_protected_aliases(), [])
        self.insert_candidate('普通候选')
        self.assertEqual(
            [row['person_id'] for row in person_cleanup_db.list_candidates_raw()],
            ['1020094'],
        )

    def test_preview_status_counts_as_protected(self):
        generation = self.ready()
        candidate = self.candidate()
        job_id = person_cleanup_db.create_cleanup_job()
        person_cleanup_db.add_cleanup_job_item(
            job_id, candidate, 'protected_library_alias',
        )
        person_cleanup_db.finish_cleanup_preview(job_id, generation)
        job = person_cleanup_db.get_cleanup_job(job_id)
        self.assertEqual(job['protected_count'], 1)
        self.assertEqual(job['verified_orphan_count'], 0)

    def test_deterministic_http_db_route_preview_execute_legacy_chain(self):
        """Exercise the real HTTP handler and real PostgreSQL safety chain."""
        request_log = []

        class FixtureHandler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def _json(self, payload, status=200):
                body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                request_log.append(('GET', parsed.path, query))
                if parsed.path == '/Library/VirtualFolders':
                    self._json([{
                        'ItemId': 'lib-protected',
                        'Name': '保护库',
                        'Guid': 'fixture-library-guid',
                        'CollectionType': 'movies',
                        'Locations': ['/protected'],
                    }])
                    return
                if parsed.path == '/Items' and query.get('PersonIds') == ['B']:
                    self._json({'Items': [{
                        'Id': 'X',
                        'Name': '保护作品',
                        'Path': '/protected/movie/file.mkv',
                        'People': [{'Id': 'A', 'Name': '田中'}],
                    }]})
                    return
                if parsed.path == '/Items' and query.get('Ids') == ['X']:
                    self._json({'Items': [{
                        'Id': 'X',
                        'People': [{'Id': 'A', 'Name': '田中'}],
                    }]})
                    return
                self._json({'Items': []})

            def do_POST(self):
                parsed = urlparse(self.path)
                request_log.append(('POST', parsed.path, {}))
                self._json({'error': 'mutation forbidden in fixture'}, status=500)

        server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f'http://127.0.0.1:{server.server_port}'
        processor = SimpleNamespace(
            emby_url=base_url,
            emby_api_key='fixture-token',
            emby_user_id='fixture-user',
            is_stop_requested=lambda: False,
            get_stop_event=lambda: None,
        )

        def candidate():
            return {
                'person_id': 'B',
                'person_name': '1田中',
                'provider_ids_json': {},
            }

        def insert_candidate():
            person_cleanup_db.replace_candidates([{
                'Id': 'B', 'Name': '1田中', 'ProviderIds': {},
            }])

        try:
            # Route verify: real route body -> VirtualFolders HTTP -> PersonIds
            # HTTP -> ownership classifier -> one PostgreSQL transaction.
            generation = self.ready(('lib-protected',))
            insert_candidate()
            route_path = Path(__file__).resolve().parents[1] / 'routes' / 'person_cleanup.py'
            tree = ast.parse(route_path.read_text())
            build_node = next(
                node for node in tree.body
                if isinstance(node, ast.FunctionDef)
                and node.name == '_build_protected_root_contract'
            )
            verify_node = next(
                node for node in tree.body
                if isinstance(node, ast.FunctionDef)
                and node.name == 'verify_person_cleanup_candidate'
            )
            verify_node.decorator_list = []
            route_namespace = {
                'person_cleanup_db': person_cleanup_db,
                'emby': emby,
                'extensions': SimpleNamespace(
                    media_processor_instance=processor,
                    EMBY_SERVER_ID='',
                ),
                'build_protected_library_root_contract': build_protected_library_root_contract,
                'classify_reference_check': classify_reference_check,
                '_serialize_reference_items': lambda items: [],
                '_refreshed_candidate': lambda person_id, fallback: (
                    person_cleanup_db.get_candidates_by_ids(
                        [person_id], include_protected=True,
                    ) or [fallback]
                )[0],
                'config_manager': config_manager,
                'constants': constants,
                'jsonify': lambda payload: payload,
            }
            exec(compile(
                ast.Module(body=[build_node, verify_node], type_ignores=[]),
                str(route_path), 'exec',
            ), route_namespace)
            response = route_namespace['verify_person_cleanup_candidate']('B')
            self.assertEqual(response['status'], 'protected_library_alias')
            self.assertTrue(response['candidate_removed'])
            self.assertEqual(person_cleanup_db.list_candidates_raw(), [])
            self.assertEqual(len(person_cleanup_db.list_protected_aliases()), 1)

            # A process restart and a ghost rescan cannot regenerate B.
            init_db()
            insert_candidate()
            self.assertEqual(person_cleanup_db.list_candidates_raw(), [])

            # Preview uses the real handler, root contract and PostgreSQL writes.
            self._reset_database()
            generation = self.ready(('lib-protected',))
            insert_candidate()
            job_id = person_cleanup_db.create_cleanup_job()
            with patch.object(
                actors, '_refresh_protected_snapshot', return_value=(generation, {}),
            ), patch.object(
                actors.task_manager, 'update_status_from_thread',
            ), patch.object(
                actors.emby, 'delete_person_custom_api_outcome',
            ) as delete_call:
                actors.task_preview_safe_person_cleanup(processor, job_id)
            preview = person_cleanup_db.get_cleanup_job(job_id, include_items=True)
            self.assertEqual(preview['state'], 'preview_ready')
            self.assertEqual(preview['items'][0]['preview_state'], 'protected_library_alias')
            self.assertEqual(preview['verified_orphan_count'], 0)
            self.assertEqual(person_cleanup_db.list_candidates_raw(), [])
            delete_call.assert_not_called()

            # Execute remains defensive even if an old preview said orphan.
            self._reset_database()
            generation = self.ready(('lib-protected',))
            insert_candidate()
            job_id = person_cleanup_db.create_cleanup_job()
            person_cleanup_db.add_cleanup_job_item(job_id, candidate(), 'verified_orphan')
            person_cleanup_db.finish_cleanup_preview(job_id, generation)
            token = person_cleanup_db.issue_cleanup_confirmation_token(job_id)
            person_cleanup_db.confirm_cleanup_job(job_id, token)
            with patch.object(
                actors, '_refresh_protected_snapshot', return_value=(generation, {}),
            ), patch.object(
                actors.task_manager, 'update_status_from_thread',
            ), patch.object(
                actors.emby, 'delete_person_custom_api_outcome',
            ) as delete_call:
                actors.task_execute_safe_person_cleanup(processor, job_id)
            executed = person_cleanup_db.get_cleanup_job(job_id, include_items=True)
            self.assertEqual(executed['items'][0]['execute_state'], 'skipped_protected_library_alias')
            self.assertEqual(person_cleanup_db.list_candidates_raw(), [])
            delete_call.assert_not_called()

            # The legacy selected-delete path applies the same realtime guard.
            self._reset_database()
            generation = self.ready(('lib-protected',))
            insert_candidate()
            person_cleanup_db.mark_candidate_checked('B', 'orphan', generation)
            with patch.object(
                actors, '_refresh_protected_snapshot', return_value=(generation, {}),
            ), patch.object(
                actors.task_manager, 'update_status_from_thread',
            ), patch.object(
                actors.emby, 'delete_person_custom_api_outcome',
            ) as delete_call:
                actors.task_delete_selected_ghost_actors(processor, ['B'])
            self.assertEqual(person_cleanup_db.list_candidates_raw(), [])
            self.assertEqual(len(person_cleanup_db.list_protected_aliases()), 1)
            delete_call.assert_not_called()

            self.assertGreaterEqual(
                sum(1 for method, path, _query in request_log
                    if method == 'GET' and path == '/Items'),
                4,
            )
            self.assertEqual(
                [entry for entry in request_log if entry[0] == 'POST'],
                [],
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
