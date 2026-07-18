import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

config_manager_stub = types.ModuleType("config_manager")
config_manager_stub.APP_CONFIG = {}
sys.modules.setdefault("config_manager", config_manager_stub)

import handler.emby as emby
from services import emby_ingest


class EmbyIngestTests(unittest.TestCase):
    def _make_strm_files(self, directory, count):
        paths = []
        for index in range(count):
            path = os.path.join(directory, f"S01E{index + 1:02d}.strm")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(f"http://example.invalid/{index}\n")
            paths.append(path)
        return paths

    def test_batch_stability_wait_is_parallel(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self._make_strm_files(directory, 40)
            started = time.monotonic()
            stable, skipped = emby_ingest.wait_for_paths_stable(
                paths,
                timeout_seconds=2,
                poll_interval=0.01,
            )

        self.assertEqual(sorted(paths), stable)
        self.assertEqual([], skipped)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_empty_config_path_never_becomes_current_directory(self):
        self.assertEqual([], emby_ingest.normalize_paths(["", "  ", None], require_existing=False))

    @mock.patch("services.emby_ingest.emby.refresh_item_by_id")
    @mock.patch("services.emby_ingest.emby.find_nearest_library_anchor")
    @mock.patch("services.emby_ingest.emby.get_all_libraries_with_paths")
    def test_library_root_is_never_refreshed_recursively(
        self,
        get_libraries,
        find_anchor,
        refresh_item,
    ):
        get_libraries.return_value = [{"info": {"Id": "library-root"}, "paths": ["/media/tv"]}]
        find_anchor.return_value = ("library-root", "TV")

        result = emby_ingest._refresh_parent_targets(
            ["/media/tv/New Show/Season 01/S01E01.strm"],
            "http://emby",
            "token",
        )

        self.assertTrue(result)
        refresh_item.assert_not_called()

    @mock.patch("services.emby_ingest.emby.refresh_item_by_id", return_value=True)
    @mock.patch("services.emby_ingest.emby.find_nearest_library_anchor")
    @mock.patch(
        "services.emby_ingest.emby.get_all_libraries_with_paths",
        return_value=[{"info": {"Id": "library-root"}, "paths": ["/media/tv"]}],
    )
    def test_same_series_anchor_is_refreshed_once(
        self,
        _get_libraries,
        find_anchor,
        refresh_item,
    ):
        find_anchor.return_value = ("series-1", "New Show")
        paths = [
            "/media/tv/New Show/Season 01/S01E01.strm",
            "/media/tv/New Show/Season 01/S01E02.strm",
        ]

        self.assertTrue(emby_ingest._refresh_parent_targets(paths, "http://emby", "token"))
        refresh_item.assert_called_once_with("series-1", "http://emby", "token")

    @mock.patch("services.emby_ingest.emby.refresh_item_by_id")
    @mock.patch("services.emby_ingest.emby.find_nearest_library_anchor")
    @mock.patch("services.emby_ingest.emby.get_all_libraries_with_paths", return_value=[])
    def test_unknown_library_roots_disable_recursive_refresh(
        self,
        _get_libraries,
        find_anchor,
        refresh_item,
    ):
        find_anchor.return_value = ("unknown-anchor", "Unknown")

        self.assertTrue(emby_ingest._refresh_parent_targets(
            ["/media/tv/New Show/Season 01/S01E01.strm"],
            "http://emby",
            "token",
        ))
        find_anchor.assert_not_called()
        refresh_item.assert_not_called()

    @mock.patch("services.emby_ingest.time.sleep")
    @mock.patch("services.emby_ingest._refresh_parent_targets", return_value=True)
    @mock.patch("services.emby_ingest.emby.notify_media_paths_updated", return_value=True)
    @mock.patch("services.emby_ingest.check_indexed_paths")
    def test_missing_path_is_retried_and_confirmed(
        self,
        check_paths,
        notify_paths,
        _refresh_targets,
        sleep,
    ):
        with tempfile.TemporaryDirectory() as directory:
            path = self._make_strm_files(directory, 1)[0]
            check_paths.side_effect = [
                (set(), {path}, set()),
                ({path}, set(), set()),
            ]
            result = emby_ingest.refresh_and_verify_paths(
                [path],
                "http://emby",
                "token",
                verify_delays=(8, 20),
            )

        self.assertEqual(1, result["indexed"])
        self.assertEqual([], result["pending"])
        self.assertEqual(2, notify_paths.call_count)
        self.assertEqual([mock.call(8), mock.call(12)], sleep.call_args_list)

    def test_realtime_queue_keeps_all_paths_for_emby(self):
        source = (Path(__file__).resolve().parents[1] / "monitor_service.py").read_text(encoding="utf-8")
        self.assertIn("args=(processor, files_to_scrape)", source)
        self.assertNotIn("args=(processor, representative_files)", source)


class EmbyHttpValidationTests(unittest.TestCase):
    @mock.patch("handler.emby.emby_client.post")
    def test_refresh_item_reports_http_failure(self, post):
        response = mock.Mock()
        response.raise_for_status.side_effect = RuntimeError("HTTP 500")
        post.return_value = response

        self.assertFalse(emby.refresh_item_by_id("item-1", "http://emby", "token"))

    @mock.patch("handler.emby.emby_client.post")
    def test_exact_path_notifications_are_chunked(self, post):
        post.return_value.raise_for_status.return_value = None
        paths = [f"/media/tv/show/S01E{index:03d}.strm" for index in range(205)]

        self.assertTrue(emby.notify_media_paths_updated(
            paths,
            "http://emby",
            "token",
            chunk_size=100,
        ))
        self.assertEqual(3, post.call_count)
        self.assertEqual(100, len(post.call_args_list[0].kwargs["json"]["Updates"]))
        self.assertEqual(5, len(post.call_args_list[2].kwargs["json"]["Updates"]))

    @mock.patch("handler.emby.emby_client.get")
    def test_ingest_queries_keep_api_key_out_of_url_params(self, get):
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"Items": []}
        response.raise_for_status.return_value = None
        get.return_value = response

        emby.find_nearest_library_anchor("/media/tv/show", "http://emby", "token")
        for call in get.call_args_list:
            self.assertNotIn("api_key", call.kwargs.get("params") or {})
            self.assertEqual("token", call.kwargs["headers"]["X-Emby-Token"])


if __name__ == "__main__":
    unittest.main()
