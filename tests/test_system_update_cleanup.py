import importlib.util
import sys
import types
import unittest
from pathlib import Path


class DockerNotFound(Exception):
    pass


docker_stub = types.ModuleType('docker')
docker_stub.errors = types.SimpleNamespace(
    NotFound=DockerNotFound,
    ImageNotFound=type('ImageNotFound', (Exception,), {}),
)
sys.modules.setdefault('docker', docker_stub)
sys.modules.setdefault('task_manager', types.ModuleType('task_manager'))
sys.modules.setdefault('config_manager', types.ModuleType('config_manager'))
sys.modules.setdefault('extensions', types.ModuleType('extensions'))

module_path = Path(__file__).resolve().parents[1] / 'tasks' / 'system_update.py'
spec = importlib.util.spec_from_file_location('system_update_cleanup_test_target', module_path)
system_update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(system_update)


class FakeContainer:
    def __init__(self, name, status, image='', command=None, labels=None):
        self.name = name
        self.status = status
        self.removed = False
        self.attrs = {
            'Config': {
                'Image': image,
                'Cmd': command or [],
                'Labels': labels or {},
            }
        }

    def reload(self):
        return None

    def remove(self, force=False):
        self.removed = force


class FakeContainers:
    def __init__(self, containers):
        self._containers = containers

    def list(self, all=False):
        return list(self._containers)


class FakeClient:
    def __init__(self, containers):
        self.containers = FakeContainers(containers)
        self.closed = False

    def close(self):
        self.closed = True


class SystemUpdateCleanupTests(unittest.TestCase):
    def test_only_stale_toolkit_updaters_are_removed(self):
        labels = {
            system_update.UPDATER_ROLE_LABEL: 'updater',
            system_update.UPDATER_TARGET_LABEL: 'emby-toolkit',
        }
        tagged_stale = FakeContainer('tagged', 'created', labels=labels)
        legacy_stale = FakeContainer(
            'legacy',
            'exited',
            image='containrrr/watchtower',
            command=['--cleanup', '--run-once', 'emby-toolkit'],
        )
        running = FakeContainer('running', 'running', labels=labels)
        unrelated = FakeContainer(
            'user-watchtower',
            'exited',
            image='containrrr/watchtower',
            command=['--cleanup', '--interval', '300'],
        )

        removed = system_update.cleanup_stale_updater_containers(
            'emby-toolkit',
            client=FakeClient([tagged_stale, legacy_stale, running, unrelated]),
        )

        self.assertEqual(removed, 2)
        self.assertTrue(tagged_stale.removed)
        self.assertTrue(legacy_stale.removed)
        self.assertFalse(running.removed)
        self.assertFalse(unrelated.removed)

    def test_updater_for_another_target_is_preserved(self):
        other_target = FakeContainer(
            'other-updater',
            'dead',
            labels={
                system_update.UPDATER_ROLE_LABEL: 'updater',
                system_update.UPDATER_TARGET_LABEL: 'another-app',
            },
        )

        removed = system_update.cleanup_stale_updater_containers(
            'emby-toolkit',
            client=FakeClient([other_target]),
        )

        self.assertEqual(removed, 0)
        self.assertFalse(other_target.removed)


if __name__ == '__main__':
    unittest.main()
