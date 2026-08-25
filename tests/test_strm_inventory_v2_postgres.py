import logging
import os
import tempfile
import threading
import time
import unittest

from psycopg2.extras import execute_values

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
            constants.CONFIG_OPTION_DB_USER: os.environ.get(
                'EVH_TEST_POSTGRES_USER', os.environ.get('EVH_DB_USER', 'evh_test')
            ),
            constants.CONFIG_OPTION_DB_PASSWORD: os.environ.get(
                'EVH_TEST_POSTGRES_PASSWORD', os.environ.get('EVH_DB_PASSWORD', 'evh_test')
            ),
            constants.CONFIG_OPTION_DB_NAME: os.environ.get(
                'EVH_TEST_POSTGRES_DB', os.environ.get('EVH_DB_NAME', 'evh_test')
            ),
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
                    'TRUNCATE strm_ingest_inventory_manual_audits, '
                    'strm_ingest_inventory_directories, '
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

    def test_clean_directory_is_never_claimed_when_legacy_interval_expires(self):
        root = '/isolated/STRM'
        strm_ingest_db.register_inventory_roots([root], audit_interval_hours=1)
        with connection.get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    'UPDATE strm_ingest_inventory_directories '
                    "SET dirty = FALSE, next_audit_at = NOW() - INTERVAL '48 hours' "
                    'WHERE root_path = %s',
                    (root,),
                )
        self.assertEqual(
            [],
            strm_ingest_db.claim_inventory_directories('legacy-24h-probe', limit=4),
        )

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
            # Restart alone leaves the clean root unclaimed. Only the explicit
            # manual audit makes the persisted directory tree eligible.
            self.assertEqual(
                [],
                strm_ingest_db.claim_inventory_directories('restart-probe', limit=1),
            )
            strm_ingest_db.request_full_inventory_audit([root])

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

    def test_one_hundred_exact_file_events_create_no_directory_scan_backlog(self):
        with tempfile.TemporaryDirectory() as root:
            strm_ingest_db.register_inventory_roots([root])
            for index in range(100):
                directory = os.path.join(root, f'Show {index:03d}')
                os.mkdir(directory)
                path = os.path.join(directory, 'E01.strm')
                with open(path, 'w', encoding='utf-8') as handle:
                    handle.write('controlled://episode')
                strm_ingest_db.enqueue_paths(
                    [path], source='watchdog_inventory', last_error='exact event'
                )
                strm_ingest_db.record_file_event(root, path, event_kind='ingest')

            self.assertEqual(0, strm_ingest_db.get_inventory_summary()['dirty_count'])
            self.assertEqual(
                [],
                strm_ingest_db.claim_inventory_directories('file-event-probe', limit=32),
            )

            new_directory = os.path.join(root, 'New Directory')
            os.mkdir(new_directory)
            strm_ingest_db.record_directory_created(root, new_directory)
            summary = IncrementalStrmInventory(
                owner='directory-event', directory_batch_limit=2,
            ).run_once()
            self.assertEqual(2, summary['claimed'])
            self.assertEqual(2, summary['physical_enumerations'])
            self.assertEqual(0, strm_ingest_db.get_inventory_summary()['dirty_count'])

    def test_exact_delete_completes_without_dirty_parent_or_inventory_claim(self):
        with tempfile.TemporaryDirectory() as root:
            directory = os.path.join(root, 'Show')
            os.mkdir(directory)
            path = os.path.join(directory, 'E01.strm')
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write('controlled://episode')
            strm_ingest_db.register_inventory_roots([root])
            strm_ingest_db.enqueue_paths(
                [path], source='watchdog_inventory', last_error='exact ingest'
            )
            strm_ingest_db.record_file_event(root, path, event_kind='ingest')
            os.unlink(path)
            strm_ingest_db.enqueue_paths(
                [path], operation='delete', source='watchdog_inventory',
                last_error='exact delete', initial_delay_seconds=0,
            )
            strm_ingest_db.record_file_event(root, path, event_kind='delete')

            self.assertEqual(0, strm_ingest_db.get_inventory_summary()['dirty_count'])
            self.assertEqual(
                [],
                strm_ingest_db.claim_inventory_directories('delete-probe', limit=4),
            )
            self.assertEqual(1, strm_ingest_db.mark_deleted([path]))
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        'SELECT operation, status FROM strm_ingest_retry_queue '
                        'WHERE file_path = %s',
                        (path,),
                    )
                    row = cursor.fetchone()
            self.assertEqual('delete', row['operation'])
            self.assertEqual('deleted', row['status'])

    def test_manual_generation_progress_cancel_and_resume_is_persistent(self):
        with tempfile.TemporaryDirectory() as root:
            for index in range(100):
                os.mkdir(os.path.join(root, f'Show {index:03d}'))
            strm_ingest_db.register_inventory_roots([root])
            for index in range(100):
                path = os.path.join(root, f'Show {index:03d}', 'E01.strm')
                with open(path, 'w', encoding='utf-8') as handle:
                    handle.write('controlled://episode')
                strm_ingest_db.enqueue_paths(
                    [path], source='test', last_error='known directory'
                )
                strm_ingest_db.record_file_event(root, path, event_kind='ingest')

            first = strm_ingest_db.create_manual_inventory_audit([root])
            first_id = first['audit_id']
            inventory = IncrementalStrmInventory(
                owner='manual-first', directory_batch_limit=4,
            )
            for _ in range(30):
                result = inventory.run_once(
                    manual_audit_id=first_id, claim_limit=1,
                )
                self.assertEqual(1, result['claimed'])
            status = strm_ingest_db.get_manual_inventory_audit(first_id)
            self.assertEqual('running', status['state'])
            self.assertEqual(30, status['completed_directories'])
            self.assertGreater(status['pending_directories'], 0)
            self.assertGreater(status['progress'], 0)
            self.assertLess(status['progress'], 100)

            self.assertTrue(strm_ingest_db.cancel_manual_inventory_audit(first_id))
            self.assertEqual(
                [],
                strm_ingest_db.claim_inventory_directories(
                    'cancelled-probe', limit=4, manual_audit_id=first_id,
                ),
            )
            cancelled = strm_ingest_db.get_manual_inventory_audit(first_id)
            self.assertEqual('cancelled', cancelled['state'])
            self.assertGreater(cancelled['pending_directories'], 0)
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT COUNT(*) AS count FROM strm_ingest_retry_queue "
                        "WHERE operation = 'delete'"
                    )
                    self.assertEqual(0, cursor.fetchone()['count'])

            second = strm_ingest_db.create_manual_inventory_audit([root])
            self.assertNotEqual(first_id, second['audit_id'])
            second_id = second['audit_id']
            resumed = IncrementalStrmInventory(
                owner='manual-second', directory_batch_limit=4,
            )
            for _ in range(150):
                status = strm_ingest_db.get_manual_inventory_audit(second_id)
                if status['state'] == 'completed':
                    break
                result = resumed.run_once(
                    manual_audit_id=second_id, claim_limit=1,
                )
                self.assertLessEqual(result['claimed'], 1)
            final = strm_ingest_db.get_manual_inventory_audit(second_id)
            self.assertEqual('completed', final['state'])
            self.assertEqual(100, final['progress'])
            self.assertEqual(0, final['pending_directories'])

    def test_manual_parent_rediscovery_does_not_create_clean_generation_ghosts(self):
        with tempfile.TemporaryDirectory() as root:
            children = []
            for index in range(100):
                directory = os.path.join(root, f'Existing {index:03d}')
                os.mkdir(directory)
                children.append(directory)

            strm_ingest_db.register_inventory_roots([root])
            for directory in children:
                strm_ingest_db.record_file_event(
                    root,
                    os.path.join(directory, 'E01.strm'),
                    event_kind='ingest',
                )

            audit = strm_ingest_db.create_manual_inventory_audit([root])
            audit_id = audit['audit_id']
            inventory = IncrementalStrmInventory(
                owner='ghost-reproduction', directory_batch_limit=1,
            )

            # Complete one child before its parent. The later successful parent
            # snapshot must not attach that clean child to the generation again.
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE strm_ingest_inventory_directories "
                        "SET next_audit_at = NOW() + INTERVAL '1 hour' "
                        "WHERE root_path = %s AND directory_path = %s",
                        (root, root),
                    )
            first = inventory.run_once(manual_audit_id=audit_id, claim_limit=1)
            self.assertEqual(1, first['completed'])
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE strm_ingest_inventory_directories "
                        "SET next_audit_at = NOW() - INTERVAL '1 hour' "
                        "WHERE root_path = %s AND directory_path = %s",
                        (root, root),
                    )
            parent = inventory.run_once(manual_audit_id=audit_id, claim_limit=1)
            self.assertEqual(1, parent['completed'])

            for _ in range(150):
                result = inventory.run_once(
                    manual_audit_id=audit_id, claim_limit=1,
                )
                if not result['claimed']:
                    break

            status = strm_ingest_db.get_manual_inventory_audit(audit_id)
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT COUNT(*) AS count "
                        "FROM strm_ingest_inventory_directories "
                        "WHERE active = TRUE AND dirty = FALSE "
                        "AND manual_audit_id = %s",
                        (audit_id,),
                    )
                    ghost_count = int(cursor.fetchone()['count'])

            self.assertEqual(0, ghost_count, status)
            self.assertEqual('completed', status['state'])
            self.assertEqual(status['total_directories'], status['completed_directories'])
            self.assertEqual(0, status['pending_directories'])
            self.assertEqual(0, status['claimed_directories'])

    def test_completed_child_rediscovery_does_not_inflate_manual_progress(self):
        with tempfile.TemporaryDirectory() as root:
            child = os.path.join(root, 'Already Complete')
            os.mkdir(child)
            strm_ingest_db.register_inventory_roots([root])
            strm_ingest_db.record_file_event(
                root, os.path.join(child, 'E01.strm'), event_kind='ingest'
            )
            audit_id = strm_ingest_db.create_manual_inventory_audit([root])['audit_id']
            inventory = IncrementalStrmInventory(
                owner='completed-child', directory_batch_limit=1,
            )
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE strm_ingest_inventory_directories "
                        "SET next_audit_at = NOW() + INTERVAL '1 hour' "
                        "WHERE root_path = %s AND directory_path = %s",
                        (root, root),
                    )
            self.assertEqual(
                1,
                inventory.run_once(
                    manual_audit_id=audit_id, claim_limit=1,
                )['completed'],
            )
            before_parent = strm_ingest_db.get_manual_inventory_audit(audit_id)
            self.assertEqual(2, before_parent['total_directories'])
            self.assertEqual(1, before_parent['completed_directories'])

            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE strm_ingest_inventory_directories "
                        "SET next_audit_at = NOW() - INTERVAL '1 hour' "
                        "WHERE root_path = %s AND directory_path = %s",
                        (root, root),
                    )
            self.assertEqual(
                1,
                inventory.run_once(
                    manual_audit_id=audit_id, claim_limit=1,
                )['completed'],
            )
            final = strm_ingest_db.get_manual_inventory_audit(audit_id)
            self.assertEqual('completed', final['state'])
            self.assertEqual(2, final['total_directories'])
            self.assertEqual(2, final['completed_directories'])
            self.assertEqual(0, final['pending_directories'])
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT dirty, manual_audit_id "
                        "FROM strm_ingest_inventory_directories "
                        "WHERE root_path = %s AND directory_path = %s",
                        (root, child),
                    )
                    row = cursor.fetchone()
            self.assertFalse(row['dirty'])
            self.assertIsNone(row['manual_audit_id'])

    def test_clean_generation_owned_child_is_repaired_to_claimable(self):
        with tempfile.TemporaryDirectory() as root:
            child = os.path.join(root, 'Pending Child')
            os.mkdir(child)
            strm_ingest_db.register_inventory_roots([root])
            strm_ingest_db.record_file_event(
                root, os.path.join(child, 'E01.strm'), event_kind='ingest'
            )
            audit_id = strm_ingest_db.create_manual_inventory_audit([root])['audit_id']
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE strm_ingest_inventory_directories "
                        "SET dirty = FALSE, next_audit_at = NOW() + INTERVAL '1 hour' "
                        "WHERE root_path = %s AND directory_path = %s",
                        (root, child),
                    )

            status = strm_ingest_db.get_manual_inventory_audit(audit_id)
            self.assertEqual(2, status['pending_directories'])
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT dirty, next_audit_at <= NOW() AS due, manual_audit_id "
                        "FROM strm_ingest_inventory_directories "
                        "WHERE root_path = %s AND directory_path = %s",
                        (root, child),
                    )
                    repaired = cursor.fetchone()
            self.assertTrue(repaired['dirty'])
            self.assertTrue(repaired['due'])
            self.assertEqual(audit_id, repaired['manual_audit_id'])

            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE strm_ingest_inventory_directories "
                        "SET next_audit_at = NOW() + INTERVAL '1 hour' "
                        "WHERE root_path = %s AND directory_path = %s",
                        (root, root),
                    )
            claims = strm_ingest_db.claim_inventory_directories(
                'repaired-claim', limit=1, manual_audit_id=audit_id,
            )
            self.assertEqual([child], [row['directory_path'] for row in claims])

    def test_manual_parent_snapshot_terminalizes_missing_child_and_generation(self):
        with tempfile.TemporaryDirectory() as root:
            missing_child = os.path.join(root, 'Historical Season')
            episode = os.path.join(missing_child, 'S01E01.strm')
            strm_ingest_db.register_inventory_roots([root])
            strm_ingest_db.record_file_event(root, episode, event_kind='ingest')
            strm_ingest_db.enqueue_paths(
                [episode], source='test', last_error='known historical file'
            )
            audit_id = strm_ingest_db.create_manual_inventory_audit([root])['audit_id']
            deleted = []
            result = IncrementalStrmInventory(
                owner='missing-parent-proof', directory_batch_limit=1,
            ).run_once(
                manual_audit_id=audit_id,
                claim_limit=1,
                on_delete=lambda paths: deleted.extend(paths),
            )
            self.assertEqual(1, result['completed'])
            self.assertEqual([episode], deleted)

            status = strm_ingest_db.get_manual_inventory_audit(audit_id)
            self.assertEqual('completed', status['state'])
            self.assertEqual(2, status['total_directories'])
            self.assertEqual(2, status['completed_directories'])
            self.assertEqual(0, status['pending_directories'])
            self.assertEqual(0, status['claimed_directories'])
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT active, dirty, manual_audit_id, last_error "
                        "FROM strm_ingest_inventory_directories "
                        "WHERE root_path = %s AND directory_path = %s",
                        (root, missing_child),
                    )
                    child_row = cursor.fetchone()
                    cursor.execute(
                        "SELECT operation, status FROM strm_ingest_retry_queue "
                        "WHERE file_path = %s",
                        (episode,),
                    )
                    file_row = cursor.fetchone()
            self.assertFalse(child_row['active'])
            self.assertFalse(child_row['dirty'])
            self.assertIsNone(child_row['manual_audit_id'])
            self.assertIsNone(child_row['last_error'])
            self.assertEqual('delete', file_row['operation'])
            self.assertEqual('pending', file_row['status'])

    def test_inaccessible_parent_never_infers_manual_child_deletion(self):
        with tempfile.TemporaryDirectory() as root:
            missing_parent = os.path.join(root, 'Unavailable Parent')
            child = os.path.join(missing_parent, 'Season 1')
            episode = os.path.join(child, 'S01E01.strm')
            strm_ingest_db.register_inventory_roots([root])
            strm_ingest_db.record_file_event(
                root,
                os.path.join(missing_parent, 'parent-placeholder.strm'),
                event_kind='ingest',
            )
            strm_ingest_db.record_file_event(root, episode, event_kind='ingest')
            strm_ingest_db.enqueue_paths(
                [episode], source='test', last_error='known historical file'
            )
            audit_id = strm_ingest_db.create_manual_inventory_audit([root])['audit_id']
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE strm_ingest_inventory_directories "
                        "SET next_audit_at = CASE "
                        "WHEN directory_path = %s THEN NOW() - INTERVAL '1 hour' "
                        "ELSE NOW() + INTERVAL '1 hour' END "
                        "WHERE root_path = %s",
                        (missing_parent, root),
                    )
            deleted = []
            result = IncrementalStrmInventory(
                owner='inaccessible-parent', directory_batch_limit=1,
            ).run_once(
                manual_audit_id=audit_id,
                claim_limit=1,
                on_delete=lambda paths: deleted.extend(paths),
            )
            self.assertEqual(1, result['failed'])
            self.assertEqual([], deleted)
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT active, dirty, manual_audit_id "
                        "FROM strm_ingest_inventory_directories "
                        "WHERE root_path = %s AND directory_path = %s",
                        (root, child),
                    )
                    child_row = cursor.fetchone()
                    cursor.execute(
                        "SELECT operation, status FROM strm_ingest_retry_queue "
                        "WHERE file_path = %s",
                        (episode,),
                    )
                    file_row = cursor.fetchone()
            self.assertTrue(child_row['active'])
            self.assertTrue(child_row['dirty'])
            self.assertEqual(audit_id, child_row['manual_audit_id'])
            self.assertNotEqual('delete', file_row['operation'])

    def _seed_orphan_generation(
        self,
        *,
        root,
        category,
        missing_parent,
        descendant_count,
        include_parent_row,
        completed_directories,
    ):
        strm_ingest_db.register_inventory_roots([root])
        audit_id = f'orphan-{time.time_ns()}'
        descendants = [
            os.path.join(missing_parent, f'Season {index + 1}')
            for index in range(descendant_count)
        ]
        files = [os.path.join(path, 'E01.strm') for path in descendants]
        with connection.get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO strm_ingest_inventory_manual_audits "
                    "(audit_id, state, total_directories, completed_directories, started_at) "
                    "VALUES (%s, 'running', %s, %s, NOW())",
                    (
                        audit_id,
                        completed_directories + descendant_count,
                        completed_directories,
                    ),
                )
                cursor.execute(
                    "INSERT INTO strm_ingest_inventory_directories "
                    "(root_path, directory_path, parent_path, active, dirty, "
                    "next_audit_at, last_verified_at) "
                    "VALUES (%s, %s, %s, TRUE, FALSE, "
                    "NOW() + INTERVAL '1 day', NOW()) "
                    "ON CONFLICT (root_path, directory_path) DO UPDATE "
                    "SET active = TRUE, dirty = FALSE, manual_audit_id = NULL, "
                    "last_verified_at = NOW()",
                    (root, category, root),
                )
                if include_parent_row:
                    cursor.execute(
                        "INSERT INTO strm_ingest_inventory_directories "
                        "(root_path, directory_path, parent_path, active, dirty, "
                        "next_audit_at) VALUES (%s, %s, %s, FALSE, FALSE, NOW())",
                        (root, missing_parent, category),
                    )
                execute_values(
                    cursor,
                    """
                    INSERT INTO strm_ingest_inventory_directories (
                        root_path, directory_path, parent_path, active, dirty,
                        next_audit_at, manual_audit_id, last_error
                    ) VALUES %s
                    """,
                    [
                        (
                            root, path, missing_parent, True, True,
                            audit_id, 'inaccessible',
                        )
                        for path in descendants
                    ],
                    template="(%s, %s, %s, %s, %s, NOW(), %s, %s)",
                    page_size=500,
                )
                execute_values(
                    cursor,
                    """
                    INSERT INTO strm_ingest_retry_queue (
                        file_path, operation, source, status, next_attempt_at,
                        inventory_root_path, inventory_directory_path
                    ) VALUES %s
                    """,
                    [
                        (path, 'ingest', 'test', 'observed', root, directory)
                        for path, directory in zip(files, descendants)
                    ],
                    template="(%s, %s, %s, %s, NOW(), %s, %s)",
                    page_size=500,
                )
        return audit_id, descendants, files

    def test_successful_ancestor_snapshot_terminalizes_inactive_parent_orphan_subtree(self):
        with tempfile.TemporaryDirectory() as root:
            category = os.path.join(root, '动漫')
            os.mkdir(category)
            missing_parent = os.path.join(category, 'OLD_SHOW')
            audit_id, descendants, files = self._seed_orphan_generation(
                root=root,
                category=category,
                missing_parent=missing_parent,
                descendant_count=2,
                include_parent_row=True,
                completed_directories=2,
            )
            deleted = []
            result = IncrementalStrmInventory(
                owner='inactive-orphan-proof', directory_batch_limit=1,
            ).run_once(
                manual_audit_id=audit_id,
                claim_limit=1,
                on_delete=lambda paths: deleted.extend(paths),
            )
            self.assertEqual(1, result['completed'])
            self.assertEqual(sorted(files), sorted(deleted))
            final = strm_ingest_db.get_manual_inventory_audit(audit_id)
            self.assertEqual('completed', final['state'])
            self.assertEqual(4, final['total_directories'])
            self.assertEqual(4, final['completed_directories'])
            self.assertEqual(0, final['pending_directories'])
            self.assertEqual(0, final['claimed_directories'])
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT COUNT(*) AS count "
                        "FROM strm_ingest_inventory_directories "
                        "WHERE root_path = %s AND active = TRUE "
                        "AND directory_path = ANY(%s)",
                        (root, descendants),
                    )
                    self.assertEqual(0, cursor.fetchone()['count'])

    def test_successful_ancestor_snapshot_terminalizes_orphans_without_parent_row(self):
        with tempfile.TemporaryDirectory() as root:
            category = os.path.join(root, '电视剧')
            os.mkdir(category)
            missing_parent = os.path.join(category, 'OLD_SHOW')
            audit_id, _descendants, _files = self._seed_orphan_generation(
                root=root,
                category=category,
                missing_parent=missing_parent,
                descendant_count=2,
                include_parent_row=False,
                completed_directories=7,
            )
            result = IncrementalStrmInventory(
                owner='missing-parent-row-proof', directory_batch_limit=1,
            ).run_once(manual_audit_id=audit_id, claim_limit=1)
            self.assertEqual(1, result['completed'])
            final = strm_ingest_db.get_manual_inventory_audit(audit_id)
            self.assertEqual('completed', final['state'])
            self.assertEqual(9, final['total_directories'])
            self.assertEqual(9, final['completed_directories'])
            self.assertEqual(0, final['pending_directories'])

    def test_observed_first_hop_reactivates_parent_without_terminalizing_descendants(self):
        with tempfile.TemporaryDirectory() as root:
            category = os.path.join(root, '动漫')
            parent = os.path.join(category, 'OLD_SHOW')
            os.makedirs(parent)
            season = os.path.join(parent, 'Season 1')
            strm_ingest_db.register_inventory_roots([root])
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO strm_ingest_inventory_directories "
                        "(root_path, directory_path, parent_path, active, dirty) "
                        "VALUES (%s, %s, %s, TRUE, TRUE)",
                        (root, category, root),
                    )
                    cursor.execute(
                        "INSERT INTO strm_ingest_inventory_directories "
                        "(root_path, directory_path, parent_path, active, dirty) "
                        "VALUES (%s, %s, %s, FALSE, FALSE)",
                        (root, parent, category),
                    )
                    cursor.execute(
                        "INSERT INTO strm_ingest_inventory_directories "
                        "(root_path, directory_path, parent_path, active, dirty) "
                        "VALUES (%s, %s, %s, TRUE, TRUE)",
                        (root, season, parent),
                    )
            claims = strm_ingest_db.claim_inventory_directories(
                'observed-first-hop', limit=1,
            )
            self.assertEqual([category], [row['directory_path'] for row in claims])
            result = IncrementalStrmInventory(
                owner='observed-first-hop', directory_batch_limit=1,
            ).scan_claim(claims[0])
            self.assertTrue(result['accepted'])
            self.assertEqual([], result['removed'])
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT directory_path, active FROM "
                        "strm_ingest_inventory_directories "
                        "WHERE root_path = %s AND directory_path = ANY(%s) "
                        "ORDER BY directory_path",
                        (root, [parent, season]),
                    )
                    rows = cursor.fetchall()
            self.assertEqual(2, len(rows))
            self.assertTrue(all(row['active'] for row in rows))

    def test_ancestor_snapshot_does_not_skip_present_first_hop_for_deeper_missing_child(self):
        with tempfile.TemporaryDirectory() as root:
            category = os.path.join(root, '动漫')
            parent = os.path.join(category, 'OLD_SHOW')
            os.makedirs(parent)
            missing_season = os.path.join(parent, 'Season 1')
            strm_ingest_db.register_inventory_roots([root])
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO strm_ingest_inventory_directories "
                        "(root_path, directory_path, parent_path, active, dirty) "
                        "VALUES (%s, %s, %s, TRUE, TRUE)",
                        (root, category, root),
                    )
                    cursor.execute(
                        "INSERT INTO strm_ingest_inventory_directories "
                        "(root_path, directory_path, parent_path, active, dirty) "
                        "VALUES (%s, %s, %s, TRUE, FALSE)",
                        (root, parent, category),
                    )
                    cursor.execute(
                        "INSERT INTO strm_ingest_inventory_directories "
                        "(root_path, directory_path, parent_path, active, dirty) "
                        "VALUES (%s, %s, %s, TRUE, TRUE)",
                        (root, missing_season, parent),
                    )
            category_claim = strm_ingest_db.claim_inventory_directories(
                'category-proof', limit=1,
            )[0]
            IncrementalStrmInventory(owner='category-proof').scan_claim(category_claim)
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT active FROM strm_ingest_inventory_directories "
                        "WHERE root_path = %s AND directory_path = %s",
                        (root, missing_season),
                    )
                    self.assertTrue(cursor.fetchone()['active'])

            strm_ingest_db.mark_directory_dirty(root, parent, event_kind='parent-proof')
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE strm_ingest_inventory_directories "
                        "SET next_audit_at = NOW() + INTERVAL '1 hour' "
                        "WHERE root_path = %s AND directory_path = %s",
                        (root, missing_season),
                    )
            parent_claim = strm_ingest_db.claim_inventory_directories(
                'parent-proof', limit=1,
            )[0]
            IncrementalStrmInventory(owner='parent-proof').scan_claim(parent_claim)
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT active FROM strm_ingest_inventory_directories "
                        "WHERE root_path = %s AND directory_path = %s",
                        (root, missing_season),
                    )
                    self.assertFalse(cursor.fetchone()['active'])

    def test_observed_symlink_first_hop_never_becomes_deletion_proof(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as target:
            category = os.path.join(root, '动漫')
            os.mkdir(category)
            symlink_parent = os.path.join(category, 'OLD_SHOW')
            os.symlink(target, symlink_parent)
            descendant = os.path.join(symlink_parent, 'Season 1')
            strm_ingest_db.register_inventory_roots([root])
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO strm_ingest_inventory_directories "
                        "(root_path, directory_path, parent_path, active, dirty) "
                        "VALUES (%s, %s, %s, TRUE, TRUE)",
                        (root, category, root),
                    )
                    cursor.execute(
                        "INSERT INTO strm_ingest_inventory_directories "
                        "(root_path, directory_path, parent_path, active, dirty) "
                        "VALUES (%s, %s, %s, TRUE, TRUE)",
                        (root, descendant, symlink_parent),
                    )
            claim = strm_ingest_db.claim_inventory_directories(
                'symlink-first-hop', limit=1,
            )[0]
            result = IncrementalStrmInventory(
                owner='symlink-first-hop', directory_batch_limit=1,
            ).scan_claim(claim)
            self.assertEqual([], result['removed'])
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT active FROM strm_ingest_inventory_directories "
                        "WHERE root_path = %s AND directory_path = %s",
                        (root, descendant),
                    )
                    self.assertTrue(cursor.fetchone()['active'])

    def test_ancestor_event_change_makes_proof_stale_without_terminalization(self):
        with tempfile.TemporaryDirectory() as root:
            category = os.path.join(root, '动漫')
            os.mkdir(category)
            missing_parent = os.path.join(category, 'OLD_SHOW')
            audit_id, descendants, _files = self._seed_orphan_generation(
                root=root,
                category=category,
                missing_parent=missing_parent,
                descendant_count=1,
                include_parent_row=False,
                completed_directories=1,
            )
            claim = strm_ingest_db.claim_inventory_directories(
                'stale-ancestor-proof', limit=1, manual_audit_id=audit_id,
            )[0]
            ancestor = strm_ingest_db.get_inventory_ancestor_candidates(
                root, descendants[0],
            )[0]
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE strm_ingest_inventory_directories "
                        "SET event_version = event_version + 1 "
                        "WHERE root_path = %s AND directory_path = %s",
                        (root, ancestor['directory_path']),
                    )
            result = strm_ingest_db.record_inventory_ancestor_proof(
                claim,
                ancestor=ancestor,
                observed_entry_paths=[],
            )
            self.assertFalse(result['accepted'])
            self.assertTrue(result['stale'])
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT active, manual_audit_id "
                        "FROM strm_ingest_inventory_directories "
                        "WHERE root_path = %s AND directory_path = %s",
                        (root, descendants[0]),
                    )
                    row = cursor.fetchone()
            self.assertTrue(row['active'])
            self.assertEqual(audit_id, row['manual_audit_id'])

    def test_production_4012_3970_42_orphan_fixture_converges_without_retry_delay(self):
        with tempfile.TemporaryDirectory() as root:
            category = os.path.join(root, '动漫')
            os.mkdir(category)
            missing_parent = os.path.join(category, 'OLD_SHOW')
            audit_id, descendants, _files = self._seed_orphan_generation(
                root=root,
                category=category,
                missing_parent=missing_parent,
                descendant_count=42,
                include_parent_row=False,
                completed_directories=3970,
            )
            before = strm_ingest_db.get_manual_inventory_audit(audit_id)
            self.assertEqual(4012, before['total_directories'])
            self.assertEqual(3970, before['completed_directories'])
            self.assertEqual(42, before['pending_directories'])

            started = time.monotonic()
            result = IncrementalStrmInventory(
                owner='production-orphan-fixture', directory_batch_limit=1,
            ).run_once(manual_audit_id=audit_id, claim_limit=1)
            elapsed = time.monotonic() - started
            self.assertEqual(1, result['claimed'])
            self.assertEqual(1, result['completed'])
            self.assertLess(elapsed, 10)
            final = strm_ingest_db.get_manual_inventory_audit(audit_id)
            self.assertEqual('completed', final['state'])
            self.assertEqual(4012, final['total_directories'])
            self.assertEqual(4012, final['completed_directories'])
            self.assertEqual(0, final['pending_directories'])
            self.assertEqual(0, final['claimed_directories'])
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT COUNT(*) AS count "
                        "FROM strm_ingest_inventory_directories "
                        "WHERE root_path = %s AND active = TRUE "
                        "AND directory_path = ANY(%s)",
                        (root, descendants),
                    )
                    self.assertEqual(0, cursor.fetchone()['count'])

    def test_manual_generation_converges_with_five_thousand_persisted_directories(self):
        with tempfile.TemporaryDirectory() as root:
            strm_ingest_db.register_inventory_roots([root])
            children = [
                os.path.join(root, f'Persisted {index:04d}')
                for index in range(4999)
            ]
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    execute_values(
                        cursor,
                        """
                        INSERT INTO strm_ingest_inventory_directories (
                            root_path, directory_path, parent_path, active, dirty
                        ) VALUES %s
                        """,
                        [(root, child, root, True, False) for child in children],
                        page_size=500,
                    )
            audit_id = strm_ingest_db.create_manual_inventory_audit([root])['audit_id']

            # Model a long-running production generation: 3969 directories are
            # already complete and clean before their parent is rediscovered.
            completed_children = children[:3969]
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE strm_ingest_inventory_directories "
                        "SET dirty = FALSE, manual_audit_id = NULL "
                        "WHERE root_path = %s AND directory_path = ANY(%s)",
                        (root, completed_children),
                    )
                    cursor.execute(
                        "UPDATE strm_ingest_inventory_manual_audits "
                        "SET state = 'running', completed_directories = 3969, "
                        "total_directories = 5000, started_at = NOW() "
                        "WHERE audit_id = %s",
                        (audit_id,),
                    )
                    cursor.execute(
                        "UPDATE strm_ingest_inventory_directories "
                        "SET next_audit_at = CASE "
                        "WHEN directory_path = %s THEN NOW() - INTERVAL '1 hour' "
                        "ELSE NOW() + INTERVAL '1 hour' END "
                        "WHERE root_path = %s",
                        (root, root),
                    )

            root_claim = strm_ingest_db.claim_inventory_directories(
                'scale-root', limit=1, manual_audit_id=audit_id,
            )
            self.assertEqual([root], [row['directory_path'] for row in root_claim])
            root_result = strm_ingest_db.record_inventory_audit_batch(
                root_claim[0],
                files={},
                child_directories=children,
                next_cursor=None,
                complete=True,
                db_batch_size=500,
                audit_interval_hours=24,
            )
            self.assertTrue(root_result['accepted'])
            self.assertEqual(10, root_result['db_batches'])

            while True:
                claims = strm_ingest_db.claim_inventory_directories(
                    'scale-children', limit=32, manual_audit_id=audit_id,
                )
                if not claims:
                    break
                for claim in claims:
                    strm_ingest_db.record_inventory_audit_batch(
                        claim,
                        files={},
                        child_directories=[],
                        next_cursor=None,
                        complete=True,
                        db_batch_size=500,
                        audit_interval_hours=24,
                    )

            final = strm_ingest_db.get_manual_inventory_audit(audit_id)
            self.assertEqual('completed', final['state'])
            self.assertEqual(5000, final['total_directories'])
            self.assertEqual(5000, final['completed_directories'])
            self.assertEqual(0, final['pending_directories'])
            self.assertEqual(0, final['claimed_directories'])
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT COUNT(*) AS count "
                        "FROM strm_ingest_inventory_directories "
                        "WHERE active = TRUE AND dirty = FALSE "
                        "AND manual_audit_id = %s",
                        (audit_id,),
                    )
                    self.assertEqual(0, cursor.fetchone()['count'])

    def test_manual_claim_four_stop_releases_unstarted_rows_and_resume_completes(self):
        with tempfile.TemporaryDirectory() as root:
            children = []
            for index in range(3):
                child = os.path.join(root, f'Child {index}')
                os.mkdir(child)
                children.append(child)
            strm_ingest_db.register_inventory_roots([root])
            for child in children:
                strm_ingest_db.record_file_event(
                    root, os.path.join(child, 'E01.strm'), event_kind='ingest',
                )
            audit_id = strm_ingest_db.create_manual_inventory_audit([root])['audit_id']
            stop_checks = 0

            def stop_after_first_started_directory():
                nonlocal stop_checks
                stop_checks += 1
                return stop_checks > 1

            stopped = IncrementalStrmInventory(
                owner='manual-stop-four', directory_batch_limit=4,
            ).run_once(
                manual_audit_id=audit_id,
                claim_limit=4,
                should_stop=stop_after_first_started_directory,
            )
            self.assertEqual(4, stopped['claimed'])
            self.assertEqual(1, stopped['completed'])
            self.assertEqual(3, stopped['released'])
            self.assertEqual(1, stopped['physical_enumerations'])
            status = strm_ingest_db.get_manual_inventory_audit(audit_id)
            self.assertEqual('running', status['state'])
            self.assertEqual(3, status['pending_directories'])
            self.assertEqual(0, status['claimed_directories'])

            resumed = IncrementalStrmInventory(
                owner='manual-resume-four', directory_batch_limit=4,
            )
            for _ in range(4):
                status = strm_ingest_db.get_manual_inventory_audit(audit_id)
                if status['state'] == 'completed':
                    break
                resumed.run_once(manual_audit_id=audit_id, claim_limit=4)
            final = strm_ingest_db.get_manual_inventory_audit(audit_id)
            self.assertEqual('completed', final['state'])
            self.assertEqual(4, final['completed_directories'])
            self.assertEqual(0, final['pending_directories'])
            self.assertEqual(0, final['claimed_directories'])

    def test_manual_claim_four_expired_lease_recovers_after_restart(self):
        with tempfile.TemporaryDirectory() as root:
            children = []
            for index in range(3):
                child = os.path.join(root, f'Restart Child {index}')
                os.mkdir(child)
                children.append(child)
            strm_ingest_db.register_inventory_roots([root])
            for child in children:
                strm_ingest_db.record_file_event(
                    root, os.path.join(child, 'E01.strm'), event_kind='ingest',
                )
            audit_id = strm_ingest_db.create_manual_inventory_audit([root])['audit_id']
            abandoned = strm_ingest_db.claim_inventory_directories(
                'old-generation-worker', limit=4, manual_audit_id=audit_id,
            )
            self.assertEqual(4, len(abandoned))
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE strm_ingest_inventory_directories "
                        "SET claim_expires_at = NOW() - INTERVAL '1 second' "
                        "WHERE claim_owner = %s",
                        ('old-generation-worker',),
                    )
            recovered = IncrementalStrmInventory(
                owner='new-generation-worker', directory_batch_limit=4,
            ).run_once(manual_audit_id=audit_id, claim_limit=4)
            self.assertEqual(4, recovered['claimed'])
            self.assertEqual(4, recovered['completed'])
            final = strm_ingest_db.get_manual_inventory_audit(audit_id)
            self.assertEqual('completed', final['state'])
            self.assertEqual(0, final['pending_directories'])
            self.assertEqual(0, final['claimed_directories'])

    def test_watch_change_signal_only_for_new_or_removed_child_set(self):
        with tempfile.TemporaryDirectory() as root:
            strm_ingest_db.register_inventory_roots([root])
            strm_ingest_db.mark_directory_dirty(root, root, event_kind='baseline')
            baseline = IncrementalStrmInventory(
                owner='watch-baseline', directory_batch_limit=1,
            ).run_once()
            self.assertFalse(baseline['watch_set_changed'])

            child = os.path.join(root, 'New Child')
            os.mkdir(child)
            strm_ingest_db.mark_directory_dirty(root, root, event_kind='child-created')
            discovered = IncrementalStrmInventory(
                owner='watch-discovered', directory_batch_limit=1,
            ).run_once()
            self.assertTrue(discovered['watch_set_changed'])

            strm_ingest_db.mark_directory_dirty(root, root, event_kind='unchanged')
            unchanged = IncrementalStrmInventory(
                owner='watch-unchanged', directory_batch_limit=1,
            ).run_once()
            self.assertFalse(unchanged['watch_set_changed'])

            os.rmdir(child)
            strm_ingest_db.mark_directory_dirty(root, root, event_kind='child-removed')
            removed = IncrementalStrmInventory(
                owner='watch-removed', directory_batch_limit=1,
            ).run_once()
            self.assertTrue(removed['watch_set_changed'])

    def test_manual_generation_converges_with_ten_thousand_persisted_directories(self):
        with tempfile.TemporaryDirectory() as root:
            strm_ingest_db.register_inventory_roots([root])
            children = [
                os.path.join(root, f'Persisted {index:05d}')
                for index in range(9999)
            ]
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    execute_values(
                        cursor,
                        """
                        INSERT INTO strm_ingest_inventory_directories (
                            root_path, directory_path, parent_path, active, dirty
                        ) VALUES %s
                        """,
                        [(root, child, root, True, False) for child in children],
                        page_size=500,
                    )
            audit_id = strm_ingest_db.create_manual_inventory_audit([root])['audit_id']
            completed_count = 7938
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE strm_ingest_inventory_directories "
                        "SET dirty = FALSE, manual_audit_id = NULL "
                        "WHERE root_path = %s AND directory_path = ANY(%s)",
                        (root, children[:completed_count]),
                    )
                    cursor.execute(
                        "UPDATE strm_ingest_inventory_manual_audits "
                        "SET state = 'running', completed_directories = %s, "
                        "total_directories = 10000, started_at = NOW() "
                        "WHERE audit_id = %s",
                        (completed_count, audit_id),
                    )
                    cursor.execute(
                        "UPDATE strm_ingest_inventory_directories "
                        "SET next_audit_at = CASE "
                        "WHEN directory_path = %s THEN NOW() - INTERVAL '1 hour' "
                        "ELSE NOW() + INTERVAL '1 hour' END "
                        "WHERE root_path = %s",
                        (root, root),
                    )
            root_claim = strm_ingest_db.claim_inventory_directories(
                'scale-root-10000', limit=1, manual_audit_id=audit_id,
            )
            self.assertEqual([root], [row['directory_path'] for row in root_claim])
            root_result = strm_ingest_db.record_inventory_audit_batch(
                root_claim[0],
                files={},
                child_directories=children,
                next_cursor=None,
                complete=True,
                db_batch_size=500,
                audit_interval_hours=24,
            )
            self.assertEqual(20, root_result['db_batches'])
            while True:
                claims = strm_ingest_db.claim_inventory_directories(
                    'scale-children-10000', limit=32, manual_audit_id=audit_id,
                )
                if not claims:
                    break
                for claim in claims:
                    strm_ingest_db.record_inventory_audit_batch(
                        claim,
                        files={},
                        child_directories=[],
                        next_cursor=None,
                        complete=True,
                        db_batch_size=500,
                        audit_interval_hours=24,
                    )
            final = strm_ingest_db.get_manual_inventory_audit(audit_id)
            self.assertEqual('completed', final['state'])
            self.assertEqual(10000, final['total_directories'])
            self.assertEqual(10000, final['completed_directories'])
            self.assertEqual(0, final['pending_directories'])
            self.assertEqual(0, final['claimed_directories'])
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT COUNT(*) AS count "
                        "FROM strm_ingest_inventory_directories "
                        "WHERE active = TRUE AND dirty = FALSE "
                        "AND manual_audit_id = %s",
                        (audit_id,),
                    )
                    self.assertEqual(0, cursor.fetchone()['count'])

    def test_manual_progress_and_cancel_are_isolated_from_directory_event_work(self):
        with tempfile.TemporaryDirectory() as root:
            manual_directory = os.path.join(root, 'Manual Target')
            existing_event_directory = os.path.join(root, 'Existing Event')
            os.mkdir(manual_directory)
            os.mkdir(existing_event_directory)
            strm_ingest_db.register_inventory_roots([root])
            for directory in (manual_directory, existing_event_directory):
                path = os.path.join(directory, 'E01.strm')
                with open(path, 'w', encoding='utf-8') as handle:
                    handle.write('controlled://episode')
                strm_ingest_db.enqueue_paths(
                    [path], source='test', last_error='known directory'
                )
                strm_ingest_db.record_file_event(root, path, event_kind='ingest')

            strm_ingest_db.mark_directory_dirty(
                root, existing_event_directory, event_kind='directory_created'
            )
            event_claim = strm_ingest_db.claim_inventory_directories(
                'event-owner', limit=1
            )
            self.assertEqual([existing_event_directory], [row['directory_path'] for row in event_claim])

            manual = strm_ingest_db.create_manual_inventory_audit([root])
            audit_id = manual['audit_id']
            status = strm_ingest_db.get_manual_inventory_audit(audit_id)
            self.assertEqual(2, status['total_directories'])
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        'SELECT dirty, claim_owner, manual_audit_id '
                        'FROM strm_ingest_inventory_directories '
                        'WHERE root_path = %s AND directory_path = %s',
                        (root, existing_event_directory),
                    )
                    existing_event = cursor.fetchone()
            self.assertTrue(existing_event['dirty'])
            self.assertEqual('event-owner', existing_event['claim_owner'])
            self.assertIsNone(existing_event['manual_audit_id'])

            new_event_directory = os.path.join(root, 'New Event')
            os.mkdir(new_event_directory)
            strm_ingest_db.record_directory_created(root, new_event_directory)
            status = strm_ingest_db.get_manual_inventory_audit(audit_id)
            self.assertEqual(1, status['total_directories'])
            self.assertEqual(1, status['pending_directories'])

            result = IncrementalStrmInventory(
                owner='manual-isolated', directory_batch_limit=1,
            ).run_once(manual_audit_id=audit_id, claim_limit=1)
            self.assertEqual(1, result['completed'])
            finished = strm_ingest_db.get_manual_inventory_audit(audit_id)
            self.assertEqual('completed', finished['state'])
            self.assertEqual(100, finished['progress'])
            self.assertGreaterEqual(strm_ingest_db.get_inventory_summary()['dirty_count'], 3)

            second = strm_ingest_db.create_manual_inventory_audit([root])
            second_id = second['audit_id']
            stop_event_directory = os.path.join(root, 'Stop Event')
            os.mkdir(stop_event_directory)
            strm_ingest_db.record_directory_created(root, stop_event_directory)
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        'SELECT dirty, claim_owner, manual_audit_id '
                        'FROM strm_ingest_inventory_directories '
                        'WHERE root_path = %s AND directory_path = %s',
                        (root, stop_event_directory),
                    )
                    before_cancel = dict(cursor.fetchone())
            self.assertTrue(strm_ingest_db.cancel_manual_inventory_audit(second_id))
            with connection.get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        'SELECT dirty, claim_owner, manual_audit_id '
                        'FROM strm_ingest_inventory_directories '
                        'WHERE root_path = %s AND directory_path = %s',
                        (root, stop_event_directory),
                    )
                    after_cancel = dict(cursor.fetchone())
            self.assertEqual(before_cancel, after_cancel)
            self.assertTrue(after_cancel['dirty'])
            self.assertIsNone(after_cancel['manual_audit_id'])


if __name__ == '__main__':
    unittest.main()
