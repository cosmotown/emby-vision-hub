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

    @staticmethod
    def _series_identity(series_id="series-1"):
        return FakeResponse({"Items": [{"Id": series_id, "Type": "Series"}]})

    def _episode(self, number, *, item_id=None, series_id="series-1"):
        return dict(
            self.item,
            Id=item_id or f"episode-{number}",
            SeriesId=series_id,
            ParentIndexNumber=1,
            IndexNumber=number,
        )

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
    def test_excluded_monitor_path_remains_visible_to_mediainfo(self, get):
        self.config[constants.CONFIG_OPTION_MONITOR_EXCLUDE_DIRS] = [
            os.path.join(self.root, "Show")
        ]
        get.side_effect = self._catalog_responses()
        snapshot = self._service().observe("episode-1", include_media=False)
        self.assertEqual("present", snapshot["strm_status"]["status"])

    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_six_episode_series_resolves_first_and_last_by_exact_series_identity(self, get):
        episodes = [self._episode(number) for number in range(1, 7)]
        for number in (1, 6):
            with self.subTest(number=number):
                get.reset_mock()
                get.side_effect = [
                    self._series_identity(),
                    FakeResponse({"Items": episodes, "TotalRecordCount": 6}),
                ]
                resolved = self._service().resolve_review_target(
                    "series-1", "Series", f"MediaInfo incomplete [S01E{number:02d}]"
                )
                self.assertEqual(f"episode-{number}", resolved["target_item_id"])
                self.assertEqual("Episode", resolved["target_item_type"])
                self.assertEqual("series_episode", resolved["target_resolution"])
                self.assertEqual(1, resolved["target_parent_index_number"])
                self.assertEqual(number, resolved["target_index_number"])
                self.assertEqual(2, get.call_count)
                episode_call = get.call_args_list[1]
                self.assertTrue(episode_call.args[0].endswith("/Shows/series-1/Episodes"))
                self.assertEqual(1, episode_call.kwargs["params"]["Season"])
                self.assertEqual(0, episode_call.kwargs["params"]["StartIndex"])
                self.assertEqual(100, episode_call.kwargs["params"]["Limit"])
                self.assertNotIn("SeriesId", episode_call.kwargs["params"])
                self.assertNotIn("SearchTerm", episode_call.kwargs["params"])
                self.assertNotIn("AnyProviderIdEquals", episode_call.kwargs["params"])

    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_series_review_missing_coordinate_is_fail_closed(self, get):
        episodes = [self._episode(number) for number in range(1, 7)]
        get.side_effect = [
            self._series_identity(),
            FakeResponse({"Items": episodes, "TotalRecordCount": 6}),
        ]
        resolved = self._service().resolve_review_target(
            "series-1", "Series", "MediaInfo incomplete S01E07"
        )
        self.assertIsNone(resolved["target_item_id"])
        self.assertEqual("episode_target_not_found", resolved["target_reason_code"])

    @mock.patch("services.mediainfo_state.EPISODE_QUERY_PAGE_SIZE", 2)
    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_large_series_is_safely_paginated_until_unique_target(self, get):
        episodes = [self._episode(number) for number in range(1, 7)]
        get.side_effect = [
            self._series_identity(),
            FakeResponse({"Items": episodes[:2], "TotalRecordCount": 6}),
            FakeResponse({"Items": episodes[2:4], "TotalRecordCount": 6}),
            FakeResponse({"Items": episodes[4:], "TotalRecordCount": 6}),
        ]
        resolved = self._service().resolve_review_target(
            "series-1", "Series", "MediaInfo incomplete S01E06"
        )
        self.assertEqual("episode-6", resolved["target_item_id"])
        self.assertEqual(
            [0, 2, 4],
            [call.kwargs["params"]["StartIndex"] for call in get.call_args_list[1:]],
        )

    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_series_review_target_is_fail_closed_when_ambiguous(self, get):
        first = self._episode(8, item_id="episode-a")
        second = dict(first, Id="episode-b")
        get.side_effect = [
            self._series_identity(),
            FakeResponse({"Items": [first, second], "TotalRecordCount": 2}),
        ]

        resolved = self._service().resolve_review_target(
            "series-1", "Series", "MediaInfo incomplete S01E08"
        )

        self.assertIsNone(resolved["target_item_id"])
        self.assertEqual("episode_target_ambiguous", resolved["target_reason_code"])

    @mock.patch("services.mediainfo_state.EPISODE_QUERY_PAGE_SIZE", 2)
    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_duplicate_coordinate_on_later_page_is_fail_closed(self, get):
        get.side_effect = [
            self._series_identity(),
            FakeResponse(
                {"Items": [self._episode(1, item_id="episode-a"), self._episode(2)], "TotalRecordCount": 4}
            ),
            FakeResponse(
                {"Items": [self._episode(1, item_id="episode-b"), self._episode(3)], "TotalRecordCount": 4}
            ),
        ]
        resolved = self._service().resolve_review_target(
            "series-1", "Series", "MediaInfo incomplete S01E01"
        )
        self.assertIsNone(resolved["target_item_id"])
        self.assertEqual("episode_target_ambiguous", resolved["target_reason_code"])

    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_series_review_api_failure_is_fail_closed(self, get):
        get.side_effect = [self._series_identity(), RuntimeError("Emby unavailable")]
        resolved = self._service().resolve_review_target(
            "series-1", "Series", "MediaInfo incomplete S01E01"
        )
        self.assertIsNone(resolved["target_item_id"])
        self.assertEqual("emby_lookup_failed", resolved["target_reason_code"])

    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_missing_series_is_historical_not_lookup_failure(self, get):
        get.return_value = FakeResponse({"Items": []})
        resolved = self._service().resolve_review_target(
            "series-gone", "Series", "MediaInfo incomplete S01E01"
        )
        self.assertIsNone(resolved["target_item_id"])
        self.assertEqual("historical_item_missing", resolved["target_reason_code"])
        self.assertEqual(1, get.call_count)

    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_missing_series_without_coordinate_is_historical_before_coordinate_parsing(self, get):
        get.return_value = FakeResponse({"Items": []})

        resolved = self._service().resolve_review_target(
            "series-gone", "Series", "处理评分 (4.00) 低于阈值"
        )

        self.assertIsNone(resolved["target_item_id"])
        self.assertEqual("historical_item_missing", resolved["target_reason_code"])
        self.assertEqual(1, get.call_count)

    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_existing_non_series_source_fails_closed_before_coordinate_parsing(self, get):
        get.return_value = FakeResponse({"Items": [{"Id": "series-1", "Type": "Movie"}]})

        resolved = self._service().resolve_review_target(
            "series-1", "Series", "处理评分 (4.00) 低于阈值"
        )

        self.assertIsNone(resolved["target_item_id"])
        self.assertEqual(
            "episode_target_source_type_mismatch",
            resolved["target_reason_code"],
        )
        self.assertEqual(1, get.call_count)

    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_series_source_lookup_failure_is_not_historical_without_coordinate(self, get):
        get.side_effect = RuntimeError("temporary Emby outage")

        resolved = self._service().resolve_review_target(
            "series-1", "Series", "处理评分 (4.00) 低于阈值"
        )

        self.assertIsNone(resolved["target_item_id"])
        self.assertEqual("emby_lookup_failed", resolved["target_reason_code"])
        self.assertEqual(1, get.call_count)

    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_exact_series_endpoint_mixing_another_series_is_fail_closed(self, get):
        get.side_effect = [
            self._series_identity(),
            FakeResponse(
                {"Items": [self._episode(1, series_id="series-other")], "TotalRecordCount": 1}
            ),
        ]
        resolved = self._service().resolve_review_target(
            "series-1", "Series", "MediaInfo incomplete S01E01"
        )
        self.assertIsNone(resolved["target_item_id"])
        self.assertEqual("episode_target_series_mismatch", resolved["target_reason_code"])

    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_existing_series_without_unique_coordinate_stops_after_identity_read(self, get):
        for reason, expected in (
            ("MediaInfo incomplete", "episode_coordinate_missing"),
            ("S01E08 and S01E09", "episode_coordinate_ambiguous"),
        ):
            with self.subTest(reason=reason):
                get.reset_mock()
                get.return_value = self._series_identity()
                resolved = self._service().resolve_review_target(
                    "series-1", "Series", reason
                )
                self.assertIsNone(resolved["target_item_id"])
                self.assertEqual(expected, resolved["target_reason_code"])
                self.assertEqual(1, get.call_count)

    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_confirmed_missing_item_stops_all_downstream_observation(self, get):
        get.return_value = FakeResponse({"Items": []})
        snapshot = self._service().observe("episode-gone", include_media=True)
        self.assertEqual("historical_item_missing", snapshot["summary_status"])
        self.assertEqual(
            "historical_item_missing", snapshot["emby_index_status"]["status"]
        )
        for layer in ("strm_status", "shenyi_persist_status", "emby_media_status"):
            self.assertEqual("not_observable", snapshot[layer]["status"])
        self.assertEqual("remove_review_record", snapshot["suggested_action"])
        self.assertEqual(1, get.call_count)

    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_temporary_lookup_failure_is_not_historical(self, get):
        get.side_effect = RuntimeError("temporary Emby outage")
        snapshot = self._service().observe("episode-1", include_media=True)
        self.assertEqual("emby_lookup_failed", snapshot["summary_status"])
        self.assertEqual("lookup_failed", snapshot["emby_index_status"]["status"])
        self.assertNotEqual(
            "identity_mismatch", snapshot["shenyi_persist_status"]["status"]
        )

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
