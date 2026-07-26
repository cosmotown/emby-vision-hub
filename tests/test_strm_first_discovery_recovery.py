import unittest
from unittest import mock

import constants
import monitor_service
from services import emby_ingest


class FirstDiscoveryFallbackTests(unittest.TestCase):
    def setUp(self):
        emby_ingest._reset_ingest_refresh_state()

    def tearDown(self):
        emby_ingest._reset_ingest_refresh_state()

    @staticmethod
    def _libraries():
        return [
            {
                'info': {'Id': 'anime-library', 'Name': '云盘动漫'},
                'paths': ['/STRM/动漫'],
            },
            {
                'info': {'Id': 'tv-library', 'Name': '云盘电视剧'},
                'paths': ['/STRM/电视剧'],
            },
        ]

    def test_new_directory_work_shallow_refreshes_only_matching_library(self):
        path = '/STRM/动漫/航海王 (1999) {tmdb-37854}/Season 21/航海王 S21E001.strm'
        with mock.patch.object(
            emby_ingest.emby,
            'get_all_libraries_with_paths',
            return_value=self._libraries(),
        ), mock.patch.object(
            emby_ingest.emby,
            'find_nearest_library_anchor_details',
            return_value=None,
        ), mock.patch.object(
            emby_ingest.emby,
            'refresh_item_by_id',
            return_value=True,
        ) as refresh:
            result = emby_ingest._refresh_parent_targets([path], 'http://emby', 'token')

        self.assertTrue(result)
        refresh.assert_called_once_with(
            'anime-library',
            'http://emby',
            'token',
            recursive=False,
        )

    def test_library_shallow_refresh_is_deduplicated_by_cooldown(self):
        first = '/STRM/动漫/航海王/Season 1/航海王 S01E001.strm'
        second = '/STRM/动漫/航海王/Season 2/航海王 S02E001.strm'
        with mock.patch.object(
            emby_ingest.emby,
            'get_all_libraries_with_paths',
            return_value=self._libraries(),
        ), mock.patch.object(
            emby_ingest.emby,
            'find_nearest_library_anchor_details',
            return_value=None,
        ), mock.patch.object(
            emby_ingest.emby,
            'refresh_item_by_id',
            return_value=True,
        ) as refresh:
            emby_ingest._refresh_parent_targets([first], 'http://emby', 'token')
            emby_ingest._refresh_parent_targets([second], 'http://emby', 'token')

        self.assertEqual(1, refresh.call_count)

    def test_flat_movie_does_not_refresh_library_root(self):
        path = '/STRM/电影/电影甲.strm'
        libraries = [
            {
                'info': {'Id': 'movie-library', 'Name': '云盘电影'},
                'paths': ['/STRM/电影'],
            }
        ]
        with mock.patch.object(
            emby_ingest.emby,
            'get_all_libraries_with_paths',
            return_value=libraries,
        ), mock.patch.object(
            emby_ingest.emby,
            'find_nearest_library_anchor_details',
            return_value=None,
        ), mock.patch.object(
            emby_ingest.emby,
            'refresh_item_by_id',
            return_value=True,
        ) as refresh:
            result = emby_ingest._refresh_parent_targets([path], 'http://emby', 'token')

        self.assertTrue(result)
        refresh.assert_not_called()

    def test_unmatched_path_never_refreshes_any_library(self):
        path = '/outside/unknown/Season 1/E01.strm'
        with mock.patch.object(
            emby_ingest.emby,
            'get_all_libraries_with_paths',
            return_value=self._libraries(),
        ), mock.patch.object(
            emby_ingest.emby,
            'find_nearest_library_anchor_details',
            return_value=None,
        ), mock.patch.object(
            emby_ingest.emby,
            'refresh_item_by_id',
            return_value=True,
        ) as refresh:
            result = emby_ingest._refresh_parent_targets([path], 'http://emby', 'token')

        self.assertTrue(result)
        refresh.assert_not_called()


class RetryRecoveryTests(unittest.TestCase):
    @staticmethod
    def _service():
        service = object.__new__(monitor_service.MonitorService)
        service.config = {
            constants.CONFIG_OPTION_EMBY_SERVER_URL: 'http://emby',
            constants.CONFIG_OPTION_EMBY_API_KEY: 'token',
        }
        service.processor = mock.Mock()
        return service

    def test_retry_precheck_completes_indexed_without_refresh(self):
        path = '/STRM/电视剧/吸血鬼日记/Season 1/E01.strm'
        service = self._service()
        with mock.patch.object(
            monitor_service,
            'check_indexed_paths',
            return_value=({path}, set(), set()),
        ), mock.patch.object(
            monitor_service,
            'refresh_and_verify_paths',
        ) as refresh, mock.patch.object(
            monitor_service.strm_ingest_db,
            'mark_completed',
        ) as completed, mock.patch.object(
            monitor_service.strm_ingest_db,
            'mark_failed_attempts',
        ) as failed:
            service._retry_existing_ingest_paths([path])

        completed.assert_called_once_with([path])
        service.processor.enqueue_confirmed_ingest_postprocessing.assert_called_once_with([path])
        refresh.assert_not_called()
        failed.assert_not_called()

    def test_retry_refreshes_only_still_missing_paths(self):
        indexed = '/STRM/电视剧/剧集/Season 1/E01.strm'
        missing = '/STRM/电视剧/剧集/Season 1/E02.strm'
        service = self._service()
        refresh_result = {
            'confirmed_paths': [],
            'pending': [missing],
            'query_failed': [],
        }
        with mock.patch.object(
            monitor_service,
            'check_indexed_paths',
            return_value=({indexed}, {missing}, set()),
        ), mock.patch.object(
            monitor_service,
            'refresh_and_verify_paths',
            return_value=refresh_result,
        ) as refresh, mock.patch.object(
            monitor_service.strm_ingest_db,
            'mark_completed',
        ), mock.patch.object(
            monitor_service.strm_ingest_db,
            'mark_failed_attempts',
            return_value={'retry': 1, 'failed': 0},
        ) as failed:
            service._retry_existing_ingest_paths([indexed, missing])

        refresh.assert_called_once_with([missing], 'http://emby', 'token')
        failed.assert_called_once_with([missing], 'Emby 在有限重试后仍未确认入库')

    def test_terminal_failed_rows_self_heal_without_refresh(self):
        confirmed = '/STRM/动漫/航海王/Season 1/E01.strm'
        unresolved = '/STRM/动漫/航海王/Season 1/E02.strm'
        service = self._service()
        with mock.patch.object(
            monitor_service.strm_ingest_db,
            'list_failed_ingest_paths',
            return_value=[confirmed, unresolved],
        ), mock.patch.object(
            monitor_service.os.path,
            'isfile',
            return_value=True,
        ), mock.patch.object(
            monitor_service,
            'check_indexed_paths',
            return_value=({confirmed}, {unresolved}, set()),
        ), mock.patch.object(
            monitor_service.strm_ingest_db,
            'mark_completed',
        ) as completed, mock.patch.object(
            monitor_service,
            'refresh_and_verify_paths',
        ) as refresh:
            recovered = service._recheck_terminal_ingest_paths(limit=200)

        self.assertEqual(1, recovered)
        completed.assert_called_once_with([confirmed])
        service.processor.enqueue_confirmed_ingest_postprocessing.assert_called_once_with([confirmed])
        refresh.assert_not_called()


if __name__ == '__main__':
    unittest.main()
