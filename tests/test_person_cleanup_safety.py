import unittest
import ast
from pathlib import Path

from services.person_cleanup_safety import (
    build_identity_provider_pairs,
    classify_reference_check,
    find_ghost_candidates,
    normalize_person_name,
    person_name_protection_keys,
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

    def test_protected_person_name_excludes_duplicate_emby_person_ids(self):
        people = [
            {'Id': '1', 'Name': '普通幽灵人物'},
            {'Id': '2', 'Name': ' 保护库 人物 '},
            {'Id': '3', 'Name': '保护库 人物'},
        ]

        candidates = find_ghost_candidates(
            people,
            referenced_person_ids={'2'},
            protected_person_names={'保护库  人物'},
        )

        self.assertEqual([person['Id'] for person in candidates], ['1'])
        self.assertEqual(normalize_person_name('  Alice  SMITH '), 'alice smith')

    def test_protected_person_name_excludes_numbered_translation_duplicates(self):
        people = [
            {'Id': '1', 'Name': '1①めぐみ'},
            {'Id': '2', 'Name': '2梅田'},
            {'Id': '3', 'Name': '2Pac'},
        ]

        candidates = find_ghost_candidates(
            people,
            referenced_person_ids=set(),
            protected_person_names={'めぐみ', '梅田'},
        )

        self.assertEqual([person['Id'] for person in candidates], ['3'])
        self.assertEqual(person_name_protection_keys('3②みさ'), {'3②みさ', 'みさ'})

    def test_only_explicit_zero_reference_count_is_deletable(self):
        self.assertEqual(classify_reference_check({'count': 0, 'items': []}), 'orphan')
        self.assertEqual(classify_reference_check({'count': 1, 'items': [{}]}), 'linked')
        self.assertEqual(classify_reference_check(None), 'verification_failed')
        self.assertEqual(classify_reference_check({}), 'verification_failed')
        self.assertEqual(classify_reference_check({'count': '0'}), 'verification_failed')
        self.assertEqual(classify_reference_check({'count': -1}), 'verification_failed')
        self.assertEqual(classify_reference_check({'count': False}), 'verification_failed')

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
        self.assertNotIn('is_verified_orphan_candidate', delete_source)
        actor_source = (repo_root / 'tasks' / 'actors.py').read_text()
        self.assertIn('get_person_media_references', actor_source)
        self.assertIn("reference_status == 'verification_failed'", actor_source)
        self.assertIn("reference_status == 'linked'", actor_source)

    def test_protected_library_snapshots_are_merged_not_replaced(self):
        repo_root = Path(__file__).resolve().parents[1]
        db_tree = ast.parse((repo_root / 'database' / 'person_cleanup_db.py').read_text())
        functions = {
            node.name: node
            for node in db_tree.body
            if isinstance(node, ast.FunctionDef)
        }
        merge_source = ast.unparse(functions['merge_protected_people_for_library'])
        replace_candidates_source = ast.unparse(functions['replace_candidates'])
        task_source = (repo_root / 'tasks' / 'actors.py').read_text()
        schema_source = (repo_root / 'database' / 'connection.py').read_text()

        self.assertNotIn('DELETE FROM person_cleanup_protected_people', merge_source)
        self.assertIn('_exclude_protected_candidates', replace_candidates_source)
        self.assertIn('ON CONFLICT', merge_source)
        self.assertIn('merge_protected_people_for_library', task_source)
        self.assertIn('get_protected_person_ids', task_source)
        self.assertIn('get_protected_person_names', task_source)
        self.assertIn('capture_library_ids=protected_library_ids', task_source)
        self.assertIn('include_protected=True', task_source)
        self.assertIn('person_id in protected_person_ids', task_source)
        self.assertIn('person_name_protection_keys(person_name)', task_source)
        self.assertIn('person_cleanup_protected_libraries', schema_source)
        self.assertIn('person_cleanup_protected_people', schema_source)


if __name__ == '__main__':
    unittest.main()
