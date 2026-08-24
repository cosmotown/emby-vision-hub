"""Bounded, persisted STRM directory reconciliation.

Normal reconciliation never recursively walks a root. Each claimed row represents
one concrete directory and is enumerated exactly once with ``os.scandir``. The
complete in-memory snapshot is then persisted in bounded database batches. Leases
prevent two EVH instances from auditing the same directory concurrently.
"""

import errno
import logging
import os
import stat
from typing import Callable, Dict, Iterable, Optional

from database import strm_ingest_db


logger = logging.getLogger(__name__)


class InventoryAuditError(RuntimeError):
    """A fail-closed directory observation error safe to persist and retry."""

    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


class IncrementalStrmInventory:
    MAX_DIRECTORY_BATCH = 8
    MAX_DB_BATCH_SIZE = 500

    def __init__(
        self,
        *,
        owner: str,
        audit_interval_hours: int = 24,
        directory_batch_limit: int = 4,
        db_batch_size: int = 500,
        entry_batch_limit: Optional[int] = None,
    ):
        self.owner = str(owner)
        self.audit_interval_hours = max(1, int(audit_interval_hours))
        self.directory_batch_limit = max(1, min(int(directory_batch_limit), self.MAX_DIRECTORY_BATCH))
        # ``entry_batch_limit`` is accepted only as a source-compatible alias
        # for v7.2.13 pre-RC callers. It no longer controls physical directory
        # enumeration; a directory is always opened and enumerated once.
        requested_batch = entry_batch_limit if entry_batch_limit is not None else db_batch_size
        self.db_batch_size = max(1, min(int(requested_batch), self.MAX_DB_BATCH_SIZE))

    @staticmethod
    def _classify_error(exc: OSError, *, is_root: bool) -> str:
        if isinstance(exc, PermissionError) or exc.errno in {errno.EACCES, errno.EPERM}:
            return 'permission_denied'
        if exc.errno in {errno.EIO, errno.ESTALE, errno.ENODEV, errno.ENXIO, errno.ENOTCONN}:
            return 'mount_unavailable' if is_root else 'transient_io_error'
        if isinstance(exc, FileNotFoundError) or exc.errno == errno.ENOENT:
            # A direct missing-directory stat is not deletion proof. Only a
            # successfully enumerated parent may confirm a missing child.
            return 'mount_unavailable' if is_root else 'inaccessible'
        return 'other_read_error'

    def _snapshot_directory(self, root: str, directory: str):
        is_root = directory == root
        try:
            metadata = os.lstat(directory)
        except OSError as exc:
            raise InventoryAuditError(self._classify_error(exc, is_root=is_root)) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise InventoryAuditError('symlink_blocked')
        if not stat.S_ISDIR(metadata.st_mode):
            raise InventoryAuditError('other_read_error')

        files = {}
        child_directories = []
        entries_seen = 0
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    entries_seen += 1
                    if entry.name.startswith('.'):
                        continue
                    path = os.path.normpath(os.path.join(directory, entry.name))
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        child_directories.append(path)
                        continue
                    if not entry.name.lower().endswith('.strm'):
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    file_stat = entry.stat(follow_symlinks=False)
                    if file_stat.st_size > 0:
                        files[path] = (int(file_stat.st_size), float(file_stat.st_mtime))
        except OSError as exc:
            raise InventoryAuditError(self._classify_error(exc, is_root=is_root)) from exc
        return files, child_directories, entries_seen

    def scan_claim(self, claim: Dict) -> Dict[str, object]:
        root = os.path.normpath(claim['root_path'])
        directory = os.path.normpath(claim['directory_path'])
        files, child_directories, entries_seen = self._snapshot_directory(root, directory)

        result = strm_ingest_db.record_inventory_audit_batch(
            claim,
            files=files,
            child_directories=child_directories,
            next_cursor=None,
            complete=True,
            db_batch_size=self.db_batch_size,
            audit_interval_hours=self.audit_interval_hours,
        )
        result['physical_enumerations'] = 1
        result['entries_seen'] = entries_seen
        return result

    def run_once(
        self,
        *,
        on_ingest: Optional[Callable[[Iterable[str]], None]] = None,
        on_delete: Optional[Callable[[Iterable[str]], None]] = None,
        manual_audit_id: Optional[str] = None,
        claim_limit: Optional[int] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, int]:
        claim_kwargs = {
            'limit': (
                self.directory_batch_limit
                if claim_limit is None
                else max(1, min(int(claim_limit), self.directory_batch_limit))
            ),
        }
        if manual_audit_id:
            claim_kwargs['manual_audit_id'] = str(manual_audit_id)
        claims = strm_ingest_db.claim_inventory_directories(self.owner, **claim_kwargs)
        summary = {
            'claimed': len(claims), 'completed': 0, 'partial': 0,
            'failed': 0, 'ingest': 0, 'delete': 0,
            'physical_enumerations': 0, 'entries_seen': 0, 'db_batches': 0,
            'released': 0, 'watch_set_changed': False,
        }
        for index, claim in enumerate(claims):
            if should_stop and should_stop():
                summary['released'] += strm_ingest_db.release_inventory_directory_claims(
                    claims[index:]
                )
                break
            try:
                result = self.scan_claim(claim)
                if not result.get('accepted'):
                    continue
                ingest = sorted(set(result.get('added') or []) | set(result.get('changed') or []))
                removed = result.get('removed') or []
                if ingest and on_ingest:
                    on_ingest(ingest)
                if removed and on_delete:
                    on_delete(removed)
                summary['ingest'] += len(ingest)
                summary['delete'] += len(removed)
                summary['physical_enumerations'] += int(result.get('physical_enumerations') or 0)
                summary['entries_seen'] += int(result.get('entries_seen') or 0)
                summary['db_batches'] += int(result.get('db_batches') or 0)
                summary['watch_set_changed'] = bool(
                    summary['watch_set_changed'] or result.get('watch_set_changed')
                )
                summary['completed' if result.get('complete') else 'partial'] += 1
            except Exception as exc:
                summary['failed'] += 1
                error_code = exc.code if isinstance(exc, InventoryAuditError) else 'other_read_error'
                strm_ingest_db.fail_inventory_directory_claim(claim, error_code)
                logger.error(
                    "STRM 增量目录核对失败 directory=%s status=%s error=%s",
                    claim.get('directory_path'),
                    error_code,
                    type(exc).__name__,
                )
        return summary
