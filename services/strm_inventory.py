"""Bounded, persisted STRM directory reconciliation.

Normal reconciliation never recursively walks a root.  Each claimed row represents
one concrete directory and is scanned with ``os.scandir``.  The database cursor and
generation make work restartable and the lease prevents two EVH instances from
auditing the same directory concurrently.
"""

import heapq
import logging
import os
from typing import Callable, Dict, Iterable, Optional

from database import strm_ingest_db


logger = logging.getLogger(__name__)


class IncrementalStrmInventory:
    MAX_DIRECTORY_BATCH = 8
    MAX_ENTRIES_PER_DIRECTORY = 500

    def __init__(
        self,
        *,
        owner: str,
        audit_interval_hours: int = 24,
        directory_batch_limit: int = 4,
        entry_batch_limit: int = 500,
    ):
        self.owner = str(owner)
        self.audit_interval_hours = max(1, int(audit_interval_hours))
        self.directory_batch_limit = max(1, min(int(directory_batch_limit), self.MAX_DIRECTORY_BATCH))
        self.entry_batch_limit = max(1, min(int(entry_batch_limit), self.MAX_ENTRIES_PER_DIRECTORY))

    def _bounded_names(self, directory: str, cursor: Optional[str]):
        """Return only the next lexical window while keeping memory and stat I/O bounded."""
        after = str(cursor or '')
        with os.scandir(directory) as entries:
            names = heapq.nsmallest(
                self.entry_batch_limit + 1,
                (
                    entry.name
                    for entry in entries
                    if not entry.name.startswith('.') and entry.name > after
                ),
            )
        complete = len(names) <= self.entry_batch_limit
        selected = names[:self.entry_batch_limit]
        return selected, complete

    def scan_claim(self, claim: Dict) -> Dict[str, object]:
        directory = os.path.normpath(claim['directory_path'])
        if os.path.islink(directory):
            raise RuntimeError('inventory_directory_symlink_blocked')

        files = {}
        child_directories = []
        if os.path.isdir(directory):
            names, complete = self._bounded_names(directory, claim.get('audit_cursor'))
            for name in names:
                path = os.path.normpath(os.path.join(directory, name))
                try:
                    stat = os.lstat(path)
                except OSError:
                    continue
                if os.path.islink(path):
                    continue
                if os.path.isdir(path):
                    child_directories.append(path)
                elif name.lower().endswith('.strm') and os.path.isfile(path) and stat.st_size > 0:
                    files[path] = (int(stat.st_size), float(stat.st_mtime))
            next_cursor = names[-1] if names and not complete else None
        else:
            complete = True
            next_cursor = None

        return strm_ingest_db.record_inventory_audit_batch(
            claim,
            files=files,
            child_directories=child_directories,
            next_cursor=next_cursor,
            complete=complete,
            audit_interval_hours=self.audit_interval_hours,
        )

    def run_once(
        self,
        *,
        on_ingest: Optional[Callable[[Iterable[str]], None]] = None,
        on_delete: Optional[Callable[[Iterable[str]], None]] = None,
    ) -> Dict[str, int]:
        claims = strm_ingest_db.claim_inventory_directories(
            self.owner,
            limit=self.directory_batch_limit,
        )
        summary = {'claimed': len(claims), 'completed': 0, 'partial': 0, 'failed': 0, 'ingest': 0, 'delete': 0}
        for claim in claims:
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
                summary['completed' if result.get('complete') else 'partial'] += 1
            except Exception as exc:
                summary['failed'] += 1
                strm_ingest_db.fail_inventory_directory_claim(claim, str(exc))
                logger.error(
                    "STRM 增量目录核对失败 directory=%s error=%s",
                    claim.get('directory_path'),
                    type(exc).__name__,
                )
        return summary
