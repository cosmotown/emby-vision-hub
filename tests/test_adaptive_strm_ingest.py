import os
import unittest
from unittest import mock

import constants
import monitor_service


class AdaptiveStrmIngestTests(unittest.TestCase):
    def setUp(self):
        monitor_service._reset_adaptive_refresh_state()

    def tearDown(self):
        monitor_service._reset_adaptive_refresh_state()

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
        self.assertEqual(4, monitor_service._MONITOR_TASK_EXECUTOR._max_workers)

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

        submit.assert_called_once_with(
            monitor_service._handle_batch_refresh_only_task,
            processor,
            [path],
            bulk_mode=False,
        )


if __name__ == '__main__':
    unittest.main()
