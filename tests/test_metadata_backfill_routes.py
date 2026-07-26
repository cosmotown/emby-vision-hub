import time
import unittest
from types import SimpleNamespace
from unittest import mock

from flask import Flask

import config_manager
import extensions
from routes import metadata_backfill


class MetadataBackfillRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = "test"
        self.app.register_blueprint(metadata_backfill.metadata_backfill_bp)
        self.client = self.app.test_client()
        self.old_auth = config_manager.APP_CONFIG.get("auth_enabled")
        config_manager.APP_CONFIG["auth_enabled"] = False
        self.old_processor = extensions.media_processor_instance
        extensions.media_processor_instance = SimpleNamespace(
            emby_url="http://emby",
            emby_api_key="redacted",
            emby_user_id="user",
        )

    def tearDown(self):
        if self.old_auth is None:
            config_manager.APP_CONFIG.pop("auth_enabled", None)
        else:
            config_manager.APP_CONFIG["auth_enabled"] = self.old_auth
        extensions.media_processor_instance = self.old_processor

    def test_preview_post_is_dry_run_and_deduplicates_ids(self):
        service = mock.Mock()
        service.preview.side_effect = lambda item_id: {
            "item_id": item_id,
            "status": "would_update",
        }
        with mock.patch.object(metadata_backfill, "_service", return_value=service):
            response = self.client.post(
                "/api/metadata-backfill/preview",
                json={"item_ids": ["one", "one", "two"]},
            )

        self.assertEqual(200, response.status_code)
        self.assertTrue(response.get_json()["dry_run"])
        self.assertEqual([mock.call("one"), mock.call("two")], service.preview.call_args_list)

    def test_batch_limit_is_enforced_before_any_execution(self):
        with mock.patch.object(metadata_backfill, "_service", return_value=mock.Mock()):
            response = self.client.post(
                "/api/metadata-backfill/tasks",
                json={"item_ids": [str(index) for index in range(101)]},
            )
        self.assertEqual(400, response.status_code)

    def test_task_get_only_reads_existing_state(self):
        service = mock.Mock()
        service.execute.return_value = {"item_id": "one", "status": "updated"}
        with mock.patch.object(metadata_backfill, "_service", return_value=service):
            created = self.client.post(
                "/api/metadata-backfill/tasks", json={"item_id": "one"}
            )
        self.assertEqual(202, created.status_code)
        task_id = created.get_json()["task_id"]

        deadline = time.time() + 2
        while time.time() < deadline:
            state = self.client.get(f"/api/metadata-backfill/tasks/{task_id}")
            if state.get_json()["status"] == "completed":
                break
            time.sleep(0.01)

        self.assertEqual(200, state.status_code)
        self.assertEqual("completed", state.get_json()["status"])
        self.assertEqual(1, service.execute.call_count)


if __name__ == "__main__":
    unittest.main()
