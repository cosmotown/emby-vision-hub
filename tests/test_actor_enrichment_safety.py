import unittest
from pathlib import Path

from services.actor_enrichment_safety import (
    apply_douban_avatar_fallbacks,
    apply_safe_actor_name_translations,
    build_douban_identity_profile,
    build_tmdb_identity_profile,
    deduplicate_cast_by_identity,
    filter_unsafe_new_cast,
    is_safe_actor_name_translation,
    resolve_douban_actor_against_tmdb_cast,
    select_cast_by_source_order,
)


class ActorEnrichmentSafetyTests(unittest.TestCase):
    def test_actor_pipeline_uses_safe_upstream_guards(self):
        source = (Path(__file__).resolve().parents[1] / 'core_processor.py').read_text()

        self.assertNotIn('_update_emby_person_names_from_final_cast', source)
        self.assertIn('current_cast_list = actor_utils.filter_unsafe_new_cast', source)
        self.assertIn('current_cast_list = actor_utils.deduplicate_cast_by_identity', source)
        self.assertIn('actor_utils.apply_douban_avatar_fallbacks', source)
        self.assertIn('rejected_name_translations = actor_utils.apply_safe_actor_name_translations', source)
        self.assertNotIn('entry.get("tmdb_person_id") and entry.get("emby_person_id")', source)
        self.assertIn('仅按外部身份对号入座，姓名只用于展示', source)
        self.assertNotIn('删除同名异人演员', source)
        self.assertNotIn('douban_name_zh', source)
        self.assertNotIn(
            'get_translation_from_db(cursor, emby_actor.get("Name"), by_translated_text=True)',
            source,
        )
        self.assertIn('f"douban:{douban_id}"', source)
        self.assertIn('_sync_douban_only_cast_to_emby', source)

    def test_new_actor_requires_stable_identity_and_profile(self):
        cast = [
            {'id': '1', 'name': 'Existing', 'emby_person_id': '10'},
            {'id': '2', 'name': 'New With Image', 'profile_path': '/two.jpg'},
            {'id': '3', 'name': 'New Without Image'},
            {'id': '4', 'name': 'Mapped Elsewhere Without Image', 'emby_person_id': '40'},
            {'douban_id': '101', 'name': 'Douban Only', 'profile_path': 'https://img1.doubanio.com/p101.jpg'},
            {'douban_id': '102', 'name': 'Douban Without Image'},
            {'name': 'Missing TMDb', 'profile_path': '/bad.jpg'},
        ]

        filtered = filter_unsafe_new_cast(cast, original_emby_person_ids={'10'})

        self.assertEqual(
            [actor['name'] for actor in filtered],
            ['Existing', 'New With Image', 'Douban Only'],
        )

    def test_matched_douban_avatar_can_fill_missing_tmdb_profile(self):
        cast, adopted_count = apply_douban_avatar_fallbacks([
            {
                'id': '2',
                'name': '豆瓣演员',
                'douban_avatar_url': 'http://img9.doubanio.com/view/celebrity/l/public/p2.jpg',
            },
            {
                'id': '3',
                'name': 'TMDb Has Priority',
                'profile_path': '/three.jpg',
                'douban_avatar_url': 'https://img1.doubanio.com/view/celebrity/l/public/p3.jpg',
            },
        ])

        self.assertEqual(adopted_count, 1)
        self.assertEqual(
            cast[0]['profile_path'],
            'https://img9.doubanio.com/view/celebrity/l/public/p2.jpg',
        )
        self.assertEqual(cast[1]['profile_path'], '/three.jpg')
        self.assertNotIn('douban_avatar_url', cast[0])
        self.assertEqual(
            [actor['name'] for actor in filter_unsafe_new_cast(cast)],
            ['豆瓣演员', 'TMDb Has Priority'],
        )

    def test_untrusted_douban_avatar_does_not_bypass_profile_gate(self):
        cast, adopted_count = apply_douban_avatar_fallbacks([
            {
                'id': '2',
                'name': 'Unsafe Avatar',
                'douban_avatar_url': 'https://example.invalid/avatar.jpg',
            },
        ])

        self.assertEqual(adopted_count, 0)
        self.assertEqual(filter_unsafe_new_cast(cast), [])

    def test_cast_is_deduplicated_by_tmdb_and_emby_identity(self):
        cast = [
            {'id': '1', 'name': 'First', 'emby_person_id': '10'},
            {'id': '1', 'name': 'Duplicate TMDb', 'profile_path': '/dup.jpg'},
            {'id': '2', 'name': 'Duplicate Emby', 'emby_person_id': '10'},
            {'id': '3', 'name': 'Unique', 'profile_path': '/three.jpg'},
        ]

        deduplicated = deduplicate_cast_by_identity(cast)

        self.assertEqual([actor['name'] for actor in deduplicated], ['First', 'Unique'])

    def test_same_name_different_tmdb_people_are_not_deduplicated(self):
        cast = [
            {'id': '1', 'name': '王伟', 'douban_id': '101', 'profile_path': '/one.jpg'},
            {'id': '2', 'name': '王伟', 'douban_id': '102', 'profile_path': '/two.jpg'},
        ]

        deduplicated = deduplicate_cast_by_identity(cast)

        self.assertEqual([actor['id'] for actor in deduplicated], ['1', '2'])

    def test_duplicate_douban_identity_is_deduplicated(self):
        cast = [
            {'id': '1', 'name': 'First', 'douban_id': '101', 'profile_path': '/one.jpg'},
            {'id': '2', 'name': 'Duplicate Douban', 'douban_id': '101', 'profile_path': '/two.jpg'},
        ]

        deduplicated = deduplicate_cast_by_identity(cast)

        self.assertEqual([actor['id'] for actor in deduplicated], ['1'])

    def test_douban_only_people_are_deduplicated_without_fake_tmdb_id(self):
        cast = [
            {'id': None, 'douban_id': '201', 'name': '豆瓣独有演员', 'profile_path': '/one.jpg'},
            {'id': None, 'douban_id': '201', 'name': '重复记录', 'profile_path': '/two.jpg'},
            {'id': None, 'douban_id': '202', 'name': '另一演员', 'profile_path': '/three.jpg'},
        ]

        deduplicated = deduplicate_cast_by_identity(cast)

        self.assertEqual([actor['douban_id'] for actor in deduplicated], ['201', '202'])
        self.assertTrue(all(actor.get('id') is None for actor in deduplicated))

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

    def test_translation_changes_display_name_but_never_identity(self):
        cast = [{
            'id': None,
            'douban_id': '301',
            'emby_person_id': 'person-301',
            'name': 'Zhang San',
        }]

        rejected = apply_safe_actor_name_translations(cast, {'Zhang San': '张三'})

        self.assertEqual(rejected, [])
        self.assertEqual(cast[0]['name'], '张三')
        self.assertIsNone(cast[0]['id'])
        self.assertEqual(cast[0]['douban_id'], '301')
        self.assertEqual(cast[0]['emby_person_id'], 'person-301')

    def test_actor_limit_is_applied_after_source_order_not_identity_phase(self):
        cast = [
            {'name': 'Late IMDb match', 'order': 40, 'profile_path': '/late.jpg'},
            {'name': 'Douban-only lead', 'order': 1, 'profile_path': '/lead.jpg'},
            {'name': 'TMDb lead', 'order': 0, 'profile_path': '/tmdb.jpg'},
            {'name': 'Supporting actor', 'order': 8, 'profile_path': '/support.jpg'},
        ]

        selected = select_cast_by_source_order(cast, 3)

        self.assertEqual(
            [actor['name'] for actor in selected],
            ['TMDb lead', 'Douban-only lead', 'Supporting actor'],
        )

    def test_name_is_only_candidate_retrieval_and_birthday_confirms_identity(self):
        douban_profile = build_douban_identity_profile(
            {'DoubanCelebrityId': '101', 'Name': '赵丽颖'},
            {
                'latin_title': 'Zanilia Zhao',
                'extra': {'info': [['出生日期', '1987-10-16']]},
            },
        )
        tmdb_profile = build_tmdb_identity_profile(
            {'id': 123, 'name': 'Zhao Liying'},
            {
                'birthday': '1987-10-16',
                'also_known_as': ['赵丽颖', 'Zanilia Zhao'],
            },
        )

        resolution = resolve_douban_actor_against_tmdb_cast(
            douban_profile,
            [tmdb_profile],
        )

        self.assertEqual(resolution['status'], 'confirmed')
        self.assertEqual(resolution['tmdb_id'], '123')

    def test_matching_name_without_birthday_is_deferred(self):
        resolution = resolve_douban_actor_against_tmdb_cast(
            {'aliases': {'sameactor'}, 'birthday': ''},
            [{'tmdb_id': '1', 'aliases': {'sameactor'}, 'birthday': '1990-01-01'}],
        )

        self.assertEqual(resolution['status'], 'ambiguous')
        self.assertIsNone(resolution['tmdb_id'])

    def test_no_name_or_alias_candidate_can_remain_douban_only(self):
        resolution = resolve_douban_actor_against_tmdb_cast(
            {'aliases': {'豆瓣独有演员'}, 'birthday': ''},
            [{'tmdb_id': '1', 'aliases': {'另一个演员'}, 'birthday': ''}],
        )

        self.assertEqual(resolution['status'], 'no_candidate')

    def test_multiple_birthday_matches_are_deferred(self):
        resolution = resolve_douban_actor_against_tmdb_cast(
            {'aliases': {'同名演员'}, 'birthday': '1990-01-01'},
            [
                {'tmdb_id': '1', 'aliases': {'同名演员'}, 'birthday': '1990-01-01'},
                {'tmdb_id': '2', 'aliases': {'同名演员'}, 'birthday': '1990-01-01'},
            ],
        )

        self.assertEqual(resolution['status'], 'ambiguous')
        self.assertIsNone(resolution['tmdb_id'])


if __name__ == '__main__':
    unittest.main()
