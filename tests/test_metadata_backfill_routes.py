import time
import threading
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
        service.execution_key.return_value = "one"
        service.execute.return_value = {
            "item_id": "one",
            "status": "refresh_submitted",
        }
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
        service.execute.assert_called_once_with("one", explicit_retry=False)

    def test_explicit_retry_is_forwarded_to_one_bounded_task(self):
        service = mock.Mock()
        service.execution_key.return_value = "one"
        service.execute.return_value = {
            "item_id": "one",
            "status": "refresh_failed",
        }
        store = metadata_backfill.MetadataBackfillTaskStore(max_workers=1)
        try:
            with (
                mock.patch.object(metadata_backfill, "_service", return_value=service),
                mock.patch.object(metadata_backfill, "metadata_backfill_tasks", store),
            ):
                created = self.client.post(
                    "/api/metadata-backfill/tasks",
                    json={"item_id": "one", "explicit_retry": True},
                )
            self.assertEqual(202, created.status_code)
            deadline = time.time() + 2
            while time.time() < deadline:
                task = store.get(created.get_json()["task_id"])
                if task["status"] == "completed":
                    break
                time.sleep(0.01)
            service.execute.assert_called_once_with("one", explicit_retry=True)
        finally:
            store.shutdown()

    def test_different_episodes_are_deduplicated_by_root_series(self):
        entered = threading.Event()
        release = threading.Event()

        class Service:
            @staticmethod
            def execution_key(_item_id):
                return "root-series"

            @staticmethod
            def execute(item_id, *, explicit_retry=False):
                entered.set()
                release.wait(2)
                return {"item_id": item_id, "status": "refresh_submitted"}

        store = metadata_backfill.MetadataBackfillTaskStore(max_workers=1)
        try:
            store.start(Service(), ["episode-1"])
            self.assertTrue(entered.wait(1))
            with self.assertRaisesRegex(ValueError, "根媒体项目"):
                store.start(Service(), ["episode-2"])
        finally:
            release.set()
            store.shutdown()

    def test_task_pool_has_a_fixed_concurrency_limit(self):
        active = 0
        maximum = 0
        lock = threading.Lock()
        release = threading.Event()
        two_entered = threading.Event()

        class Service:
            @staticmethod
            def execution_key(item_id):
                return item_id

            @staticmethod
            def execute(item_id, *, explicit_retry=False):
                nonlocal active, maximum
                with lock:
                    active += 1
                    maximum = max(maximum, active)
                    if active == 2:
                        two_entered.set()
                release.wait(2)
                with lock:
                    active -= 1
                return {"item_id": item_id, "status": "refresh_submitted"}

        store = metadata_backfill.MetadataBackfillTaskStore(max_workers=2)
        try:
            for index in range(4):
                store.start(Service(), [f"item-{index}"])
            self.assertTrue(two_entered.wait(1))
            self.assertEqual(2, maximum)
        finally:
            release.set()
            store.shutdown()


if __name__ == "__main__":
    unittest.main()
