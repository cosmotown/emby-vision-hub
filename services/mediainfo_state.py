"""Read-only four-layer MediaInfo state observation.

This module never opens the media URL, runs a probe, mutates Shenyi JSON, or
submits an Emby mutation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Optional, Tuple
from urllib.parse import urlsplit

import config_manager
import constants
import handler.emby as emby


MAX_STRM_BYTES = 64 * 1024
MAX_PERSIST_BYTES = 16 * 1024 * 1024
SUPPORTED_ITEM_TYPES = {"Movie", "Episode"}
REVIEW_DIRECT_ITEM_TYPES = {"Movie", "Episode"}
ACTIVE_JOB_STATES = {"pending", "running", "submitting", "submitted"}
CONFIRMED_INCOMPLETE_MEDIA_STATES = {
    "media_source_missing",
    "media_streams_empty",
    "video_stream_missing",
    "partial",
}
logger = logging.getLogger(__name__)

_VOLATILE_FINGERPRINT_KEYS = {
    "observed_at",
    "last_checked_at",
    "snapshot_fingerprint",
    "submitted_at",
    "started_at",
    "completed_at",
    "updated_at",
    "heartbeat_at",
    "lease_expires_at",
    "request_id",
    "job_id",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _semantic_projection(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _semantic_projection(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in _VOLATILE_FINGERPRINT_KEYS
        }
    if isinstance(value, (list, tuple, set)):
        projected = [_semantic_projection(item) for item in value]
        return sorted(
            projected,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ),
        )
    return value


def _semantic_fingerprint(snapshot: Dict[str, Any]) -> str:
    return _fingerprint(_semantic_projection(snapshot))


def _path_hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _normalize_path(value: str) -> str:
    return os.path.normcase(os.path.normpath(str(value or "")))


def _safe_hint(value: str) -> str:
    name = os.path.basename(str(value or "").rstrip(os.sep))
    if not name:
        return "unmapped"
    return re.sub(r"[\r\n\t]", "_", name)[:160]


def _safe_relative_parts(root: str, candidate: str) -> Tuple[str, list[str]]:
    if "\x00" in root or "\x00" in candidate:
        raise ValueError("NUL in path")
    if not os.path.isabs(root) or not os.path.isabs(candidate):
        raise ValueError("paths must be absolute")
    if os.path.islink(root):
        raise ValueError("configured root is a symlink")
    raw_parts = candidate.replace("\\", "/").split("/")
    if any(part == ".." for part in raw_parts):
        raise ValueError("path traversal")
    lexical_root = os.path.abspath(os.path.normpath(root))
    lexical_candidate = os.path.abspath(os.path.normpath(candidate))
    if os.path.commonpath([lexical_candidate, lexical_root]) != lexical_root:
        raise ValueError("candidate escaped root")
    relative = os.path.relpath(lexical_candidate, lexical_root)
    parts = relative.split(os.sep)
    if relative in {"", "."} or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("candidate has invalid components")
    return lexical_root, parts


def _safe_read_under_root(
    root: str,
    candidate: str,
    *,
    max_bytes: int,
) -> Tuple[str, Optional[bytes], Optional[os.stat_result]]:
    """Open every child descriptor-relative and reject all child symlinks."""
    try:
        canonical_root, parts = _safe_relative_parts(root, candidate)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(canonical_root, directory_flags | nofollow)
    except FileNotFoundError:
        return "missing", None, None
    except (OSError, ValueError):
        return "unsafe", None, None

    opened_dirs = [directory_fd]
    file_fd: Optional[int] = None
    try:
        for component in parts[:-1]:
            next_fd = os.open(
                component,
                directory_flags | nofollow,
                dir_fd=directory_fd,
            )
            opened_dirs.append(next_fd)
            directory_fd = next_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | nofollow, dir_fd=directory_fd)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            return "unsafe", None, before
        if before.st_size <= 0 or before.st_size > max_bytes:
            return "invalid_size", None, before
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(file_fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(file_fd)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if len(raw) > max_bytes or identity_before != identity_after:
            return "unstable", None, before
        return "ok", raw, before
    except FileNotFoundError:
        return "missing", None, None
    except (PermissionError, OSError):
        return "unsafe", None, None
    finally:
        if file_fd is not None:
            try:
                os.close(file_fd)
            except OSError:
                pass
        for opened_fd in reversed(opened_dirs):
            try:
                os.close(opened_fd)
            except OSError:
                pass


def _observation(
    status_value: str,
    reason_code: Optional[str] = None,
    evidence_summary: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    evidence = dict(evidence_summary or {})
    return {
        "status": status_value,
        "observed_at": _utc_now(),
        "reason_code": reason_code,
        "evidence_summary": evidence,
        "evidence_fingerprint": _fingerprint(evidence),
    }


class MediaInfoStateService:
    def __init__(
        self,
        config_provider: Callable[[], Dict[str, Any]] | None = None,
        *,
        sleep: Callable[[float], None] | None = None,
    ):
        self._config_provider = config_provider or (lambda: config_manager.APP_CONFIG)
        self._sleep = sleep

    @property
    def config(self) -> Dict[str, Any]:
        return dict(self._config_provider() or {})

    def _emby_config(self) -> Tuple[str, str, str]:
        config = self.config
        return (
            str(config.get(constants.CONFIG_OPTION_EMBY_SERVER_URL) or "").rstrip("/"),
            str(config.get(constants.CONFIG_OPTION_EMBY_API_KEY) or ""),
            str(config.get(constants.CONFIG_OPTION_EMBY_USER_ID) or ""),
        )

    def _read_exact_catalog_item(
        self,
        item_id: str,
    ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        base_url, api_key, _ = self._emby_config()
        if not base_url or not api_key:
            return _observation(
                "lookup_failed",
                "emby_lookup_failed",
                {"configured": False},
            ), None
        try:
            response = emby.emby_client.get(
                f"{base_url}/Items",
                params={
                    "Ids": item_id,
                    "Limit": 2,
                    "Fields": (
                        "Id,Name,Type,Path,ParentId,SeriesId,"
                        "ParentIndexNumber,IndexNumber"
                    ),
                },
                headers={"X-Emby-Token": api_key},
            )
            response.raise_for_status()
            payload = response.json()
            items = payload.get("Items") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                raise ValueError("invalid Emby item response")
            exact_items = [
                item
                for item in items
                if isinstance(item, dict)
                and str(item.get("Id") or "").strip() == item_id
            ]
            if not exact_items:
                return _observation(
                    "not_indexed",
                    "emby_item_not_found",
                    {"candidate_count": 0},
                ), None
            if len(exact_items) != 1:
                return _observation(
                    "duplicate_match",
                    "emby_duplicate_match",
                    {"candidate_count": len(exact_items)},
                ), None
            item = exact_items[0]
            item_path = str(item.get("Path") or "").strip()
            if not item_path:
                return _observation(
                    "path_mismatch",
                    "path_unmapped",
                    {"candidate_count": 1, "has_path": False},
                ), item
        except Exception:
            return _observation(
                "lookup_failed",
                "emby_lookup_failed",
                {"configured": True},
            ), None

        try:
            response = emby.emby_client.get(
                f"{base_url}/Items",
                params={
                    "Path": item_path,
                    "Recursive": "true",
                    "Limit": 10,
                    "Fields": (
                        "Id,Name,Type,Path,ParentId,SeriesId,"
                        "ParentIndexNumber,IndexNumber"
                    ),
                },
                headers={"X-Emby-Token": api_key},
            )
            response.raise_for_status()
            payload = response.json()
            candidates = payload.get("Items") if isinstance(payload, dict) else None
            if not isinstance(candidates, list):
                raise ValueError("invalid Emby path response")
            target = _normalize_path(item_path)
            exact = [
                candidate
                for candidate in candidates
                if isinstance(candidate, dict)
                and _normalize_path(str(candidate.get("Path") or "")) == target
            ]
            exact_ids = {
                str(candidate.get("Id") or "").strip()
                for candidate in exact
                if str(candidate.get("Id") or "").strip()
            }
            if len(exact) > 1 or len(exact_ids) > 1:
                return _observation(
                    "duplicate_match",
                    "emby_duplicate_match",
                    {"candidate_count": len(exact), "exact_id_count": len(exact_ids)},
                ), item
            if len(exact) != 1 or item_id not in exact_ids:
                return _observation(
                    "path_mismatch",
                    "path_unmapped",
                    {"candidate_count": len(exact), "item_id_matches": False},
                ), item
            return _observation(
                "indexed",
                None,
                {
                    "candidate_count": 1,
                    "item_type": str(item.get("Type") or "Unknown"),
                    "path_fingerprint": _path_hash(item_path),
                },
            ), item
        except Exception:
            return _observation(
                "lookup_failed",
                "emby_lookup_failed",
                {"configured": True},
            ), item

    def resolve_review_target(
        self,
        item_id: str,
        item_type: str,
        reason: str = "",
    ) -> Dict[str, Any]:
        """Resolve one review row to an explicit Movie/Episode MediaInfo target.

        Review rows for Series may describe a failed episode in their reason.  The
        reason is only a coordinate hint; Emby identity is authoritative and must
        resolve to exactly one Episode before any status or repair action is shown.
        """
        source_id = str(item_id or "").strip()
        source_type = str(item_type or "").strip()
        result: Dict[str, Any] = {
            "source_item_id": source_id,
            "source_item_type": source_type or "Unknown",
            "target_item_id": None,
            "target_item_type": None,
            "target_series_id": None,
            "target_parent_index_number": None,
            "target_index_number": None,
            "target_resolution": "unresolved",
            "target_reason_code": "unsupported_review_item_type",
        }
        if not source_id:
            result["target_reason_code"] = "review_item_id_missing"
            return result
        if source_type in REVIEW_DIRECT_ITEM_TYPES:
            result.update(
                {
                    "target_item_id": source_id,
                    "target_item_type": source_type,
                    "target_resolution": "self",
                    "target_reason_code": None,
                }
            )
            return result
        if source_type != "Series":
            return result

        coordinates = {
            (int(season), int(episode))
            for season, episode in re.findall(
                r"(?i)(?<![A-Z0-9])S(\d{1,4})E(\d{1,4})(?![A-Z0-9])",
                str(reason or ""),
            )
        }
        if len(coordinates) != 1:
            result["target_reason_code"] = (
                "episode_coordinate_missing"
                if not coordinates
                else "episode_coordinate_ambiguous"
            )
            return result
        season_number, episode_number = next(iter(coordinates))

        base_url, api_key, _ = self._emby_config()
        if not base_url or not api_key:
            result["target_reason_code"] = "emby_lookup_failed"
            return result
        try:
            response = emby.emby_client.get(
                f"{base_url}/Items",
                params={
                    "SeriesId": source_id,
                    "IncludeItemTypes": "Episode",
                    "Recursive": "true",
                    # Emby 4.9 accepts these coordinate parameters but does not
                    # apply them on the global /Items route.  Read one bounded
                    # Series page and enforce the coordinate locally instead.
                    "Limit": 500,
                    "Fields": (
                        "Id,Name,Type,Path,ParentId,SeriesId,"
                        "ParentIndexNumber,IndexNumber"
                    ),
                },
                headers={"X-Emby-Token": api_key},
            )
            response.raise_for_status()
            payload = response.json()
            items = payload.get("Items") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                raise ValueError("invalid Emby episode response")
            total = payload.get("TotalRecordCount", len(items))
            if not isinstance(total, int) or total > len(items):
                result["target_reason_code"] = "episode_target_query_truncated"
                return result
        except Exception:
            result["target_reason_code"] = "emby_lookup_failed"
            return result

        exact = [
            item
            for item in items
            if isinstance(item, dict)
            and str(item.get("Id") or "").strip()
            and str(item.get("Type") or "") == "Episode"
            and str(item.get("SeriesId") or "").strip() == source_id
            and item.get("ParentIndexNumber") == season_number
            and item.get("IndexNumber") == episode_number
        ]
        if len(exact) != 1:
            result["target_reason_code"] = (
                "episode_target_not_found" if not exact else "episode_target_ambiguous"
            )
            return result
        target = exact[0]
        result.update(
            {
                "target_item_id": str(target["Id"]),
                "target_item_type": "Episode",
                "target_series_id": source_id,
                "target_parent_index_number": season_number,
                "target_index_number": episode_number,
                "target_resolution": "series_episode",
                "target_reason_code": None,
            }
        )
        return result

    def _configured_strm_roots(self) -> list[str]:
        config = self.config
        roots: list[str] = []
        values = config.get(constants.CONFIG_OPTION_MONITOR_PATHS) or []
        if isinstance(values, str):
            values = [values]
        for value in values:
            normalized = os.path.normpath(str(value or "").strip())
            if normalized and normalized not in roots:
                roots.append(normalized)
        return sorted(roots, key=len, reverse=True)

    def _configured_excluded_paths(self) -> list[str]:
        values = self.config.get(constants.CONFIG_OPTION_MONITOR_EXCLUDE_DIRS) or []
        if isinstance(values, str):
            values = [values]
        return [
            os.path.normpath(str(value or "").strip())
            for value in values
            if str(value or "").strip()
        ]

    def _match_visible_root(self, item_path: str) -> Optional[str]:
        if not os.path.isabs(str(item_path or "")) or "\x00" in str(item_path):
            return None
        target = _normalize_path(item_path)
        for root in self._configured_strm_roots():
            normalized_root = _normalize_path(root)
            try:
                within = os.path.commonpath([target, normalized_root]) == normalized_root
            except ValueError:
                within = False
            if within and os.path.isdir(root) and not os.path.islink(root):
                return root
        return None

    def _observe_strm(
        self,
        item_path: str,
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        if not item_path:
            return _observation(
                "path_unmapped",
                "path_unmapped",
                {"has_item_path": False},
            ), None
        root = self._match_visible_root(item_path)
        if not root:
            return _observation(
                "path_unmapped",
                "path_unmapped",
                {
                    "has_item_path": True,
                    "configured_root_count": len(self._configured_strm_roots()),
                },
            ), None
        read_status, raw, file_stat = _safe_read_under_root(
            root,
            item_path,
            max_bytes=MAX_STRM_BYTES,
        )
        if read_status == "missing":
            return _observation(
                "missing",
                "strm_missing",
                {"path_fingerprint": _path_hash(item_path)},
            ), root
        if read_status == "unstable":
            return _observation(
                "unreadable",
                "strm_unreadable",
                {"path_fingerprint": _path_hash(item_path)},
            ), root
        if read_status == "unsafe":
            return _observation(
                "unreadable",
                "strm_unreadable",
                {"path_fingerprint": _path_hash(item_path)},
            ), root
        if read_status == "invalid_size" or file_stat is None or raw is None:
            return _observation(
                "invalid_content",
                "strm_invalid_content",
                {
                    "path_fingerprint": _path_hash(item_path),
                    "regular_file": bool(
                        file_stat and stat.S_ISREG(file_stat.st_mode)
                    ),
                },
            ), root

        evidence = {
            "path_fingerprint": _path_hash(item_path),
            "size": int(file_stat.st_size),
            "mtime_ns": int(file_stat.st_mtime_ns),
            "is_strm": item_path.lower().endswith(".strm"),
        }
        if item_path.lower().endswith(".strm"):
            try:
                text = raw.decode("utf-8-sig").strip()
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                if len(lines) != 1:
                    raise ValueError("STRM must contain one non-empty line")
                parsed = urlsplit(lines[0])
                if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
                    raise ValueError("unsupported STRM target")
                evidence.update(
                    {
                        "content_sha256": hashlib.sha256(raw).hexdigest(),
                        "target_scheme": parsed.scheme.lower(),
                    }
                )
            except (OSError, UnicodeError, ValueError):
                return _observation(
                    "invalid_content",
                    "strm_invalid_content",
                    evidence,
                ), root
        return _observation("present", None, evidence), root

    def _observe_shenyi_persist(
        self,
        item_path: str,
        visible_source_root: Optional[str],
    ) -> Dict[str, Any]:
        json_root_value = str(
            self.config.get(constants.CONFIG_OPTION_SHENYI_MEDIAINFO_JSON_ROOT) or ""
        ).strip()
        if not json_root_value:
            return _observation(
                "not_configured",
                "shenyi_persist_not_configured",
                {"configured": False},
            )
        json_root = os.path.abspath(os.path.normpath(json_root_value))
        if not os.path.isdir(json_root) or os.path.islink(json_root):
            return _observation(
                "not_observable",
                "shenyi_persist_not_observable",
                {"configured": True, "root_visible": False},
            )
        if not visible_source_root or not item_path:
            return _observation(
                "identity_mismatch",
                "shenyi_identity_mismatch",
                {"source_root_mapped": False},
            )
        try:
            if "\x00" in item_path or ".." in item_path.replace("\\", "/").split("/"):
                raise ValueError("unsafe media path")
            normalized_item_path = os.path.normpath(item_path)
            if not os.path.isabs(normalized_item_path):
                raise ValueError("media path is not absolute")
            _drive, absolute_tail = os.path.splitdrive(normalized_item_path)
            mirrored_path = absolute_tail.lstrip("/\\")
            if not mirrored_path:
                raise ValueError("media path has no mirrorable components")
            relative_parent = os.path.dirname(mirrored_path)
            media_name = os.path.basename(mirrored_path)
            media_stem, _media_extension = os.path.splitext(media_name)
            json_name = (media_stem or media_name) + "-mediainfo.json"
            candidate = os.path.normpath(
                os.path.join(json_root, relative_parent, json_name)
            )
            if os.path.commonpath(
                [_normalize_path(candidate), _normalize_path(json_root)]
            ) != _normalize_path(json_root):
                raise ValueError("JSON path escaped root")
        except (OSError, ValueError):
            return _observation(
                "identity_mismatch",
                "shenyi_identity_mismatch",
                {"source_root_mapped": True},
            )

        evidence = {
            "path_fingerprint": _path_hash(candidate),
            "source_path_fingerprint": _path_hash(item_path),
            "identity_rule": "absolute_path_mirror_without_media_extension",
        }
        read_status, raw, before = _safe_read_under_root(
            json_root,
            candidate,
            max_bytes=MAX_PERSIST_BYTES,
        )
        if read_status == "missing":
            return _observation(
                "missing",
                "shenyi_persist_missing",
                evidence,
            )
        if read_status == "unstable":
            return _observation(
                "unknown",
                "shenyi_write_in_progress",
                evidence,
            )
        if read_status == "unsafe":
            return _observation(
                "present_unreadable",
                "shenyi_persist_not_observable",
                evidence,
            )
        if before is not None:
            evidence.update(
                {"size": int(before.st_size), "mtime_ns": int(before.st_mtime_ns)}
            )
        if read_status == "invalid_size" or raw is None:
            return _observation(
                "present_invalid",
                "shenyi_persist_invalid",
                evidence,
            )

        try:
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, list) or not parsed:
                raise ValueError("top level is not a non-empty array")
            valid_sources = []
            streams: list[Dict[str, Any]] = []
            for entry in parsed:
                if not isinstance(entry, dict):
                    continue
                source_info = entry.get("MediaSourceInfo")
                if not isinstance(source_info, dict):
                    continue
                source_streams = source_info.get("MediaStreams")
                if not isinstance(source_streams, list) or not source_streams:
                    continue
                if any(not isinstance(stream, dict) for stream in source_streams):
                    continue
                valid_sources.append(source_info)
                streams.extend(source_streams)
            video_count = sum(
                1
                for stream in streams
                if str(stream.get("Type") or "").lower() == "video"
            )
            if not valid_sources or not streams or video_count <= 0:
                raise ValueError("no valid video MediaSourceInfo")
            evidence.update(
                {
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "media_source_count": len(valid_sources),
                    "stream_count": len(streams),
                    "video_stream_count": video_count,
                }
            )
            return _observation("present_valid", None, evidence)
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            return _observation(
                "present_invalid",
                "shenyi_persist_invalid",
                evidence,
            )

    def read_emby_media(self, item_id: str) -> Dict[str, Any]:
        base_url, api_key, user_id = self._emby_config()
        if not base_url or not api_key or not user_id:
            return _observation(
                "read_failed",
                "emby_lookup_failed",
                {"configured": False},
            )
        try:
            response = emby.emby_client.get(
                f"{base_url}/Users/{user_id}/Items/{item_id}",
                params={
                    "Fields": (
                        "Id,Name,Type,Path,ParentId,SeriesId,"
                        "MediaSources,MediaStreams,Size,RunTimeTicks"
                    )
                },
                headers={"X-Emby-Token": api_key},
            )
            response.raise_for_status()
            item = response.json()
            if not isinstance(item, dict) or str(item.get("Id") or "") != item_id:
                raise ValueError("invalid exact Item response")
        except Exception:
            return _observation(
                "read_failed",
                "emby_lookup_failed",
                {"configured": True},
            )

        media_sources = item.get("MediaSources")
        if not isinstance(media_sources, list) or not media_sources:
            return _observation(
                "media_source_missing",
                "media_source_missing",
                {"media_source_count": 0, "stream_count": 0, "video_stream_count": 0},
            )

        streams: list[Dict[str, Any]] = []
        incomplete_source_count = 0
        for source in media_sources:
            if not isinstance(source, dict):
                incomplete_source_count += 1
                continue
            source_streams = source.get("MediaStreams")
            if isinstance(source_streams, list):
                streams.extend(
                    stream for stream in source_streams if isinstance(stream, dict)
                )
                incomplete_source_count += sum(
                    1 for stream in source_streams if not isinstance(stream, dict)
                )
            else:
                incomplete_source_count += 1
        evidence = {
            "media_source_count": len(media_sources),
            "stream_count": len(streams),
            "video_stream_count": sum(
                1
                for stream in streams
                if str(stream.get("Type") or "").lower() == "video"
            ),
            "size_present": any(
                source.get("Size") not in (None, 0, "")
                for source in media_sources
                if isinstance(source, dict)
            )
            or item.get("Size") not in (None, 0, ""),
            "runtime_present": any(
                source.get("RunTimeTicks") not in (None, 0, "")
                for source in media_sources
                if isinstance(source, dict)
            )
            or item.get("RunTimeTicks") not in (None, 0, ""),
            "incomplete_source_count": incomplete_source_count,
        }
        if not streams:
            if incomplete_source_count:
                return _observation(
                    "partial",
                    "media_stream_partial",
                    evidence,
                )
            return _observation(
                "media_streams_empty",
                "media_streams_empty",
                evidence,
            )
        if evidence["video_stream_count"] <= 0:
            return _observation(
                "video_stream_missing",
                "video_stream_missing",
                evidence,
            )
        if incomplete_source_count:
            return _observation(
                "partial",
                "media_stream_partial",
                evidence,
            )
        return _observation("ready", None, evidence)

    def observe(
        self,
        item_id: str,
        *,
        include_media: bool,
        previous_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        normalized_item_id = str(item_id or "").strip()
        if not normalized_item_id:
            raise ValueError("exact item ID is required")

        emby_index, item = self._read_exact_catalog_item(normalized_item_id)
        item_path = str((item or {}).get("Path") or "").strip()
        item_type = str((item or {}).get("Type") or "Unknown")
        root_series_key = (
            str((item or {}).get("SeriesId") or "").strip()
            if item_type == "Episode"
            else normalized_item_id
        ) or normalized_item_id

        strm_status, visible_root = self._observe_strm(item_path)
        persist_status = self._observe_shenyi_persist(item_path, visible_root)
        media_recheck_status = None
        if include_media and emby_index["status"] == "indexed":
            observed_media = self.read_emby_media(normalized_item_id)
            prior_media = (previous_snapshot or {}).get("emby_media_status")
            if (
                observed_media.get("status") == "read_failed"
                and isinstance(prior_media, dict)
                and prior_media.get("status") == "ready"
            ):
                media_status = prior_media
                media_recheck_status = observed_media
            else:
                media_status = observed_media
        else:
            prior_media = (previous_snapshot or {}).get("emby_media_status")
            media_status = (
                prior_media
                if isinstance(prior_media, dict)
                else _observation("unknown", None, {"detail_read": False})
            )

        snapshot: Dict[str, Any] = {
            "identity": {
                "exact_item_id": normalized_item_id,
                "item_type": item_type,
                "root_series_key": root_series_key,
                "exact_strm_path_hash": _path_hash(
                    _normalize_path(item_path or normalized_item_id)
                ),
                "redacted_path_hint": _safe_hint(item_path),
                "season_number": (item or {}).get("ParentIndexNumber"),
                "episode_number": (item or {}).get("IndexNumber"),
            },
            "strm_status": strm_status,
            "emby_index_status": emby_index,
            "shenyi_persist_status": persist_status,
            "emby_media_status": media_status,
            "last_checked_at": _utc_now(),
        }
        if media_recheck_status is not None:
            snapshot["emby_media_recheck_status"] = media_recheck_status
        snapshot["summary_status"] = self.derive_summary(snapshot)
        snapshot["suggested_action"] = self.suggest_action(snapshot)
        snapshot["snapshot_fingerprint"] = _semantic_fingerprint(snapshot)
        return snapshot

    @staticmethod
    def derive_summary(snapshot: Dict[str, Any]) -> str:
        media = (snapshot.get("emby_media_status") or {}).get("status")
        if media == "ready":
            return "ready"
        strm_status = (snapshot.get("strm_status") or {}).get("status")
        if strm_status != "present":
            return f"strm_{strm_status or 'unknown'}"
        index_status = (snapshot.get("emby_index_status") or {}).get("status")
        if index_status != "indexed":
            return f"emby_{index_status or 'unknown'}"
        if media in {
            "partial",
            "media_source_missing",
            "media_streams_empty",
            "video_stream_missing",
        }:
            return "media_info_incomplete"
        if media == "read_failed":
            return "media_info_read_failed"
        return "unknown"

    @staticmethod
    def suggest_action(snapshot: Dict[str, Any]) -> str:
        summary = MediaInfoStateService.derive_summary(snapshot)
        if summary == "ready":
            return "none"
        if summary == "emby_not_indexed":
            return "use_precise_strm_ingest"
        if summary.startswith("strm_"):
            return "resolve_strm_state"
        if summary in {"media_info_incomplete", "media_info_read_failed", "unknown"}:
            return "manual_recheck"
        return "resolve_identity"

    def repair_eligibility(
        self,
        snapshot: Dict[str, Any],
        *,
        active_job: Optional[Dict[str, Any]] = None,
        feature_enabled: Optional[bool] = None,
        now: Optional[datetime] = None,
    ) -> Tuple[bool, Optional[str]]:
        raw_enabled = (
            self.config.get(
                constants.CONFIG_OPTION_SHENYI_MEDIAINFO_REPAIR_ENABLED,
                False,
            )
            if feature_enabled is None
            else feature_enabled
        )
        enabled = config_manager.parse_strict_boolean(
            raw_enabled,
            option_name=constants.CONFIG_OPTION_SHENYI_MEDIAINFO_REPAIR_ENABLED,
        )
        if not enabled:
            return False, "repair_disabled"
        if (snapshot.get("strm_status") or {}).get("status") != "present":
            return False, "repair_not_eligible"
        if (snapshot.get("emby_index_status") or {}).get("status") != "indexed":
            return False, "repair_not_eligible"
        if (
            (snapshot.get("shenyi_persist_status") or {}).get("status")
            == "identity_mismatch"
        ):
            return False, "repair_not_eligible"
        if (snapshot.get("identity") or {}).get("item_type") not in SUPPORTED_ITEM_TYPES:
            return False, "repair_not_eligible"
        media_status = (snapshot.get("emby_media_status") or {}).get("status")
        if media_status not in CONFIRMED_INCOMPLETE_MEDIA_STATES:
            return False, "repair_not_eligible"
        if active_job and active_job.get("state") in ACTIVE_JOB_STATES:
            return False, "repair_already_active"
        retry_after = (active_job or {}).get("retry_after")
        if retry_after:
            try:
                retry_dt = datetime.fromisoformat(str(retry_after).replace("Z", "+00:00"))
                current = now or datetime.now(timezone.utc)
                if retry_dt > current:
                    return False, "repair_cooldown"
            except (TypeError, ValueError):
                return False, "repair_cooldown"
        return True, None


def public_snapshot(
    snapshot: Dict[str, Any],
    *,
    active_job: Optional[Dict[str, Any]] = None,
    repair_eligible: bool = False,
    eligibility_reason: Optional[str] = None,
) -> Dict[str, Any]:
    identity = snapshot.get("identity") or {}
    return {
        "identity": {
            "exact_item_id": identity.get("exact_item_id"),
            "item_type": identity.get("item_type"),
            "redacted_path_hint": identity.get("redacted_path_hint"),
            "season_number": identity.get("season_number"),
            "episode_number": identity.get("episode_number"),
        },
        "strm_status": snapshot.get("strm_status"),
        "emby_index_status": snapshot.get("emby_index_status"),
        "shenyi_persist_status": snapshot.get("shenyi_persist_status"),
        "emby_media_status": snapshot.get("emby_media_status"),
        "emby_media_recheck_status": snapshot.get("emby_media_recheck_status"),
        "summary_status": snapshot.get("summary_status"),
        "suggested_action": snapshot.get("suggested_action"),
        "last_checked_at": snapshot.get("last_checked_at"),
        "repair_eligible": bool(repair_eligible),
        "repair_eligibility_reason": eligibility_reason,
        "retry_after": (active_job or {}).get("retry_after"),
        "active_job": (
            {
                "id": active_job.get("id"),
                "state": active_job.get("state"),
                "reason_code": active_job.get("reason_code"),
                "response_kind": active_job.get("response_kind"),
                "retry_after": active_job.get("retry_after"),
                "submitted_at": active_job.get("submitted_at"),
                "started_at": active_job.get("started_at"),
                "completed_at": active_job.get("completed_at"),
            }
            if active_job and active_job.get("state") != "idle"
            else None
        ),
    }
