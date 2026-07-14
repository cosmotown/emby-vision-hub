import unittest
import ast
from pathlib import Path

from services.person_cleanup_safety import (
    build_identity_provider_pairs,
    classify_reference_check,
    find_ghost_candidates,
    is_verified_orphan_candidate,
)


class PersonCleanupSafetyTests(unittest.TestCase):
    def test_candidate_scan_excludes_every_referenced_person(self):
        people = [
            {'Id': '1', 'Name': '在用人物'},
            {'Id': '2', 'Name': '候选人物'},
            {'Name': '无 ID 人物'},
        ]

        candidates = find_ghost_candidates(people, {'1'})

        self.assertEqual([person['Id'] for person in candidates], ['2'])

    def test_protected_person_without_provider_ids_is_never_a_candidate(self):
        people = [
            {'Id': '1', 'Name': '普通幽灵人物', 'ProviderIds': {}},
            {'Id': '2', 'Name': '保护库人物', 'ProviderIds': {}},
        ]

        candidates = find_ghost_candidates(people, set() | {'2'})

        self.assertEqual([person['Id'] for person in candidates], ['1'])

    def test_only_explicit_zero_reference_count_is_deletable(self):
        self.assertEqual(classify_reference_check({'count': 0, 'items': []}), 'orphan')
        self.assertEqual(classify_reference_check({'count': 1, 'items': [{}]}), 'linked')
        self.assertEqual(classify_reference_check(None), 'verification_failed')
        self.assertEqual(classify_reference_check({}), 'verification_failed')
        self.assertEqual(classify_reference_check({'count': '0'}), 'verification_failed')
        self.assertEqual(classify_reference_check({'count': -1}), 'verification_failed')
        self.assertEqual(classify_reference_check({'count': False}), 'verification_failed')

    def test_candidate_requires_successful_manual_verification(self):
        self.assertTrue(is_verified_orphan_candidate({
            'last_checked_at': '2026-07-14T10:00:00+08:00',
            'last_error': None,
        }))
        self.assertFalse(is_verified_orphan_candidate({'last_checked_at': None, 'last_error': None}))
        self.assertFalse(is_verified_orphan_candidate({
            'last_checked_at': '2026-07-14T10:00:00+08:00',
            'last_error': '核对失败',
        }))
        self.assertFalse(is_verified_orphan_candidate(None))

    def test_identity_comparison_uses_only_exact_supported_provider_ids(self):
        self.assertEqual(
            build_identity_provider_pairs({
                'Tmdb': '12345',
                'Imdb': 'nm0012345',
                'Douban': '67890',
                'Bad': 'value,other',
            }),
            ['imdb.nm0012345', 'tmdb.12345'],
        )
        self.assertEqual(build_identity_provider_pairs('{"Tmdb": "12345"}'), ['tmdb.12345'])
        self.assertEqual(build_identity_provider_pairs(None), [])

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

    def test_manual_verification_never_calls_person_delete_api(self):
        repo_root = Path(__file__).resolve().parents[1]
        route_tree = ast.parse((repo_root / 'routes' / 'person_cleanup.py').read_text())
        functions = {
            node.name: node
            for node in route_tree.body
            if isinstance(node, ast.FunctionDef)
        }
        verify_source = ast.unparse(functions['verify_person_cleanup_candidate'])
        delete_source = ast.unparse(functions['delete_person_cleanup_candidates'])

        self.assertIn('get_person_media_references', verify_source)
        self.assertIn('remove_candidate', verify_source)
        self.assertNotIn('delete_person_custom_api', verify_source)
        self.assertIn('is_verified_orphan_candidate', delete_source)

    def test_protected_library_snapshots_are_merged_not_replaced(self):
        repo_root = Path(__file__).resolve().parents[1]
        db_tree = ast.parse((repo_root / 'database' / 'person_cleanup_db.py').read_text())
        functions = {
            node.name: node
            for node in db_tree.body
            if isinstance(node, ast.FunctionDef)
        }
        merge_source = ast.unparse(functions['merge_protected_people_for_library'])
        task_source = (repo_root / 'tasks' / 'actors.py').read_text()
        schema_source = (repo_root / 'database' / 'connection.py').read_text()

        self.assertNotIn('DELETE FROM person_cleanup_protected_people', merge_source)
        self.assertIn('ON CONFLICT', merge_source)
        self.assertIn('merge_protected_people_for_library', task_source)
        self.assertIn('get_protected_person_ids', task_source)
        self.assertIn('capture_library_ids=protected_library_ids', task_source)
        self.assertIn('person_cleanup_protected_libraries', schema_source)
        self.assertIn('person_cleanup_protected_people', schema_source)


if __name__ == '__main__':
    unittest.main()
