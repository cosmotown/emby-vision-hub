import os
import sys
import tempfile
import time
import types
import ast
import typing
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

    def test_strm_inventory_tracks_each_exact_path_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = self._make_strm_files(directory, 2)
            inventory = emby_ingest.collect_strm_inventory(directory)

            self.assertEqual({first, second}, set(inventory))
            self.assertGreater(inventory[first][0], 0)
            self.assertIsInstance(inventory[first][1], float)

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
        self.assertEqual([path], result["confirmed_paths"])
        self.assertEqual([], result["pending"])
        self.assertEqual(2, notify_paths.call_count)
        self.assertEqual([mock.call(8), mock.call(12)], sleep.call_args_list)

    def test_realtime_queue_keeps_all_paths_for_emby(self):
        source = (Path(__file__).resolve().parents[1] / "monitor_service.py").read_text(encoding="utf-8")
        self.assertIn("args=(processor, files_to_scrape)", source)
        self.assertNotIn("args=(processor, representative_files)", source)

    @mock.patch("services.emby_ingest.emby.get_media_item_by_path")
    def test_confirmed_items_are_deduplicated_by_emby_id(self, get_item):
        with tempfile.TemporaryDirectory() as directory:
            paths = self._make_strm_files(directory, 2)
            get_item.side_effect = [
                {"Id": "episode-1", "Name": "E01", "Type": "Episode"},
                {"Id": "episode-1", "Name": "E01", "Type": "Episode"},
            ]
            items = emby_ingest.get_confirmed_media_items(
                paths,
                "http://emby",
                "token",
            )

        self.assertEqual(1, len(items))
        self.assertEqual("episode-1", items[0]["Id"])

    @mock.patch("services.emby_ingest.verify_deleted_paths")
    @mock.patch("services.emby_ingest.emby.refresh_item_by_id", return_value=True)
    @mock.patch("services.emby_ingest.emby.notify_media_paths_updated", return_value=True)
    @mock.patch("services.emby_ingest.get_confirmed_media_items")
    def test_deleted_path_refreshes_only_its_exact_emby_item(
        self,
        get_items,
        notify_paths,
        refresh_item,
        verify_paths,
    ):
        path = "/media/Movies/Example/Example.strm"
        get_items.return_value = [{"Id": "movie-1", "Type": "Movie", "Path": path}]
        verify_paths.return_value = {
            "requested": 1,
            "confirmed_paths": [path],
            "pending": [],
            "query_failed": [],
        }

        result = emby_ingest.delete_and_verify_paths([path], "http://emby", "token")

        self.assertEqual([path], result["confirmed_paths"])
        refresh_item.assert_called_once_with("movie-1", "http://emby", "token")
        self.assertEqual("Deleted", notify_paths.call_args_list[0].kwargs["update_type"])
        self.assertEqual("Modified", notify_paths.call_args_list[1].kwargs["update_type"])

    @mock.patch("services.emby_ingest.refresh_and_verify_paths")
    @mock.patch("services.emby_ingest.wait_for_paths_stable")
    @mock.patch("services.emby_ingest.check_indexed_paths")
    @mock.patch("services.emby_ingest.collect_recent_media_paths")
    def test_reconcile_returns_old_and_new_confirmed_paths(
        self,
        collect_paths,
        check_paths,
        wait_stable,
        refresh_paths,
    ):
        collect_paths.return_value = ["/media/a.strm", "/media/b.strm"]
        check_paths.return_value = ({"/media/a.strm"}, {"/media/b.strm"}, set())
        wait_stable.return_value = (["/media/b.strm"], [])
        refresh_paths.return_value = {
            "requested": 1,
            "indexed": 1,
            "confirmed_paths": ["/media/b.strm"],
            "pending": [],
            "query_failed": [],
            "refresh_ok": True,
        }

        result = emby_ingest.reconcile_recent_paths(
            ["/media"],
            [".strm"],
            0,
            "http://emby",
            "token",
        )

        self.assertEqual(["/media/a.strm", "/media/b.strm"], result["confirmed_paths"])

    @mock.patch("services.emby_ingest.refresh_and_verify_paths")
    @mock.patch("services.emby_ingest.wait_for_paths_stable")
    @mock.patch("services.emby_ingest.check_indexed_paths")
    @mock.patch("services.emby_ingest.collect_recent_media_paths")
    def test_reconcile_keeps_failed_paths_separate_from_time_window(
        self,
        collect_paths,
        check_paths,
        wait_stable,
        refresh_paths,
    ):
        with tempfile.TemporaryDirectory() as directory:
            old_path, new_path = self._make_strm_files(directory, 2)
            collect_paths.return_value = [new_path]
            check_paths.return_value = (set(), {old_path, new_path}, set())
            wait_stable.return_value = ([new_path], [old_path])
            refresh_paths.return_value = {
                "requested": 1,
                "indexed": 0,
                "confirmed_paths": [],
                "pending": [new_path],
                "query_failed": [],
                "refresh_ok": True,
            }

            result = emby_ingest.reconcile_recent_paths(
                [directory],
                [".strm"],
                time.time(),
                "http://emby",
                "token",
                retry_paths=[old_path],
            )

        checked_paths = check_paths.call_args.args[0]
        self.assertEqual({old_path, new_path}, set(checked_paths))
        self.assertEqual([old_path, new_path], result["unresolved_paths"])

    def test_excluded_delete_path_keeps_external_deletion_ownership(self):
        root = Path(__file__).resolve().parents[1]
        processor_source = (root / "core_processor.py").read_text(encoding="utf-8")
        monitor_source = (root / "monitor_service.py").read_text(encoding="utf-8")

        cleanup_method = processor_source.split(
            "def cleanup_file_deletion_records", 1
        )[1].split("def process_file_deletion_batch", 1)[0]
        self.assertIn("_cleanup_local_db_for_deleted_file", cleanup_method)
        self.assertNotIn("deep.delete", cleanup_method)
        self.assertNotIn("delete_file", cleanup_method)
        self.assertIn("processor.cleanup_file_deletion_records(file_paths)", monitor_source)

    def test_realtime_monitor_handles_same_path_content_replacement(self):
        source = (Path(__file__).resolve().parents[1] / "monitor_service.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        handler = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "MediaFileHandler"
        )
        method_names = {
            node.name for node in handler.body if isinstance(node, ast.FunctionDef)
        }

        self.assertIn("on_modified", method_names)
        self.assertIn("on_moved", method_names)
        self.assertIn("on_deleted", method_names)

    def test_confirmed_ingest_bypasses_thread_local_webhook_debounce(self):
        source = (
            Path(__file__).resolve().parents[1] / "routes" / "webhook.py"
        ).read_text(encoding="utf-8")
        method = source.split(
            "def enqueue_verified_ingest_item", 1
        )[1].split("def _wait_for_stream_data_and_enqueue", 1)[0]

        self.assertIn("_enqueue_persistent_webhook_task", method)
        self.assertNotIn("_enqueue_webhook_event", method)
        self.assertIn("'new_episode_ids': episode_ids", method)

    def test_confirmed_ingest_checks_existing_ids_in_one_batch(self):
        source = (
            Path(__file__).resolve().parents[1] / "core_processor.py"
        ).read_text(encoding="utf-8")
        method = source.split(
            "def enqueue_confirmed_ingest_postprocessing", 1
        )[1].split("def _cleanup_local_db_for_deleted_file", 1)[0]

        self.assertIn("media_db.get_in_library_emby_ids(item_ids)", method)
        self.assertNotIn("media_db.is_emby_id_in_library(item_id)", method)

    def test_ambiguous_same_name_deletion_is_never_guessed(self):
        source_path = Path(__file__).resolve().parents[1] / "database" / "media_db.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        helper_names = {
            "_normalize_asset_path",
            "_path_suffixes_for_match",
            "_select_unique_media_path_match",
        }
        helpers = [
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in helper_names
        ]
        namespace = {
            "os": os,
            "List": typing.List,
            "Dict": typing.Dict,
            "Optional": typing.Optional,
            "Any": typing.Any,
        }
        exec(compile(ast.Module(body=helpers, type_ignores=[]), str(source_path), "exec"), namespace)
        select_match = namespace["_select_unique_media_path_match"]
        rows = [
            {"asset_path": "/data/tv/Show A/Season 01/S01E01.strm", "target_emby_id": "1"},
            {"asset_path": "/data/tv/Show B/Season 01/S01E01.strm", "target_emby_id": "2"},
        ]

        selected = select_match(rows, "/mnt/tv/Show A/Season 01/S01E01.strm")
        ambiguous = select_match(rows, "/mnt/tv/Show C/Season 01/S01E01.strm")
        wrong_single_path = select_match(
            [rows[0]],
            "/mnt/tv/Show C/Season 01/S01E01.strm",
        )

        self.assertEqual("1", selected["target_emby_id"])
        self.assertIsNone(ambiguous)
        self.assertIsNone(wrong_single_path)


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

    @mock.patch("handler.emby.emby_client.get")
    def test_exact_path_lookup_returns_matching_emby_item(self, get):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "Items": [
                {
                    "Id": "episode-1",
                    "Name": "E01",
                    "Type": "Episode",
                    "Path": "/media/show/S01E01.strm",
                }
            ]
        }
        get.return_value = response

        item = emby.get_media_item_by_path(
            "/media/show/S01E01.strm",
            "http://emby",
            "token",
        )

        self.assertEqual("episode-1", item["Id"])
        self.assertNotIn("api_key", get.call_args.kwargs["params"])
        self.assertEqual("true", get.call_args.kwargs["params"]["Recursive"])
        self.assertEqual("token", get.call_args.kwargs["headers"]["X-Emby-Token"])


if __name__ == "__main__":
    unittest.main()
