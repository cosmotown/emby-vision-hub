import unittest
from unittest import mock

from services.review_cleanup import ReviewCleanupService


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


if __name__ == "__main__":
    unittest.main()
