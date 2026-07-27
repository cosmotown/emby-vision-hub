"""Canonical metadata contracts shared by Shenyi files and the EVH database."""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, Iterable, List


PLACEHOLDER_TITLES = {
    "unknown",
    "untitled",
    "tba",
    "n/a",
    "placeholder",
    "未知",
    "未知标题",
    "暂无",
    "待定",
    "占位",
    "占位标题",
}
PLACEHOLDER_DATES = {"0000-00-00", "0001-01-01", "1900-01-01"}
STRUCTURED_FIELDS = {
    "official_rating_json",
    "countries_json",
    "genres_json",
    "production_companies_json",
    "networks_json",
}


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in {"[", "{"}:
            try:
                return json.loads(stripped)
            except (TypeError, ValueError) as exc:
                raise ValueError("结构化字段不是有效 JSON") from exc
    return value


def is_missing(
    value: Any,
    *,
    semantic: str | None = None,
    numeric_zero: bool = False,
    title: bool = False,
) -> bool:
    """Use one missing-value definition across preview, files, and SQL."""
    if value is None:
        return True
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or stripped.lower() in {"null", "none"}:
            return True
        if stripped in {"[]", "{}"}:
            return True
        if (semantic == "date") and stripped[:10] in PLACEHOLDER_DATES:
            return True
        if (semantic == "title" or title) and stripped.casefold() in PLACEHOLDER_TITLES:
            return True
        if semantic == "number" or numeric_zero:
            try:
                return float(stripped) <= 0
            except ValueError:
                return True
        return False
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) == 0
    if (semantic == "number" or numeric_zero) and isinstance(value, (int, float)):
        return not isinstance(value, bool) and value <= 0
    return False


def _country_code(value: Any) -> str:
    code = str(value or "").strip().upper()
    if len(code) != 2 or not code.isascii() or not code.isalpha():
        raise ValueError("国家代码必须为两个 ASCII 字母")
    return code


def _ratings_from_database(value: Any) -> Dict[str, str]:
    value = _json_value(value)
    if not isinstance(value, dict):
        raise ValueError("EVH official_rating_json 必须为国家到分级的对象")
    result: Dict[str, str] = {}
    for country, rating in value.items():
        code = _country_code(country)
        text = str(rating or "").strip()
        if text:
            result[code] = text
    return result


def _ratings_from_movie(value: Any) -> Dict[str, str]:
    value = _json_value(value)
    if not isinstance(value, dict) or not isinstance(value.get("results"), list):
        raise ValueError("Movie release_dates 不符合 TMDb schema")
    result: Dict[str, str] = {}
    for country_entry in value["results"]:
        if not isinstance(country_entry, dict):
            raise ValueError("Movie release_dates.results 条目必须为对象")
        code = _country_code(country_entry.get("iso_3166_1"))
        releases = country_entry.get("release_dates")
        if not isinstance(releases, list):
            raise ValueError("Movie release_dates.release_dates 必须为数组")
        for release in releases:
            if not isinstance(release, dict):
                raise ValueError("Movie release_dates 条目必须为对象")
            certification = str(release.get("certification") or "").strip()
            if certification:
                result[code] = certification
                break
    return result


def _ratings_from_series(value: Any) -> Dict[str, str]:
    value = _json_value(value)
    if not isinstance(value, dict) or not isinstance(value.get("results"), list):
        raise ValueError("Series content_ratings 不符合 TMDb schema")
    result: Dict[str, str] = {}
    for entry in value["results"]:
        if not isinstance(entry, dict):
            raise ValueError("Series content_ratings.results 条目必须为对象")
        code = _country_code(entry.get("iso_3166_1"))
        rating = str(entry.get("rating") or "").strip()
        if rating:
            result[code] = rating
    return result


def _countries_from_database(value: Any) -> List[str]:
    value = _json_value(value)
    if not isinstance(value, list):
        raise ValueError("EVH countries_json 必须为国家代码数组")
    return list(dict.fromkeys(_country_code(entry) for entry in value))


