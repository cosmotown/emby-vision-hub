import logging
import os
import tempfile
import threading
import time
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

    def test_inaccessible_retry_paths_are_deferred_without_operation_or_attempt_change(self):
        ingest_path = '/isolated/offline/S01E01.strm'
        delete_path = '/isolated/offline/S01E02.strm'
        strm_ingest_db.enqueue_paths(
            [ingest_path], source='test', last_error='test', initial_delay_seconds=0,
        )
        strm_ingest_db.enqueue_paths(
            [delete_path], source='test', last_error='test', operation='delete',
            initial_delay_seconds=0,
        )
        claimed = strm_ingest_db.claim_due_paths(limit=2)
        self.assertEqual(2, len(claimed))
        self.assertEqual(
            2,
            strm_ingest_db.defer_claimed_paths(
                [ingest_path, delete_path], 'mount unavailable', delay_seconds=300,
            ),
        )
        with connection.get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    'SELECT file_path, operation, status, attempt_count, '
                    'next_attempt_at > NOW() AS backed_off '
                    'FROM strm_ingest_retry_queue WHERE file_path = ANY(%s) '
                    'ORDER BY file_path',
                    ([ingest_path, delete_path],),
                )
                rows = cursor.fetchall()
        self.assertEqual(['ingest', 'delete'], [row['operation'] for row in rows])
        self.assertTrue(all(row['status'] == 'retry' for row in rows))
        self.assertTrue(all(row['attempt_count'] == 0 for row in rows))
        self.assertTrue(all(row['backed_off'] for row in rows))

    def test_dirty_state_survives_restart_and_one_snapshot_completes_directory(self):
        with tempfile.TemporaryDirectory() as root:
            for index in range(7):
                with open(os.path.join(root, f'{index:02d}.strm'), 'w', encoding='utf-8') as handle:
                    handle.write('url')
            strm_ingest_db.register_inventory_roots([root])
            strm_ingest_db.request_full_inventory_audit([root])
            first = IncrementalStrmInventory(owner='first', db_batch_size=3, directory_batch_limit=1)
            summary = first.run_once()
            self.assertEqual(1, summary['completed'])
            self.assertEqual(1, summary['physical_enumerations'])
            self.assertEqual(3, summary['db_batches'])
            persisted = strm_ingest_db.get_inventory_summary()
            self.assertEqual(0, persisted['dirty_count'])

            second = IncrementalStrmInventory(owner='second', db_batch_size=3, directory_batch_limit=1)
            final = strm_ingest_db.get_inventory_summary()
            self.assertEqual(0, final['dirty_count'])
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute('SELECT COUNT(*) AS count FROM strm_ingest_retry_queue')
                    self.assertEqual(7, cursor.fetchone()['count'])

    def test_mount_unavailable_preserves_inventory_and_records_backoff(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = os.path.join(workspace, 'STRM')
            os.mkdir(root)
            episode = os.path.join(root, 'S01E01.strm')
            with open(episode, 'w', encoding='utf-8') as handle:
                handle.write('controlled://episode')
            strm_ingest_db.register_inventory_roots([root])
            strm_ingest_db.request_full_inventory_audit([root])
            inventory = IncrementalStrmInventory(owner='first', directory_batch_limit=1)
            self.assertEqual(1, inventory.run_once()['completed'])

            unavailable = os.path.join(workspace, 'STRM.offline')
            os.rename(root, unavailable)
            strm_ingest_db.mark_directory_dirty(root, root, event_kind='scheduled_audit')
            failed = IncrementalStrmInventory(owner='second', directory_batch_limit=1).run_once()
            self.assertEqual(1, failed['failed'])
            self.assertEqual(0, failed['delete'])
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        'SELECT active, dirty, claim_owner, last_error, next_audit_at > NOW() AS backed_off '
                        'FROM strm_ingest_inventory_directories WHERE root_path = %s AND directory_path = %s',
                        (root, root),
                    )
                    directory = cursor.fetchone()
                    cursor.execute(
                        'SELECT operation, status FROM strm_ingest_retry_queue WHERE file_path = %s',
                        (episode,),
                    )
                    file_row = cursor.fetchone()
            self.assertTrue(directory['active'])
            self.assertTrue(directory['dirty'])
            self.assertIsNone(directory['claim_owner'])
            self.assertEqual('mount_unavailable', directory['last_error'])
            self.assertTrue(directory['backed_off'])
            self.assertNotEqual('delete', file_row['operation'])
            self.assertEqual(
                [],
                strm_ingest_db.claim_inventory_directories('backoff-probe', limit=1),
                'a dirty failed directory must remain unclaimable until next_audit_at',
            )

            os.rename(unavailable, root)
            strm_ingest_db.mark_directory_dirty(root, root, event_kind='mount_restored')
            recovered = IncrementalStrmInventory(owner='third', directory_batch_limit=1).run_once()
            self.assertEqual(1, recovered['completed'])
            self.assertEqual(0, recovered['delete'])

    def test_confirmed_file_missing_requires_successful_parent_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            episode = os.path.join(root, 'S01E01.strm')
            with open(episode, 'w', encoding='utf-8') as handle:
                handle.write('controlled://episode')
            strm_ingest_db.register_inventory_roots([root])
            strm_ingest_db.request_full_inventory_audit([root])
            inventory = IncrementalStrmInventory(owner='first', directory_batch_limit=1)
            inventory.run_once()
            os.unlink(episode)
            strm_ingest_db.mark_directory_dirty(root, root, event_kind='scheduled_audit')
            deleted = []
            result = IncrementalStrmInventory(owner='second', directory_batch_limit=1).run_once(
                on_delete=lambda paths: deleted.extend(paths)
            )
            self.assertEqual(1, result['delete'])
            self.assertEqual([episode], deleted)

    def test_missing_child_is_removed_only_after_successful_parent_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            show = os.path.join(root, 'Show')
            season = os.path.join(show, 'Season 1')
            os.makedirs(season)
            episode = os.path.join(season, 'S01E01.strm')
            with open(episode, 'w', encoding='utf-8') as handle:
                handle.write('controlled://episode')
            strm_ingest_db.register_inventory_roots([root])
            strm_ingest_db.request_full_inventory_audit([root])
            for owner in ('root', 'show', 'season'):
                IncrementalStrmInventory(owner=owner, directory_batch_limit=1).run_once()

            os.unlink(episode)
            os.rmdir(season)
            strm_ingest_db.mark_directory_dirty(root, show, event_kind='scheduled_audit')
            deleted = []
            result = IncrementalStrmInventory(owner='parent-confirmation', directory_batch_limit=1).run_once(
                on_delete=lambda paths: deleted.extend(paths)
            )
            self.assertEqual(1, result['delete'])
            self.assertEqual([episode], deleted)
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        'SELECT active FROM strm_ingest_inventory_directories '
                        'WHERE root_path = %s AND directory_path = %s',
                        (root, season),
                    )
                    self.assertFalse(cursor.fetchone()['active'])

    def test_stopped_period_directory_is_found_by_two_bounded_audits(self):
        with tempfile.TemporaryDirectory() as root:
            strm_ingest_db.register_inventory_roots([root])
            strm_ingest_db.request_full_inventory_audit([root])
            IncrementalStrmInventory(owner='before-stop', directory_batch_limit=1).run_once()

            show = os.path.join(root, 'New Show')
            os.mkdir(show)
            episode = os.path.join(show, 'S01E01.strm')
            with open(episode, 'w', encoding='utf-8') as handle:
                handle.write('controlled://episode')
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        'UPDATE strm_ingest_inventory_directories SET next_audit_at = NOW() '
                        'WHERE root_path = %s AND directory_path = %s',
                        (root, root),
                    )

            started = time.monotonic()
            first = IncrementalStrmInventory(owner='after-restart-1', directory_batch_limit=1).run_once()
            second = IncrementalStrmInventory(owner='after-restart-2', directory_batch_limit=1).run_once()
            elapsed = time.monotonic() - started
            self.assertEqual(2, first['entries_seen'] + second['entries_seen'])
            self.assertEqual(1, second['ingest'])
            self.assertLess(elapsed, 5)
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        'SELECT COUNT(*) AS count FROM strm_ingest_retry_queue WHERE file_path = %s',
                        (episode,),
                    )
                    self.assertEqual(1, cursor.fetchone()['count'])

    def test_expired_claim_can_be_recovered_by_another_instance(self):
        root = '/isolated/STRM'
        strm_ingest_db.register_inventory_roots([root])
        strm_ingest_db.mark_directory_dirty(root, root, event_kind='test')
        first = strm_ingest_db.claim_inventory_directories('owner-one', limit=1, lease_seconds=30)
        self.assertEqual(1, len(first))
        with connection.get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    'UPDATE strm_ingest_inventory_directories SET claim_expires_at = NOW() - INTERVAL \'1 second\' '
                    'WHERE root_path = %s AND directory_path = %s',
                    (root, root),
                )
        second = strm_ingest_db.claim_inventory_directories('owner-two', limit=1)
        self.assertEqual(1, len(second))
        self.assertEqual('owner-two', second[0]['claim_owner'])

    def test_flat_10000_entry_directory_uses_one_enumeration_and_twenty_db_batches(self):
        with tempfile.TemporaryDirectory() as root:
            for index in range(10000):
                with open(os.path.join(root, f'{index:05d}.strm'), 'w', encoding='utf-8') as handle:
                    handle.write('controlled://episode')
            strm_ingest_db.register_inventory_roots([root])
            strm_ingest_db.request_full_inventory_audit([root])
            started = time.monotonic()
            result = IncrementalStrmInventory(
                owner='large-directory', directory_batch_limit=1, db_batch_size=500,
            ).run_once()
            elapsed = time.monotonic() - started
            self.assertEqual(1, result['physical_enumerations'])
            self.assertEqual(10000, result['entries_seen'])
            self.assertEqual(20, result['db_batches'])
            self.assertEqual(10000, result['ingest'])
            self.assertLess(elapsed, 30)

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
