import os
import threading
import time
import unittest
from unittest import mock

import constants
import monitor_service


class AdaptiveStrmIngestTests(unittest.TestCase):
    def setUp(self):
        if monitor_service._MONITOR_TASK_POOL.state != 'stopped':
            monitor_service._MONITOR_TASK_POOL.shutdown()
        monitor_service._reset_adaptive_refresh_state()
        with monitor_service.QUEUE_LOCK:
            monitor_service.FILE_EVENT_QUEUE.clear()
            monitor_service.DEBOUNCE_TIMER = None
        with monitor_service.DELETE_QUEUE_LOCK:
            monitor_service.DELETE_EVENT_QUEUE.clear()
            monitor_service.DELETE_DEBOUNCE_TIMER = None
        monitor_service.MonitorService.processor_instance = None

    def tearDown(self):
        if monitor_service._MONITOR_TASK_POOL.state != 'stopped':
            monitor_service._MONITOR_TASK_POOL.shutdown()
        monitor_service._reset_adaptive_refresh_state()
        with monitor_service.QUEUE_LOCK:
            monitor_service.FILE_EVENT_QUEUE.clear()
            monitor_service.DEBOUNCE_TIMER = None
        with monitor_service.DELETE_QUEUE_LOCK:
            monitor_service.DELETE_EVENT_QUEUE.clear()
            monitor_service.DELETE_DEBOUNCE_TIMER = None
        monitor_service.MonitorService.processor_instance = None

    @staticmethod
    def _path(episode):
        return f"/STRM/动漫/航海王 (1999) {{tmdb-37854}}/Season 13/航海王 S13E{episode:03d}.strm"

    def test_small_batches_keep_fast_path(self):
        processor = object()
        for index, now in enumerate((0, 15, 30), start=1):
            path = self._path(index)
            immediate, activated = monitor_service._register_adaptive_refresh_paths(
                processor,
                [path],
                exclude_paths=["/STRM/动漫"],
                now=now,
            )
            self.assertEqual([path], immediate)
            self.assertEqual([], activated)

    def test_fourth_arrival_switches_to_bulk_and_waits_for_quiet(self):
        processor = object()
        for index, now in enumerate((0, 15, 30), start=1):
            immediate, activated = monitor_service._register_adaptive_refresh_paths(
                processor,
                [self._path(index)],
                exclude_paths=["/STRM/动漫"],
                now=now,
            )
            self.assertEqual(1, len(immediate))
            self.assertEqual([], activated)

        fourth = self._path(4)
        immediate, activated = monitor_service._register_adaptive_refresh_paths(
            processor,
            [fourth],
            exclude_paths=["/STRM/动漫"],
            now=45,
        )
        self.assertEqual([], immediate)
        self.assertEqual(1, len(activated))

        fifth = self._path(5)
        immediate, activated = monitor_service._register_adaptive_refresh_paths(
            processor,
            [fifth],
            exclude_paths=["/STRM/动漫"],
            now=60,
        )
        self.assertEqual([], immediate)
        self.assertEqual([], activated)
        self.assertEqual([], monitor_service._pop_due_adaptive_refresh_batches(now=119))

        due = monitor_service._pop_due_adaptive_refresh_batches(now=120)
        self.assertEqual(1, len(due))
        self.assertEqual([fourth, fifth], due[0]['paths'])
        self.assertEqual('quiet', due[0]['reason'])

    def test_large_single_batch_enters_bulk_immediately(self):
        paths = [self._path(index) for index in range(1, 26)]
        immediate, activated = monitor_service._register_adaptive_refresh_paths(
            object(),
            paths,
            exclude_paths=["/STRM/动漫"],
            now=0,
        )
        self.assertEqual([], immediate)
        self.assertEqual(1, len(activated))
        due = monitor_service._pop_due_adaptive_refresh_batches(now=60)
        self.assertEqual(paths, due[0]['paths'])

    def test_flat_library_movies_are_not_merged(self):
        first = "/STRM/电影/电影甲.strm"
        second = "/STRM/电影/电影乙.strm"
        first_key = monitor_service._adaptive_work_key(first, ["/STRM/电影"])
        second_key = monitor_service._adaptive_work_key(second, ["/STRM/电影"])
        self.assertEqual(first, first_key)
        self.assertEqual(second, second_key)
        self.assertNotEqual(first_key, second_key)

    def test_bulk_handler_uses_one_delayed_verification(self):
        path = self._path(450)
        processor = mock.Mock()
        config = {
            constants.CONFIG_OPTION_EMBY_SERVER_URL: "http://emby",
            constants.CONFIG_OPTION_EMBY_API_KEY: "token",
            constants.CONFIG_OPTION_MONITOR_EXCLUDE_REFRESH_DELAY: 0,
        }
        result = {
            'requested': 1,
            'indexed': 1,
            'confirmed_paths': [path],
            'pending': [],
            'query_failed': [],
            'refresh_ok': True,
        }
        with mock.patch.object(
            monitor_service.config_manager,
            'APP_CONFIG',
            config,
        ), mock.patch.object(
            monitor_service,
            'wait_for_paths_stable',
            return_value=([path], []),
        ), mock.patch.object(
            monitor_service,
            'refresh_and_verify_paths',
            return_value=result,
        ) as refresh, mock.patch.object(
            monitor_service.strm_ingest_db,
            'mark_completed',
        ):
            monitor_service._handle_batch_refresh_only_task(
                processor,
                [path],
                bulk_mode=True,
            )

        self.assertEqual(
            (monitor_service.ADAPTIVE_BULK_VERIFY_DELAY_SECONDS,),
            refresh.call_args.kwargs['verify_delays'],
        )
        processor.enqueue_confirmed_ingest_postprocessing.assert_called_once_with([path])

    def test_monitor_background_work_uses_fixed_worker_pool(self):
        pool = monitor_service._MonitorTaskPool()
        pool.start()
        try:
            self.assertEqual(4, pool._executor._max_workers)
            self.assertEqual(64, pool._slots._initial_value)
        finally:
            pool.shutdown()

        path = self._path(1)
        processor = object()
        with mock.patch.object(
            monitor_service,
            '_register_adaptive_refresh_paths',
            return_value=([path], []),
        ), mock.patch.object(
            monitor_service,
            '_submit_monitor_task',
        ) as submit, mock.patch.object(
            monitor_service,
            '_ensure_adaptive_refresh_worker',
        ):
            monitor_service._enqueue_adaptive_refresh_only(processor, [path])

        self.assertEqual(1, submit.call_count)
        self.assertEqual(
            (
                monitor_service._handle_batch_refresh_only_task,
                processor,
                [path],
            ),
            submit.call_args.args,
        )
        self.assertFalse(submit.call_args.kwargs["bulk_mode"])
        self.assertTrue(callable(submit.call_args.kwargs["on_cancel"]))

    def test_pool_start_submit_stop_and_restart_uses_new_executor(self):
        pool = monitor_service._MonitorTaskPool()
        started = threading.Event()
        release = threading.Event()
        completed = []

        def blocking_task():
            started.set()
            release.wait(timeout=2)
            completed.append("old")

        first_generation = pool.start()
        first_executor = pool._executor
        self.assertIsNotNone(pool.submit(blocking_task))
        self.assertTrue(started.wait(timeout=1))

        stop_thread = threading.Thread(target=pool.shutdown)
        stop_thread.start()
        deadline = time.monotonic() + 1
        while pool.state != 'draining' and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual('draining', pool.state)
        self.assertIsNone(pool.submit(lambda: completed.append("rejected")))
        release.set()
        stop_thread.join(timeout=2)

        self.assertFalse(stop_thread.is_alive())
        self.assertEqual(['old'], completed)
        self.assertEqual('stopped', pool.state)

        second_generation = pool.start()
        second_executor = pool._executor
        try:
            self.assertGreater(second_generation, first_generation)
            self.assertIsNot(second_executor, first_executor)
            pool.submit(lambda: completed.append("new")).result(timeout=1)
        finally:
            pool.shutdown()
        self.assertEqual(['old', 'new'], completed)

    def test_monitor_service_owns_pool_start_stop_and_restart(self):
        processor = mock.Mock()
        config = {
            constants.CONFIG_OPTION_MONITOR_ENABLED: True,
            constants.CONFIG_OPTION_MONITOR_PATHS: ["/watch"],
            constants.CONFIG_OPTION_MONITOR_EXTENSIONS: [".strm"],
            constants.CONFIG_OPTION_MONITOR_EXCLUDE_DIRS: [],
            constants.CONFIG_OPTION_MONITOR_FULL_SCAN_INTERVAL_HOURS: 0,
        }
        observer = mock.Mock(watch_count=1, backend_thread_count=2, max_user_watches=1048576)
        with mock.patch.object(
            monitor_service,
            'PersistedDirectoryObserver',
            return_value=observer,
        ), mock.patch.object(
            monitor_service.os.path,
            'exists',
            return_value=True,
        ), mock.patch.object(
            monitor_service.os.path,
            'isdir',
            return_value=True,
        ), mock.patch.object(
            monitor_service.logger,
            'trace',
            create=True,
        ):
            service = monitor_service.MonitorService(config, processor)
            service.start()
            first_generation = monitor_service._MONITOR_TASK_POOL.generation
            monitor_service._submit_monitor_task(lambda: None).result(timeout=1)
            service.stop()

            self.assertEqual(
                'stopped',
                monitor_service._MONITOR_TASK_POOL.state,
            )
            self.assertIsNone(monitor_service.MonitorService.processor_instance)

            service.start()
            second_generation = monitor_service._MONITOR_TASK_POOL.generation
            self.assertGreater(second_generation, first_generation)
            service.stop()

    def test_pending_old_generation_task_is_cancelled_and_not_run(self):
        pool = monitor_service._MonitorTaskPool()
        pool.start()
        release = threading.Event()
        all_running = threading.Event()
        running_lock = threading.Lock()
        running_count = 0
        old_task_ran = threading.Event()
        old_task_restored = threading.Event()

        def occupy_worker():
            nonlocal running_count
            with running_lock:
                running_count += 1
                if running_count == pool.MAX_WORKERS:
                    all_running.set()
            release.wait(timeout=2)

        for _ in range(pool.MAX_WORKERS):
            pool.submit(occupy_worker)
        self.assertTrue(all_running.wait(timeout=1))
        pool.submit(
            old_task_ran.set,
            on_cancel=old_task_restored.set,
        )

        stop_thread = threading.Thread(target=pool.shutdown)
        stop_thread.start()
        self.assertTrue(old_task_restored.wait(timeout=1))
        release.set()
        stop_thread.join(timeout=2)

        self.assertFalse(old_task_ran.is_set())
        self.assertFalse(stop_thread.is_alive())
        pool.start()
        try:
            pool.submit(lambda: None).result(timeout=1)
            self.assertFalse(old_task_ran.is_set())
        finally:
            pool.shutdown()

    def test_full_queue_wait_is_interruptible_and_thread_count_is_fixed(self):
        pool = monitor_service._MonitorTaskPool()
        pool.start()
        release = threading.Event()
        all_running = threading.Event()
        running_lock = threading.Lock()
        running_count = 0

        def occupy_slot():
            nonlocal running_count
            with running_lock:
                running_count += 1
                if running_count == pool.MAX_WORKERS:
                    all_running.set()
            release.wait(timeout=3)

        for _ in range(pool.MAX_TASKS):
            self.assertIsNotNone(pool.submit(occupy_slot))
        self.assertTrue(all_running.wait(timeout=1))

        result = []
        waiting_submitter = threading.Thread(
            target=lambda: result.append(pool.submit(lambda: None)),
            name='monitor-slot-waiter',
        )
        waiting_submitter.start()
        time.sleep(pool.SLOT_WAIT_SECONDS * 2)
        self.assertTrue(waiting_submitter.is_alive())
        worker_threads = [
            thread
            for thread in threading.enumerate()
            if thread.name.startswith('evh-monitor-task-')
        ]
        self.assertLessEqual(len(worker_threads), pool.MAX_WORKERS)

        pool.stop_accepting()
        waiting_submitter.join(timeout=1)
        self.assertFalse(waiting_submitter.is_alive())
        self.assertEqual([None], result)

        release.set()
        pool.shutdown()
        self.assertEqual('stopped', pool.state)

    def test_cancel_callback_exception_does_not_leak_slot(self):
        pool = monitor_service._MonitorTaskPool()
        pool.start()
        slots = pool._slots
        release = threading.Event()
        all_running = threading.Event()
        running_lock = threading.Lock()
        running_count = 0

        def occupy_worker():
            nonlocal running_count
            with running_lock:
                running_count += 1
                if running_count == pool.MAX_WORKERS:
                    all_running.set()
            release.wait(timeout=2)

        for _ in range(pool.MAX_WORKERS):
            pool.submit(occupy_worker)
        self.assertTrue(all_running.wait(timeout=1))
        pool.submit(
            lambda: None,
            on_cancel=mock.Mock(side_effect=RuntimeError("restore failed")),
        )

        with self.assertLogs(monitor_service.logger, level="ERROR"):
            stop_thread = threading.Thread(target=pool.shutdown)
            stop_thread.start()
            time.sleep(0.1)
            release.set()
            stop_thread.join(timeout=2)

        self.assertFalse(stop_thread.is_alive())
        self.assertEqual(pool.MAX_TASKS, slots._value)

    def test_executor_submit_exception_releases_slot_and_stops_accepting(self):
        pool = monitor_service._MonitorTaskPool()
        pool.start()
        slots = pool._slots
        with mock.patch.object(
            pool._executor,
            'submit',
            side_effect=RuntimeError("executor closed"),
        ), self.assertLogs(monitor_service.logger, level="ERROR"):
            self.assertIsNone(pool.submit(lambda: None))

        self.assertEqual('draining', pool.state)
        self.assertEqual(pool.MAX_TASKS, slots._value)
        pool.shutdown()

    def test_task_exception_is_logged_and_releases_slot(self):
        pool = monitor_service._MonitorTaskPool()
        pool.start()
        slots = pool._slots

        def fail():
            raise RuntimeError("task failed")

        with self.assertLogs(monitor_service.logger, level="ERROR"):
            future = pool.submit(fail)
            with self.assertRaisesRegex(RuntimeError, "task failed"):
                future.result(timeout=1)
            deadline = time.monotonic() + 1
            while pool.active_count and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertEqual(0, pool.active_count)
        self.assertEqual(pool.MAX_TASKS, slots._value)
        pool.shutdown()

    def test_submit_failure_returns_batch_to_file_event_source(self):
        path = self._path(1)
        config = {
            constants.CONFIG_OPTION_MONITOR_ENABLED: True,
            constants.CONFIG_OPTION_MONITOR_EXCLUDE_DIRS: [],
        }
        fake_pool = mock.Mock(accepting=True)
        monitor_service.MonitorService.processor_instance = object()
        with monitor_service.QUEUE_LOCK:
            monitor_service.FILE_EVENT_QUEUE.add(path)

        with mock.patch.object(
            monitor_service.config_manager,
            'APP_CONFIG',
            config,
        ), mock.patch.object(
            monitor_service,
            '_MONITOR_TASK_POOL',
            fake_pool,
        ), mock.patch.object(
            monitor_service,
            '_submit_monitor_task',
            return_value=None,
        ), mock.patch.object(
            monitor_service,
            'spawn_later',
            return_value=mock.Mock(),
        ):
            monitor_service.process_batch_queue()

        with monitor_service.QUEUE_LOCK:
            self.assertEqual({path}, monitor_service.FILE_EVENT_QUEUE)

    def test_submit_failure_returns_delete_batch_to_delete_source(self):
        path = self._path(2)
        config = {
            constants.CONFIG_OPTION_MONITOR_ENABLED: True,
            constants.CONFIG_OPTION_MONITOR_EXCLUDE_DIRS: [],
        }
        fake_pool = mock.Mock(accepting=True)
        processor = mock.Mock()
        monitor_service.MonitorService.processor_instance = processor
        with monitor_service.DELETE_QUEUE_LOCK:
            monitor_service.DELETE_EVENT_QUEUE.add(path)

        with mock.patch.object(
            monitor_service.config_manager,
            'APP_CONFIG',
            config,
        ), mock.patch.object(
            monitor_service,
            '_MONITOR_TASK_POOL',
            fake_pool,
        ), mock.patch.object(
            monitor_service,
            '_submit_monitor_task',
            return_value=None,
        ), mock.patch.object(
            monitor_service,
            'spawn_later',
            return_value=mock.Mock(),
        ):
            monitor_service.process_delete_batch_queue()

        with monitor_service.DELETE_QUEUE_LOCK:
            self.assertEqual({path}, monitor_service.DELETE_EVENT_QUEUE)

    def test_immediate_refresh_submit_failure_returns_to_file_source(self):
        path = self._path(3)
        fake_pool = mock.Mock(accepting=True)
        config = {
            constants.CONFIG_OPTION_MONITOR_ENABLED: True,
        }
        with mock.patch.object(
            monitor_service.config_manager,
            'APP_CONFIG',
            config,
        ), mock.patch.object(
            monitor_service,
            '_MONITOR_TASK_POOL',
            fake_pool,
        ), mock.patch.object(
            monitor_service,
            '_register_adaptive_refresh_paths',
            return_value=([path], []),
        ), mock.patch.object(
            monitor_service,
            '_submit_monitor_task',
            return_value=None,
        ), mock.patch.object(
            monitor_service,
            '_ensure_adaptive_refresh_worker',
        ), mock.patch.object(
            monitor_service,
            'spawn_later',
            return_value=mock.Mock(),
        ):
            monitor_service._enqueue_adaptive_refresh_only(object(), [path])

        with monitor_service.QUEUE_LOCK:
            self.assertEqual({path}, monitor_service.FILE_EVENT_QUEUE)

    def test_adaptive_due_submit_failure_is_restored_idempotently(self):
        paths = [
            self._path(index)
            for index in range(
                1,
                monitor_service.ADAPTIVE_BULK_PATH_THRESHOLD + 1,
            )
        ]
        processor = object()
        monitor_service._register_adaptive_refresh_paths(
            processor,
            paths,
            exclude_paths=["/STRM/动漫"],
            now=0,
        )
        batch = monitor_service._pop_due_adaptive_refresh_batches(now=60)[0]

        with mock.patch.object(
            monitor_service,
            '_submit_monitor_task',
            return_value=None,
        ) as submit:
            self.assertIsNone(
                monitor_service._submit_adaptive_refresh_batch(batch)
            )
        submit.assert_called_once()
        monitor_service._restore_adaptive_refresh_batch(batch)

        due = monitor_service._pop_due_adaptive_refresh_batches(
            now=time.monotonic() + monitor_service.ADAPTIVE_BULK_QUIET_SECONDS
        )
        self.assertEqual(1, len(due))
        self.assertEqual(paths, due[0]['paths'])

    def test_stop_preserves_unsubmitted_adaptive_paths_for_restart(self):
        paths = [
            self._path(index)
            for index in range(
                1,
                monitor_service.ADAPTIVE_BULK_PATH_THRESHOLD + 1,
            )
        ]
        monitor_service._register_adaptive_refresh_paths(
            object(),
            paths,
            exclude_paths=["/STRM/动漫"],
            now=0,
        )

        monitor_service._preserve_adaptive_paths_for_restart()

        self.assertEqual({}, monitor_service._ADAPTIVE_REFRESH_STATES)
        with monitor_service.QUEUE_LOCK:
            self.assertEqual(set(paths), monitor_service.FILE_EVENT_QUEUE)


if __name__ == '__main__':
    unittest.main()
