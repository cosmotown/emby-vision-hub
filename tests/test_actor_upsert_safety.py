import ast
import unittest
from pathlib import Path


class ActorUpsertSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.actor_source = (cls.repo_root / "database" / "actor_db.py").read_text()
        cls.actor_tree = ast.parse(cls.actor_source)
        cls.actor_class = next(
            node
            for node in cls.actor_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ActorDBManager"
        )
        cls.methods = {
            node.name: node
            for node in cls.actor_class.body
            if isinstance(node, ast.FunctionDef)
        }

    def test_provider_ids_use_batch_emby_fetch(self):
        source = ast.unparse(self.methods["enrich_actors_with_provider_ids"])
        self.assertIn("get_emby_items_by_id", source)
        self.assertIn("item_ids=ids_to_fetch_from_api", source)
        self.assertNotIn("for person_id in ids_to_fetch_from_api", source)
        self.assertNotIn("get_emby_item_details", source)
        self.assertIn("if provider_ids and emby_id in enriched_actors_map", source)

    def test_each_actor_write_is_isolated_by_savepoint(self):
        wrapper = ast.unparse(self.methods["upsert_person"])
        inner = ast.unparse(self.methods["_upsert_person_without_savepoint"])
        self.assertIn("SAVEPOINT", wrapper)
        self.assertIn("ROLLBACK TO SAVEPOINT", wrapper)
        self.assertIn("RELEASE SAVEPOINT", wrapper)
        self.assertIn("_upsert_person_without_savepoint", wrapper)
        self.assertNotIn(".rollback()", inner)
        self.assertNotIn("conn.rollback()", self.actor_source)

    def test_core_processor_does_not_rollback_the_whole_actor_batch(self):
        core_source = (self.repo_root / "core_processor.py").read_text()
        self.assertNotIn("cursor.connection.rollback()", core_source)
        self.assertIn("upsert_person 内部使用 SAVEPOINT", core_source)


if __name__ == "__main__":
    unittest.main()
