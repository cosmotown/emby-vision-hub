"""Field-level metadata backfill for Shenyi Pro cache/override files.

This module intentionally has no TMDb, Douban, person, image, MediaInfo, ffprobe,
MoviePilot, or library-scan dependency.  Shenyi remains Emby's metadata
provider; EVH only fills missing values in Shenyi's documented JSON layout.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ALLOWED_TYPES = {"Movie", "Series", "Season", "Episode"}
IDENTITY_KEYS = {"id", "season_number", "episode_number"}
PLACEHOLDER_TITLES = {
    "unknown", "untitled", "tba", "n/a", "未知", "未知标题", "暂无", "待定",
}
PLACEHOLDER_DATES = {"0000-00-00", "0001-01-01", "1900-01-01"}


def is_missing(value: Any, *, numeric_zero: bool = False, title: bool = False) -> bool:
    """Return whether a value is unusable as a backfill source or target."""
    if value is None:
        return True
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return True
        if stripped.lower() in {"null", "none"}:
            return True
        if stripped in {"[]", "{}"}:
            return True
        if stripped[:10] in PLACEHOLDER_DATES:
            return True
        if title and stripped.casefold() in PLACEHOLDER_TITLES:
            return True
        return False
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) == 0
    if numeric_zero and isinstance(value, (int, float)) and not isinstance(value, bool):
        return value <= 0
    return False


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
        "episodes", "credits", "external_ids", "videos", "images",
        "translations",
    },
    "Episode": {
        "id", "name", "overview", "air_date", "season_number", "episode_number",
        "crew", "guest_stars", "production_code", "runtime", "still_path",
        "vote_average", "vote_count", "credits", "external_ids", "videos",
        "images", "translations",
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


@dataclass(frozen=True)
class ShenyiLocation:
    relative_path: str
    cache_path: Path
    override_path: Path


class ShenyiStore:
    """Resolve and atomically update documented Shenyi metadata locations."""

    _locks_guard = threading.Lock()
    _locks: Dict[str, threading.Lock] = {}

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
    def _lock_for(cls, path: Path) -> threading.Lock:
        key = str(path)
        with cls._locks_guard:
            return cls._locks.setdefault(key, threading.Lock())

    def merge_override(
        self,
        location: ShenyiLocation,
        item_type: str,
        fill_values: Dict[str, Any],
        _expected_fingerprint: Optional[Tuple[int, int, str]],
    ) -> List[str]:
        """Fill missing keys and atomically replace; never overwrite good values."""
        with self._lock_for(location.override_path):
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
                    if cache is None or not _complete_cache_base(item_type, cache):
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
                    if is_missing(
                        current.get(key),
                        numeric_zero=key in {
                            "vote_average", "number_of_episodes"
                        },
                        title=key in {
                            "title", "name", "original_title", "original_name"
                        },
                    ):
                        current[key] = candidate
                        changed.append(key)

                if not changed and current_fingerprint is not None:
                    return []
                if not _valid_identity(item_type, current):
                    raise ValueError("合并后的 override 未通过关键字段校验")

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
    _item_locks: Dict[str, threading.Lock] = {}

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
        sleep: Callable[[float], None] = time.sleep,
    ):
        if get_item is None or refresh is None:
            import handler.emby as emby

            get_item = get_item or emby.get_metadata_backfill_item
            refresh = refresh or emby.refresh_metadata_backfill_item
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
        self.sleep = sleep

    @classmethod
    def _lock_for(cls, item_id: str) -> threading.Lock:
        with cls._item_guard:
            return cls._item_locks.setdefault(item_id, threading.Lock())

    def _identity(self, item: Dict[str, Any]) -> Dict[str, Any]:
        item_type = str(item.get("Type") or "").strip()
        if item_type not in ALLOWED_TYPES:
            raise ValueError("只支持 Movie/Series/Season/Episode")

        provider_ids = item.get("ProviderIds") or {}
        item_tmdb_id = provider_ids.get("Tmdb") or provider_ids.get("TMDB")
        series_tmdb_id = item_tmdb_id
        root_series_id = None
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
        if not item_tmdb_id:
            item_tmdb_id = self.get_tmdb_by_emby(str(item.get("Id") or ""))
        identity = {
            "item_type": item_type,
            "item_tmdb_id": _positive_id(item_tmdb_id),
            "series_tmdb_id": _positive_id(series_tmdb_id),
            "root_series_id": root_series_id,
            "season_number": None,
            "episode_number": None,
        }
        if item_type == "Season":
            identity["season_number"] = int(item.get("IndexNumber"))
        elif item_type == "Episode":
            identity["season_number"] = int(item.get("ParentIndexNumber"))
            identity["episode_number"] = int(item.get("IndexNumber"))
        return identity

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
            if emby_key and not is_missing(
                emby_value,
                numeric_zero=numeric_zero,
                title=semantic in {"title", "original_title"},
            ):
                continue
            if (
                emby_key is None
                and not is_missing(
                    override_value,
                    numeric_zero=numeric_zero,
                    title=semantic in {"title", "original_title"},
                )
                and not is_missing(
                    database_value,
                    numeric_zero=numeric_zero,
                    title=semantic in {"title", "original_title"},
                )
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
                if not is_missing(
                    source_value,
                    numeric_zero=numeric_zero,
                    title=semantic in {"title", "original_title"},
                ):
                    source, candidate = source_name, source_value
                    break
            if source is None:
                continue

            candidate = _date_text(candidate) if db_key == "release_date" else candidate
            changes.append(
                {
                    "field": semantic,
                    "before": None if is_missing(emby_value) else emby_value,
                    "after": candidate,
                    "source": source,
                }
            )
            if is_missing(
                override_value,
                numeric_zero=numeric_zero,
                title=semantic in {"title", "original_title"},
            ):
                file_fill[json_key] = candidate
            if is_missing(
                database_value,
                numeric_zero=numeric_zero,
                title=semantic in {"title", "original_title"},
            ):
                db_fill[db_key] = candidate

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
        for delay in self.verification_delays:
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
                emby_key, _db_key, _json_key, numeric_zero = specs[semantic]
                if is_missing(
                    item.get(emby_key),
                    numeric_zero=numeric_zero,
                    title=semantic in {"title", "original_title"},
                ):
                    remaining.append(semantic)
            if not remaining:
                return {"status": "confirmed", "remaining_fields": []}
        return {
            "status": "pending" if saw_item else "read_failed",
            "remaining_fields": sorted(remaining),
        }

    def execute(self, item_id: str) -> Dict[str, Any]:
        normalized_id = str(item_id or "").strip()
        if not normalized_id:
            raise ValueError("item_id 不能为空")
        initial_plan = self._analyze(normalized_id)
        lock_id = str(initial_plan["root_series_id"] or normalized_id)
        item_lock = self._lock_for(lock_id)
        if not item_lock.acquire(blocking=False):
            return {"item_id": normalized_id, "status": "duplicate_in_progress"}
        try:
            # Re-read every source after lock acquisition. A retry therefore
            # becomes a no-op once the fields are already present.
            plan = self._analyze(normalized_id)
            public = self._public_result(plan)
            if not plan["changes"]:
                return {**public, "status": "unchanged", "refreshed": False}

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
            changed = bool(file_fields or database_fields)
            if file_fields and self.provider_settle_delay > 0:
                # Shenyi watches the override tree asynchronously. Give the
                # provider one bounded visibility window before the single
                # refresh POST; never compensate by replaying that mutation.
                self.sleep(self.provider_settle_delay)
            refreshed = (
                self.refresh(normalized_id, self.base_url, self.api_key)
                if changed
                else False
            )
            verification = (
                self._verify_refresh(
                    normalized_id, plan["item_type"], plan["changes"]
                )
                if refreshed
                else {"status": "not_submitted", "remaining_fields": []}
            )
            return {
                **public,
                "status": "updated" if changed else "unchanged",
                "file_fields": file_fields,
                "database_fields": database_fields,
                "refreshed": refreshed,
                "verification": verification,
            }
        finally:
            item_lock.release()
