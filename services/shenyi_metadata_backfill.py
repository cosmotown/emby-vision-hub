"""Field-level metadata backfill for Shenyi Pro cache/override files.

This module intentionally has no TMDb, Douban, person, image, MediaInfo, ffprobe,
MoviePilot, or library-scan dependency.  Shenyi remains Emby's metadata
provider; EVH only fills missing values in Shenyi's documented JSON layout.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from metadata_contracts import (
    STRUCTURED_FIELDS,
    is_missing,
    structured_from_canonical,
    structured_to_canonical,
)

ALLOWED_TYPES = {"Movie", "Series", "Season", "Episode"}
IDENTITY_KEYS = {"id", "season_number", "episode_number"}


def _positive_id(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized.isdigit() or int(normalized) <= 0:
        raise ValueError("TMDb ID 必须为正整数")
    return normalized


def _date_text(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    if isinstance(value, str) and len(value) >= 10:
        return value[:10]
    return value


def _missing_semantic(field: str) -> str:
    if field in {"title", "original_title"}:
        return "title"
    if field == "release_date":
        return "date"
    if field in {"rating", "total_episodes", "release_year"}:
        return "number"
    if field in STRUCTURED_FIELDS:
        return "json"
    return "text"


def _field_missing(field: str, value: Any) -> bool:
    return is_missing(value, semantic=_missing_semantic(field))


def _fingerprint(path: Path) -> Optional[Tuple[int, int, str]]:
    if not path.exists():
        return None
    stat = path.stat()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return stat.st_mtime_ns, stat.st_size, digest


def _json_object(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("神医 JSON 根节点必须为对象")
    return value


COMPLETE_CACHE_KEYS = {
    "Movie": {
        "id", "title", "original_title", "overview", "release_date", "genres",
        "adult", "backdrop_path", "belongs_to_collection", "budget", "homepage",
        "imdb_id", "original_language", "popularity", "poster_path",
        "production_companies", "production_countries", "revenue", "runtime",
        "spoken_languages", "status", "tagline", "video", "vote_average",
        "vote_count", "credits", "keywords", "external_ids", "videos",
        "release_dates", "alternative_titles",
    },
    "Series": {
        "id", "name", "original_name", "overview", "first_air_date", "genres",
        "alternative_titles", "backdrop_path", "content_ratings", "created_by",
        "credits", "episode_run_time", "external_ids", "homepage",
        "in_production", "keywords", "languages", "last_air_date", "networks",
        "number_of_episodes", "number_of_seasons", "origin_country",
        "popularity", "poster_path", "status", "tagline", "videos",
        "vote_average", "vote_count",
    },
    "Season": {
        "id", "name", "overview", "air_date", "season_number", "poster_path",
        "credits", "external_ids", "videos",
    },
    "Episode": {
        "id", "name", "overview", "air_date", "season_number", "episode_number",
        "production_code", "still_path", "vote_average", "vote_count", "credits",
        "external_ids", "videos",
    },
}


def _valid_identity(item_type: str, value: Dict[str, Any]) -> bool:
    required = {
        "Movie": {"id", "title"},
        "Series": {"id", "name"},
        "Season": {"id", "name", "season_number"},
        "Episode": {"id", "name", "season_number", "episode_number"},
    }[item_type]
    if not required.issubset(value):
        return False
    if is_missing(value.get("id"), numeric_zero=True):
        return False
    title_key = "title" if item_type == "Movie" else "name"
    return not is_missing(value.get(title_key), title=True)


def _complete_cache_base(item_type: str, value: Dict[str, Any]) -> bool:
    return COMPLETE_CACHE_KEYS[item_type].issubset(value) and _valid_identity(
        item_type, value
    )


def _identity_matches_location(
    item_type: str,
    value: Dict[str, Any],
    relative_path: str,
) -> bool:
    patterns = {
        "Movie": r"tmdb-movies2/(\d+)/all\.json",
        "Series": r"tmdb-tv/(\d+)/series\.json",
        "Season": r"tmdb-tv/(\d+)/season-(\d+)\.json",
        "Episode": r"tmdb-tv/(\d+)/season-(\d+)-episode-(\d+)\.json",
    }
    matched = re.fullmatch(patterns[item_type], relative_path)
    if matched is None:
        return False
    if item_type in {"Movie", "Series"}:
        try:
            return int(value.get("id")) == int(matched.group(1))
        except (TypeError, ValueError):
            return False
    try:
        if int(value.get("season_number")) != int(matched.group(2)):
            return False
        return item_type != "Episode" or int(value.get("episode_number")) == int(
            matched.group(3)
        )
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class ShenyiLocation:
    relative_path: str
    cache_path: Path
    override_path: Path


@dataclass
class _LockEntry:
    lock: threading.Lock
    users: int = 0


class ShenyiStore:
    """Resolve and atomically update documented Shenyi metadata locations."""

    _locks_guard = threading.Lock()
    _locks: Dict[str, _LockEntry] = {}

    def __init__(self, root: str):
        self.root = Path(str(root or "")).expanduser().resolve(strict=True)
        self.cache_root = self._resolve_tree("cache")
        self.override_root = self._resolve_tree("override")

    def _resolve_tree(self, name: str) -> Path:
        path = self.root / name
        if not path.is_dir():
            raise ValueError(f"神医 {name} 根目录不存在")
        if path.is_symlink():
            raise ValueError(f"神医 {name} 根目录不能是符号链接")
        resolved = path.resolve(strict=True)
        if os.path.commonpath((str(self.root), str(resolved))) != str(self.root):
            raise ValueError("神医目录越界")
        return resolved

    @staticmethod
    def relative_path(
        item_type: str,
        series_tmdb_id: str,
        season_number: Optional[int] = None,
        episode_number: Optional[int] = None,
    ) -> str:
        tmdb_id = _positive_id(series_tmdb_id)
        if item_type == "Movie":
            return f"tmdb-movies2/{tmdb_id}/all.json"
        if item_type == "Series":
            return f"tmdb-tv/{tmdb_id}/series.json"
        if item_type == "Season":
            if season_number is None or int(season_number) < 0:
                raise ValueError("季号必须是非负整数")
            return f"tmdb-tv/{tmdb_id}/season-{int(season_number)}.json"
        if item_type == "Episode":
            if (
                season_number is None
                or episode_number is None
                or int(season_number) < 0
                or int(episode_number) < 0
            ):
                raise ValueError("季号和集号必须是非负整数")
            return (
                f"tmdb-tv/{tmdb_id}/season-{int(season_number)}"
                f"-episode-{int(episode_number)}.json"
            )
        raise ValueError("不支持的媒体类型")

    def _safe_path(self, tree: Path, relative_path: str, *, create_parent: bool) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("拒绝越界的神医相对路径")
        candidate = tree.joinpath(relative)
        current = tree
        for part in relative.parts[:-1]:
            current = current / part
            if current.exists() and current.is_symlink():
                raise ValueError("拒绝符号链接目录")
            if create_parent:
                current.mkdir(exist_ok=True)
        if candidate.exists() and candidate.is_symlink():
            raise ValueError("拒绝符号链接目标")
        resolved_parent = candidate.parent.resolve(strict=False)
        if os.path.commonpath((str(tree), str(resolved_parent))) != str(tree):
            raise ValueError("神医目标路径越界")
        return candidate

    def locate(
        self,
        item_type: str,
        series_tmdb_id: str,
        season_number: Optional[int] = None,
        episode_number: Optional[int] = None,
    ) -> ShenyiLocation:
        relative = self.relative_path(
            item_type, series_tmdb_id, season_number, episode_number
        )
        return ShenyiLocation(
            relative_path=relative,
            cache_path=self._safe_path(self.cache_root, relative, create_parent=False),
            override_path=self._safe_path(
                self.override_root, relative, create_parent=False
            ),
        )

    @classmethod
    def _acquire_lock(cls, path: Path) -> Tuple[str, _LockEntry]:
        key = str(path)
        with cls._locks_guard:
            entry = cls._locks.setdefault(key, _LockEntry(threading.Lock()))
            entry.users += 1
        entry.lock.acquire()
        return key, entry

    @classmethod
    def _release_lock(cls, key: str, entry: _LockEntry) -> None:
        entry.lock.release()
        with cls._locks_guard:
            entry.users -= 1
            if entry.users == 0 and cls._locks.get(key) is entry:
                cls._locks.pop(key, None)

    def merge_override(
        self,
        location: ShenyiLocation,
        item_type: str,
        fill_values: Dict[str, Any],
        _expected_fingerprint: Optional[Tuple[int, int, str]],
    ) -> List[str]:
        """Fill missing keys and atomically replace; never overwrite good values."""
        lock_key, lock_entry = self._acquire_lock(location.override_path)
        try:
            for _attempt in range(3):
                # Resolve and re-read inside every attempt. This handles both a
                # change after preview and a Shenyi write during our merge.
                override_path = self._safe_path(
                    self.override_root, location.relative_path, create_parent=True
                )
                current_fingerprint = _fingerprint(override_path)
                current = (
                    _json_object(override_path) if current_fingerprint else None
                )
                created = current is None
                cache = _json_object(location.cache_path)

                if current is None:
                    if (
                        cache is None
                        or not _complete_cache_base(item_type, cache)
                        or not _identity_matches_location(
                            item_type, cache, location.relative_path
                        )
                    ):
                        raise ValueError(
                            "override 不存在且神医 cache 不是完整基础对象"
                        )
                    current = dict(cache)
                else:
                    current = dict(current)

                changed = []
                for key, candidate in fill_values.items():
                    if key in IDENTITY_KEYS or is_missing(candidate):
                        continue
                    if _json_target_missing(item_type, key, current.get(key)):
                        current[key] = candidate
                        changed.append(key)

                if not changed and current_fingerprint is not None:
                    return []
                if not _valid_identity(
                    item_type, current
                ) or not _identity_matches_location(
                    item_type, current, location.relative_path
                ):
                    raise ValueError("合并后的 override 未通过身份字段校验")

                payload = json.dumps(
                    current, ensure_ascii=False, indent=2, sort_keys=True
                ).encode("utf-8")
                fd, temporary = tempfile.mkstemp(
                    prefix=f".{override_path.name}.",
                    suffix=".tmp",
                    dir=str(override_path.parent),
                )
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(payload)
                        handle.flush()
                        os.fsync(handle.fileno())
                    if _fingerprint(override_path) != current_fingerprint:
                        os.unlink(temporary)
                        continue
                    os.replace(temporary, override_path)
                    directory_fd = os.open(
                        str(override_path.parent), os.O_RDONLY
                    )
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                    return (["__created__"] if created else []) + changed
                except Exception:
                    try:
                        os.unlink(temporary)
                    except FileNotFoundError:
                        pass
                    raise
            raise RuntimeError(
                "神医 override 持续并发变化，已安全放弃本次写入"
            )
        finally:
            self._release_lock(lock_key, lock_entry)


FIELD_SPECS = {
    "Movie": {
        "title": ("Name", "title", "title", False),
        "original_title": ("OriginalTitle", "original_title", "original_title", False),
        "overview": ("Overview", "overview", "overview", False),
        "release_date": ("PremiereDate", "release_date", "release_date", False),
        "rating": ("CommunityRating", "rating", "vote_average", True),
        "genres_json": ("Genres", "genres_json", "genres", False),
        "production_companies_json": (
            "Studios", "production_companies_json", "production_companies", False
        ),
        "countries_json": (None, "countries_json", "production_countries", False),
        "original_language": (None, "original_language", "original_language", False),
        "official_rating_json": (
            "OfficialRating", "official_rating_json", "release_dates", False
        ),
    },
    "Series": {
        "title": ("Name", "title", "name", False),
        "original_title": ("OriginalTitle", "original_title", "original_name", False),
        "overview": ("Overview", "overview", "overview", False),
        "release_date": ("PremiereDate", "release_date", "first_air_date", False),
        "rating": ("CommunityRating", "rating", "vote_average", True),
        "genres_json": ("Genres", "genres_json", "genres", False),
        "production_companies_json": (
            "Studios", "production_companies_json", "production_companies", False
        ),
        "networks_json": ("Studios", "networks_json", "networks", False),
        "countries_json": (None, "countries_json", "origin_country", False),
        "original_language": (None, "original_language", "original_language", False),
        "total_episodes": (
            "RecursiveItemCount", "total_episodes", "number_of_episodes", True
        ),
        "official_rating_json": (
            "OfficialRating", "official_rating_json", "content_ratings", False
        ),
    },
    "Season": {
        "title": ("Name", "title", "name", False),
        "overview": ("Overview", "overview", "overview", False),
        "release_date": ("PremiereDate", "release_date", "air_date", False),
    },
    "Episode": {
        "title": ("Name", "title", "name", False),
        "overview": ("Overview", "overview", "overview", False),
        "release_date": ("PremiereDate", "release_date", "air_date", False),
        "rating": ("CommunityRating", "rating", "vote_average", True),
    },
}


def _json_target_missing(item_type: str, json_key: str, value: Any) -> bool:
    semantic = next(
        (
            field
            for field, (_emby, _db, target_key, _zero) in FIELD_SPECS[
                item_type
            ].items()
            if target_key == json_key
        ),
        json_key,
    )
    if _field_missing(semantic, value):
        return True
    if semantic in STRUCTURED_FIELDS:
        canonical = structured_to_canonical(
            semantic, item_type, value, "shenyi_override"
        )
        return is_missing(canonical, semantic="json")
    return False


LOCK_FIELD_NAMES = {
    "title": "Name",
    "original_title": "Name",
    "overview": "Overview",
    "release_date": "PremiereDate",
    "rating": "CommunityRating",
    "official_rating_json": "OfficialRating",
    "genres_json": "Genres",
    "production_companies_json": "Studios",
    "networks_json": "Studios",
}


class ShenyiMetadataBackfillService:
    _item_guard = threading.Lock()
    _item_locks: Dict[str, _LockEntry] = {}
    _refresh_guard = threading.Lock()
    _refresh_records: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    _refresh_record_limit = 512

    def __init__(
        self,
        local_data_path: str,
        base_url: str,
        api_key: str,
        user_id: str,
        *,
        get_item: Optional[Callable[..., Optional[Dict[str, Any]]]] = None,
        get_db: Optional[Callable[..., Optional[Dict[str, Any]]]] = None,
        fill_db: Optional[Callable[..., List[str]]] = None,
        get_tmdb_by_emby: Optional[Callable[[str], Optional[str]]] = None,
        refresh: Optional[Callable[..., bool]] = None,
        provider_settle_delay: float = 0.5,
        verification_delays: Tuple[float, ...] = (0.1, 0.4, 1.0),
        ambiguous_cooldown: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if get_item is None or refresh is None:
            import handler.emby as emby

            get_item = get_item or emby.get_metadata_backfill_item
            refresh = refresh or (
                lambda item_id, base_url, api_key: emby.refresh_metadata_backfill_item(
                    item_id, base_url, api_key, detailed=True
                )
            )
        if get_db is None or fill_db is None or get_tmdb_by_emby is None:
            from database import media_db

            get_db = get_db or media_db.get_media_details
            fill_db = fill_db or media_db.selectively_fill_media_metadata
            get_tmdb_by_emby = (
                get_tmdb_by_emby or media_db.get_tmdb_id_from_emby_id
            )
        self.store = ShenyiStore(local_data_path)
        self.base_url = base_url
        self.api_key = api_key
        self.user_id = user_id
        self.get_item = get_item
        self.get_db = get_db
        self.fill_db = fill_db
        self.get_tmdb_by_emby = get_tmdb_by_emby
        self.refresh = refresh
        self.provider_settle_delay = max(float(provider_settle_delay), 0.0)
        self.verification_delays = verification_delays
        self.ambiguous_cooldown = max(float(ambiguous_cooldown), 0.0)
        self.sleep = sleep
        self.clock = clock

    @classmethod
    def _acquire_item_lock(cls, item_id: str) -> Optional[_LockEntry]:
        with cls._item_guard:
            entry = cls._item_locks.setdefault(
                item_id, _LockEntry(threading.Lock())
            )
            entry.users += 1
        if entry.lock.acquire(blocking=False):
            return entry
        with cls._item_guard:
            entry.users -= 1
            if entry.users == 0 and cls._item_locks.get(item_id) is entry:
                cls._item_locks.pop(item_id, None)
        return None

    @classmethod
    def _release_item_lock(cls, item_id: str, entry: _LockEntry) -> None:
        entry.lock.release()
        with cls._item_guard:
            entry.users -= 1
            if entry.users == 0 and cls._item_locks.get(item_id) is entry:
                cls._item_locks.pop(item_id, None)

    def _refresh_key(self, item_id: str) -> str:
        return f"{self.store.root}:{item_id}"

    def _get_refresh_record(self, item_id: str) -> Optional[Dict[str, Any]]:
        key = self._refresh_key(item_id)
        with self._refresh_guard:
            record = self._refresh_records.get(key)
            if record is not None:
                self._refresh_records.move_to_end(key)
                return dict(record)
        return None

    def _set_refresh_record(self, item_id: str, **values: Any) -> Dict[str, Any]:
        key = self._refresh_key(item_id)
        with self._refresh_guard:
            record = dict(self._refresh_records.get(key) or {})
            record.update(values)
            record["updated_at"] = self.clock()
            self._refresh_records[key] = record
            self._refresh_records.move_to_end(key)
            while len(self._refresh_records) > self._refresh_record_limit:
                self._refresh_records.popitem(last=False)
            return dict(record)

    def _identity(self, item: Dict[str, Any]) -> Dict[str, Any]:
        item_type = str(item.get("Type") or "").strip()
        if item_type not in ALLOWED_TYPES:
            raise ValueError("只支持 Movie/Series/Season/Episode")

        item_id = str(item.get("Id") or "").strip()
        if not item_id:
            raise ValueError("Emby 项目缺少 Id")
        provider_ids = item.get("ProviderIds") or {}
        item_tmdb_id = provider_ids.get("Tmdb") or provider_ids.get("TMDB")
        series_tmdb_id = item_tmdb_id
        root_series_id = None
        season_number = None
        episode_number = None
        if item_type in {"Season", "Episode"}:
            root_series_id = str(item.get("SeriesId") or "").strip()
            if not root_series_id:
                raise ValueError("季/集缺少 SeriesId")
            series = self.get_item(
                root_series_id, self.base_url, self.api_key, self.user_id
            )
            if not series or series.get("Type") != "Series":
                raise ValueError("无法只读核验根 Series")
            series_provider_ids = series.get("ProviderIds") or {}
            series_tmdb_id = (
                series_provider_ids.get("Tmdb") or series_provider_ids.get("TMDB")
            )
            if not series_tmdb_id:
                series_tmdb_id = self.get_tmdb_by_emby(root_series_id)
            series_tmdb_id = _positive_id(series_tmdb_id)
            if item_type == "Season":
                season_number = int(item.get("IndexNumber"))
            else:
                season_number = int(item.get("ParentIndexNumber"))
                episode_number = int(item.get("IndexNumber"))
        if not item_tmdb_id:
            item_tmdb_id = self.get_tmdb_by_emby(item_id)
        if item_type in {"Movie", "Series"}:
            series_tmdb_id = item_tmdb_id
        if not item_tmdb_id and item_type in {"Season", "Episode"}:
            fallback_location = self.store.locate(
                item_type,
                series_tmdb_id,
                season_number,
                episode_number,
            )
            fallback_cache = _json_object(fallback_location.cache_path)
            if fallback_cache and _identity_matches_location(
                item_type,
                fallback_cache,
                fallback_location.relative_path,
            ):
                item_tmdb_id = fallback_cache.get("id")
        identity = {
            "item_type": item_type,
            "item_tmdb_id": _positive_id(item_tmdb_id),
            "series_tmdb_id": _positive_id(series_tmdb_id),
            "root_series_id": root_series_id,
            "season_number": season_number,
            "episode_number": episode_number,
        }
        return identity

    def execution_key(self, item_id: str) -> str:
        """Resolve the bounded-task dedupe key without mutating any source."""
        normalized_id = str(item_id or "").strip()
        if not normalized_id:
            raise ValueError("item_id 不能为空")
        item = self.get_item(
            normalized_id, self.base_url, self.api_key, self.user_id
        )
        if not item:
            raise ValueError("无法只读获取 Emby 项目")
        identity = self._identity(item)
        return str(identity["root_series_id"] or normalized_id)

    @staticmethod
    def _canonical_source(
        semantic: str,
        item_type: str,
        source_name: str,
        value: Any,
    ) -> Any:
        if _field_missing(semantic, value):
            return None
        if semantic in STRUCTURED_FIELDS:
            value = structured_to_canonical(
                semantic, item_type, value, source_name
            )
            return None if is_missing(value, semantic="json") else value
        return _date_text(value) if semantic == "release_date" else value

    @classmethod
    def _target_missing(
        cls,
        semantic: str,
        item_type: str,
        target_name: str,
        value: Any,
    ) -> bool:
        if _field_missing(semantic, value):
            return True
        if semantic in STRUCTURED_FIELDS:
            canonical = structured_to_canonical(
                semantic, item_type, value, target_name
            )
            return is_missing(canonical, semantic="json")
        return False

    @staticmethod
    def _target_value(
        semantic: str,
        item_type: str,
        target_name: str,
        canonical: Any,
    ) -> Any:
        if semantic in STRUCTURED_FIELDS:
            return structured_from_canonical(
                semantic, item_type, canonical, target_name
            )
        return canonical

    def _analyze(self, item_id: str) -> Dict[str, Any]:
        item = self.get_item(item_id, self.base_url, self.api_key, self.user_id)
        if not item:
            raise ValueError("无法只读获取 Emby 项目")
        identity = self._identity(item)
        location = self.store.locate(
            identity["item_type"],
            identity["series_tmdb_id"],
            identity["season_number"],
            identity["episode_number"],
        )
        override = _json_object(location.override_path)
        cache = _json_object(location.cache_path)
        for source_name, source_value in (
            ("override", override),
            ("cache", cache),
        ):
            if source_value is not None and not _identity_matches_location(
                identity["item_type"],
                source_value,
                location.relative_path,
            ):
                raise ValueError(f"神医 {source_name} 身份字段与路径不一致")
        db_row = self.get_db(identity["item_tmdb_id"], identity["item_type"]) or {}

        locked = {str(value) for value in (item.get("LockedFields") or [])}
        lock_all = item.get("LockData") is True
        changes = []
        file_fill = {}
        db_fill = {}
        for semantic, (emby_key, db_key, json_key, numeric_zero) in FIELD_SPECS[
            identity["item_type"]
        ].items():
            emby_value = item.get(emby_key) if emby_key else None
            override_value = (override or {}).get(json_key)
            database_value = db_row.get(db_key)
            if emby_key and not _field_missing(semantic, emby_value):
                continue
            override_missing = self._target_missing(
                semantic,
                identity["item_type"],
                "shenyi_override",
                override_value,
            )
            database_missing = self._target_missing(
                semantic,
                identity["item_type"],
                "evh_database",
                database_value,
            )
            if (
                emby_key is None
                and not override_missing
                and not database_missing
            ):
                continue
            if lock_all or LOCK_FIELD_NAMES.get(semantic) in locked:
                continue

            source = None
            candidate = None
            for source_name, source_value in (
                ("shenyi_override", override_value),
                ("evh_database", database_value),
                ("shenyi_tmdb_cache", (cache or {}).get(json_key)),
            ):
                canonical = self._canonical_source(
                    semantic,
                    identity["item_type"],
                    source_name,
                    source_value,
                )
                if canonical is not None:
                    source, candidate = source_name, canonical
                    break
            if source is None:
                continue

            changes.append(
                {
                    "field": semantic,
                    "before": (
                        None if _field_missing(semantic, emby_value) else emby_value
                    ),
                    "after": candidate,
                    "source": source,
                }
            )
            if override_missing:
                file_fill[json_key] = self._target_value(
                    semantic,
                    identity["item_type"],
                    "shenyi_override",
                    candidate,
                )
            if database_missing:
                db_fill[db_key] = self._target_value(
                    semantic,
                    identity["item_type"],
                    "evh_database",
                    candidate,
                )

        if (
            "release_date" in db_fill
            and isinstance(db_fill["release_date"], str)
            and len(db_fill["release_date"]) >= 4
            and db_fill["release_date"][:4].isdigit()
        ):
            db_fill["release_year"] = int(db_fill["release_date"][:4])

        return {
            "item_id": str(item.get("Id") or item_id),
            "item_type": identity["item_type"],
            "root_series_id": identity["root_series_id"],
            "tmdb_id": identity["item_tmdb_id"],
            "relative_override_path": f"override/{location.relative_path}",
            "changes": changes,
            "file_fill": file_fill,
            "db_fill": db_fill,
            "location": location,
            "override_fingerprint": _fingerprint(location.override_path),
        }

    @staticmethod
    def _public_result(plan: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: plan[key]
            for key in (
                "item_id",
                "item_type",
                "root_series_id",
                "tmdb_id",
                "relative_override_path",
                "changes",
            )
        }

    def preview(self, item_id: str) -> Dict[str, Any]:
        plan = self._analyze(str(item_id or "").strip())
        result = self._public_result(plan)
        would_update = bool(plan["file_fill"] or plan["db_fill"])
        result["status"] = (
            "would_update"
            if would_update
            else ("pending_provider" if plan["changes"] else "unchanged")
        )
        result["would_write_file"] = bool(plan["file_fill"])
        result["would_write_database"] = sorted(plan["db_fill"])
        result["would_refresh"] = would_update
        return result

    def _verify_refresh(
        self,
        item_id: str,
        item_type: str,
        changes: List[Dict[str, Any]],
        *,
        delays: Optional[Tuple[float, ...]] = None,
    ) -> Dict[str, Any]:
        specs = FIELD_SPECS[item_type]
        verifiable = [
            change["field"]
            for change in changes
            if specs[change["field"]][0] is not None
        ]
        if not verifiable:
            return {"status": "not_applicable", "remaining_fields": []}

        saw_item = False
        remaining = sorted(verifiable)
        for delay in self.verification_delays if delays is None else delays:
            if delay > 0:
                self.sleep(delay)
            item = self.get_item(
                item_id, self.base_url, self.api_key, self.user_id
            )
            if not item:
                continue
            saw_item = True
            remaining = []
            for semantic in verifiable:
                emby_key, _db_key, _json_key, _numeric_zero = specs[semantic]
                if _field_missing(semantic, item.get(emby_key)):
                    remaining.append(semantic)
            if not remaining:
                return {"status": "confirmed", "remaining_fields": []}
        return {
            "status": "pending" if saw_item else "read_failed",
            "remaining_fields": sorted(remaining),
        }

    @staticmethod
    def _state_result(
        public: Dict[str, Any],
        state: str,
        *,
        states: List[str],
        file_fields: Optional[List[str]] = None,
        database_fields: Optional[List[str]] = None,
        verification: Optional[Dict[str, Any]] = None,
        retry_after_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        result = {
            **public,
            "status": state,
            "states": states,
            "source_updated": "source_updated" in states,
            "refresh_submitted": "refresh_submitted" in states,
            "provider_confirmed": state == "provider_confirmed",
            "refresh_failed": state == "refresh_failed",
            "refresh_ambiguous": state == "refresh_ambiguous",
            "file_fields": file_fields or [],
            "database_fields": database_fields or [],
            "refreshed": "refresh_submitted" in states,
            "verification": verification
            or {"status": "not_submitted", "remaining_fields": []},
        }
        if retry_after_seconds is not None:
            result["retry_after_seconds"] = max(
                0, int(retry_after_seconds + 0.999)
            )
        return result

    def _submit_refresh(self, item_id: str) -> str:
        try:
            outcome = self.refresh(item_id, self.base_url, self.api_key)
        except (requests.Timeout, TimeoutError):
            return "ambiguous"
        except Exception:
            return "http_failed"
        if isinstance(outcome, dict):
            normalized = str(outcome.get("outcome") or "").strip().lower()
            if normalized in {"submitted", "http_failed", "ambiguous"}:
                return normalized
            return "submitted" if outcome.get("submitted") is True else "http_failed"
        return "submitted" if outcome is True else "http_failed"

    def execute(
        self,
        item_id: str,
        *,
        explicit_retry: bool = False,
    ) -> Dict[str, Any]:
        normalized_id = str(item_id or "").strip()
        if not normalized_id:
            raise ValueError("item_id 不能为空")
        initial_plan = self._analyze(normalized_id)
        lock_id = str(initial_plan["root_series_id"] or normalized_id)
        item_lock = self._acquire_item_lock(lock_id)
        if item_lock is None:
            return {"item_id": normalized_id, "status": "duplicate_in_progress"}
        try:
            # Re-read every source after lock acquisition. A retry first checks
            # provider state and never repeats a mutation automatically.
            plan = self._analyze(normalized_id)
            public = self._public_result(plan)
            if not plan["changes"]:
                record = self._get_refresh_record(normalized_id) or {}
                remembered_fields = list(record.get("fields") or [])
                if (
                    record.get("state")
                    in {
                        "source_updated",
                        "refresh_submitted",
                        "refresh_ambiguous",
                    }
                    and remembered_fields
                ):
                    verification = self._verify_refresh(
                        normalized_id,
                        plan["item_type"],
                        [{"field": field} for field in remembered_fields],
                        delays=(0,),
                    )
                    if verification["status"] == "confirmed":
                        self._set_refresh_record(
                            normalized_id,
                            state="provider_confirmed",
                            root_key=lock_id,
                        )
                        return self._state_result(
                            public,
                            "provider_confirmed",
                            states=["provider_confirmed"],
                            verification=verification,
                        )
                return self._state_result(public, "unchanged", states=[])

            file_fields = []
            if plan["file_fill"]:
                file_fields = self.store.merge_override(
                    plan["location"],
                    plan["item_type"],
                    plan["file_fill"],
                    plan["override_fingerprint"],
                )
            database_fields = self.fill_db(
                plan["tmdb_id"], plan["item_type"], plan["db_fill"]
            )
            source_updated = bool(file_fields or database_fields)
            states = ["source_updated"] if source_updated else []
            if source_updated:
                self._set_refresh_record(
                    normalized_id,
                    state="source_updated",
                    root_key=lock_id,
                    fields=[change["field"] for change in plan["changes"]],
                )
            else:
                read_only_verification = self._verify_refresh(
                    normalized_id,
                    plan["item_type"],
                    plan["changes"],
                    delays=(0,),
                )
                if read_only_verification["status"] == "confirmed":
                    self._set_refresh_record(
                        normalized_id,
                        state="provider_confirmed",
                        root_key=lock_id,
                    )
                    return self._state_result(
                        public,
                        "provider_confirmed",
                        states=["provider_confirmed"],
                        verification=read_only_verification,
                    )
                record = self._get_refresh_record(normalized_id) or {}
                if record.get("state") == "refresh_submitted":
                    return self._state_result(
                        public,
                        "refresh_submitted",
                        states=["refresh_submitted"],
                        verification=read_only_verification,
                    )
                if not explicit_retry:
                    remembered = record.get("state")
                    state = (
                        remembered
                        if remembered
                        in {
                            "refresh_submitted",
                            "refresh_failed",
                            "refresh_ambiguous",
                        }
                        else "refresh_ambiguous"
                    )
                    return self._state_result(
                        public,
                        state,
                        states=[state],
                        verification=read_only_verification,
                    )
                if (
                    record.get("state") == "refresh_failed"
                    and record.get("explicit_retry_used") is True
                ):
                    return self._state_result(
                        public,
                        "refresh_failed",
                        states=["refresh_failed"],
                        verification=read_only_verification,
                    )
                ambiguous_until = float(record.get("ambiguous_until") or 0)
                if (
                    record.get("state") == "refresh_ambiguous"
                    and self.clock() < ambiguous_until
                ):
                    return self._state_result(
                        public,
                        "refresh_ambiguous",
                        states=["refresh_ambiguous"],
                        verification=read_only_verification,
                        retry_after_seconds=ambiguous_until - self.clock(),
                    )

            if file_fields and self.provider_settle_delay > 0:
                # Shenyi watches the override tree asynchronously. Give the
                # provider one bounded visibility window before the single
                # refresh POST; never compensate by replaying that mutation.
                self.sleep(self.provider_settle_delay)
            retry_submission = bool(explicit_retry and not source_updated)
            refresh_outcome = self._submit_refresh(normalized_id)
            if refresh_outcome == "http_failed":
                record = self._get_refresh_record(normalized_id) or {}
                self._set_refresh_record(
                    normalized_id,
                    state="refresh_failed",
                    root_key=lock_id,
                    explicit_retry_used=bool(
                        record.get("explicit_retry_used") or retry_submission
                    ),
                )
                return self._state_result(
                    public,
                    "refresh_failed",
                    states=states + ["refresh_failed"],
                    file_fields=file_fields,
                    database_fields=database_fields,
                )
            if refresh_outcome == "ambiguous":
                self._set_refresh_record(
                    normalized_id,
                    state="refresh_ambiguous",
                    root_key=lock_id,
                    ambiguous_until=self.clock() + self.ambiguous_cooldown,
                )
                return self._state_result(
                    public,
                    "refresh_ambiguous",
                    states=states + ["refresh_ambiguous"],
                    file_fields=file_fields,
                    database_fields=database_fields,
                    retry_after_seconds=self.ambiguous_cooldown,
                )

            states.append("refresh_submitted")
            self._set_refresh_record(
                normalized_id,
                state="refresh_submitted",
                root_key=lock_id,
            )
            verification = self._verify_refresh(
                normalized_id, plan["item_type"], plan["changes"]
            )
            if verification["status"] == "confirmed":
                states.append("provider_confirmed")
                self._set_refresh_record(
                    normalized_id,
                    state="provider_confirmed",
                    root_key=lock_id,
                )
                state = "provider_confirmed"
            else:
                state = "refresh_submitted"
            return self._state_result(
                public,
                state,
                states=states,
                file_fields=file_fields,
                database_fields=database_fields,
                verification=verification,
            )
        finally:
            self._release_item_lock(lock_id, item_lock)
