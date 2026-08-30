import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import requests

import config_manager  # Initialize settings before importing database.connection.
from database import person_cleanup_db
from handler import emby
from services.person_cleanup_safety import (
    candidate_fingerprint,
    canonical_person_provider_identities,
    is_explicit_verified_orphan,
)
from tasks import actors


class PersonCleanupV7217Tests(unittest.TestCase):
    def setUp(self):
        self.processor = SimpleNamespace(
            emby_url='http://emby',
            emby_api_key='secret-token',
            emby_user_id='user',
            is_stop_requested=lambda: False,
        )
        self.candidate = {
            'person_id': 'p1',
            'person_name': '人物甲',
            'provider_ids_json': {'Tmdb': '123'},
        }

    def test_only_explicit_current_orphan_is_deletable(self):
        fingerprint = candidate_fingerprint(self.candidate)
        explicit = {
            **self.candidate,
            'verification_status': 'orphan',
            'verification_snapshot_generation': 9,
            'verification_fingerprint': fingerprint,
            'last_error': None,
        }
        self.assertTrue(is_explicit_verified_orphan(explicit, 9))
        for status in (
            'unverified', 'linked', 'identity_alias_only',
            'people_unavailable', 'connection_failed', 'invalid_response',
        ):
            self.assertFalse(is_explicit_verified_orphan({**explicit, 'verification_status': status}, 9))
        self.assertFalse(is_explicit_verified_orphan({**explicit, 'verification_snapshot_generation': 8}, 9))
        self.assertFalse(is_explicit_verified_orphan({**explicit, 'verification_fingerprint': 'changed'}, 9))

    def test_canonical_provider_identity_is_exact_and_protection_only(self):
        self.assertEqual(
            canonical_person_provider_identities({
                'Tmdb': '00123', 'Imdb': 'NM0042', 'Douban': '0007', 'Other': '123',
            }, strict=True),
            {('tmdb', '123'), ('imdb', 'nm0042'), ('douban', '7')},
        )
        for malformed in (
            {'Tmdb': '12,13'}, {'Imdb': 'tt123'}, {'Douban': ['7']},
        ):
            with self.assertRaises(ValueError):
                canonical_person_provider_identities(malformed, strict=True)

    def test_protected_snapshot_rejects_missing_people_on_any_item(self):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            'Items': [{'Id': 'm1'}],
            'TotalRecordCount': 1,
        }
        with patch.object(actors.emby.emby_client, 'get', return_value=response), \
             patch.object(actors.emby, 'get_person_details_strict') as detail:
            with self.assertRaisesRegex(RuntimeError, 'People 不可核验'):
                actors._scan_protected_library_people(
                    self.processor,
                    [{'library_id': 'lib1', 'library_name': '保护库'}],
                )
        detail.assert_not_called()

    def test_protected_snapshot_rejects_incomplete_person_detail(self):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            'Items': [{'Id': 'm1', 'People': [{'Id': 'p1', 'Name': '人物甲'}]}],
            'TotalRecordCount': 1,
        }
        with patch.object(actors.emby.emby_client, 'get', return_value=response), \
             patch.object(actors.emby, 'get_person_details_strict', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'detail 读取不完整'):
                actors._scan_protected_library_people(
                    self.processor,
                    [{'library_id': 'lib1', 'library_name': '保护库'}],
                )

    def test_normal_library_reference_scan_rejects_person_without_exact_id(self):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            'Items': [{'Id': 'm1', 'People': [{'Name': '人物甲'}]}],
            'TotalRecordCount': 1,
        }
        with patch.object(emby.emby_client, 'get', return_value=response):
            result = emby.get_referenced_person_ids_strict(
                'http://emby', 'secret-token', ['lib-normal'],
            )
        self.assertIsNone(result)

    def test_preview_alias_only_is_never_a_delete_candidate(self):
        db = actors.person_cleanup_db
        with patch.object(db, 'get_protection_contract', return_value={
                'generation': 10, 'person_ids': set(), 'name_keys': set(),
                'provider_identities': set(),
             }), \
             patch.object(db, 'list_candidates_raw', return_value=[self.candidate]), \
             patch.object(db, 'initialize_cleanup_job_candidate_total') as initialize_total, \
             patch.object(db, 'candidate_protection_reason', return_value=None), \
             patch.object(db, 'mark_candidate_checked') as checked, \
             patch.object(db, 'add_cleanup_job_item') as add_item, \
             patch.object(db, 'finish_cleanup_preview'), \
             patch.object(db, 'cleanup_job_stop_requested', return_value=False), \
             patch.object(actors, '_refresh_protected_snapshot', return_value=(10, {})), \
             patch.object(actors, '_build_protected_root_contract', return_value={'complete': True}), \
             patch.object(actors.emby, 'get_person_media_references', return_value={
                 'status': 'identity_alias_only', 'count': 0, 'query_count': 1,
             }), \
             patch.object(actors.emby, 'delete_person_custom_api_outcome') as delete:
            actors.task_preview_safe_person_cleanup(self.processor, 'job1')

        initialize_total.assert_called_once_with('job1', 1)
        checked.assert_called_once_with(
            'p1', 'identity_alias_only', 10,
            '仅命中同身份其他 Person；不属于 verified orphan',
        )
        self.assertEqual(add_item.call_args.args[2], 'identity_alias_only')
        delete.assert_not_called()

    def test_execute_requires_persisted_attempt_before_delete_post(self):
        item = {**self.candidate, 'candidate_fingerprint': candidate_fingerprint(self.candidate), 'post_attempts': 0}
        db = actors.person_cleanup_db
        with patch.object(db, 'start_cleanup_job'), \
             patch.object(db, 'get_protection_contract', return_value={
                 'generation': 11, 'person_ids': set(), 'name_keys': set(),
                 'provider_identities': set(),
             }), \
             patch.object(db, 'list_cleanup_job_orphans', return_value=[item]), \
             patch.object(db, 'cleanup_job_stop_requested', return_value=False), \
             patch.object(db, 'get_candidates_by_ids', return_value=[self.candidate]), \
             patch.object(db, 'candidate_protection_reason', return_value=None), \
             patch.object(db, 'mark_cleanup_job_item', return_value=False) as mark_item, \
             patch.object(db, 'finish_cleanup_job'), \
             patch.object(actors, '_refresh_protected_snapshot', return_value=(11, {})), \
             patch.object(actors, '_build_protected_root_contract', return_value={'complete': True}), \
             patch.object(actors.emby, 'get_person_media_references', return_value={
                 'status': 'orphan', 'count': 0, 'items': [],
             }), \
             patch.object(actors.emby, 'delete_person_custom_api_outcome') as delete:
            actors.task_execute_safe_person_cleanup(self.processor, 'job1')

        self.assertTrue(mark_item.call_args_list[0].kwargs['submitted'])
        self.assertEqual(mark_item.call_args_list[1].args[2], 'delete_ambiguous')
        self.assertTrue(mark_item.call_args_list[1].kwargs['completed'])
        delete.assert_not_called()

    def test_execute_ambiguous_post_is_called_once_and_never_replayed(self):
        item = {**self.candidate, 'candidate_fingerprint': candidate_fingerprint(self.candidate), 'post_attempts': 0}
        db = actors.person_cleanup_db
        marks = []

        def mark(*args, **kwargs):
            marks.append((args, kwargs))
            return True

        with patch.object(db, 'start_cleanup_job'), \
             patch.object(db, 'get_protection_contract', return_value={
                 'generation': 12, 'person_ids': set(), 'name_keys': set(),
                 'provider_identities': set(),
             }), \
             patch.object(db, 'list_cleanup_job_orphans', return_value=[item]), \
             patch.object(db, 'cleanup_job_stop_requested', return_value=False), \
             patch.object(db, 'get_candidates_by_ids', return_value=[self.candidate]), \
             patch.object(db, 'candidate_protection_reason', return_value=None), \
             patch.object(db, 'mark_cleanup_job_item', side_effect=mark), \
             patch.object(db, 'finish_cleanup_job'), \
             patch.object(actors, '_refresh_protected_snapshot', return_value=(12, {})), \
             patch.object(actors, '_build_protected_root_contract', return_value={'complete': True}), \
             patch.object(actors.emby, 'get_person_media_references', return_value={
                 'status': 'orphan', 'count': 0, 'items': [],
             }), \
             patch.object(actors.emby, 'delete_person_custom_api_outcome', return_value='ambiguous') as delete:
            actors.task_execute_safe_person_cleanup(self.processor, 'job1')

        self.assertEqual(delete.call_count, 1)
        self.assertEqual(marks[0][0][2], 'deleting')
        self.assertTrue(marks[0][1]['submitted'])
        self.assertEqual(marks[1][0][2], 'delete_ambiguous')

    def test_delete_transport_is_single_post_and_classifies_5xx_ambiguous(self):
        response = MagicMock(status_code=500)
        with patch.object(emby, 'logger', MagicMock()), \
             patch.object(emby, 'wait_for_server_idle'), \
             patch.object(emby, 'get_admin_access_token', return_value=('admin-token', 'admin')), \
             patch.object(emby.emby_client, 'post_once', return_value=response) as post:
            outcome = emby.delete_person_custom_api_outcome(
                'http://emby', 'ordinary-token', 'p1',
            )
        self.assertEqual(outcome, 'ambiguous')
        post.assert_called_once()
        self.assertNotIn('admin-token', post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs['headers'], {'X-Emby-Token': 'admin-token'})

    def test_delete_transport_timeout_is_not_replayed(self):
        with patch.object(emby, 'logger', MagicMock()), \
             patch.object(emby, 'wait_for_server_idle'), \
             patch.object(emby, 'get_admin_access_token', return_value=('admin-token', 'admin')), \
             patch.object(
                 emby.emby_client,
                 'post_once',
                 side_effect=requests.exceptions.Timeout('timeout'),
             ) as post:
            outcome = emby.delete_person_custom_api_outcome(
                'http://emby', 'ordinary-token', 'p1',
            )
        self.assertEqual(outcome, 'ambiguous')
        post.assert_called_once()

    def test_registry_and_legacy_paths_are_fail_closed(self):
        repo = Path(__file__).resolve().parents[1]
        core = (repo / 'tasks' / 'core.py').read_text()
        actors_source = (repo / 'tasks' / 'actors.py').read_text()
        self.assertNotIn('scan-ghost-actors', core)
        self.assertNotIn('task_scan_ghost_actor_candidates', core)
        legacy_tail = actors_source[actors_source.index('def task_merge_duplicate_actors'):]
        self.assertNotIn('delete_person_custom_api', legacy_tail)


if __name__ == '__main__':
    unittest.main()
