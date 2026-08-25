import ast
import errno
import logging
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import constants
import monitor_service
from tasks import core as task_core
from tasks import media as media_tasks
from services.strm_inventory import IncrementalStrmInventory, InventoryAuditError

if not hasattr(logging.Logger, 'trace'):
    logging.Logger.trace = logging.Logger.debug


class _Event:
    def __init__(self, src_path, *, dest_path=None, is_directory=False):
        self.src_path = src_path
        self.dest_path = dest_path
        self.is_directory = is_directory


class StrmInventoryV2Tests(unittest.TestCase):
    def test_retry_path_missing_requires_successful_parent_snapshot(self):
        existing = '/STRM/Shows/A/S01E01.strm'
        missing = '/STRM/Shows/A/S01E02.strm'
        inaccessible = '/STRM/Offline/S01E03.strm'

        class _Entry:
            name = 'S01E01.strm'

        class _Scandir:
            def __init__(self, entries):
                self.entries = entries

            def __enter__(self):
                return iter(self.entries)

            def __exit__(self, *_args):
                return False

        def scandir(path):
            if path == '/STRM/Offline':
                raise OSError('mount unavailable')
            return _Scandir([_Entry()])

        with mock.patch.object(monitor_service.os.path, 'isfile', return_value=False), \
             mock.patch.object(monitor_service.os, 'scandir', side_effect=scandir):
            existing_paths, missing_paths, inaccessible_paths = (
                monitor_service._classify_retry_paths([existing, missing, inaccessible])
            )

        self.assertEqual([], existing_paths)
        self.assertEqual([missing], missing_paths)
        self.assertEqual(sorted([existing, inaccessible]), inaccessible_paths)

    def test_monitor_runtime_contains_no_recursive_walk_or_legacy_inventory_call(self):
        source = Path(monitor_service.__file__).read_text(encoding='utf-8')
        tree = ast.parse(source)
        calls = [ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)]
        self.assertNotIn('os.walk', calls)
        self.assertNotIn('collect_strm_inventory', source)
        self.assertNotIn('recursive=True', source)
        self.assertIn('PersistedDirectoryObserver', source)
        self.assertNotIn('_run_incremental_reconcile_loop', source)
        self.assertIn('_run_requested_inventory_loop', source)
        self.assertNotIn('CONFIG_OPTION_MONITOR_FULL_SCAN_INTERVAL_HOURS', source)

    def test_start_registers_inventory_without_full_walk(self):
        processor = mock.Mock()
        config = {
            constants.CONFIG_OPTION_MONITOR_ENABLED: True,
            constants.CONFIG_OPTION_MONITOR_PATHS: ['/STRM'],
            constants.CONFIG_OPTION_MONITOR_EXTENSIONS: ['.strm'],
            constants.CONFIG_OPTION_MONITOR_EXCLUDE_DIRS: ['/STRM'],
            # A legacy non-zero value must be accepted but ignored.
            constants.CONFIG_OPTION_MONITOR_FULL_SCAN_INTERVAL_HOURS: 24,
        }
        observer = mock.Mock(watch_count=1, backend_thread_count=2, max_user_watches=1048576)
        with mock.patch.object(monitor_service, 'PersistedDirectoryObserver', return_value=observer), \
             mock.patch.object(monitor_service.os.path, 'exists', return_value=True), \
             mock.patch.object(monitor_service.os.path, 'isdir', return_value=True), \
             mock.patch.object(monitor_service.os, 'walk', side_effect=AssertionError('startup walk')), \
             mock.patch.object(monitor_service.strm_ingest_db, 'register_inventory_roots') as register, \
             mock.patch.object(monitor_service.strm_ingest_db, 'list_active_inventory_directories', return_value=['/STRM']), \
             mock.patch.object(monitor_service.strm_ingest_db, 'recover_processing', return_value=0), \
             mock.patch.object(monitor_service.strm_ingest_db, 'prune_completed'), \
             mock.patch.object(monitor_service.strm_ingest_db, 'request_full_inventory_audit') as request_full, \
             mock.patch.object(monitor_service.threading, 'Thread') as thread:
            thread.return_value.is_alive.return_value = False
            service = monitor_service.MonitorService(config, processor)
            service.start()
            service.stop()
        register.assert_called_once_with(['/STRM'], audit_interval_hours=24)
        request_full.assert_not_called()
        thread.assert_any_call(
            target=mock.ANY,
            name='strm-inventory-explicit',
            daemon=True,
        )

    def test_requested_worker_never_runs_for_elapsed_time_or_legacy_interval(self):
        processor = mock.Mock()
        processor.is_stop_requested.return_value = False
        service = monitor_service.MonitorService({
            constants.CONFIG_OPTION_MONITOR_FULL_SCAN_INTERVAL_HOURS: 1,
        }, processor)
        service._started = True
        service._inventory = mock.Mock()
        worker = threading.Thread(target=service._run_requested_inventory_loop)
        worker.start()
        time.sleep(0.05)
        service._inventory.run_once.assert_not_called()
        service._reconcile_stop.set()
        service._inventory_requested.set()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())

    def test_manual_request_wakes_bounded_worker_and_drains_only_claimed_work(self):
        processor = mock.Mock()
        processor.is_stop_requested.return_value = False
        service = monitor_service.MonitorService({}, processor)
        service._started = True
        service.observer = mock.Mock()
        service.observer.sync_from_persistence.return_value = {'watched': 2}
        completed = threading.Event()

        def run_once(**_kwargs):
            if service._inventory.run_once.call_count == 1:
                return {
                    'claimed': 1, 'completed': 1, 'ingest': 0, 'delete': 0,
                    'failed': 0, 'physical_enumerations': 1,
                    'entries_seen': 1, 'db_batches': 0,
                }
            completed.set()
            return {'claimed': 0}

        service._inventory = mock.Mock()
        service._inventory.run_once.side_effect = run_once
        with mock.patch.object(
            monitor_service.strm_ingest_db,
            'list_active_manual_inventory_audits',
            return_value=[],
        ):
            worker = threading.Thread(target=service._run_requested_inventory_loop)
            worker.start()
            self.assertTrue(service.request_inventory_audit_processing())
            self.assertTrue(completed.wait(timeout=1))
            self.assertEqual(2, service._inventory.run_once.call_count)
            service.observer.sync_from_persistence.assert_not_called()
            service._reconcile_stop.set()
            service._inventory_requested.set()
            worker.join(timeout=1)

    def test_manual_worker_claims_four_directories_serially_with_stop_callback(self):
        processor = mock.Mock()
        processor.is_stop_requested.return_value = False
        service = monitor_service.MonitorService({}, processor)
        service._started = True
        service.observer = mock.Mock()
        service.observer.sync_from_persistence.return_value = {'watched': 1}
        service._inventory = mock.Mock()
        results = [
            {'claimed': 0},
            {
                'claimed': 1, 'completed': 1, 'ingest': 0, 'delete': 0,
                'failed': 0, 'physical_enumerations': 1,
                'entries_seen': 0, 'db_batches': 0,
            },
        ]
        service._inventory.run_once.side_effect = results
        manual_done = threading.Event()

        def status(_audit_id):
            if service._inventory.run_once.call_count < 2:
                return {'state': 'running', 'pending_directories': 1}
            manual_done.set()
            return {'state': 'completed', 'pending_directories': 0}

        with mock.patch.object(
            monitor_service.strm_ingest_db,
            'list_active_manual_inventory_audits', return_value=['audit-1'],
        ), mock.patch.object(
            monitor_service.strm_ingest_db,
            'get_manual_inventory_audit', side_effect=status,
        ), mock.patch.object(
            monitor_service.time, 'sleep', wraps=time.sleep,
        ) as cooperative_sleep:
            worker = threading.Thread(target=service._run_requested_inventory_loop)
            worker.start()
            self.assertTrue(service.request_inventory_audit_processing())
            self.assertTrue(manual_done.wait(timeout=1))
            service._reconcile_stop.set()
            service._inventory_requested.set()
            worker.join(timeout=1)
        manual_call = service._inventory.run_once.call_args_list[1]
        self.assertEqual('audit-1', manual_call.kwargs['manual_audit_id'])
        self.assertEqual(4, manual_call.kwargs['claim_limit'])
        self.assertIs(manual_call.kwargs['should_stop'], processor.is_stop_requested)
        cooperative_sleep.assert_any_call(0.001)

    def test_watch_sync_happens_once_for_changed_batch_and_never_for_unchanged_batch(self):
        processor = mock.Mock()
        processor.is_stop_requested.return_value = False
        service = monitor_service.MonitorService({}, processor)
        service._started = True
        service.observer = mock.Mock()
        service.observer.sync_from_persistence.return_value = {'watched': 5}
        service._inventory = mock.Mock()
        results = [
            {
                'claimed': 4, 'completed': 4, 'ingest': 0, 'delete': 0,
                'failed': 0, 'physical_enumerations': 4,
                'entries_seen': 0, 'db_batches': 0,
                'watch_set_changed': False,
            },
            {
                'claimed': 4, 'completed': 4, 'ingest': 0, 'delete': 0,
                'failed': 0, 'physical_enumerations': 4,
                'entries_seen': 1, 'db_batches': 1,
                'watch_set_changed': True,
            },
            {'claimed': 0, 'watch_set_changed': False},
        ]
        drained = threading.Event()

        def run_once(**kwargs):
            value = results.pop(0)
            if not value.get('claimed'):
                drained.set()
            return value

        service._inventory.run_once.side_effect = run_once
        with mock.patch.object(
            monitor_service.strm_ingest_db,
            'list_active_manual_inventory_audits',
            return_value=[],
        ):
            worker = threading.Thread(target=service._run_requested_inventory_loop)
            worker.start()
            self.assertTrue(service.request_inventory_audit_processing())
            self.assertTrue(drained.wait(timeout=1))
            service._reconcile_stop.set()
            service._inventory_requested.set()
            worker.join(timeout=1)
        service.observer.sync_from_persistence.assert_called_once_with()

    def test_directory_event_wakes_inventory_but_file_event_uses_exact_queue_only(self):
        notifier = mock.Mock()
        handler = monitor_service.MediaFileHandler(
            ['.strm'], inventory_roots=['/STRM'],
            inventory_audit_notifier=notifier,
        )
        with mock.patch.object(
            monitor_service.strm_ingest_db, 'record_directory_created'
        ), mock.patch.object(handler, '_enqueue_file') as enqueue:
            handler.on_created(_Event('/STRM/Show', is_directory=True))
            handler.on_created(_Event('/STRM/Show/S01E04.strm'))
        notifier.assert_called_once_with(True)
        enqueue.assert_called_once_with('/STRM/Show/S01E04.strm')

    def test_directory_move_and_remove_request_watch_set_sync(self):
        notifier = mock.Mock()
        handler = monitor_service.MediaFileHandler(
            ['.strm'], inventory_roots=['/STRM'],
            inventory_audit_notifier=notifier,
        )
        with mock.patch.object(
            monitor_service.strm_ingest_db, 'record_directory_moved', return_value=[],
        ), mock.patch.object(
            monitor_service.strm_ingest_db, 'record_directory_removed', return_value=[],
        ):
            handler.on_moved(_Event('/STRM/Old', dest_path='/STRM/New', is_directory=True))
            handler.on_deleted(_Event('/STRM/New', is_directory=True))
        self.assertEqual([mock.call(True), mock.call(True)], notifier.call_args_list)

    def test_directory_move_uses_persisted_paths_and_never_walks_destination(self):
        handler = monitor_service.MediaFileHandler(
            ['.strm'], inventory_roots=['/STRM'],
        )
        old = '/STRM/Show'
        new = '/STRM/Renamed Show'
        pair = (f'{old}/S01E01.strm', f'{new}/S01E01.strm')
        with mock.patch.object(
            monitor_service.strm_ingest_db,
            'record_directory_moved',
            return_value=[pair],
        ) as moved, mock.patch.object(
            monitor_service.os, 'walk', side_effect=AssertionError('directory move walk')
        ), mock.patch.object(handler, '_enqueue_delete') as deleted, mock.patch.object(
            handler, '_enqueue_file'
        ) as created:
            handler.on_moved(_Event(old, dest_path=new, is_directory=True))
        moved.assert_called_once_with('/STRM', old, new)
        deleted.assert_called_once_with(pair[0])
        created.assert_called_once_with(pair[1])

    def test_file_events_persist_old_and_new_sides(self):
        handler = monitor_service.MediaFileHandler(['.strm'], inventory_roots=['/STRM'])
        with mock.patch.object(monitor_service.strm_ingest_db, 'enqueue_paths') as enqueue, \
             mock.patch.object(monitor_service.strm_ingest_db, 'record_file_event') as record:
            handler._persist_file_event('/STRM/Show/old.strm', operation='delete')
            handler._persist_file_event('/STRM/Show/new.strm', operation='ingest')
        self.assertEqual(['delete', 'ingest'], [call.kwargs['operation'] for call in enqueue.call_args_list])
        self.assertEqual(
            [('delete', '/STRM/Show/old.strm'), ('ingest', '/STRM/Show/new.strm')],
            [(call.kwargs['event_kind'], call.args[1]) for call in record.call_args_list],
        )

    def test_one_hundred_file_events_never_wake_or_mark_directory_inventory(self):
        notifier = mock.Mock()
        handler = monitor_service.MediaFileHandler(
            ['.strm'], inventory_roots=['/STRM'],
            inventory_audit_notifier=notifier,
        )
        with mock.patch.object(
            monitor_service.strm_ingest_db, 'enqueue_paths'
        ) as enqueue, mock.patch.object(
            monitor_service.strm_ingest_db, 'record_file_event'
        ) as record, mock.patch.object(
            monitor_service.strm_ingest_db, 'mark_directory_dirty'
        ) as dirty:
            for index in range(100):
                handler._persist_file_event(
                    f'/STRM/Show {index:03d}/E01.strm', operation='ingest'
                )
        self.assertEqual(100, enqueue.call_count)
        self.assertEqual(100, record.call_count)
        dirty.assert_not_called()
        notifier.assert_not_called()

    def test_exact_delete_does_not_require_directory_inventory(self):
        handler = monitor_service.MediaFileHandler(['.strm'], inventory_roots=['/STRM'])
        with mock.patch.object(
            monitor_service.strm_ingest_db, 'enqueue_paths'
        ) as enqueue, mock.patch.object(
            monitor_service.strm_ingest_db, 'record_file_event'
        ) as record, mock.patch.object(
            monitor_service.strm_ingest_db, 'mark_directory_dirty'
        ) as dirty:
            handler._persist_file_event('/STRM/Show/E01.strm', operation='delete')
        self.assertEqual('delete', enqueue.call_args.kwargs['operation'])
        self.assertEqual('delete', record.call_args.kwargs['event_kind'])
        dirty.assert_not_called()

    def test_large_directory_is_enumerated_once_then_uses_bounded_db_batches(self):
        with tempfile.TemporaryDirectory() as root:
            for index in range(120):
                Path(root, f'{index:04d}.strm').write_text('url', encoding='utf-8')
            claim = {
                'root_path': root,
                'directory_path': root,
                'audit_cursor': None,
                'audit_generation': 1,
                'event_version': 0,
                'claim_owner': 'one',
            }
            inventory = IncrementalStrmInventory(owner='one', db_batch_size=25)
            real_scandir = os.scandir
            scandir_calls = []

            def counted_scandir(path):
                scandir_calls.append(path)
                return real_scandir(path)

            with mock.patch(
                'services.strm_inventory.strm_ingest_db.record_inventory_audit_batch',
                return_value={'accepted': True, 'db_batches': 5},
            ) as record, mock.patch('services.strm_inventory.os.scandir', side_effect=counted_scandir):
                result = inventory.scan_claim(claim)
        self.assertEqual([root], scandir_calls)
        self.assertEqual(120, len(record.call_args.kwargs['files']))
        self.assertTrue(record.call_args.kwargs['complete'])
        self.assertIsNone(record.call_args.kwargs['next_cursor'])
        self.assertEqual(25, record.call_args.kwargs['db_batch_size'])
        self.assertEqual(1, result['physical_enumerations'])
        self.assertEqual(120, result['entries_seen'])

    def test_run_once_respects_directory_claim_limit(self):
        inventory = IncrementalStrmInventory(owner='owner', directory_batch_limit=3)
        with mock.patch(
            'services.strm_inventory.strm_ingest_db.claim_inventory_directories',
            return_value=[],
        ) as claim:
            result = inventory.run_once()
        self.assertEqual(0, result['claimed'])
        self.assertEqual(3, claim.call_args.kwargs['limit'])

    def test_stop_releases_unstarted_claims_without_parallel_scans(self):
        claims = [
            {
                'root_path': '/STRM', 'directory_path': f'/STRM/{index}',
                'audit_generation': 1, 'event_version': 0,
                'claim_owner': 'owner', 'manual_audit_id': 'audit',
            }
            for index in range(4)
        ]
        inventory = IncrementalStrmInventory(owner='owner', directory_batch_limit=4)
        stop = mock.Mock(side_effect=[False, True])
        with mock.patch(
            'services.strm_inventory.strm_ingest_db.claim_inventory_directories',
            return_value=claims,
        ) as claim, mock.patch.object(
            inventory, 'scan_claim',
            return_value={
                'accepted': True, 'complete': True, 'added': [], 'changed': [],
                'removed': [], 'physical_enumerations': 1, 'entries_seen': 0,
                'db_batches': 0, 'watch_set_changed': False,
            },
        ) as scan, mock.patch(
            'services.strm_inventory.strm_ingest_db.release_inventory_directory_claims',
            return_value=3,
        ) as release:
            result = inventory.run_once(
                manual_audit_id='audit', claim_limit=4, should_stop=stop,
            )
        self.assertEqual(4, claim.call_args.kwargs['limit'])
        self.assertEqual(1, scan.call_count)
        release.assert_called_once_with(claims[1:])
        self.assertEqual(1, result['completed'])
        self.assertEqual(3, result['released'])
        self.assertEqual(1, result['physical_enumerations'])

    def test_permission_error_is_fail_closed_with_zero_delete(self):
        claim = {
            'root_path': '/STRM', 'directory_path': '/STRM',
            'audit_generation': 1, 'event_version': 0, 'claim_owner': 'owner',
        }
        inventory = IncrementalStrmInventory(owner='owner')
        deleted = mock.Mock()
        with mock.patch.object(
            monitor_service.os.path, 'exists', return_value=True,
        ), mock.patch(
            'services.strm_inventory.os.lstat',
            return_value=mock.Mock(st_mode=0o040755),
        ), mock.patch(
            'services.strm_inventory.os.scandir', side_effect=PermissionError(errno.EACCES, 'denied'),
        ), mock.patch(
            'services.strm_inventory.strm_ingest_db.claim_inventory_directories', return_value=[claim],
        ), mock.patch(
            'services.strm_inventory.strm_ingest_db.fail_inventory_directory_claim',
        ) as failed, mock.patch(
            'services.strm_inventory.strm_ingest_db.record_inventory_audit_batch',
        ) as record:
            result = inventory.run_once(on_delete=deleted)
        self.assertEqual(1, result['failed'])
        self.assertEqual(0, result['delete'])
        deleted.assert_not_called()
        record.assert_not_called()
        failed.assert_called_once_with(claim, 'permission_denied')

    def test_transient_io_error_is_fail_closed_with_zero_delete(self):
        claim = {
            'root_path': '/STRM', 'directory_path': '/STRM/Shows',
            'audit_generation': 1, 'event_version': 0, 'claim_owner': 'owner',
        }
        inventory = IncrementalStrmInventory(owner='owner')
        with mock.patch(
            'services.strm_inventory.os.lstat',
            return_value=mock.Mock(st_mode=0o040755),
        ), mock.patch(
            'services.strm_inventory.os.scandir', side_effect=OSError(errno.EIO, 'io'),
        ), mock.patch(
            'services.strm_inventory.strm_ingest_db.claim_inventory_directories', return_value=[claim],
        ), mock.patch(
            'services.strm_inventory.strm_ingest_db.fail_inventory_directory_claim',
        ) as failed:
            result = inventory.run_once(on_delete=mock.Mock())
        self.assertEqual(1, result['failed'])
        self.assertEqual(0, result['delete'])
        failed.assert_called_once_with(claim, 'transient_io_error')

    def test_missing_root_is_mount_unavailable_not_empty_snapshot(self):
        claim = {
            'root_path': '/STRM/unavailable', 'directory_path': '/STRM/unavailable',
            'audit_generation': 1, 'event_version': 0, 'claim_owner': 'owner',
        }
        inventory = IncrementalStrmInventory(owner='owner')
        with mock.patch(
            'services.strm_inventory.os.lstat', side_effect=FileNotFoundError(errno.ENOENT, 'missing'),
        ), mock.patch(
            'services.strm_inventory.strm_ingest_db.claim_inventory_directories', return_value=[claim],
        ), mock.patch(
            'services.strm_inventory.strm_ingest_db.fail_inventory_directory_claim',
        ) as failed, mock.patch(
            'services.strm_inventory.strm_ingest_db.record_inventory_audit_batch',
        ) as record:
            result = inventory.run_once(on_delete=mock.Mock())
        self.assertEqual(1, result['failed'])
        self.assertEqual(0, result['delete'])
        record.assert_not_called()
        failed.assert_called_once_with(claim, 'mount_unavailable')

    def test_missing_manual_claim_uses_ancestor_snapshot_proof_once(self):
        claim = {
            'root_path': '/STRM',
            'directory_path': '/STRM/Shows/Old/Season 1',
            'audit_generation': 1,
            'event_version': 0,
            'claim_owner': 'owner',
            'manual_audit_id': 'audit',
        }
        inventory = IncrementalStrmInventory(owner='owner')
        recovered = {
            'accepted': True, 'proven': True, 'complete': True,
            'removed': ['/STRM/Shows/Old/Season 1/E01.strm'],
            'physical_enumerations': 1, 'entries_seen': 3,
            'db_batches': 0, 'watch_set_changed': True,
        }
        deleted = mock.Mock()
        with mock.patch(
            'services.strm_inventory.strm_ingest_db.claim_inventory_directories',
            return_value=[claim],
        ), mock.patch.object(
            inventory, 'scan_claim', side_effect=InventoryAuditError('inaccessible'),
        ), mock.patch.object(
            inventory, '_recover_missing_claim_from_ancestor', return_value=recovered,
        ) as recover, mock.patch(
            'services.strm_inventory.strm_ingest_db.fail_inventory_directory_claim',
        ) as failed:
            result = inventory.run_once(
                manual_audit_id='audit', claim_limit=1, on_delete=deleted,
            )
        recover.assert_called_once_with(claim)
        failed.assert_not_called()
        deleted.assert_called_once_with(recovered['removed'])
        self.assertEqual(1, result['completed'])
        self.assertEqual(0, result['failed'])
        self.assertEqual(1, result['physical_enumerations'])
        self.assertTrue(result['watch_set_changed'])

    def test_ancestor_permission_failure_remains_fail_closed(self):
        claim = {
            'root_path': '/STRM',
            'directory_path': '/STRM/Shows/Old/Season 1',
            'audit_generation': 1,
            'event_version': 0,
            'claim_owner': 'owner',
            'manual_audit_id': 'audit',
        }
        inventory = IncrementalStrmInventory(owner='owner')
        with mock.patch(
            'services.strm_inventory.strm_ingest_db.claim_inventory_directories',
            return_value=[claim],
        ), mock.patch.object(
            inventory, 'scan_claim', side_effect=InventoryAuditError('inaccessible'),
        ), mock.patch.object(
            inventory, '_recover_missing_claim_from_ancestor',
            side_effect=InventoryAuditError('permission_denied'),
        ), mock.patch(
            'services.strm_inventory.strm_ingest_db.fail_inventory_directory_claim',
        ) as failed, mock.patch(
            'services.strm_inventory.strm_ingest_db.record_inventory_ancestor_proof',
        ) as proof:
            result = inventory.run_once(
                manual_audit_id='audit', claim_limit=1, on_delete=mock.Mock(),
            )
        self.assertEqual(1, result['failed'])
        self.assertEqual(0, result['delete'])
        failed.assert_called_once_with(claim, 'permission_denied')
        proof.assert_not_called()

    def test_unavailable_configured_root_blocks_ancestor_proof(self):
        claim = {
            'root_path': '/STRM',
            'directory_path': '/STRM/Shows/Old/Season 1',
            'claim_owner': 'owner',
            'manual_audit_id': 'audit',
        }
        inventory = IncrementalStrmInventory(owner='owner')
        candidates = [
            {'directory_path': '/STRM/Shows/Old', 'manual_audit_id': None, 'claim_owner': None},
            {'directory_path': '/STRM', 'manual_audit_id': None, 'claim_owner': None},
        ]
        with mock.patch(
            'services.strm_inventory.strm_ingest_db.get_inventory_ancestor_candidates',
            return_value=candidates,
        ), mock.patch.object(
            inventory,
            '_snapshot_directory',
            side_effect=[
                InventoryAuditError('inaccessible'),
                InventoryAuditError('mount_unavailable'),
            ],
        ), mock.patch(
            'services.strm_inventory.strm_ingest_db.record_inventory_ancestor_proof',
        ) as proof:
            with self.assertRaisesRegex(InventoryAuditError, 'mount_unavailable'):
                inventory._recover_missing_claim_from_ancestor(claim)
        proof.assert_not_called()

    def test_symlink_ancestor_blocks_proof(self):
        claim = {
            'root_path': '/STRM',
            'directory_path': '/STRM/Shows/Old/Season 1',
            'claim_owner': 'owner',
            'manual_audit_id': 'audit',
        }
        inventory = IncrementalStrmInventory(owner='owner')
        with mock.patch(
            'services.strm_inventory.strm_ingest_db.get_inventory_ancestor_candidates',
            return_value=[{
                'directory_path': '/STRM/Shows/Old',
                'manual_audit_id': None,
                'claim_owner': None,
            }],
        ), mock.patch.object(
            inventory,
            '_snapshot_directory',
            side_effect=InventoryAuditError('symlink_blocked'),
        ), mock.patch(
            'services.strm_inventory.strm_ingest_db.record_inventory_ancestor_proof',
        ) as proof:
            with self.assertRaisesRegex(InventoryAuditError, 'symlink_blocked'):
                inventory._recover_missing_claim_from_ancestor(claim)
        proof.assert_not_called()

    def test_manual_full_audit_is_persisted_bounded_work_not_recursive_scan(self):
        source = Path('routes/tasks.py').read_text(encoding='utf-8')
        self.assertIn("'/strm-inventory/full-audit'", source)
        self.assertIn('create_manual_inventory_audit', source)
        self.assertIn("'audit_id': audit_id", source)
        self.assertIn("'recursive_os_walk': False", source)
        self.assertIn("'manual_only': True", source)

    def test_strm_gap_task_is_manual_only_and_metadata_sync_remains_separate(self):
        all_tasks = task_core.get_task_registry(context='all')
        chain_tasks = task_core.get_task_registry(context='chain')
        self.assertEqual('STRM 查漏', all_tasks['scan-monitor-folders'][1])
        self.assertNotIn('scan-monitor-folders', chain_tasks)
        self.assertIn('populate-metadata', chain_tasks)
        populate_source = Path(media_tasks.task_populate_metadata_cache.__code__.co_filename).read_text(
            encoding='utf-8'
        )
        start = populate_source.index('def task_populate_metadata_cache')
        end = populate_source.index('\ndef ', start + 1)
        body = populate_source[start:end]
        self.assertNotIn('request_full_inventory_audit', body)
        self.assertNotIn('IncrementalStrmInventory', body)

    def test_manual_task_remains_running_until_persisted_generation_completes(self):
        processor = mock.Mock()
        processor.is_stop_requested.return_value = False
        processor.config = {
            constants.CONFIG_OPTION_MONITOR_ENABLED: True,
            constants.CONFIG_OPTION_MONITOR_PATHS: ['/STRM'],
            constants.CONFIG_OPTION_MONITOR_EXCLUDE_DIRS: ['/STRM'],
        }
        with mock.patch(
            'monitor_service.inventory_audit_processing_available', return_value=True,
        ), mock.patch(
            'monitor_service.request_inventory_audit_processing', return_value=True,
        ) as wake, mock.patch.object(
            media_tasks.strm_ingest_db,
            'create_manual_inventory_audit',
            return_value={'audit_id': 'audit-1', 'total_directories': 100},
        ) as create, mock.patch.object(
            media_tasks.strm_ingest_db,
            'get_manual_inventory_audit',
            side_effect=[
                {'state': 'queued', 'total_directories': 100, 'completed_directories': 0},
                {
                    'state': 'running', 'progress': 30, 'total_directories': 100,
                    'completed_directories': 30, 'claimed_directories': 1,
                },
                {
                    'state': 'completed', 'progress': 100, 'total_directories': 100,
                    'completed_directories': 100, 'claimed_directories': 0,
                },
            ],
        ), mock.patch.object(
            media_tasks.task_manager, 'update_status_from_thread'
        ) as update, mock.patch.object(media_tasks.time, 'sleep'):
            media_tasks.task_scan_monitor_folders(processor)
        create.assert_called_once_with(['/STRM'])
        wake.assert_called_once_with()
        self.assertIn(mock.call(30, 'STRM 查漏正在处理：30/100，处理中 1'), update.call_args_list)
        self.assertEqual(
            mock.call(100, 'STRM 查漏完成：已核对 100 个目录'),
            update.call_args_list[-1],
        )

    def test_manual_task_stop_cancels_generation_and_preserves_pending_work(self):
        processor = mock.Mock()
        processor.config = {
            constants.CONFIG_OPTION_MONITOR_ENABLED: True,
            constants.CONFIG_OPTION_MONITOR_PATHS: ['/STRM'],
            constants.CONFIG_OPTION_MONITOR_EXCLUDE_DIRS: ['/STRM'],
        }
        processor.is_stop_requested.side_effect = [False, True]
        with mock.patch(
            'monitor_service.inventory_audit_processing_available', return_value=True,
        ), mock.patch(
            'monitor_service.request_inventory_audit_processing', return_value=True,
        ), mock.patch.object(
            media_tasks.strm_ingest_db, 'create_manual_inventory_audit',
            return_value={'audit_id': 'audit-stop'},
        ), mock.patch.object(
            media_tasks.strm_ingest_db, 'get_manual_inventory_audit',
            side_effect=[
                {'state': 'queued', 'total_directories': 100},
                {
                    'state': 'running', 'progress': 30, 'total_directories': 100,
                    'completed_directories': 30, 'claimed_directories': 1,
                },
                {
                    'state': 'cancelled', 'progress': 30,
                    'pending_directories': 70,
                },
            ],
        ), mock.patch.object(
            media_tasks.strm_ingest_db, 'cancel_manual_inventory_audit', return_value=True,
        ) as cancel, mock.patch.object(
            media_tasks.task_manager, 'update_status_from_thread'
        ) as update, mock.patch.object(media_tasks.time, 'sleep'):
            media_tasks.task_scan_monitor_folders(processor)
        cancel.assert_called_once_with('audit-stop')
        self.assertEqual(
            mock.call(30, 'STRM 查漏已中止；未处理目录已保留，可再次运行恢复。'),
            update.call_args_list[-1],
        )

    def test_task_manager_preserves_manual_audit_cancelled_status(self):
        class _Processor:
            def __init__(self):
                self.stopped = False

            def clear_stop_signal(self):
                self.stopped = False

            def signal_stop(self):
                self.stopped = True

            def is_stop_requested(self):
                return self.stopped

        processor = _Processor()

        def cancelled_task(current_processor):
            media_tasks.task_manager.update_status_from_thread(
                30,
                'STRM 查漏已中止；未处理目录已保留，可再次运行恢复。',
            )
            current_processor.signal_stop()

        media_tasks.task_manager._execute_task_with_lock(
            cancelled_task, 'STRM 查漏', processor,
        )
        status = media_tasks.task_manager.get_task_status()
        self.assertFalse(status['is_running'])
        self.assertEqual(30, status['progress'])
        self.assertIn('STRM 查漏已中止', status['message'])


if __name__ == '__main__':
    unittest.main()
