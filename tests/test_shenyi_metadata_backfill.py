import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from services.shenyi_metadata_backfill import (
    ShenyiMetadataBackfillService,
    ShenyiStore,
    _fingerprint,
    is_missing,
)

try:
    import config_manager  # initialize the application's DB import order
    from database import media_db as database_media_db
except (ImportError, ModuleNotFoundError):
    database_media_db = None


def complete_series(**updates):
    value = {
        "id": 42,
        "name": "缓存剧名",
        "original_name": "Cached Show",
        "overview": "缓存简介",
        "first_air_date": "2025-01-01",
        "genres": [{"id": 18, "name": "剧情"}],
        "alternative_titles": {},
        "backdrop_path": None,
        "created_by": [],
        "credits": {},
        "episode_run_time": [],
        "external_ids": {},
        "homepage": "",
        "in_production": False,
        "keywords": {},
        "languages": ["zh"],
        "last_air_date": "2025-01-01",
        "networks": [],
        "vote_average": 8.2,
        "vote_count": 10,
        "number_of_episodes": 12,
        "number_of_seasons": 1,
        "origin_country": ["CN"],
        "popularity": 1.0,
        "poster_path": None,
        "status": "Ended",
        "tagline": "",
        "videos": {},
        "content_ratings": {"results": [{"iso_3166_1": "US", "rating": "TV-14"}]},
    }
    value.update(updates)
    return value


def complete_movie(**updates):
    value = {
        "id": 7,
        "title": "缓存电影",
        "original_title": "Cached Movie",
        "overview": "缓存简介",
        "release_date": "2025-02-02",
        "genres": [{"id": 18, "name": "剧情"}],
        "adult": False,
        "backdrop_path": None,
        "belongs_to_collection": None,
        "budget": 0,
        "homepage": "",
        "imdb_id": "tt0000007",
        "original_language": "zh",
        "popularity": 1.0,
        "poster_path": None,
        "production_companies": [],
        "production_countries": [],
        "revenue": 0,
        "runtime": 90,
        "spoken_languages": [],
        "status": "Released",
        "tagline": "",
        "video": False,
        "vote_average": 7.5,
        "vote_count": 10,
        "credits": {},
        "keywords": {},
        "external_ids": {},
        "videos": {},
        "release_dates": {},
        "alternative_titles": {},
    }
    value.update(updates)
    return value


class ShenyiBackfillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "cache").mkdir()
        (self.root / "override").mkdir()
        self.items = {
            "series": {
                "Id": "series",
                "Type": "Series",
                "Name": "占位",
                "Overview": "",
                "ProviderIds": {"Tmdb": "42"},
                "LockedFields": [],
            },
            "season0": {
                "Id": "season0",
                "Type": "Season",
                "Name": "",
                "Overview": "",
                "SeriesId": "series",
                "ProviderIds": {"Tmdb": "420"},
                "IndexNumber": 0,
                "LockedFields": [],
            },
            "episode": {
                "Id": "episode",
                "Type": "Episode",
                "Name": "",
                "Overview": "",
                "SeriesId": "series",
                "ProviderIds": {"Tmdb": "421"},
                "ParentIndexNumber": 0,
                "IndexNumber": 1,
                "LockedFields": [],
            },
            "episode2": {
                "Id": "episode2",
                "Type": "Episode",
                "Name": "",
                "Overview": "",
                "SeriesId": "series",
                "ProviderIds": {"Tmdb": "422"},
                "ParentIndexNumber": 0,
                "IndexNumber": 2,
                "LockedFields": [],
            },
            "movie": {
                "Id": "movie",
                "Type": "Movie",
                "Name": "",
                "Overview": "",
                "ProviderIds": {"Tmdb": "7"},
                "LockedFields": [],
            },
        }
        self.db_rows = {}
        self.db_calls = []
        self.refresh_calls = []

    def tearDown(self):
        self.temp.cleanup()

    def write_json(self, tree, relative, value):
        path = self.root / tree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def service(self):
        def fill_db(tmdb_id, item_type, values):
            row = self.db_rows.setdefault((tmdb_id, item_type), {})
            changed = []
            for key, value in values.items():
                semantic = (
                    "title"
                    if key in {"title", "original_title"}
                    else (
                        "date"
                        if key == "release_date"
                        else (
                            "number"
                            if key in {"rating", "total_episodes", "release_year"}
                            else ("json" if key.endswith("_json") else "text")
                        )
                    )
                )
                if is_missing(row.get(key), semantic=semantic):
                    row[key] = value
                    changed.append(key)
            self.db_calls.append((tmdb_id, item_type, dict(values)))
            return changed

        return ShenyiMetadataBackfillService(
            str(self.root),
            "http://emby",
            "secret",
            "user",
            get_item=lambda item_id, *_: self.items.get(item_id),
            get_db=lambda tmdb_id, item_type: dict(
                self.db_rows.get((tmdb_id, item_type), {})
            ),
            fill_db=fill_db,
            get_tmdb_by_emby=lambda _item_id: None,
            refresh=lambda item_id, *_: self.refresh_calls.append(item_id) or True,
            provider_settle_delay=0,
            verification_delays=(0,),
        )

    def test_missing_value_rules_cover_blanks_json_zero_and_placeholders(self):
        for value in (None, "", "  ", [], {}, "[]", "{}"):
            self.assertTrue(is_missing(value))
        self.assertTrue(is_missing("0001-01-01", semantic="date"))
        self.assertTrue(is_missing(0, numeric_zero=True))
        self.assertFalse(is_missing(0))
        self.assertTrue(is_missing("TBA", title=True))
        self.assertTrue(is_missing("占位", semantic="title"))
        self.assertFalse(is_missing("真实标题", title=True))

    def test_emby_locked_overview_is_never_changed(self):
        self.items["series"]["LockedFields"] = ["Overview"]
        self.write_json("cache", "tmdb-tv/42/series.json", complete_series())

        result = self.service().execute("series")

        self.assertNotIn("overview", {change["field"] for change in result["changes"]})
        self.assertNotIn("overview", self.db_rows[("42", "Series")])
        self.assertEqual(["series"], self.refresh_calls)

    def test_override_nonempty_title_beats_database_and_cache(self):
        override = complete_series(name="人工标题", overview="")
        self.items["series"]["Name"] = ""
        self.write_json("override", "tmdb-tv/42/series.json", override)
        self.write_json("cache", "tmdb-tv/42/series.json", complete_series(name="缓存标题"))
        self.db_rows[("42", "Series")] = {"title": "数据库标题", "overview": "数据库简介"}

        result = self.service().execute("series")
        written = json.loads(
            (self.root / "override/tmdb-tv/42/series.json").read_text("utf-8")
        )

        self.assertEqual("人工标题", written["name"])
        self.assertEqual("数据库简介", written["overview"])
        title_change = next(item for item in result["changes"] if item["field"] == "title")
        self.assertEqual("shenyi_override", title_change["source"])

    def test_database_value_beats_empty_cache_and_zero_never_clears_it(self):
        self.write_json(
            "cache",
            "tmdb-tv/42/series.json",
            complete_series(overview="", vote_average=0, number_of_episodes=0),
        )
        self.db_rows[("42", "Series")] = {
            "overview": "数据库简介",
            "rating": 8.8,
            "total_episodes": 24,
        }

        result = self.service().execute("series")
        written = json.loads(
            (self.root / "override/tmdb-tv/42/series.json").read_text("utf-8")
        )

        self.assertEqual("数据库简介", written["overview"])
        self.assertEqual(8.8, written["vote_average"])
        self.assertEqual(24, written["number_of_episodes"])
        self.assertTrue(result["refreshed"])

    def test_placeholder_title_and_date_are_really_updated(self):
        self.write_json("cache", "tmdb-movies2/7/all.json", complete_movie())
        self.db_rows[("7", "Movie")] = {
            "title": "占位",
            "release_date": "1900-01-01",
        }

        result = self.service().execute("movie")

        self.assertIn("title", result["database_fields"])
        self.assertIn("release_date", result["database_fields"])
        self.assertEqual("缓存电影", self.db_rows[("7", "Movie")]["title"])
        self.assertEqual("2025-02-02", self.db_rows[("7", "Movie")]["release_date"])

    def test_preview_file_and_database_results_share_missing_semantics(self):
        cache = complete_movie()
        override = complete_movie(title="占位", release_date="1900-01-01")
        self.write_json("cache", "tmdb-movies2/7/all.json", cache)
        self.write_json("override", "tmdb-movies2/7/all.json", override)
        self.items["movie"].update(
            {
                "Name": "占位",
                "OriginalTitle": "Cached Movie",
                "Overview": "缓存简介",
                "PremiereDate": "1900-01-01",
                "CommunityRating": 7.5,
                "Genres": ["剧情"],
                "Studios": [{"Name": "已存在"}],
            }
        )
        self.db_rows[("7", "Movie")] = {
            "title": "占位",
            "original_title": "Cached Movie",
            "overview": "缓存简介",
            "release_date": "1900-01-01",
            "rating": 7.5,
            "genres_json": [{"id": 18, "name": "剧情"}],
            "original_language": "zh",
        }
        service = self.service()

        preview = service.preview("movie")
        result = service.execute("movie")

        self.assertTrue(preview["would_write_file"])
        self.assertEqual(
            ["release_date", "release_year", "title"],
            preview["would_write_database"],
        )
        self.assertEqual(
            ["release_date", "release_year", "title"],
            sorted(result["database_fields"]),
        )
        self.assertEqual(
            ["release_date", "title"],
            sorted(result["file_fields"]),
        )

    def test_cache_only_fills_missing_fields_and_creates_complete_override(self):
        self.write_json("cache", "tmdb-movies2/7/all.json", complete_movie())

        result = self.service().execute("movie")

        self.assertEqual("refresh_submitted", result["status"])
        self.assertTrue(result["source_updated"])
        self.assertTrue(result["refresh_submitted"])
        self.assertTrue((self.root / "override/tmdb-movies2/7/all.json").exists())
        self.assertIn("__created__", result["file_fields"])
        self.assertEqual(["movie"], self.refresh_calls)

    def test_cache_identity_must_match_its_documented_location(self):
        self.write_json(
            "cache",
            "tmdb-movies2/7/all.json",
            complete_movie(id=8),
        )

        with self.assertRaisesRegex(ValueError, "身份字段与路径不一致"):
            self.service().preview("movie")

        self.write_json(
            "cache",
            "tmdb-tv/42/season-0.json",
            {
                "id": 420,
                "name": "错误季号",
                "overview": "不会被采用",
                "air_date": "2025-01-01",
                "season_number": 1,
            },
        )
        with self.assertRaisesRegex(ValueError, "身份字段与路径不一致"):
            self.service().preview("season0")

    def test_official_rating_isolated_from_custom_rating(self):
        self.write_json("cache", "tmdb-tv/42/series.json", complete_series())
        self.db_rows[("42", "Series")] = {"custom_rating": "人工保留"}

        self.service().execute("series")
        values = self.db_calls[-1][2]

        self.assertIn("official_rating_json", values)
        self.assertNotIn("custom_rating", values)
        self.assertEqual(
            "人工保留", self.db_rows[("42", "Series")]["custom_rating"]
        )

    def test_season_zero_and_episode_use_series_cache_directory(self):
        season = {
            "id": 420,
            "name": "特别篇",
            "overview": "季简介",
            "air_date": "2025-01-01",
            "season_number": 0,
        }
        episode = {
            "id": 421,
            "name": "特别集",
            "overview": "集简介",
            "air_date": "2025-01-02",
            "season_number": 0,
            "episode_number": 1,
        }
        self.write_json("cache", "tmdb-tv/42/season-0.json", season)
        self.write_json("cache", "tmdb-tv/42/season-0-episode-1.json", episode)

        season_result = self.service().preview("season0")
        episode_result = self.service().preview("episode")

        self.assertEqual("override/tmdb-tv/42/season-0.json", season_result["relative_override_path"])
        self.assertEqual(
            "override/tmdb-tv/42/season-0-episode-1.json",
            episode_result["relative_override_path"],
        )
        self.assertEqual("series", episode_result["root_series_id"])

    def test_invalid_tmdb_id_is_rejected(self):
        self.items["movie"]["ProviderIds"]["Tmdb"] = "../7"
        with self.assertRaisesRegex(ValueError, "正整数"):
            self.service().preview("movie")

    def test_incomplete_cache_does_not_create_override(self):
        self.write_json(
            "cache",
            "tmdb-movies2/7/all.json",
            {
                "id": 7,
                "title": "看似可用的残缺对象",
                "original_title": "Partial",
                "overview": "残缺简介",
                "release_date": "2025-01-01",
                "genres": [],
            },
        )
        with self.assertRaisesRegex(ValueError, "完整基础对象"):
            self.service().execute("movie")
        self.assertFalse((self.root / "override/tmdb-movies2/7/all.json").exists())

    def test_dry_run_never_creates_missing_shenyi_directories(self):
        empty_root = self.root / "empty"
        empty_root.mkdir()
        with self.assertRaisesRegex(ValueError, "根目录不存在"):
            ShenyiStore(str(empty_root))
        self.assertEqual([], list(empty_root.iterdir()))

    def test_refresh_verification_is_read_only_and_never_resubmits_post(self):
        self.write_json("cache", "tmdb-movies2/7/all.json", complete_movie())
        result = self.service().execute("movie")

        self.assertEqual("pending", result["verification"]["status"])
        self.assertIn("overview", result["verification"]["remaining_fields"])
        self.assertEqual(["movie"], self.refresh_calls)

    def test_http_refresh_failure_allows_one_explicit_retry_only(self):
        self.write_json("cache", "tmdb-movies2/7/all.json", complete_movie())
        outcomes = [False, True]
        service = self.service()
        service.refresh = (
            lambda item_id, *_: self.refresh_calls.append(item_id)
            or outcomes.pop(0)
        )

        first = service.execute("movie")
        passive = service.execute("movie")
        retried = service.execute("movie", explicit_retry=True)
        repeated = service.execute("movie", explicit_retry=True)

        self.assertEqual("refresh_failed", first["status"])
        self.assertEqual("refresh_failed", passive["status"])
        self.assertFalse(passive["source_updated"])
        self.assertEqual("refresh_submitted", retried["status"])
        self.assertEqual("refresh_submitted", repeated["status"])
        self.assertEqual(["movie", "movie"], self.refresh_calls)

    def test_failed_explicit_retry_cannot_form_a_manual_refresh_loop(self):
        self.write_json("cache", "tmdb-movies2/7/all.json", complete_movie())
        service = self.service()
        service.refresh = (
            lambda item_id, *_: self.refresh_calls.append(item_id) or False
        )

        first = service.execute("movie")
        retried = service.execute("movie", explicit_retry=True)
        blocked = service.execute("movie", explicit_retry=True)

        self.assertEqual("refresh_failed", first["status"])
        self.assertEqual("refresh_failed", retried["status"])
        self.assertEqual("refresh_failed", blocked["status"])
        self.assertEqual(["movie", "movie"], self.refresh_calls)

    def test_timeout_requires_read_only_confirmation_or_cooled_explicit_retry(self):
        self.write_json("cache", "tmdb-movies2/7/all.json", complete_movie())
        now = [10.0]
        service = self.service()
        service.clock = lambda: now[0]
        service.ambiguous_cooldown = 60
        outcomes = [TimeoutError("unknown delivery"), True]

        def refresh(item_id, *_):
            self.refresh_calls.append(item_id)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        service.refresh = refresh
        first = service.execute("movie")
        blocked = service.execute("movie", explicit_retry=True)
        now[0] = 71.0
        retried = service.execute("movie", explicit_retry=True)

        self.assertEqual("refresh_ambiguous", first["status"])
        self.assertGreater(blocked["retry_after_seconds"], 0)
        self.assertEqual("refresh_ambiguous", blocked["status"])
        self.assertEqual("refresh_submitted", retried["status"])
        self.assertEqual(["movie", "movie"], self.refresh_calls)

    def test_timeout_is_resolved_by_read_only_confirmation_without_post(self):
        cache = complete_movie()
        self.write_json("cache", "tmdb-movies2/7/all.json", cache)
        self.items["movie"].update(
            {
                "Name": cache["title"],
                "OriginalTitle": cache["original_title"],
                "Overview": "",
                "PremiereDate": cache["release_date"],
                "CommunityRating": cache["vote_average"],
                "OfficialRating": "PG",
                "Genres": ["剧情"],
                "Studios": [{"Name": "哨兵制片厂"}],
            }
        )
        service = self.service()

        def timeout(item_id, *_):
            self.refresh_calls.append(item_id)
            raise TimeoutError("unknown delivery")

        service.refresh = timeout
        first = service.execute("movie")
        self.items["movie"]["Overview"] = cache["overview"]
        confirmed = service.execute("movie")

        self.assertEqual("refresh_ambiguous", first["status"])
        self.assertEqual("provider_confirmed", confirmed["status"])
        self.assertEqual(["movie"], self.refresh_calls)

    def test_atomic_replace_failure_preserves_original_override(self):
        original = complete_movie(overview="")
        path = self.write_json("override", "tmdb-movies2/7/all.json", original)
        self.write_json("cache", "tmdb-movies2/7/all.json", complete_movie())
        self.db_rows[("7", "Movie")] = {"overview": "数据库简介"}

        with mock.patch(
            "services.shenyi_metadata_backfill.os.replace",
            side_effect=OSError("disk failure"),
        ):
            with self.assertRaises(OSError):
                self.service().execute("movie")

        self.assertEqual(original, json.loads(path.read_text("utf-8")))
        self.assertEqual([], self.refresh_calls)

    def test_concurrent_fingerprint_change_merges_new_shenyi_content(self):
        store = ShenyiStore(str(self.root))
        location = store.locate("Movie", "7")
        initial = complete_movie(title="旧标题", overview="")
        path = self.write_json("override", location.relative_path, initial)
        expected = _fingerprint(path)
        concurrent = complete_movie(title="神医新标题", overview="")
        path.write_text(json.dumps(concurrent), encoding="utf-8")

        store.merge_override(
            location, "Movie", {"overview": "待补简介"}, expected
        )
        written = json.loads(path.read_text("utf-8"))

        self.assertEqual("神医新标题", written["title"])
        self.assertEqual("待补简介", written["overview"])

    def test_change_immediately_before_replace_retries_with_new_content(self):
        store = ShenyiStore(str(self.root))
        location = store.locate("Movie", "7")
        initial = complete_movie(title="旧标题", overview="")
        path = self.write_json("override", location.relative_path, initial)
        expected = _fingerprint(path)
        concurrent = complete_movie(title="神医临界写入", overview="")
        real_fingerprint = _fingerprint
        calls = 0

        def race(target):
            nonlocal calls
            calls += 1
            if calls == 2:
                target.write_text(json.dumps(concurrent), encoding="utf-8")
            return real_fingerprint(target)

        with mock.patch(
            "services.shenyi_metadata_backfill._fingerprint",
            side_effect=race,
        ):
            store.merge_override(
                location, "Movie", {"overview": "待补简介"}, expected
            )

        written = json.loads(path.read_text("utf-8"))
        self.assertEqual("神医临界写入", written["title"])
        self.assertEqual("待补简介", written["overview"])

    def test_symlink_escape_is_rejected(self):
        outside = Path(self.temp.name).parent / "evh-backfill-outside"
        outside.mkdir(exist_ok=True)
        symlink = self.root / "override/tmdb-movies2"
        symlink.symlink_to(outside, target_is_directory=True)
        try:
            with self.assertRaisesRegex(ValueError, "符号链接"):
                ShenyiStore(str(self.root)).locate("Movie", "7")
        finally:
            symlink.unlink()
            outside.rmdir()

    def test_paths_never_reference_mediainfo_json(self):
        store = ShenyiStore(str(self.root))
        for item_type, args in (
            ("Movie", ("7",)),
            ("Series", ("42",)),
            ("Season", ("42", 0)),
            ("Episode", ("42", 0, 1)),
        ):
            location = store.locate(item_type, *args)
            self.assertNotIn("JSON", str(location.cache_path))
            self.assertNotIn("mediainfo", str(location.override_path).lower())

    def test_retry_after_success_performs_zero_refresh(self):
        self.write_json("cache", "tmdb-movies2/7/all.json", complete_movie())
        service = self.service()

        first = service.execute("movie")
        preview = service.preview("movie")
        second = service.execute("movie")

        self.assertEqual("refresh_submitted", first["status"])
        self.assertEqual("pending_provider", preview["status"])
        self.assertFalse(preview["would_refresh"])
        self.assertEqual("refresh_submitted", second["status"])
        self.assertFalse(second["source_updated"])
        self.assertEqual(["movie"], self.refresh_calls)

    def test_completed_auxiliary_fields_are_not_reported_again(self):
        override = complete_series(original_language="ko")
        self.write_json("override", "tmdb-tv/42/series.json", override)
        self.db_rows[("42", "Series")] = {
            "original_language": "ko",
            "countries_json": ["KR"],
        }
        self.items["series"].update(
            {
                "Name": "完整剧名",
                "OriginalTitle": "Complete Show",
                "Overview": "完整简介",
                "PremiereDate": "2025-01-01",
                "CommunityRating": 8.2,
                "OfficialRating": "TV-14",
                "Genres": ["剧情"],
                "Studios": [{"Name": "电视台"}],
                "RecursiveItemCount": 12,
            }
        )

        preview = self.service().preview("series")

        self.assertEqual("unchanged", preview["status"])
        self.assertEqual([], preview["changes"])

    def test_same_item_concurrent_request_runs_once(self):
        self.write_json("cache", "tmdb-movies2/7/all.json", complete_movie())
        entered = threading.Event()
        release = threading.Event()

        def refresh(item_id, *_):
            entered.set()
            release.wait(2)
            self.refresh_calls.append(item_id)
            return True

        service = self.service()
        service.refresh = refresh
        first_result = []
        thread = threading.Thread(target=lambda: first_result.append(service.execute("movie")))
        thread.start()
        self.assertTrue(entered.wait(1))

        duplicate = service.execute("movie")
        release.set()
        thread.join(2)

        self.assertEqual("duplicate_in_progress", duplicate["status"])
        self.assertEqual("refresh_submitted", first_result[0]["status"])
        self.assertEqual(["movie"], self.refresh_calls)

    def test_episodes_of_same_series_share_one_root_lock(self):
        episode = {
            "id": 421,
            "name": "特别集",
            "overview": "第一集简介",
            "air_date": "2025-01-02",
            "season_number": 0,
            "episode_number": 1,
        }
        episode2 = {
            **episode,
            "id": 422,
            "name": "特别集二",
            "overview": "第二集简介",
            "episode_number": 2,
        }
        self.write_json(
            "override", "tmdb-tv/42/season-0-episode-1.json", episode
        )
        self.write_json(
            "override", "tmdb-tv/42/season-0-episode-2.json", episode2
        )
        entered = threading.Event()
        release = threading.Event()

        def refresh(item_id, *_):
            entered.set()
            release.wait(2)
            self.refresh_calls.append(item_id)
            return True

        service = self.service()
        service.refresh = refresh
        first_result = []
        thread = threading.Thread(
            target=lambda: first_result.append(service.execute("episode"))
        )
        thread.start()
        self.assertTrue(entered.wait(1))

        duplicate = service.execute("episode2")
        release.set()
        thread.join(2)

        self.assertEqual("duplicate_in_progress", duplicate["status"])
        self.assertEqual("refresh_submitted", first_result[0]["status"])
        self.assertEqual(["episode"], self.refresh_calls)

    def test_series_tmdb_id_falls_back_to_evh_database(self):
        self.items["series"]["ProviderIds"] = {}
        self.write_json("cache", "tmdb-tv/42/series.json", complete_series())
        service = self.service()
        service.get_tmdb_by_emby = (
            lambda item_id: "42" if item_id == "series" else None
        )

        preview = service.preview("series")

        self.assertEqual("42", preview["tmdb_id"])
        self.assertEqual(
            "override/tmdb-tv/42/series.json",
            preview["relative_override_path"],
        )

    def test_season_and_episode_ids_fall_back_to_located_cache(self):
        self.items["season0"]["ProviderIds"] = {}
        self.items["episode"]["ProviderIds"] = {}
        self.items["series"]["ProviderIds"] = {}
        self.write_json(
            "cache",
            "tmdb-tv/42/season-0.json",
            {
                "id": 420,
                "name": "特别篇",
                "overview": "季简介",
                "air_date": "2025-01-01",
                "season_number": 0,
            },
        )
        self.write_json(
            "cache",
            "tmdb-tv/42/season-0-episode-1.json",
            {
                "id": 421,
                "name": "特别集",
                "overview": "集简介",
                "air_date": "2025-01-02",
                "season_number": 0,
                "episode_number": 1,
            },
        )
        service = self.service()
        service.get_tmdb_by_emby = (
            lambda item_id: "42" if item_id == "series" else None
        )

        season = service.preview("season0")
        episode = service.preview("episode")

        self.assertEqual("420", season["tmdb_id"])
        self.assertEqual("421", episode["tmdb_id"])
        self.assertEqual("series", episode["root_series_id"])

    def test_unused_item_and_file_locks_are_removed(self):
        self.write_json("cache", "tmdb-movies2/7/all.json", complete_movie())
        service = self.service()

        service.execute("movie")

        self.assertEqual({}, ShenyiMetadataBackfillService._item_locks)
        self.assertEqual({}, ShenyiStore._locks)


