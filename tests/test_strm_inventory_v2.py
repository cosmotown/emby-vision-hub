import ast
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import constants
import monitor_service
from services.strm_inventory import IncrementalStrmInventory

if not hasattr(logging.Logger, 'trace'):
    logging.Logger.trace = logging.Logger.debug


class _Event:
    def __init__(self, src_path, *, dest_path=None, is_directory=False):
        self.src_path = src_path
        self.dest_path = dest_path
        self.is_directory = is_directory


class StrmInventoryV2Tests(unittest.TestCase):
    def test_monitor_runtime_contains_no_recursive_walk_or_legacy_inventory_call(self):
        source = Path(monitor_service.__file__).read_text(encoding='utf-8')
        tree = ast.parse(source)
        calls = [ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)]
        self.assertNotIn('os.walk', calls)
        self.assertNotIn('collect_strm_inventory', source)
        self.assertIn('_run_incremental_reconcile_loop', source)

    def test_start_registers_inventory_without_full_walk(self):
        processor = mock.Mock()
        config = {
            constants.CONFIG_OPTION_MONITOR_ENABLED: True,
            constants.CONFIG_OPTION_MONITOR_PATHS: ['/STRM'],
            constants.CONFIG_OPTION_MONITOR_EXTENSIONS: ['.strm'],
            constants.CONFIG_OPTION_MONITOR_EXCLUDE_DIRS: ['/STRM'],
            constants.CONFIG_OPTION_MONITOR_FULL_SCAN_INTERVAL_HOURS: 0,
        }
        observer = mock.Mock()
        with mock.patch.object(monitor_service, 'Observer', return_value=observer), \
             mock.patch.object(monitor_service.os.path, 'exists', return_value=True), \
             mock.patch.object(monitor_service.os.path, 'isdir', return_value=True), \
             mock.patch.object(monitor_service.os, 'walk', side_effect=AssertionError('startup walk')), \
             mock.patch.object(monitor_service.strm_ingest_db, 'register_inventory_roots') as register, \
             mock.patch.object(monitor_service.strm_ingest_db, 'recover_processing', return_value=0), \
             mock.patch.object(monitor_service.strm_ingest_db, 'prune_completed'), \
             mock.patch.object(monitor_service.threading, 'Thread') as thread:
            thread.return_value.is_alive.return_value = False
            service = monitor_service.MonitorService(config, processor)
            service.start()
            service.stop()
        register.assert_called_once_with(['/STRM'], audit_interval_hours=24)

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

    def test_scandir_batch_is_bounded_and_persists_cursor(self):
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
            inventory = IncrementalStrmInventory(owner='one', entry_batch_limit=25)
            with mock.patch(
                'services.strm_inventory.strm_ingest_db.record_inventory_audit_batch',
                return_value={'accepted': True},
            ) as record, mock.patch('services.strm_inventory.os.walk', side_effect=AssertionError('audit walk')):
                inventory.scan_claim(claim)
        self.assertEqual(25, len(record.call_args.kwargs['files']))
        self.assertFalse(record.call_args.kwargs['complete'])
        self.assertEqual('0024.strm', record.call_args.kwargs['next_cursor'])

    def test_run_once_respects_directory_claim_limit(self):
        inventory = IncrementalStrmInventory(owner='owner', directory_batch_limit=3)
        with mock.patch(
            'services.strm_inventory.strm_ingest_db.claim_inventory_directories',
            return_value=[],
        ) as claim:
            result = inventory.run_once()
        self.assertEqual(0, result['claimed'])
        self.assertEqual(3, claim.call_args.kwargs['limit'])

    def test_manual_full_audit_is_persisted_bounded_work_not_recursive_scan(self):
        source = Path('routes/tasks.py').read_text(encoding='utf-8')
        self.assertIn("'/strm-inventory/full-audit'", source)
        self.assertIn('request_full_inventory_audit', source)
        self.assertIn("'recursive_os_walk': False", source)


if __name__ == '__main__':
    unittest.main()