def _countries_from_movie(value: Any) -> List[str]:
    value = _json_value(value)
    if not isinstance(value, list):
        raise ValueError("Movie production_countries 必须为对象数组")
    codes = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("Movie production_countries 条目必须为对象")
        codes.append(_country_code(entry.get("iso_3166_1")))
    return list(dict.fromkeys(codes))


def _countries_from_series(value: Any) -> List[str]:
    value = _json_value(value)
    if not isinstance(value, list):
        raise ValueError("Series origin_country 必须为国家代码数组")
    return list(dict.fromkeys(_country_code(entry) for entry in value))


def _object_list(value: Any, *, field: str) -> List[Dict[str, Any]]:
    value = _json_value(value)
    if not isinstance(value, list):
        raise ValueError(f"{field} 必须为对象数组")
    result = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError(f"{field} 条目必须为对象")
        if not str(entry.get("name") or "").strip():
            raise ValueError(f"{field} 条目缺少 name")
        result.append(copy.deepcopy(entry))
    return result


def structured_to_canonical(
    field: str,
    item_type: str,
    value: Any,
    source: str,
) -> Any:
    """Convert a source-specific structured value to EVH's canonical form."""
    if field not in STRUCTURED_FIELDS:
        return copy.deepcopy(value)
    if field == "official_rating_json":
        if source == "evh_database":
            return _ratings_from_database(value)
        return (
            _ratings_from_movie(value)
            if item_type == "Movie"
            else _ratings_from_series(value)
        )
    if field == "countries_json":
        if source == "evh_database":
            return _countries_from_database(value)
        return (
            _countries_from_movie(value)
            if item_type == "Movie"
            else _countries_from_series(value)
        )
    return _object_list(value, field=field)


def structured_from_canonical(
    field: str,
    item_type: str,
    value: Any,
    target: str,
) -> Any:
    """Convert canonical values to EVH DB or genuine TMDb/Shenyi schemas."""
    canonical = structured_to_canonical(field, item_type, value, "evh_database")
    if target == "evh_database":
        return canonical
    if field == "official_rating_json":
        if item_type == "Movie":
            return {
                "results": [
                    {
                        "iso_3166_1": country,
                        "release_dates": [
                            {
                                "certification": rating,
                                "descriptors": [],
                                "iso_639_1": None,
                                "note": "",
                                "release_date": "",
                                "type": 6,
                            }
                        ],
                    }
                    for country, rating in canonical.items()
                ]
            }
        return {
            "results": [
                {
                    "descriptors": [],
                    "iso_3166_1": country,
                    "rating": rating,
                }
                for country, rating in canonical.items()
            ]
        }
    if field == "countries_json":
        if item_type == "Movie":
            return [
                {"iso_3166_1": country, "name": country}
                for country in canonical
            ]
        return canonical
    return copy.deepcopy(canonical)


def validate_database_structured(field: str, item_type: str, value: Any) -> Any:
    """Validate and normalize a structured value before a JSONB assignment."""
    canonical = structured_to_canonical(field, item_type, value, "evh_database")
    if is_missing(canonical):
        raise ValueError(f"{field} 不能写入空结构")
    return canonical


def postgres_missing_predicate(column: str, semantic: str) -> str:
    """Return the SQL predicate matching :func:`is_missing` semantics."""
    if semantic == "json":
        return (
            f"({column} IS NULL OR {column} = 'null'::jsonb OR "
            f"{column} = '[]'::jsonb OR {column} = '{{}}'::jsonb)"
        )
    if semantic == "title":
        placeholders = ", ".join(
            "'" + value.replace("'", "''") + "'"
            for value in sorted(PLACEHOLDER_TITLES)
        )
        return (
            f"({column} IS NULL OR BTRIM({column}) = '' OR "
            f"LOWER(BTRIM({column})) IN ({placeholders}))"
        )
    if semantic == "text":
        return f"({column} IS NULL OR BTRIM({column}) = '')"
    if semantic == "number":
        return f"({column} IS NULL OR {column} <= 0)"
    if semantic == "date":
        return (
            f"({column} IS NULL OR {column} IN "
            "(DATE '0001-01-01', DATE '1900-01-01'))"
        )
    raise ValueError(f"unknown missing semantic: {semantic}")
