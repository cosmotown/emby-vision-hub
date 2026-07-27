import copy
import ast
import json
import tempfile
import unittest
from pathlib import Path

from metadata_contracts import is_missing
from services.shenyi_metadata_backfill import (
    ShenyiMetadataBackfillService,
    ShenyiStore,
)
from services import shenyi_metadata_backfill


FIXTURES = Path(__file__).parent / "fixtures" / "shenyi_real_sanitized"


class ShenyiFullRefreshContractE2E(unittest.TestCase):
    CASES = (
        ("movie", "Movie", "movie.json", "707", None, None, "overview"),
        ("series", "Series", "series.json", "808", None, None, "overview"),
        ("season0", "Season", "season-0.json", "808", 0, None, "release_date"),
        ("season1", "Season", "season-1.json", "808", 1, None, "release_date"),
        ("episode", "Episode", "episode-s1e1.json", "808", 1, 1, "overview"),
    )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "cache").mkdir()
        (self.root / "override").mkdir()
        self.items = {
            "movie": self._sentinel_item("movie", "Movie", "707"),
            "series": self._sentinel_item("series", "Series", None),
            "season0": self._sentinel_item("season0", "Season", None, 0),
            "season1": self._sentinel_item("season1", "Season", None, 1),
            "episode": self._sentinel_item("episode", "Episode", None, 1, 1),
        }
        for item_id in ("season0", "season1"):
            self.items[item_id]["Overview"] = f"SENTINEL-{item_id}-OVERVIEW"
            self.items[item_id]["PremiereDate"] = None
            self.items[item_id]["LockedFields"].remove("PremiereDate")
            self.items[item_id]["LockedFields"].append("Overview")
        self.db_rows = {}
        self.refresh_calls = []
        self.read_calls = []

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _sentinel_item(
        item_id, item_type, tmdb_id, season_number=None, episode_number=None
    ):
        item = {
            "Id": item_id,
            "Type": item_type,
            "Name": f"SENTINEL-{item_type}-NAME",
            "Overview": "",
            "PremiereDate": "2040-04-04",
            "CommunityRating": 9.9,
            "OfficialRating": "SENTINEL-RATING",
            "Genres": ["SENTINEL-GENRE"],
            "Studios": [{"Name": "SENTINEL-STUDIO"}],
            "Path": f"/STRM/sentinel/{item_id}.strm",
            "ImageTags": {"Primary": "SENTINEL-IMAGE"},
            "BackdropImageTags": ["SENTINEL-BACKDROP"],
            "LockedFields": [
                "Name",
                "PremiereDate",
                "CommunityRating",
                "OfficialRating",
                "Genres",
                "Studios",
            ],
            "LockData": False,
            "ProviderIds": {"Tmdb": tmdb_id} if tmdb_id else {},
        }
        if item_type in {"Series", "Movie"}:
            item["OriginalTitle"] = f"SENTINEL-{item_type}-ORIGINAL"
        if item_type == "Series":
            item["RecursiveItemCount"] = 999
        if item_type in {"Season", "Episode"}:
            item["SeriesId"] = "series"
        if item_type == "Season":
            item["IndexNumber"] = season_number
        if item_type == "Episode":
            item["ParentIndexNumber"] = season_number
            item["IndexNumber"] = episode_number
        return item

    @staticmethod
    def _fixture(name):
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    def _write_fixture(self, tree, relative, fixture_name, *, empty_field=None):
        value = self._fixture(fixture_name)
        if tree == "override" and fixture_name == "series.json":
            # EVH may persist this optional field even though the observed
            # Shenyi Series cache omits it; keep it as a non-target sentinel.
            value["original_language"] = "zz"
        if empty_field == "overview":
            value["overview"] = ""
        elif empty_field == "release_date":
            value["air_date"] = ""
        path = self.root / tree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path, value

    @staticmethod
    def _db_sentinel(item_type):
        row = {
            "title": "SENTINEL-DB-TITLE",
            "overview": "",
            "release_date": "2040-04-04",
            "release_year": 2040,
            "rating": 9.9,
        }
        if item_type in {"Movie", "Series"}:
            row.update(
                {
                    "original_title": "SENTINEL-DB-ORIGINAL",
                    "genres_json": [{"id": 999, "name": "SENTINEL"}],
                    "production_companies_json": [
                        {"id": 999, "name": "SENTINEL"}
                    ],
                    "countries_json": ["ZZ"],
                    "original_language": "zz",
                    "official_rating_json": {"US": "SENTINEL-RATING"},
                }
            )
        if item_type == "Series":
            row["networks_json"] = [{"id": 999, "name": "SENTINEL"}]
            row["total_episodes"] = 999
        return row

    def _service(self):
        def get_item(item_id, *_):
            self.read_calls.append(item_id)
            return self.items.get(item_id)

        def fill_db(tmdb_id, item_type, values):
            row = self.db_rows[(tmdb_id, item_type)]
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
                            if key in {"rating", "release_year", "total_episodes"}
                            else ("json" if key.endswith("_json") else "text")
                        )
                    )
                )
                if is_missing(row.get(key), semantic=semantic):
                    row[key] = copy.deepcopy(value)
                    changed.append(key)
            return changed

        def refresh(item_id, *_):
            self.refresh_calls.append(item_id)
            item = self.items[item_id]
            item_type = item["Type"]
            if item_type == "Movie":
                relative = "tmdb-movies2/707/all.json"
            elif item_type == "Series":
                relative = "tmdb-tv/808/series.json"
            elif item_type == "Season":
                relative = f"tmdb-tv/808/season-{item['IndexNumber']}.json"
            else:
                relative = (
                    "tmdb-tv/808/"
                    f"season-{item['ParentIndexNumber']}"
                    f"-episode-{item['IndexNumber']}.json"
                )
            override = json.loads(
                (self.root / "override" / relative).read_text(encoding="utf-8")
            )
            if item_type == "Season":
                item["PremiereDate"] = override["air_date"]
            else:
                item["Overview"] = override["overview"]
            return True

        return ShenyiMetadataBackfillService(
            str(self.root),
            "http://emby",
            "redacted",
            "user",
            get_item=get_item,
            get_db=lambda tmdb_id, item_type: copy.deepcopy(
                self.db_rows.get((tmdb_id, item_type), {})
            ),
            fill_db=fill_db,
            get_tmdb_by_emby=lambda item_id: "808" if item_id == "series" else None,
            refresh=refresh,
            provider_settle_delay=0,
            verification_delays=(0,),
        )

    def test_full_refresh_changes_only_one_target_field_for_all_item_types(self):
        service = self._service()
        for (
            item_id,
            item_type,
            fixture_name,
            root_tmdb,
            season,
            episode,
            target_field,
        ) in self.CASES:
            with self.subTest(item_type=item_type, season=season):
                relative = ShenyiStore.relative_path(
                    item_type, root_tmdb, season, episode
                )
                self._write_fixture("cache", relative, fixture_name)
                override_path, override_before = self._write_fixture(
                    "override", relative, fixture_name, empty_field=target_field
                )
                item_tmdb = str(self._fixture(fixture_name)["id"])
                self.db_rows[(item_tmdb, item_type)] = self._db_sentinel(item_type)
                if target_field == "release_date":
                    self.db_rows[(item_tmdb, item_type)]["overview"] = (
                        f"SENTINEL-{item_id}-DB-OVERVIEW"
                    )
                    self.db_rows[(item_tmdb, item_type)]["release_date"] = "0001-01-01"
                item_before = copy.deepcopy(self.items[item_id])
                db_before = copy.deepcopy(self.db_rows[(item_tmdb, item_type)])

                result = service.execute(item_id)

                self.assertEqual("provider_confirmed", result["status"])
                self.assertEqual([target_field], result["database_fields"])
                emby_target = (
                    "PremiereDate" if target_field == "release_date" else "Overview"
                )
                source_target = (
                    "air_date" if target_field == "release_date" else "overview"
                )
                self.assertEqual([source_target], result["file_fields"])
                self.assertEqual(
                    self._fixture(fixture_name)[source_target],
                    self.items[item_id][emby_target],
                )
                for key, value in item_before.items():
                    if key != emby_target:
                        self.assertEqual(value, self.items[item_id][key], key)
                db_after = self.db_rows[(item_tmdb, item_type)]
                for key, value in db_before.items():
                    if key != target_field:
                        self.assertEqual(value, db_after[key], key)
                override_after = json.loads(
                    override_path.read_text(encoding="utf-8")
                )
                for key, value in override_before.items():
                    if key != source_target:
                        self.assertEqual(value, override_after[key], key)

        self.assertEqual(
            ["movie", "series", "season0", "season1", "episode"],
            self.refresh_calls,
        )
        self.assertNotIn("MediaSources", json.dumps(self.read_calls))

    def test_real_shape_cache_fixtures_create_complete_overrides(self):
        store = ShenyiStore(str(self.root))
        for (
            _item_id,
            item_type,
            fixture_name,
            root_tmdb,
            season,
            episode,
            _target_field,
        ) in self.CASES:
            with self.subTest(item_type=item_type, season=season):
                relative = ShenyiStore.relative_path(
                    item_type, root_tmdb, season, episode
                )
                cache_path, fixture = self._write_fixture(
                    "cache", relative, fixture_name
                )
                location = store.locate(item_type, root_tmdb, season, episode)

                changed = store.merge_override(
                    location,
                    item_type,
                    {"overview": fixture["overview"]},
                    None,
                )

                self.assertIn("__created__", changed)
                self.assertEqual(
                    json.loads(cache_path.read_text(encoding="utf-8")),
                    json.loads(location.override_path.read_text(encoding="utf-8")),
                )

    def test_backfill_dependency_graph_cannot_trigger_forbidden_subsystems(self):
        tree = ast.parse(
            Path(shenyi_metadata_backfill.__file__).read_text(encoding="utf-8")
        )
        imported = set()
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    called.add(node.func.attr)

        self.assertFalse(
            imported
            & {
                "handler.tmdb",
                "handler.douban",
                "handler.p115_service",
                "subprocess",
                "core_processor",
            }
        )
        self.assertFalse(
            called
            & {
                "trigger_media_info_refresh",
                "update_emby_item_details",
                "get_emby_item_people_details",
                "upload_item_primary_image_from_url",
                "sync_to_moviepilot",
                "ffprobe",
                "dffmpeg",
            }
        )


if __name__ == "__main__":
    unittest.main()
