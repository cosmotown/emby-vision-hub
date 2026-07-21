import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any


class DockerImageMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.config_source = (cls.repo_root / "config_manager.py").read_text()
        cls.config_tree = ast.parse(cls.config_source)
        cls.functions = {
            node.name: node
            for node in cls.config_tree.body
            if isinstance(node, ast.FunctionDef)
        }

        normalize_node = cls.functions["_normalize_docker_image_name"]
        namespace = {
            "Any": Any,
            "constants": SimpleNamespace(
                DEFAULT_DOCKER_IMAGE_NAME="tzyzero186/emby-vision-hub:latest",
                LEGACY_DOCKER_IMAGE_NAMES=frozenset({
                    "hbq0405/emby-toolkit:latest",
                    "hbq0405/emby-toolkit",
                    "tzyzero186/emby-toolkit:latest",
                    "tzyzero186/emby-toolkit",
                }),
            ),
        }
        exec(
            compile(
                ast.Module(body=[normalize_node], type_ignores=[]),
                "config_manager.py",
                "exec",
            ),
            namespace,
        )
        cls.normalize = staticmethod(namespace["_normalize_docker_image_name"])

    def test_known_legacy_images_migrate_to_evh(self):
        expected = "tzyzero186/emby-vision-hub:latest"
        for legacy in (
            "hbq0405/emby-toolkit:latest",
            "hbq0405/emby-toolkit",
            "tzyzero186/emby-toolkit:latest",
            "tzyzero186/emby-toolkit",
        ):
            with self.subTest(legacy=legacy):
                self.assertEqual(self.normalize(legacy), expected)

    def test_current_and_unknown_images_are_not_rewritten(self):
        self.assertEqual(
            self.normalize("tzyzero186/emby-vision-hub:latest"),
            "tzyzero186/emby-vision-hub:latest",
        )
        self.assertEqual(
            self.normalize("example/custom-image:latest"),
            "example/custom-image:latest",
        )

    def test_load_save_and_getter_all_use_normalization(self):
        load_source = ast.unparse(self.functions["load_config"])
        save_source = ast.unparse(self.functions["save_config"])
        get_source = ast.unparse(self.functions["get_docker_image_name"])

        self.assertIn("_normalize_docker_image_name(stored_docker_image)", load_source)
        self.assertIn(
            "settings_db.save_setting('dynamic_app_config', dynamic_config_from_db)",
            load_source,
        )
        self.assertIn("_normalize_docker_image_name", save_source)
        self.assertIn("_normalize_docker_image_name", get_source)


if __name__ == "__main__":
    unittest.main()
