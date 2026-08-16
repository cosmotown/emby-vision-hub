import inspect
import os
import platform
import tempfile
import threading
import time
import unittest
from importlib.metadata import version
from pathlib import Path
from unittest import mock

from watchdog.events import FileSystemEventHandler

from services.persisted_directory_watcher import PersistedDirectoryObserver


LINUX = platform.system() == 'Linux'


class _CaptureHandler(FileSystemEventHandler):
    def __init__(self):
        self.events = []
        self.condition = threading.Condition()

    def on_any_event(self, event):
        with self.condition:
            self.events.append(event)
            self.condition.notify_all()

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        with self.condition:
            while time.monotonic() < deadline:
                for event in self.events:
                    if predicate(event):
                        return event
                self.condition.wait(timeout=0.05)
        return None


class WatchdogRuntimeAuditTests(unittest.TestCase):
    def test_rc_watchdog_version_is_the_audited_release(self):
        self.assertEqual('6.0.0', version('watchdog'))

    @unittest.skipUnless(LINUX, 'real inotify backend requires Linux')
    def test_watchdog_recursive_source_would_walk_but_product_adapter_never_requests_it(self):
        from watchdog.observers.inotify_c import Inotify

        dependency_source = inspect.getsource(Inotify._add_dir_watch)
        product_source = inspect.getsource(PersistedDirectoryObserver)
        self.assertIn('os.walk(path)', dependency_source)
        self.assertIn('recursive=False', product_source)
        self.assertNotIn('recursive=True', product_source)


@unittest.skipUnless(LINUX, 'real inotify backend requires Linux')
class PersistedDirectoryObserverLinuxTests(unittest.TestCase):
    def _observer(self, root, anchor, handler, provider):
        return PersistedDirectoryObserver(
            handler,
            watch_roots=[root],
            persisted_directory_provider=provider,
            anchor_path=anchor,
        )

    def test_real_startup_does_not_enumerate_unpersisted_tree(self):
        with tempfile.TemporaryDirectory() as workspace:
            anchor = os.path.join(workspace, 'anchor')
            root = os.path.join(workspace, 'STRM')
            os.mkdir(anchor)
            os.mkdir(root)
            for index in range(1001):
                os.mkdir(os.path.join(root, f'd{index:04d}'))
            handler = _CaptureHandler()
            observer = self._observer(root, anchor, handler, lambda: [root])
            observer.start()
            try:
                self.assertEqual(1, observer.watch_count)
                self.assertEqual(2, observer.backend_thread_count)
            finally:
                observer.stop()
                observer.join(timeout=5)

    def test_new_directory_adds_non_recursive_watch_and_receives_file_event(self):
        with tempfile.TemporaryDirectory() as workspace:
            anchor = os.path.join(workspace, 'anchor')
            root = os.path.join(workspace, 'STRM')
            os.mkdir(anchor)
            os.mkdir(root)
            handler = _CaptureHandler()
            observer = self._observer(root, anchor, handler, lambda: [root])
            observer.start()
            try:
                child = os.path.join(root, 'Show')
                os.mkdir(child)
                self.assertIsNotNone(handler.wait_for(lambda event: event.is_directory and event.src_path == child))
                deadline = time.monotonic() + 5
                while observer.watch_count != 2 and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual(2, observer.watch_count)
                episode = os.path.join(child, 'S01E01.strm')
                Path(episode).write_text('controlled://episode', encoding='utf-8')
                self.assertIsNotNone(handler.wait_for(lambda event: event.src_path == episode))
            finally:
                observer.stop()
                observer.join(timeout=5)

    def test_directory_delete_cleans_watch_without_dispatching_delete_self(self):
        with tempfile.TemporaryDirectory() as workspace:
            anchor = os.path.join(workspace, 'anchor')
            root = os.path.join(workspace, 'STRM')
            child = os.path.join(root, 'Show')
            os.mkdir(anchor)
            os.makedirs(child)
            handler = _CaptureHandler()
            observer = self._observer(root, anchor, handler, lambda: [root, child])
            observer.start()
            try:
                self.assertEqual(2, observer.watch_count)
                os.rmdir(child)
                deleted = handler.wait_for(
                    lambda event: event.is_directory and event.event_type == 'deleted' and event.src_path == child
                )
                self.assertIsNotNone(deleted)
                deadline = time.monotonic() + 5
                while observer.watch_count != 1 and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual(1, observer.watch_count)
                matching = [
                    event for event in handler.events
                    if event.is_directory and event.event_type == 'deleted' and event.src_path == child
                ]
                self.assertEqual(1, len(matching))
            finally:
                observer.stop()
                observer.join(timeout=5)

    def test_directory_move_remaps_known_nested_watches_without_enumeration(self):
        with tempfile.TemporaryDirectory() as workspace:
            anchor = os.path.join(workspace, 'anchor')
            root = os.path.join(workspace, 'STRM')
            old = os.path.join(root, 'A')
            old_season = os.path.join(old, 'Season 1')
            new = os.path.join(root, 'B')
            new_season = os.path.join(new, 'Season 1')
            os.mkdir(anchor)
            os.makedirs(old_season)
            handler = _CaptureHandler()
            observer = self._observer(root, anchor, handler, lambda: [root, old, old_season])
            observer.start()
            try:
                self.assertEqual(3, observer.watch_count)
                os.rename(old, new)
                moved = handler.wait_for(
                    lambda event: event.is_directory
                    and event.event_type == 'moved'
                    and event.src_path == old
                    and event.dest_path == new
                )
                self.assertIsNotNone(moved)
                self.assertEqual(3, observer.watch_count)
                episode = os.path.join(new_season, 'S01E01.strm')
                Path(episode).write_text('controlled://episode', encoding='utf-8')
                self.assertIsNotNone(handler.wait_for(lambda event: event.src_path == episode))
            finally:
                observer.stop()
                observer.join(timeout=5)

    def test_watch_count_matches_persisted_directories_under_one_backend(self):
        with tempfile.TemporaryDirectory() as workspace:
            anchor = os.path.join(workspace, 'anchor')
            root = os.path.join(workspace, 'STRM')
            os.mkdir(anchor)
            os.mkdir(root)
            directories = [root]
            for index in range(128):
                child = os.path.join(root, f'd{index:03d}')
                os.mkdir(child)
                directories.append(child)
            observer = self._observer(root, anchor, _CaptureHandler(), lambda: directories)
            observer.start()
            try:
                self.assertEqual(len(directories), observer.watch_count)
                self.assertEqual(2, observer.backend_thread_count)
                self.assertIsNotNone(observer.max_user_watches)
            finally:
                observer.stop()
                observer.join(timeout=5)

    def test_unmount_signal_never_dispatches_directory_delete(self):
        from watchdog.observers.inotify_c import InotifyConstants

        with tempfile.TemporaryDirectory() as workspace:
            anchor = os.path.join(workspace, 'anchor')
            root = os.path.join(workspace, 'STRM')
            os.mkdir(anchor)
            os.mkdir(root)
            handler = _CaptureHandler()
            observer = self._observer(root, anchor, handler, lambda: [root])
            observer.start()
            try:
                raw = mock.Mock(
                    src_path=os.fsencode(root),
                    mask=InotifyConstants.IN_UNMOUNT,
                    is_delete_self=False,
                    is_move_self=False,
                    is_ignored=False,
                )
                observer._dispatch_raw(raw)
                self.assertFalse(handler.events)
                self.assertEqual(0, observer.watch_count)
            finally:
                observer.stop()
                observer.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
