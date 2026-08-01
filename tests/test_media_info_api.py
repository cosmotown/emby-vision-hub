import unittest
from unittest import mock

from flask import Flask

import config_manager
from routes.media_info import media_info_bp
from services.mediainfo_state import public_snapshot


class FakeCoordinator:
    def __init__(self):
        self.calls = []
        self.fail = False

    def get_status(self, item_id, *, recheck=False):
        self.calls.append(("status", item_id, recheck))
        if self.fail:
            raise RuntimeError("token=secret pickcode=sensitive")
        snapshot = {
            "identity": {
                "exact_item_id": item_id,
                "item_type": "Episode",
                "redacted_path_hint": "E01.strm",
            },
            "strm_status": {"status": "present", "evidence_summary": {}},
            "emby_index_status": {"status": "indexed", "evidence_summary": {}},
            "shenyi_persist_status": {
                "status": "not_configured",
                "evidence_summary": {},
            },
            "emby_media_status": {
                "status": "media_streams_empty",
                "evidence_summary": {"stream_count": 0},
            },
            "summary_status": "media_info_incomplete",
            "suggested_action": "manual_recheck",
            "last_checked_at": "2026-07-30T00:00:00+00:00",
        }
        return public_snapshot(snapshot, repair_eligible=True)

    def submit(self, item_id):
        self.calls.append(("submit", item_id))
        return {
            "result": "accepted",
            "reason_code": None,
            "job": {"id": 7, "state": "pending"},
        }

    def submit_batch(self, item_ids):
        self.calls.append(("batch", list(item_ids)))
        if len(item_ids) > 20:
            raise ValueError("at most 20 items may be submitted")
        return {
            "accepted": [{"item_id": item_ids[0]}] if item_ids else [],
            "skipped": [],
            "rejected": [{"item_id": value} for value in item_ids[1:]],
        }

    def get_job(self, job_id):
        self.calls.append(("job", job_id))
        return {"id": job_id, "state": "pending", "post_attempts": 0}

    def cancel(self, job_id):
        self.calls.append(("cancel", job_id))
        return {"id": job_id, "state": "cancelled"}


class MediaInfoApiTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = "test"
        self.app.register_blueprint(media_info_bp)
        self.client = self.app.test_client()
        self.coordinator = FakeCoordinator()
        self.old_config = dict(config_manager.APP_CONFIG)
        config_manager.APP_CONFIG.clear()
        config_manager.APP_CONFIG["auth_enabled"] = False
        self.patch = mock.patch(
            "routes.media_info.get_media_info_coordinator",
            return_value=self.coordinator,
        )
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        config_manager.APP_CONFIG.clear()
        config_manager.APP_CONFIG.update(self.old_config)

    def test_status_and_recheck_are_separate(self):
        status = self.client.get("/api/media-info/items/7/status")
        recheck = self.client.post("/api/media-info/items/7/recheck")
        self.assertEqual(200, status.status_code)
        self.assertEqual(200, recheck.status_code)
        self.assertEqual(
            [("status", "7", False), ("status", "7", True)],
            self.coordinator.calls,
        )

    def test_single_repair_returns_job(self):
        response = self.client.post("/api/media-info/items/7/repair")
        self.assertEqual(202, response.status_code)
        self.assertEqual(7, response.get_json()["job"]["id"])
        self.assertEqual([("submit", "7")], self.coordinator.calls)

    def test_batch_is_independent_and_limited(self):
        response = self.client.post(
            "/api/media-info/repair-batch",
            json={"item_ids": ["1", "2"]},
        )
        self.assertEqual(202, response.status_code)
        self.assertEqual(1, len(response.get_json()["accepted"]))
        too_many = self.client.post(
            "/api/media-info/repair-batch",
            json={"item_ids": [str(index) for index in range(21)]},
        )
        self.assertEqual(400, too_many.status_code)

    def test_job_and_pending_cancel(self):
        job = self.client.get("/api/media-info/jobs/9")
        cancel = self.client.post("/api/media-info/jobs/9/cancel")
        self.assertEqual("pending", job.get_json()["state"])
        self.assertEqual("cancelled", cancel.get_json()["state"])

    def test_internal_error_does_not_leak_exception_text(self):
        self.coordinator.fail = True
        response = self.client.post("/api/media-info/items/7/recheck")
        body = response.get_data(as_text=True)
        self.assertEqual(503, response.status_code)
        self.assertNotIn("secret", body)
        self.assertNotIn("pickcode", body)
        self.assertNotIn("token", body)

    def test_public_status_does_not_expose_full_path_or_url(self):
        body = self.client.get("/api/media-info/items/7/status").get_data(
            as_text=True
        )
        self.assertNotIn("https://", body)
        self.assertNotIn("/STRM/", body)
        self.assertIn("E01.strm", body)


if __name__ == "__main__":
    unittest.main()
