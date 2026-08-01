import ast
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class MediaInfoSafetyContractTests(unittest.TestCase):
    def test_new_backend_never_references_unsafe_refresh_or_playback_paths(self):
        files = [
            ROOT / "services" / "mediainfo_state.py",
            ROOT / "services" / "shenyi_mediainfo.py",
            ROOT / "services" / "mediainfo_repair_queue.py",
            ROOT / "routes" / "media_info.py",
        ]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in files)
        self.assertNotIn("PlaybackInfo", combined)
        self.assertNotIn("/Refresh", combined)
        self.assertNotIn("Library/Media/Updated", combined)
        self.assertNotIn("ffprobe", combined.lower())
        self.assertNotIn("dffmpeg", combined.lower())
        self.assertNotIn("moviepilot", combined.lower())

    def test_adapter_has_only_one_post_call_site(self):
        path = ROOT / "services" / "shenyi_mediainfo.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        post_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_post"
        ]
        self.assertEqual(1, len(post_calls))

    def test_no_json_body_restore_mode_exists(self):
        source = (ROOT / "services" / "shenyi_mediainfo.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('data=b""', source)
        self.assertNotIn("json=", source)
        self.assertNotIn("MediaSourceInfo\":", source)

    def test_database_schema_has_no_secret_or_full_response_columns(self):
        source = (ROOT / "database" / "connection.py").read_text(
            encoding="utf-8"
        )
        table = source.split("CREATE TABLE IF NOT EXISTS media_info_repair_jobs", 1)[1]
        table = table.split('""")', 1)[0].lower()
        for forbidden in (
            "cookie",
            "token",
            "pickcode",
            "strm_url",
            "response_body",
            "mediainfo_json",
        ):
            self.assertNotIn(forbidden, table)

    def test_ui_warning_states_shenyi_ownership_and_scope(self):
        review = (ROOT / "emby-actor-ui" / "src" / "components" / "ReviewList.vue").read_text(
            encoding="utf-8"
        )
        self.assertIn("神医单 Item MediaInfo 接口", review)
        self.assertIn("不会刷新整季、整剧或媒体库", review)
        self.assertIn("EVH 不会自行执行 ffprobe", review)


if __name__ == "__main__":
    unittest.main()
