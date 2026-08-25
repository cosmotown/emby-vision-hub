import unittest
from unittest import mock

import constants
from services.mediainfo_state import MediaInfoStateService
from services.review_cleanup import ReviewCleanupService


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeStateService:
    def __init__(self):
        self.statuses = {
            "movie-ready": "ready",
            "movie-broken": "media_info_incomplete",
            "movie-lookup-failed": "emby_lookup_failed",
            "movie-gone": "historical_item_missing",
        }
        self.observed = []

    def resolve_review_target(self, item_id, item_type, reason):
        if item_id == "series-gone":
            return {
                "target_item_id": None,
                "target_reason_code": "historical_item_missing",
            }
        if item_id == "series-unresolved":
            return {
                "target_item_id": None,
                "target_reason_code": "episode_target_not_found",
            }
        return {"target_item_id": item_id, "target_reason_code": None}

    def observe(self, item_id, *, include_media):
        self.observed.append((item_id, include_media))
        return {"summary_status": self.statuses[item_id]}


class ReviewCleanupTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {"item_id": "movie-ready", "item_type": "Movie", "reason": "缺失媒体信息"},
            {"item_id": "movie-broken", "item_type": "Movie", "reason": "缺失媒体信息"},
            {"item_id": "movie-lookup-failed", "item_type": "Movie", "reason": "缺失媒体信息"},
            {"item_id": "movie-gone", "item_type": "Movie", "reason": "旧记录"},
            {"item_id": "series-gone", "item_type": "Series", "reason": "[S01E01] 缺失媒体信息"},
            {"item_id": "series-unresolved", "item_type": "Series", "reason": "[S01E07] 缺失媒体信息"},
        ]
        self.state = FakeStateService()
        self.service = ReviewCleanupService(self.state)

    @mock.patch("services.review_cleanup.log_db.get_all_review_items")
    def test_ready_preview_is_fresh_read_only_and_excludes_other_states(self, get_rows):
        get_rows.return_value = self.rows
        result = self.service.preview("ready")
        self.assertEqual(1, result["candidate_count"])
        self.assertNotIn("candidate_ids", result)
        self.assertIn(("movie-ready", True), self.state.observed)
        self.assertNotIn(("series-unresolved", True), self.state.observed)

    @mock.patch("services.review_cleanup.log_db.remove_review_items")
    @mock.patch("services.review_cleanup.log_db.get_all_review_items")
    def test_execute_rechecks_instead_of_using_preview_cache(self, get_rows, remove):
        get_rows.return_value = self.rows
        self.assertEqual(1, self.service.preview("ready")["candidate_count"])
        self.state.statuses["movie-ready"] = "media_info_incomplete"
        remove.return_value = 0

        result = self.service.execute("ready")

        remove.assert_called_once_with([])
        self.assertEqual(0, result["candidate_count"])
        self.assertEqual(0, result["removed_count"])

    @mock.patch("services.review_cleanup.log_db.remove_review_items")
    @mock.patch("services.review_cleanup.log_db.get_all_review_items")
    def test_historical_cleanup_never_includes_lookup_failure(self, get_rows, remove):
        get_rows.return_value = self.rows
        remove.return_value = 2

        result = self.service.execute("historical_item_missing")

        remove.assert_called_once_with(["movie-gone", "series-gone"])
        self.assertEqual(2, result["candidate_count"])
        self.assertEqual(2, result["removed_count"])
        self.assertNotIn("movie-lookup-failed", remove.call_args.args[0])
        self.assertNotIn("series-unresolved", remove.call_args.args[0])

    def test_unknown_cleanup_category_is_rejected_before_any_write(self):
        with self.assertRaises(ValueError):
            self.service.execute("lookup_failed")

    @staticmethod
    def _real_resolver_service():
        state = MediaInfoStateService(
            lambda: {
                constants.CONFIG_OPTION_EMBY_SERVER_URL: "http://emby.invalid",
                constants.CONFIG_OPTION_EMBY_API_KEY: "secret-token",
            }
        )
        return ReviewCleanupService(state)

    @mock.patch("services.review_cleanup.log_db.remove_review_items")
    @mock.patch("services.review_cleanup.log_db.get_all_review_items")
    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_stale_series_without_coordinate_is_previewed_and_removed(
        self, emby_get, get_rows, remove
    ):
        get_rows.return_value = [
            {
                "item_id": "series-gone",
                "item_type": "Series",
                "reason": "处理评分 (4.00) 低于阈值",
            }
        ]
        emby_get.return_value = FakeResponse({"Items": []})
        remove.return_value = 1
        service = self._real_resolver_service()

        preview = service.preview("historical_item_missing")
        executed = service.execute("historical_item_missing")

        self.assertEqual(1, preview["candidate_count"])
        self.assertEqual(1, executed["candidate_count"])
        self.assertEqual(1, executed["removed_count"])
        remove.assert_called_once_with(["series-gone"])

    @mock.patch("services.review_cleanup.log_db.remove_review_items")
    @mock.patch("services.review_cleanup.log_db.get_all_review_items")
    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_series_lookup_failure_never_enters_historical_cleanup(
        self, emby_get, get_rows, remove
    ):
        get_rows.return_value = [
            {
                "item_id": "series-unavailable",
                "item_type": "Series",
                "reason": "处理评分 (4.00) 低于阈值",
            }
        ]
        emby_get.side_effect = RuntimeError("temporary Emby outage")
        remove.return_value = 0

        result = self._real_resolver_service().execute("historical_item_missing")

        self.assertEqual(0, result["candidate_count"])
        self.assertEqual(0, result["removed_count"])
        remove.assert_called_once_with([])


if __name__ == "__main__":
    unittest.main()
