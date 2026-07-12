import unittest
import ast
from pathlib import Path

from services.person_cleanup_safety import classify_reference_check, find_ghost_candidates


class PersonCleanupSafetyTests(unittest.TestCase):
    def test_candidate_scan_excludes_every_referenced_person(self):
        people = [
            {'Id': '1', 'Name': '在用人物'},
            {'Id': '2', 'Name': '候选人物'},
            {'Name': '无 ID 人物'},
        ]

        candidates = find_ghost_candidates(people, {'1'})

        self.assertEqual([person['Id'] for person in candidates], ['2'])

    def test_only_explicit_zero_reference_count_is_deletable(self):
        self.assertEqual(classify_reference_check({'count': 0, 'items': []}), 'orphan')
        self.assertEqual(classify_reference_check({'count': 1, 'items': [{}]}), 'linked')
        self.assertEqual(classify_reference_check(None), 'verification_failed')
        self.assertEqual(classify_reference_check({}), 'verification_failed')
        self.assertEqual(classify_reference_check({'count': '0'}), 'verification_failed')
        self.assertEqual(classify_reference_check({'count': -1}), 'verification_failed')
        self.assertEqual(classify_reference_check({'count': False}), 'verification_failed')

    def test_legacy_destructive_tasks_are_disabled_and_unregistered(self):
        repo_root = Path(__file__).resolve().parents[1]
        actor_tree = ast.parse((repo_root / 'tasks' / 'actors.py').read_text())
        functions = {
            node.name: node
            for node in actor_tree.body
            if isinstance(node, ast.FunctionDef)
        }
        for function_name in (
            '_disabled_legacy_task_purge_ghost_actors',
            '_disabled_legacy_task_purge_unregistered_actors',
        ):
            self.assertIsInstance(functions[function_name].body[0], ast.Raise)

        registry_source = (repo_root / 'tasks' / 'core.py').read_text()
        self.assertNotIn("'purge-ghost-actors':", registry_source)
        self.assertNotIn("'purge-unregistered-actors':", registry_source)


if __name__ == '__main__':
    unittest.main()
