import os
import tempfile
import unittest
from unittest import mock

from services import self_update
from tasks import system_update


class FakeContainer:
    def __init__(self, name, status, labels):
        self.name = name
        self.id = name
        self.status = status
        self.removed = False
        self.attrs = {"Image": "sha256:source", "Config": {"Labels": labels}}

    def reload(self):
        return None

    def remove(self, force=False):
        self.removed = force


class FakeContainers:
    def __init__(self, containers):
        self._containers = containers

    def list(self, all=False, filters=None):
        return list(self._containers)


class FakeClient:
    def __init__(self, containers):
        self.containers = FakeContainers(containers)
        self.closed = False

    def close(self):
        self.closed = True


class SystemUpdateCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.environment = mock.patch.dict(os.environ, {"APP_DATA_DIR": self.temporary.name})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def _transaction(self, state):
        transaction = self_update.create_transaction(
            source_container_id="source-id",
            source_container_name="emby-toolkit",
            source_image_id="sha256:source",
            source_version="7.2.31",
            source_schema_contract="evh-7.2-additive-v1",
            target_version="7.2.32",
            target_image="tzyzero186/emby-vision-hub:7.2.32",
            deployment_type="standalone_docker",
        )
        self_update.update_transaction(transaction["transaction_id"], worker_container_id="terminal-worker")
        return self_update.append_transaction_event(
            transaction["transaction_id"], state, state, result=state.lower()
        )

    def test_only_stopped_terminal_worker_is_removed(self):
        terminal = self._transaction("SUCCESS")
        labels = {
            self_update.UPDATER_ROLE_LABEL: self_update.UPDATER_ROLE_VALUE,
            self_update.UPDATER_TARGET_LABEL: "emby-toolkit",
            self_update.UPDATER_TRANSACTION_LABEL: terminal["transaction_id"],
            self_update.UPDATER_SOURCE_LABEL: "source-id",
        }
        stopped = FakeContainer("terminal-worker", "exited", labels)
        running = FakeContainer("running-worker", "running", labels)
        unrelated = FakeContainer("unrelated", "exited", {})

        removed = system_update.cleanup_stale_updater_containers(
            "emby-toolkit", client=FakeClient([stopped, running, unrelated])
        )

        self.assertEqual(removed, 1)
        self.assertTrue(stopped.removed)
        self.assertFalse(running.removed)
        self.assertFalse(unrelated.removed)

    def test_worker_for_another_target_is_preserved(self):
        terminal = self._transaction("FAILED")
        worker = FakeContainer(
            "other-worker",
            "dead",
            {
                self_update.UPDATER_ROLE_LABEL: self_update.UPDATER_ROLE_VALUE,
                self_update.UPDATER_TARGET_LABEL: "another-app",
                self_update.UPDATER_TRANSACTION_LABEL: terminal["transaction_id"],
            },
        )
        removed = system_update.cleanup_stale_updater_containers(
            "emby-toolkit", client=FakeClient([worker])
        )
        self.assertEqual(removed, 0)
        self.assertFalse(worker.removed)


if __name__ == "__main__":
    unittest.main()
