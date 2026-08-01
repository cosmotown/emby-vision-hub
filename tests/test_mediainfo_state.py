import hashlib
import json
import os
import tempfile
import types
import unittest
from unittest import mock

import constants
from services.mediainfo_state import MediaInfoStateService


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class MediaInfoStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.temp.name, "STRM")
        self.json_root = os.path.join(self.temp.name, "JSON")
        os.makedirs(self.root)
        self.item_path = os.path.join(self.root, "Show", "Season 1", "E01.strm")
        os.makedirs(os.path.dirname(self.item_path))
        with open(self.item_path, "wb") as handle:
            handle.write(b"https://media.invalid/episode\n")
        self.config = {
            constants.CONFIG_OPTION_EMBY_SERVER_URL: "http://emby.invalid",
            constants.CONFIG_OPTION_EMBY_API_KEY: "secret-token",
            constants.CONFIG_OPTION_EMBY_USER_ID: "user-1",
            constants.CONFIG_OPTION_MONITOR_PATHS: [self.root],
            constants.CONFIG_OPTION_MONITOR_EXCLUDE_DIRS: [],
            constants.CONFIG_OPTION_SHENYI_MEDIAINFO_JSON_ROOT: "",
            constants.CONFIG_OPTION_SHENYI_MEDIAINFO_REPAIR_ENABLED: True,
        }
        self.item = {
            "Id": "episode-1",
            "Name": "Episode",
            "Type": "Episode",
            "Path": self.item_path,
            "SeriesId": "series-1",
            "ParentIndexNumber": 1,
            "IndexNumber": 1,
        }

    def tearDown(self):
        self.temp.cleanup()

    def _service(self):
        return MediaInfoStateService(lambda: self.config)

    def _catalog_responses(self, media=None):
        responses = [
            FakeResponse({"Items": [self.item]}),
            FakeResponse({"Items": [self.item]}),
        ]
        if media is not None:
            responses.append(FakeResponse(media))
        return responses

    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_lightweight_observation_never_requests_media_sources(self, get):
        get.side_effect = self._catalog_responses()
        snapshot = self._service().observe("episode-1", include_media=False)
        self.assertEqual("present", snapshot["strm_status"]["status"])
        self.assertEqual("indexed", snapshot["emby_index_status"]["status"])
        self.assertEqual("unknown", snapshot["emby_media_status"]["status"])
        self.assertEqual("unknown", snapshot["summary_status"])
        self.assertEqual(2, get.call_count)
        for call in get.call_args_list:
            fields = call.kwargs["params"]["Fields"]
            self.assertNotIn("MediaSources", fields)
            self.assertNotIn("MediaStreams", fields)

    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_json_not_configured_is_distinct_from_missing(self, get):
        get.side_effect = self._catalog_responses()
        unconfigured = self._service().observe("episode-1", include_media=False)
        self.assertEqual(
            "not_configured",
            unconfigured["shenyi_persist_status"]["status"],
        )

        os.makedirs(self.json_root)
        self.config[constants.CONFIG_OPTION_SHENYI_MEDIAINFO_JSON_ROOT] = self.json_root
        get.side_effect = self._catalog_responses()
        missing = self._service().observe("episode-1", include_media=False)
        self.assertEqual("missing", missing["shenyi_persist_status"]["status"])

    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_json_root_not_visible_is_not_observable(self, get):
        self.config[constants.CONFIG_OPTION_SHENYI_MEDIAINFO_JSON_ROOT] = os.path.join(
            self.temp.name, "not-mounted"
        )
        get.side_effect = self._catalog_responses()
        snapshot = self._service().observe("episode-1", include_media=False)
        self.assertEqual(
            "not_observable",
            snapshot["shenyi_persist_status"]["status"],
        )

    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_excluded_strm_path_is_not_treated_as_a_visible_root(self, get):
        self.config[constants.CONFIG_OPTION_MONITOR_EXCLUDE_DIRS] = [
            os.path.join(self.root, "Show")
        ]
        get.side_effect = self._catalog_responses()
        snapshot = self._service().observe("episode-1", include_media=False)
        self.assertEqual("path_unmapped", snapshot["strm_status"]["status"])

    def _json_path(self):
        _drive, absolute_tail = os.path.splitdrive(os.path.normpath(self.item_path))
        mirrored_path = absolute_tail.lstrip("/\\")
        relative_parent = os.path.dirname(mirrored_path)
        media_name = os.path.basename(mirrored_path)
        return os.path.join(
            self.json_root,
            relative_parent,
            os.path.splitext(media_name)[0] + "-mediainfo.json",
        )

    def test_json_path_matches_shenyi_absolute_mirror_and_drops_strm_extension(self):
        path = self._json_path()
        self.assertTrue(path.startswith(self.json_root + os.sep))
        self.assertTrue(path.endswith(os.path.join("Season 1", "E01-mediainfo.json")))
        self.assertNotIn(".strm-mediainfo.json", path)

    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_valid_json_is_read_only_and_minimally_summarized(self, get):
        os.makedirs(os.path.dirname(self._json_path()))
        payload = [
            {
                "MediaSourceInfo": {
                    "MediaStreams": [{"Type": "Video", "Codec": "h264"}],
                    "Path": "https://redacted.invalid/media",
                }
            }
        ]
        raw = json.dumps(payload).encode("utf-8")
        with open(self._json_path(), "wb") as handle:
            handle.write(raw)
        with open(self._json_path(), "rb") as handle:
            before = hashlib.sha256(handle.read()).hexdigest()
        self.config[constants.CONFIG_OPTION_SHENYI_MEDIAINFO_JSON_ROOT] = self.json_root
        get.side_effect = self._catalog_responses()

        snapshot = self._service().observe("episode-1", include_media=False)

        with open(self._json_path(), "rb") as handle:
            after = hashlib.sha256(handle.read()).hexdigest()
        layer = snapshot["shenyi_persist_status"]
        self.assertEqual("present_valid", layer["status"])
        self.assertEqual(1, layer["evidence_summary"]["video_stream_count"])
        self.assertNotIn("MediaSourceInfo", json.dumps(layer))
        self.assertEqual(before, after)

    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_invalid_json_and_missing_streams_are_invalid(self, get):
        os.makedirs(os.path.dirname(self._json_path()))
        self.config[constants.CONFIG_OPTION_SHENYI_MEDIAINFO_JSON_ROOT] = self.json_root
        for raw in (b"{bad", b'[{"MediaSourceInfo": {}}]'):
            with self.subTest(raw=raw):
                with open(self._json_path(), "wb") as handle:
                    handle.write(raw)
                get.side_effect = self._catalog_responses()
                snapshot = self._service().observe("episode-1", include_media=False)
                self.assertEqual(
                    "present_invalid",
                    snapshot["shenyi_persist_status"]["status"],
                )

    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_json_changed_during_read_is_unknown_and_file_is_untouched(self, get):
        os.makedirs(os.path.dirname(self._json_path()))
        raw = b'[{"MediaSourceInfo":{"MediaStreams":[{"Type":"Video"}]}}]'
        with open(self._json_path(), "wb") as handle:
            handle.write(raw)
        self.config[constants.CONFIG_OPTION_SHENYI_MEDIAINFO_JSON_ROOT] = self.json_root
        get.side_effect = self._catalog_responses()
        with mock.patch(
            "services.mediainfo_state._safe_read_under_root",
            return_value=("unstable", None, None),
        ):
            snapshot = self._service().observe("episode-1", include_media=False)

        self.assertEqual("unknown", snapshot["shenyi_persist_status"]["status"])
        self.assertEqual(
            "shenyi_write_in_progress",
            snapshot["shenyi_persist_status"]["reason_code"],
        )
        with open(self._json_path(), "rb") as handle:
            self.assertEqual(raw, handle.read())

    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_identity_mismatch_is_not_repair_eligible(self, get):
        os.makedirs(self.json_root)
        self.config[constants.CONFIG_OPTION_MONITOR_PATHS] = [
            os.path.join(self.temp.name, "different-root")
        ]
        self.config[constants.CONFIG_OPTION_SHENYI_MEDIAINFO_JSON_ROOT] = self.json_root
        get.side_effect = self._catalog_responses()
        snapshot = self._service().observe("episode-1", include_media=False)
        self.assertEqual(
            "identity_mismatch",
            snapshot["shenyi_persist_status"]["status"],
        )
        eligible, reason = self._service().repair_eligibility(snapshot)
        self.assertFalse(eligible)
        self.assertEqual("repair_not_eligible", reason)

    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_duplicate_exact_path_is_fail_closed(self, get):
        duplicate = dict(self.item, Id="episode-duplicate")
        get.side_effect = [
            FakeResponse({"Items": [self.item]}),
            FakeResponse({"Items": [self.item, duplicate]}),
        ]
        snapshot = self._service().observe("episode-1", include_media=False)
        self.assertEqual("duplicate_match", snapshot["emby_index_status"]["status"])
        eligible, reason = self._service().repair_eligibility(snapshot)
        self.assertFalse(eligible)
        self.assertEqual("repair_not_eligible", reason)

    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_json_missing_does_not_override_ready_media(self, get):
        media = dict(
            self.item,
            MediaSources=[
                {
                    "MediaStreams": [{"Type": "Video"}],
                    "Size": None,
                    "RunTimeTicks": None,
                }
            ],
        )
        os.makedirs(self.json_root)
        self.config[constants.CONFIG_OPTION_SHENYI_MEDIAINFO_JSON_ROOT] = self.json_root
        get.side_effect = self._catalog_responses(media)
        snapshot = self._service().observe("episode-1", include_media=True)
        self.assertEqual("missing", snapshot["shenyi_persist_status"]["status"])
        self.assertEqual("ready", snapshot["emby_media_status"]["status"])
        self.assertEqual("ready", snapshot["summary_status"])
        eligible, reason = self._service().repair_eligibility(snapshot)
        self.assertFalse(eligible)
        self.assertEqual("repair_not_eligible", reason)

    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_valid_json_with_empty_emby_streams_remains_eligible(self, get):
        os.makedirs(os.path.dirname(self._json_path()))
        with open(self._json_path(), "w", encoding="utf-8") as handle:
            json.dump(
                [{"MediaSourceInfo": {"MediaStreams": [{"Type": "Video"}]}}],
                handle,
            )
        self.config[constants.CONFIG_OPTION_SHENYI_MEDIAINFO_JSON_ROOT] = self.json_root
        media = dict(self.item, MediaSources=[{"MediaStreams": []}])
        get.side_effect = self._catalog_responses(media)
        snapshot = self._service().observe("episode-1", include_media=True)
        self.assertEqual("present_valid", snapshot["shenyi_persist_status"]["status"])
        self.assertEqual(
            "media_streams_empty",
            snapshot["emby_media_status"]["status"],
        )
        eligible, reason = self._service().repair_eligibility(snapshot)
        self.assertTrue(eligible)
        self.assertIsNone(reason)

    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_incomplete_sources_are_partial_not_ready(self, get):
        media = dict(
            self.item,
            MediaSources=[
                {"MediaStreams": [{"Type": "Video"}]},
                {"MediaStreams": None},
            ],
        )
        get.side_effect = self._catalog_responses(media)
        snapshot = self._service().observe("episode-1", include_media=True)
        self.assertEqual("partial", snapshot["emby_media_status"]["status"])
        self.assertEqual(
            "media_stream_partial",
            snapshot["emby_media_status"]["reason_code"],
        )
        self.assertEqual("media_info_incomplete", snapshot["summary_status"])

    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_top_level_streams_do_not_replace_empty_media_source_streams(self, get):
        media = dict(
            self.item,
            MediaSources=[{"MediaStreams": []}],
            MediaStreams=[{"Type": "Video"}],
        )
        get.side_effect = self._catalog_responses(media)
        snapshot = self._service().observe("episode-1", include_media=True)
        self.assertEqual(
            "media_streams_empty",
            snapshot["emby_media_status"]["status"],
        )

    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_feature_flag_and_cooldown_fail_closed(self, get):
        media = dict(self.item, MediaSources=[{"MediaStreams": []}])
        get.side_effect = self._catalog_responses(media)
        snapshot = self._service().observe("episode-1", include_media=True)
        self.config[constants.CONFIG_OPTION_SHENYI_MEDIAINFO_REPAIR_ENABLED] = False
        eligible, reason = self._service().repair_eligibility(snapshot)
        self.assertFalse(eligible)
        self.assertEqual("repair_disabled", reason)


if __name__ == "__main__":
    unittest.main()
