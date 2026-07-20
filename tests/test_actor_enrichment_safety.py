import unittest
from pathlib import Path

from services.actor_enrichment_safety import (
    apply_safe_actor_name_translations,
    deduplicate_cast_by_identity,
    filter_unsafe_new_cast,
    is_safe_actor_name_translation,
)


class ActorEnrichmentSafetyTests(unittest.TestCase):
    def test_actor_pipeline_uses_safe_upstream_guards(self):
        source = (Path(__file__).resolve().parents[1] / 'core_processor.py').read_text()

        self.assertNotIn('_update_emby_person_names_from_final_cast', source)
        self.assertIn('current_cast_list = actor_utils.filter_unsafe_new_cast', source)
        self.assertIn('current_cast_list = actor_utils.deduplicate_cast_by_identity', source)
        self.assertIn('rejected_name_translations = actor_utils.apply_safe_actor_name_translations', source)
        self.assertNotIn('entry.get("tmdb_person_id") and entry.get("emby_person_id")', source)

    def test_new_actor_requires_tmdb_identity_and_profile(self):
        cast = [
            {'id': '1', 'name': 'Existing', 'emby_person_id': '10'},
            {'id': '2', 'name': 'New With Image', 'profile_path': '/two.jpg'},
            {'id': '3', 'name': 'New Without Image'},
            {'id': '4', 'name': 'Mapped Elsewhere Without Image', 'emby_person_id': '40'},
            {'name': 'Missing TMDb', 'profile_path': '/bad.jpg'},
        ]

        filtered = filter_unsafe_new_cast(cast, original_emby_person_ids={'10'})

        self.assertEqual([actor['name'] for actor in filtered], ['Existing', 'New With Image'])

    def test_cast_is_deduplicated_by_tmdb_and_emby_identity(self):
        cast = [
            {'id': '1', 'name': 'First', 'emby_person_id': '10'},
            {'id': '1', 'name': 'Duplicate TMDb', 'profile_path': '/dup.jpg'},
            {'id': '2', 'name': 'Duplicate Emby', 'emby_person_id': '10'},
            {'id': '3', 'name': 'Unique', 'profile_path': '/three.jpg'},
        ]

        deduplicated = deduplicate_cast_by_identity(cast)

        self.assertEqual([actor['name'] for actor in deduplicated], ['First', 'Unique'])

    def test_translation_cannot_add_numbering_or_name_collisions(self):
        cast = [
            {'id': '1', 'name': 'Megumi'},
            {'id': '2', 'name': 'Yuka'},
            {'id': '3', 'name': 'Umeda'},
        ]

        rejected = apply_safe_actor_name_translations(
            cast,
            {
                'Megumi': '1①めぐみ',
                'Yuka': '梅田',
                'Umeda': '梅田',
                'Invented': '凭空新增',
            },
        )

        self.assertEqual([actor['name'] for actor in cast], ['Megumi', 'Yuka', 'Umeda'])
        self.assertEqual(len(cast), 3)
        self.assertEqual(rejected, ['Megumi', 'Umeda', 'Yuka'])
        self.assertFalse(is_safe_actor_name_translation('Megumi', '1①めぐみ'))
        self.assertTrue(is_safe_actor_name_translation('2Pac', '2帕克'))


if __name__ == '__main__':
    unittest.main()
