"""One Linux inotify backend for explicitly persisted, non-recursive directories.

The regular watchdog Linux observer creates one emitter/inotify instance per
scheduled watch.  More importantly, ``recursive=True`` walks the complete tree
before the observer starts.  This adapter keeps watchdog's audited low-level
inotify event pairing, but adds only explicit directory watches from PostgreSQL
and from subsequent directory events.  It never discovers directories by
walking the filesystem.
"""

from __future__ import annotations

import logging
import os
import platform
import stat
import threading
from typing import Callable, Iterable, Optional

from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    DirModifiedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEventHandler,
)


logger = logging.getLogger(__name__)


class PersistedDirectoryObserver:
    """Dispatch inotify events from one backend and many explicit watches."""

    WATCHDOG_VERSION = "6.0.0"

    def __init__(
        self,
        event_handler: FileSystemEventHandler,
        *,
        watch_roots: Iterable[str],
        persisted_directory_provider: Callable[[], Iterable[str]],
        anchor_path: Optional[str] = None,
    ) -> None:
        self._handler = event_handler
        self._roots = tuple(
            sorted(
                {os.path.normpath(str(path)) for path in watch_roots if str(path or "").strip()},
                key=len,
                reverse=True,
            )
        )
        self._provider = persisted_directory_provider
        self._anchor_path = os.path.normpath(
            anchor_path or os.environ.get("APP_DATA_DIR") or os.getcwd()
        )
        self._buffer = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._watched_paths: set[str] = set()
        self._max_user_watches = self._read_sysctl("/proc/sys/fs/inotify/max_user_watches")

    @staticmethod
    def _read_sysctl(path: str) -> Optional[int]:
        try:
            with open(path, "r", encoding="ascii") as handle:
                return int(handle.read().strip())
        except (OSError, TypeError, ValueError):
            return None

    @property
    def watch_count(self) -> int:
        with self._lock:
            return len(self._watched_paths)

    @property
    def backend_thread_count(self) -> int:
        with self._lock:
            buffer_thread = self._buffer
            dispatch_thread = self._thread
        return int(bool(buffer_thread and buffer_thread.is_alive())) + int(
            bool(dispatch_thread and dispatch_thread.is_alive())
        )

    @property
    def max_user_watches(self) -> Optional[int]:
        return self._max_user_watches

    def _contains(self, path: str) -> bool:
        normalized = os.path.normpath(path)
        for root in self._roots:
            try:
                if os.path.commonpath([normalized, root]) == root:
                    return True
            except ValueError:
                continue
        return False

    @staticmethod
    def _existing_plain_directory(path: str) -> bool:
        try:
            metadata = os.lstat(path)
        except OSError:
            return False
        return stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)

    def _watch_budget(self) -> Optional[int]:
        if self._max_user_watches is None:
            return None
        return max(1, self._max_user_watches - max(1024, self._max_user_watches // 10))

    def start(self) -> None:
        if platform.system() != "Linux":
            raise RuntimeError("persisted_directory_observer_requires_linux")
        if not self._existing_plain_directory(self._anchor_path):
            raise RuntimeError("persisted_directory_observer_anchor_unavailable")

        from importlib.metadata import version
        from watchdog.observers.inotify_buffer import InotifyBuffer

        installed = version("watchdog")
        if installed != self.WATCHDOG_VERSION:
            raise RuntimeError(f"unsupported_watchdog_version:{installed}")

        with self._lock:
            if self._buffer is not None:
                return
            # The stable anchor keeps the single InotifyBuffer alive if a media
            # mount disappears. It is non-recursive and its events are ignored.
            self._buffer = InotifyBuffer(os.fsencode(self._anchor_path), recursive=False)
            self._stop.clear()

        self.sync_from_persistence()
        self._thread = threading.Thread(
            target=self._dispatch_loop,
            name="evh-persisted-inotify-dispatch",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            buffer = self._buffer
        if buffer is not None:
            buffer.close()

    def join(self, timeout: Optional[float] = None) -> None:
        with self._lock:
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        with self._lock:
            self._thread = None
            self._buffer = None
            self._watched_paths.clear()

    def sync_from_persistence(self) -> dict[str, int]:
        desired = set(self._roots)
        desired.update(
            os.path.normpath(str(path))
            for path in self._provider() or []
            if str(path or "").strip() and self._contains(str(path))
        )
        with self._lock:
            current = set(self._watched_paths)
        added = 0
        removed = 0
        for path in sorted(current - desired, key=len, reverse=True):
            removed += int(self.remove_watch(path))
        for path in sorted(desired - current):
            added += int(self.add_watch(path))
        return {"desired": len(desired), "added": added, "removed": removed, "watched": self.watch_count}

    def add_watch(self, path: str) -> bool:
        normalized = os.path.normpath(path)
        if not self._contains(normalized) or not self._existing_plain_directory(normalized):
            return False
        with self._lock:
            if normalized in self._watched_paths or self._buffer is None:
                return normalized in self._watched_paths
            budget = self._watch_budget()
            if budget is not None and len(self._watched_paths) >= budget:
                logger.error(
                    "STRM inotify watch budget exhausted watched=%s budget=%s",
                    len(self._watched_paths),
                    budget,
                )
                return False
            try:
                self._buffer._inotify.add_watch(os.fsencode(normalized))
            except OSError as exc:
                logger.warning(
                    "STRM inotify watch add failed path=%s error=%s",
                    normalized,
                    type(exc).__name__,
                )
                return False
            self._watched_paths.add(normalized)
            return True

    def remove_watch(self, path: str) -> bool:
        normalized = os.path.normpath(path)
        with self._lock:
            if normalized not in self._watched_paths:
                return False
            self._watched_paths.discard(normalized)
            if self._buffer is None:
                return True
            try:
                # watchdog 6.0.0's public remove_watch() drops its path maps
                # before the reader consumes the kernel's queued IN_IGNORED,
                # which can race into KeyError. Ask the kernel to remove the
                # descriptor but retain maps until read_events handles that
                # acknowledgement.
                from watchdog.observers.inotify_c import inotify_rm_watch

                backend = self._buffer._inotify
                encoded = os.fsencode(normalized)
                with backend._lock:
                    wd = backend._wd_for_path.get(encoded)
                    if wd is not None:
                        inotify_rm_watch(backend._inotify_fd, wd)
            except (KeyError, OSError):
                pass
            return True

    def _remove_prefix(self, prefix: str) -> list[str]:
        normalized = os.path.normpath(prefix)
        with self._lock:
            affected = [
                path
                for path in self._watched_paths
                if path == normalized or path.startswith(normalized + os.sep)
            ]
        for path in sorted(affected, key=len, reverse=True):
            self.remove_watch(path)
        return affected

    def _remap_prefix(self, source: str, destination: str) -> None:
        old = os.path.normpath(source)
        new = os.path.normpath(destination)
        with self._lock:
            affected = sorted(
                [
                    path
                    for path in self._watched_paths
                    if path == old or path.startswith(old + os.sep)
                ],
                key=len,
            )
            buffer = self._buffer
            if affected and buffer is not None:
                backend = buffer._inotify
                with backend._lock:
                    for path in affected:
                        mapped = os.path.normpath(new + path[len(old) :])
                        encoded_old = os.fsencode(path)
                        encoded_new = os.fsencode(mapped)
                        wd = backend._wd_for_path.pop(encoded_old, None)
                        if wd is not None:
                            backend._wd_for_path[encoded_new] = wd
                            backend._path_for_wd[wd] = encoded_new
                        self._watched_paths.discard(path)
                        self._watched_paths.add(mapped)
        if not affected:
            self.add_watch(new)

    def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                buffer = self._buffer
            if buffer is None:
                return
            raw = buffer.read_event()
            if raw is None:
                return
            try:
                self._dispatch_raw(raw)
            except Exception:
                logger.exception("STRM persisted inotify event dispatch failed")

    def _dispatch_raw(self, raw) -> None:
        from watchdog.observers.inotify_c import InotifyConstants

        if isinstance(raw, tuple):
            source_raw, destination_raw = raw
            source = os.fsdecode(source_raw.src_path)
            destination = os.fsdecode(destination_raw.src_path)
            if not (self._contains(source) or self._contains(destination)):
                return
            is_directory = bool(source_raw.is_directory or destination_raw.is_directory)
            if is_directory:
                self._remap_prefix(source, destination)
                event = DirMovedEvent(source, destination)
            else:
                event = FileMovedEvent(source, destination)
            self._handler.dispatch(event)
            return

        path = os.fsdecode(raw.src_path)
        if not self._contains(path):
            return

        # IN_UNMOUNT/DELETE_SELF/MOVE_SELF can describe a mount or watched
        # directory becoming unavailable. They are not proof that the parent
        # was successfully listed, so never translate them into delete work.
        if raw.mask & InotifyConstants.IN_UNMOUNT or raw.is_delete_self or raw.is_move_self:
            self._remove_prefix(path)
            return
        if raw.is_ignored:
            with self._lock:
                self._watched_paths.discard(os.path.normpath(path))
            return

        if raw.is_moved_from or raw.is_delete:
            if raw.is_directory:
                self._remove_prefix(path)
                event = DirDeletedEvent(path)
            else:
                event = FileDeletedEvent(path)
        elif raw.is_moved_to or raw.is_create:
            if raw.is_directory:
                self.add_watch(path)
                event = DirCreatedEvent(path)
            else:
                event = FileCreatedEvent(path)
        elif raw.is_modify or raw.is_attrib or raw.is_close_write:
            event = DirModifiedEvent(path) if raw.is_directory else FileModifiedEvent(path)
        else:
            return
        self._handler.dispatch(event)