@unittest.skipUnless(database_media_db is not None, "requires application dependencies")
class SelectiveDatabaseFillTests(unittest.TestCase):
    def test_only_allowlisted_nonempty_missing_fields_are_updated(self):
        connection = mock.MagicMock()
        connection_context = mock.MagicMock()
        connection_context.__enter__.return_value = connection
        cursor = mock.MagicMock()
        cursor_context = mock.MagicMock()
        cursor_context.__enter__.return_value = cursor
        connection.cursor.return_value = cursor_context

        def execute(sql, _params):
            normalized = " ".join(sql.split())
            if normalized.startswith("UPDATE") and "official_rating_json" in normalized:
                cursor.rowcount = 1
            elif normalized.startswith("UPDATE"):
                cursor.rowcount = 0
            else:
                cursor.rowcount = 1

        cursor.execute.side_effect = execute
        with mock.patch.object(
            database_media_db, "get_db_connection", return_value=connection_context
        ):
            updated = database_media_db.selectively_fill_media_metadata(
                "42",
                "Series",
                {
                    "overview": " ",
                    "rating": 0,
                    "total_episodes": "0",
                    "official_rating_json": {"US": "TV-14"},
                    "custom_rating": "不得进入官方字段",
                },
            )

        self.assertEqual(["official_rating_json"], updated)
        sql_text = "\n".join(str(call.args[0]) for call in cursor.execute.call_args_list)
        self.assertIn("official_rating_json", sql_text)
        self.assertNotIn("custom_rating", sql_text)
        self.assertNotIn("total_episodes =", sql_text)

    def test_sql_predicates_match_placeholder_title_and_date_semantics(self):
        connection = mock.MagicMock()
        connection_context = mock.MagicMock()
        connection_context.__enter__.return_value = connection
        cursor = mock.MagicMock()
        cursor_context = mock.MagicMock()
        cursor_context.__enter__.return_value = cursor
        connection.cursor.return_value = cursor_context

        def execute(sql, _params):
            cursor.rowcount = 1

        cursor.execute.side_effect = execute
        with mock.patch.object(
            database_media_db, "get_db_connection", return_value=connection_context
        ):
            updated = database_media_db.selectively_fill_media_metadata(
                "7",
                "Movie",
                {"title": "真实标题", "release_date": "2025-02-02"},
            )

        self.assertEqual(["title", "release_date"], updated)
        sql_text = "\n".join(
            " ".join(str(call.args[0]).split())
            for call in cursor.execute.call_args_list
        )
        self.assertIn("LOWER(BTRIM(title)) IN", sql_text)
        self.assertIn("'占位'", sql_text)
        self.assertIn("release_date IN (DATE '0001-01-01', DATE '1900-01-01')", sql_text)

    def test_invalid_identity_is_rejected_before_database_access(self):
        with self.assertRaises(ValueError):
            database_media_db.selectively_fill_media_metadata(
                "../42", "Series", {"overview": "x"}
            )


if __name__ == "__main__":
    unittest.main()
