import json
import os
import tempfile
import unittest
from unittest import mock

import config_manager
import constants
from services.mediainfo_state import (
    MAX_PERSIST_BYTES,
    MediaInfoStateService,
    _safe_read_under_root,
    _semantic_fingerprint,
)


def eligible_snapshot(media_status):
    return {
        "identity": {
            "exact_item_id": "episode-1",
            "item_type": "Episode",
            "exact_strm_path_hash": "path-hash",
            "root_series_key": "series-1",
        },
        "strm_status": {"status": "present"},
        "emby_index_status": {"status": "indexed"},
        "shenyi_persist_status": {"status": "missing"},
        "emby_media_status": {"status": media_status},
    }


class MediaInfoStateRemediationTests(unittest.TestCase):
    def test_strict_boolean_parser_fails_closed(self):
        truthy = [True, "true", " TRUE "]
        falsey = [False, "false", " FALSE ", "0", 0, 1, None, [], {}]
        for value in truthy:
            with self.subTest(value=value):
                self.assertTrue(config_manager.parse_strict_boolean(value))
        for value in falsey:
            with self.subTest(value=value):
                self.assertFalse(config_manager.parse_strict_boolean(value))

    def test_eligibility_uses_positive_incomplete_allowlist(self):
        service = MediaInfoStateService(
            lambda: {
                constants.CONFIG_OPTION_SHENYI_MEDIAINFO_REPAIR_ENABLED: True
            }
        )
        for state in (
            "media_source_missing",
            "media_streams_empty",
            "video_stream_missing",
            "partial",
        ):
            self.assertEqual((True, None), service.repair_eligibility(eligible_snapshot(state)))
        for state in ("unknown", "read_failed", "lookup_failed", "ready", "unexpected"):
            eligible, reason = service.repair_eligibility(eligible_snapshot(state))
            self.assertFalse(eligible)
            self.assertEqual("repair_not_eligible", reason)

    def test_string_false_never_enables_repair(self):
        service = MediaInfoStateService(
            lambda: {
                constants.CONFIG_OPTION_SHENYI_MEDIAINFO_REPAIR_ENABLED: "false"
            }
        )
        self.assertEqual(
            (False, "repair_disabled"),
            service.repair_eligibility(eligible_snapshot("media_streams_empty")),
        )

    def test_semantic_fingerprint_ignores_time_and_list_order(self):
        first = {
            "identity": {"exact_item_id": "1", "path_hash": "abc"},
            "last_checked_at": "2026-01-01T00:00:00Z",
            "layer": {
                "status": "ready",
                "observed_at": "2026-01-01T00:00:00Z",
                "evidence_summary": {"counts": [2, 1], "video": 1},
            },
        }
        second = {
            **first,
            "last_checked_at": "2026-02-02T00:00:00Z",
            "layer": {
                **first["layer"],
                "observed_at": "2026-02-02T00:00:00Z",
                "evidence_summary": {"counts": [1, 2], "video": 1},
            },
        }
        self.assertEqual(_semantic_fingerprint(first), _semantic_fingerprint(second))
        second["layer"]["evidence_summary"]["video"] = 0
        self.assertNotEqual(_semantic_fingerprint(first), _semantic_fingerprint(second))

    def test_descriptor_relative_reader_rejects_all_child_symlinks_and_escape(self):
        with tempfile.TemporaryDirectory() as temp:
            root = os.path.join(temp, "root")
            outside = os.path.join(temp, "outside")
            os.makedirs(os.path.join(root, "普通", "inside"))
            os.makedirs(outside)
            ordinary = os.path.join(root, "普通", "inside", "E01.strm")
            with open(ordinary, "wb") as handle:
                handle.write(b"https://example.invalid/media")
            self.assertEqual(
                "ok",
                _safe_read_under_root(root, ordinary, max_bytes=1024)[0],
            )

            external_file = os.path.join(outside, "E01.strm")
            with open(external_file, "wb") as handle:
                handle.write(b"https://example.invalid/outside")
            os.symlink(outside, os.path.join(root, "external-link"))
            self.assertEqual(
                "unsafe",
                _safe_read_under_root(
                    root,
                    os.path.join(root, "external-link", "E01.strm"),
                    max_bytes=1024,
                )[0],
            )

            os.symlink(os.path.join(root, "普通"), os.path.join(root, "inside-link"))
            self.assertEqual(
                "unsafe",
                _safe_read_under_root(
                    root,
                    os.path.join(root, "inside-link", "inside", "E01.strm"),
                    max_bytes=1024,
                )[0],
            )

            os.symlink(ordinary, os.path.join(root, "final.strm"))
            self.assertEqual(
                "unsafe",
                _safe_read_under_root(
                    root, os.path.join(root, "final.strm"), max_bytes=1024
                )[0],
            )
            root_link = os.path.join(temp, "root-link")
            os.symlink(root, root_link)
            self.assertEqual(
                "unsafe",
                _safe_read_under_root(
                    root_link,
                    os.path.join(root_link, "普通", "inside", "E01.strm"),
                    max_bytes=1024,
                )[0],
            )
            os.symlink(os.path.join(root, "loop"), os.path.join(root, "loop"))
            self.assertEqual(
                "unsafe",
                _safe_read_under_root(
                    root,
                    os.path.join(root, "loop", "E01.strm"),
                    max_bytes=1024,
                )[0],
            )
            self.assertEqual(
                "unsafe",
                _safe_read_under_root(
                    root,
                    os.path.join(root, "..", "outside", "E01.strm"),
                    max_bytes=1024,
                )[0],
            )
            prefix = os.path.join(temp, "root-other", "E01.strm")
            os.makedirs(os.path.dirname(prefix))
            with open(prefix, "wb") as handle:
                handle.write(b"x")
            self.assertEqual(
                "unsafe", _safe_read_under_root(root, prefix, max_bytes=1024)[0]
            )

    def test_json_requires_nonempty_video_stream_across_valid_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            strm_root = os.path.join(temp, "strm")
            json_root = os.path.join(temp, "json")
            item_path = os.path.join(strm_root, "剧集", "E01.strm")
            os.makedirs(os.path.dirname(item_path))
            os.makedirs(json_root)
            config = {
                constants.CONFIG_OPTION_SHENYI_MEDIAINFO_JSON_ROOT: json_root,
            }
            service = MediaInfoStateService(lambda: config)
            mirrored_parent = os.path.join(json_root, item_path.lstrip(os.sep))
            json_path = os.path.splitext(mirrored_parent)[0] + "-mediainfo.json"
            os.makedirs(os.path.dirname(json_path), exist_ok=True)

            invalid_values = [
                [{"MediaSourceInfo": {"MediaStreams": []}}],
                [{"MediaSourceInfo": {"MediaStreams": [{"Type": "Audio"}]}}],
                [{"MediaSourceInfo": {"MediaStreams": [{"Type": "Subtitle"}]}}],
                [{"MediaSourceInfo": {"MediaStreams": ["bad"]}}],
            ]
            for value in invalid_values:
                with self.subTest(value=value):
                    with open(json_path, "w", encoding="utf-8") as handle:
                        json.dump(value, handle)
                    result = service._observe_shenyi_persist(item_path, strm_root)
                    self.assertEqual("present_invalid", result["status"])

            with open(json_path, "w", encoding="utf-8") as handle:
                json.dump(
                    [
                        {"ignored": True},
                        {"MediaSourceInfo": {"MediaStreams": [{"Type": "Video"}]}},
                    ],
                    handle,
                )
            result = service._observe_shenyi_persist(item_path, strm_root)
            self.assertEqual("present_valid", result["status"])
            self.assertEqual(1, result["evidence_summary"]["video_stream_count"])

    def test_failed_detail_read_preserves_previous_ready_truth(self):
        service = MediaInfoStateService(lambda: {})
        previous = {"emby_media_status": {"status": "ready", "evidence_summary": {"video_stream_count": 1}}}
        item = {"Id": "episode-1", "Type": "Episode", "Path": "/root/E01.strm", "SeriesId": "series-1"}
        with mock.patch.object(service, "_read_exact_catalog_item", return_value=({"status": "indexed"}, item)), mock.patch.object(
            service, "_observe_strm", return_value=({"status": "present"}, "/root")
        ), mock.patch.object(
            service, "_observe_shenyi_persist", return_value={"status": "missing"}
        ), mock.patch.object(
            service, "read_emby_media", return_value={"status": "read_failed", "reason_code": "emby_lookup_failed"}
        ):
            result = service.observe("episode-1", include_media=True, previous_snapshot=previous)
        self.assertEqual("ready", result["emby_media_status"]["status"])
        self.assertEqual("read_failed", result["emby_media_recheck_status"]["status"])
        self.assertFalse(service.repair_eligibility(result, feature_enabled=True)[0])


if __name__ == "__main__":
    unittest.main()
