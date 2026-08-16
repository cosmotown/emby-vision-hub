import logging
import os
import tempfile
import threading
import unittest

import config_manager
import constants
from database import connection, strm_ingest_db
from services.strm_inventory import IncrementalStrmInventory


POSTGRES_HOST = os.environ.get('EVH_TEST_POSTGRES_HOST')

if not hasattr(logging.Logger, 'trace'):
    logging.Logger.trace = logging.Logger.debug


@unittest.skipUnless(POSTGRES_HOST, 'isolated PostgreSQL is not configured')
class StrmInventoryV2PostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_config = dict(config_manager.APP_CONFIG)
        config_manager.APP_CONFIG.update({
            constants.CONFIG_OPTION_DB_HOST: POSTGRES_HOST,
            constants.CONFIG_OPTION_DB_PORT: int(os.environ.get('EVH_TEST_POSTGRES_PORT', '5432')),
            constants.CONFIG_OPTION_DB_USER: os.environ.get('EVH_TEST_POSTGRES_USER', 'evh_test'),
            constants.CONFIG_OPTION_DB_PASSWORD: os.environ.get('EVH_TEST_POSTGRES_PASSWORD', 'evh_test'),
            constants.CONFIG_OPTION_DB_NAME: os.environ.get('EVH_TEST_POSTGRES_DB', 'evh_test'),
        })
        connection.init_db()

    @classmethod
    def tearDownClass(cls):
        config_manager.APP_CONFIG.clear()
        config_manager.APP_CONFIG.update(cls.old_config)

    def setUp(self):
        with connection.get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    'TRUNCATE strm_ingest_inventory_directories, '
                    'strm_ingest_inventory_roots, strm_ingest_retry_queue RESTART IDENTITY'
                )

    def test_two_instances_cannot_claim_the_same_directory(self):
        root = '/isolated/STRM'
        strm_ingest_db.register_inventory_roots([root])
        self.assertEqual(
            [],
            strm_ingest_db.claim_inventory_directories('startup-probe', limit=1),
            'registering a root must not start a recursive startup inventory',
        )
        strm_ingest_db.mark_directory_dirty(root, root, event_kind='test')
        barrier = threading.Barrier(2)
        results = []
        lock = threading.Lock()

        def claim(owner):
            barrier.wait(timeout=5)
            value = strm_ingest_db.claim_inventory_directories(owner, limit=1)
            with lock:
                results.append(value)

        threads = [threading.Thread(target=claim, args=(f'owner-{index}',)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual([0, 1], sorted(len(value) for value in results))

    def test_dirty_state_and_cursor_survive_new_inventory_instance(self):
        with tempfile.TemporaryDirectory() as root:
            for index in range(7):
                with open(os.path.join(root, f'{index:02d}.strm'), 'w', encoding='utf-8') as handle:
                    handle.write('url')
            strm_ingest_db.register_inventory_roots([root])
            strm_ingest_db.request_full_inventory_audit([root])
            first = IncrementalStrmInventory(owner='first', entry_batch_limit=3, directory_batch_limit=1)
            summary = first.run_once()
            self.assertEqual(1, summary['partial'])
            persisted = strm_ingest_db.get_inventory_summary()
            self.assertEqual(1, persisted['dirty_count'])

            second = IncrementalStrmInventory(owner='second', entry_batch_limit=3, directory_batch_limit=1)
            second.run_once()
            second.run_once()
            final = strm_ingest_db.get_inventory_summary()
            self.assertEqual(0, final['dirty_count'])
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute('SELECT COUNT(*) AS count FROM strm_ingest_retry_queue')
                    self.assertEqual(7, cursor.fetchone()['count'])

    def test_legacy_retry_rows_are_adopted_without_database_reset(self):
        root = '/isolated/STRM'
        path = f'{root}/Show/S01E01.strm'
        with connection.get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO strm_ingest_retry_queue (file_path, status) VALUES (%s, 'completed')",
                    (path,),
                )
        strm_ingest_db.register_inventory_roots([root])
        with connection.get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    'SELECT inventory_root_path, inventory_directory_path '
                    'FROM strm_ingest_retry_queue WHERE file_path = %s',
                    (path,),
                )
                row = cursor.fetchone()
        self.assertEqual(root, row['inventory_root_path'])
        self.assertEqual(f'{root}/Show', row['inventory_directory_path'])
        self.assertEqual(
            [],
            strm_ingest_db.claim_inventory_directories('legacy-startup-probe', limit=4),
            'legacy adoption must stagger known directories instead of scanning on startup',
        )

    def test_directory_move_persists_old_and_new_sides_without_filesystem_walk(self):
        root = '/isolated/STRM'
        old = f'{root}/Show'
        new = f'{root}/Renamed Show'
        old_file = f'{old}/S01E01.strm'
        new_file = f'{new}/S01E01.strm'
        strm_ingest_db.register_inventory_roots([root])
        strm_ingest_db.record_directory_created(root, old)
        strm_ingest_db.enqueue_paths(
            [old_file], operation='ingest', source='test', last_error='test fixture'
        )
        strm_ingest_db.record_file_event(root, old_file, event_kind='ingest')

        pairs = strm_ingest_db.record_directory_moved(root, old, new)

        self.assertEqual([(old_file, new_file)], pairs)
        with connection.get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT directory_path, active, dirty
                    FROM strm_ingest_inventory_directories
                    WHERE root_path = %s AND directory_path IN (%s, %s)
                    ORDER BY directory_path
                    """,
                    (root, old, new),
                )
                rows = {row['directory_path']: dict(row) for row in cursor.fetchall()}
        self.assertFalse(rows[old]['active'])
        self.assertTrue(rows[new]['active'])
        self.assertTrue(rows[new]['dirty'])

    def test_explicit_full_audit_only_marks_persisted_directories_dirty(self):
        root = '/isolated/STRM'
        strm_ingest_db.register_inventory_roots([root])
        strm_ingest_db.record_directory_created(root, f'{root}/Show')
        scheduled = strm_ingest_db.request_full_inventory_audit([root])
        self.assertEqual(2, scheduled)
        self.assertEqual(2, strm_ingest_db.get_inventory_summary()['dirty_count'])


if __name__ == '__main__':
    unittest.main()
