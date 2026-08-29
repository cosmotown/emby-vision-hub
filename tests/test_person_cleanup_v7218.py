import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from handler import emby
from services.person_cleanup_safety import (
    build_protected_library_root_contract,
    candidate_fingerprint,
    classify_reference_check,
    find_ghost_candidates,
    match_item_to_protected_library,
)
from tasks import actors


def protected_contract(*roots):
    libraries = [
        {
            'info': {'Id': library_id, 'Name': library_name},
            'paths': list(paths),
        }
        for library_id, library_name, paths in roots
    ]
    selected = [
        {'library_id': library_id, 'library_name': library_name}
        for library_id, library_name, _paths in roots
    ]
    return build_protected_library_root_contract(libraries, selected)


class ProtectedLibraryOwnershipTests(unittest.TestCase):
    def test_posix_boundary_longest_and_ambiguity(self):
        contract = protected_contract(
            ('base', '基础库', ['/media']),
            ('protected', '保护库', ['/media/protected']),
        )
        self.assertEqual(
            match_item_to_protected_library(
                {'Id': 'm1', 'Path': '/media/protected/a.mkv'}, contract,
            )['protected_library_id'],
            'protected',
        )
        single = protected_contract(('a', 'A', ['/media/a']))
        self.assertIsNotNone(match_item_to_protected_library(
            {'Id': 'm2', 'Path': '/media/a/movie/file.mkv'}, single,
        ))
        self.assertIsNone(match_item_to_protected_library(
            {'Id': 'm3', 'Path': '/media/abc/file.mkv'}, single,
        ))
        ambiguous = protected_contract(
            ('a', 'A', ['/same/root']),
            ('b', 'B', ['/same/root']),
        )
        self.assertIsNone(match_item_to_protected_library(
            {'Id': 'm4', 'Path': '/same/root/file.mkv'}, ambiguous,
        ))

    def test_windows_and_unc_component_boundaries(self):
        windows = protected_contract(
            ('win', 'Windows', [r'D:\Media\Protected']),
        )
        self.assertIsNotNone(match_item_to_protected_library(
            {'Id': 'w1', 'Path': r'd:\media\protected\A\file.mkv'}, windows,
        ))
        protect = protected_contract(('win', 'Windows', [r'D:\Media\Protect']))
        self.assertIsNone(match_item_to_protected_library(
            {'Id': 'w2', 'Path': r'D:\Media\Protected\file.mkv'}, protect,
        ))
        unc = protected_contract(
            ('unc', 'UNC', [r'\\server\share\protected']),
        )
        self.assertIsNotNone(match_item_to_protected_library(
            {'Id': 'u1', 'Path': r'\\server\share\protected\A\file.mkv'}, unc,
        ))
        self.assertIsNone(match_item_to_protected_library(
            {'Id': 'u2', 'Path': r'\\server\share\protected-other\file.mkv'}, unc,
        ))

    def test_missing_invalid_or_incomplete_contract_is_unknown(self):
        valid = protected_contract(('p', '保护库', ['/media/protected']))
        for item in ({'Id': 'm1'}, {'Id': 'm1', 'Path': 'relative/file.mkv'}, {'Path': '/media/protected/a'}):
            self.assertIsNone(match_item_to_protected_library(item, valid))
        incomplete = build_protected_library_root_contract(
            [], [{'library_id': 'missing', 'library_name': '缺失库'}],
        )
        self.assertFalse(incomplete['complete'])
        self.assertIsNone(match_item_to_protected_library(
            {'Id': 'm1', 'Path': '/media/protected/a.mkv'}, incomplete,
        ))

    def test_unselected_malformed_library_does_not_poison_selected_roots(self):
        contract = build_protected_library_root_contract(
            [
                {'unexpected': 'unrelated malformed virtual folder'},
                {
                    'info': {'Id': 'protected', 'Name': '保护库'},
                    'paths': ['/media/protected'],
                },
            ],
            [{'library_id': 'protected', 'library_name': '保护库'}],
        )
        self.assertTrue(contract['complete'])
        self.assertEqual(
            match_item_to_protected_library(
                {'Id': 'm1', 'Path': '/media/protected/a.mkv'}, contract,
            )['protected_library_id'],
            'protected',
        )


class ProtectedLibraryReferenceTests(unittest.TestCase):
    @staticmethod
    def response(items):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {'Items': items}
        return response

    def setUp(self):
        self.contract = protected_contract(
            ('protected', '保护库', ['/media/protected']),
        )

    def test_production_style_aliases_use_path_not_name_or_provider(self):
        for person_id, person_name in (
            ('1020094', '1田中'),
            ('619579', '2◎ゆうか'),
        ):
            client = MagicMock()
            client.get.return_value = self.response([{
                'Id': 'protected-item',
                'Path': '/media/protected/show/episode.mkv',
                'People': [{'Id': 'actual-person', 'Name': '真实人物'}],
            }])
            with patch.object(emby, 'emby_client', client):
                result = emby.get_person_media_references(
                    'http://emby', 'token', person_id,
                    person_name=person_name,
                    protected_root_contract=self.contract,
                )
            self.assertEqual(result['status'], 'protected_library_alias')
            self.assertEqual(result['protected_library_id'], 'protected')
            self.assertEqual(result['evidence_item_id'], 'protected-item')
            self.assertEqual(result['count'], 0)
            self.assertIn('Path', client.get.call_args.kwargs['params']['Fields'])

    def test_people_unavailable_in_protected_library_is_protected(self):
        client = MagicMock()
        client.get.return_value = self.response([{
            'Id': 'protected-item',
            'Path': '/media/protected/show/episode.mkv',
            'People': [],
        }])
        with patch.object(emby, 'emby_client', client), \
             patch.object(emby, 'get_emby_items_by_id', return_value=[{
                 'Id': 'protected-item', 'People': [],
             }]), \
             patch.object(emby, 'get_emby_item_people_details', return_value=[{
                 'Id': 'protected-item', 'People': [],
             }]):
            result = emby.get_person_media_references(
                'http://emby', 'token', '619697', person_name='----',
                protected_root_contract=self.contract,
            )
        self.assertEqual(result['status'], 'protected_library_unverifiable')
        self.assertEqual(result['protected_library_id'], 'protected')
        self.assertEqual(classify_reference_check(result), result['status'])

    def test_ordinary_or_unknown_ownership_keeps_existing_fail_closed_status(self):
        for item in (
            {
                'Id': 'normal-item', 'Path': '/media/normal/a.mkv',
                'People': [{'Id': 'other'}],
            },
            {'Id': 'unknown-item', 'People': [{'Id': 'other'}]},
        ):
            client = MagicMock()
            client.get.return_value = self.response([item])
            with patch.object(emby, 'emby_client', client):
                result = emby.get_person_media_references(
                    'http://emby', 'token', 'candidate',
                    protected_root_contract=self.contract,
                )
            self.assertEqual(result['status'], 'identity_alias_only')


class ProtectedLibraryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.processor = SimpleNamespace(
            emby_url='http://emby',
            emby_api_key='token',
            emby_user_id='user',
            is_stop_requested=lambda: False,
        )
        self.candidate = {
            'person_id': '1020094',
            'person_name': '1田中',
            'provider_ids_json': {},
        }
        self.reference = {
            'status': 'protected_library_alias',
            'count': 0,
            'query_count': 1,
            'protected_library_id': 'protected',
            'protected_library_name': '保护库',
            'evidence_item_id': 'media1',
        }

    def empty_contract(self, generation):
        return {
            'generation': generation,
            'person_ids': set(),
            'name_keys': set(),
            'provider_identities': set(),
            'alias_statuses': {},
        }

    def test_preview_persists_and_removes_alias_without_delete(self):
        db = actors.person_cleanup_db
        with patch.object(db, 'get_protection_contract', return_value=self.empty_contract(20)), \
             patch.object(db, 'list_candidates_raw', return_value=[self.candidate]), \
             patch.object(db, 'candidate_protection_reason', return_value=None), \
             patch.object(db, 'persist_protected_alias_and_remove_candidate') as persist, \
             patch.object(db, 'add_cleanup_job_item') as add_item, \
             patch.object(db, 'finish_cleanup_preview'), \
             patch.object(db, 'cleanup_job_stop_requested', return_value=False), \
             patch.object(actors, '_refresh_protected_snapshot', return_value=(20, {})), \
             patch.object(actors, '_build_protected_root_contract', return_value={'complete': True}) as roots, \
             patch.object(actors.emby, 'get_person_media_references', return_value=self.reference), \
             patch.object(actors.emby, 'delete_person_custom_api_outcome') as delete:
            actors.task_preview_safe_person_cleanup(self.processor, 'job1')

        roots.assert_called_once()
        persist.assert_called_once_with(
            self.candidate, 'protected', 'protected_library_alias', 'media1',
        )
        self.assertEqual(add_item.call_args.args[2], 'protected_library_alias')
        delete.assert_not_called()

    def test_execute_defensively_blocks_new_alias(self):
        item = {
            **self.candidate,
            'candidate_fingerprint': candidate_fingerprint(self.candidate),
            'post_attempts': 0,
        }
        db = actors.person_cleanup_db
        with patch.object(db, 'start_cleanup_job'), \
             patch.object(db, 'get_protection_contract', return_value=self.empty_contract(21)), \
             patch.object(db, 'list_cleanup_job_orphans', return_value=[item]), \
             patch.object(db, 'cleanup_job_stop_requested', return_value=False), \
             patch.object(db, 'get_candidates_by_ids', return_value=[self.candidate]), \
             patch.object(db, 'candidate_protection_reason', return_value=None), \
             patch.object(db, 'persist_protected_alias_and_remove_candidate') as persist, \
             patch.object(db, 'mark_cleanup_job_item') as mark_item, \
             patch.object(db, 'finish_cleanup_job'), \
             patch.object(actors, '_refresh_protected_snapshot', return_value=(21, {})), \
             patch.object(actors, '_build_protected_root_contract', return_value={'complete': True}), \
             patch.object(actors.emby, 'get_person_media_references', return_value=self.reference), \
             patch.object(actors.emby, 'delete_person_custom_api_outcome') as delete:
            actors.task_execute_safe_person_cleanup(self.processor, 'job1')

        persist.assert_called_once()
        self.assertEqual(mark_item.call_args.args[2], 'skipped_protected_library_alias')
        delete.assert_not_called()

    def test_legacy_selected_delete_defensively_revokes_alias(self):
        db = actors.person_cleanup_db
        with patch.object(db, 'get_candidates_by_ids', return_value=[self.candidate]), \
             patch.object(db, 'require_ready_protection_snapshot', return_value=19), \
             patch.object(db, 'get_protection_contract', return_value=self.empty_contract(20)), \
             patch.object(db, 'candidate_protection_reason', return_value=None), \
             patch.object(db, 'persist_protected_alias_and_remove_candidate') as persist, \
             patch.object(actors, 'is_explicit_verified_orphan', return_value=True), \
             patch.object(actors, '_refresh_protected_snapshot', return_value=(20, {})), \
             patch.object(actors, '_build_protected_root_contract', return_value={'complete': True}), \
             patch.object(actors.emby, 'get_person_media_references', return_value=self.reference), \
             patch.object(actors.emby, 'delete_person_custom_api_outcome') as delete:
            actors.task_delete_selected_ghost_actors(
                self.processor,
                [self.candidate['person_id']],
            )

        persist.assert_called_once_with(
            self.candidate, 'protected', 'protected_library_alias', 'media1',
        )
        delete.assert_not_called()

    def test_initial_scan_adds_persistent_phase_two_reference_checks(self):
        people = [
            {'Id': str(index), 'Name': f'人物{index}', 'ProviderIds': {}}
            for index in range(22908)
        ]
        self.assertEqual(
            len(find_ghost_candidates(
                people,
                referenced_person_ids=set(),
                protected_alias_person_ids={'1024'},
            )),
            22907,
        )
        source = Path(actors.__file__).read_text()
        scan_source = source[
            source.index('def task_scan_ghost_actor_candidates'):
            source.index('def task_delete_selected_ghost_actors')
        ]
        self.assertIn('_run_readonly_alias_scan', scan_source)
        self.assertIn('start_readonly_alias_scan', scan_source)

    def test_phase_two_is_bounded_and_get_only(self):
        source = Path(actors.__file__).read_text()
        checker_source = source[
            source.index('def _check_readonly_alias_candidate'):
            source.index('def _run_readonly_alias_scan')
        ]
        phase_two_source = source[
            source.index('def _run_readonly_alias_scan'):
            source.index('def task_scan_ghost_actor_candidates')
        ]
        self.assertIn('max_workers=PERSON_ALIAS_SCAN_WORKERS', phase_two_source)
        self.assertEqual(actors.PERSON_ALIAS_SCAN_WORKERS, 4)
        self.assertLessEqual(actors.PERSON_ALIAS_SCAN_CLAIM_LIMIT, 6)
        self.assertIn('get_person_media_references', checker_source)
        self.assertIn('detail_workers=1', checker_source)
        self.assertNotIn('.post(', checker_source)
        self.assertNotIn('delete_person', checker_source)
        self.assertNotIn('delete_person', phase_two_source)
        self.assertNotIn('.post(', phase_two_source)

    def test_route_verify_atomically_revokes_protected_alias(self):
        route_path = Path(__file__).resolve().parents[1] / 'routes' / 'person_cleanup.py'
        route_tree = ast.parse(route_path.read_text())
        verify_node = next(
            node for node in route_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == 'verify_person_cleanup_candidate'
        )
        verify_node.decorator_list = []
        db = MagicMock()
        db.require_ready_protection_snapshot.return_value = 22
        db.get_protection_contract.return_value = self.empty_contract(22)
        db.get_candidates_by_ids.return_value = [self.candidate]
        db.candidate_protection_reason.return_value = None
        emby_mock = MagicMock()
        emby_mock.get_person_media_references.return_value = self.reference
        build_roots = MagicMock(return_value={'complete': True})
        namespace = {
            'person_cleanup_db': db,
            'emby': emby_mock,
            'extensions': SimpleNamespace(
                media_processor_instance=SimpleNamespace(
                    emby_url='http://emby', emby_api_key='token',
                ),
                EMBY_SERVER_ID='',
            ),
            '_build_protected_root_contract': build_roots,
            'classify_reference_check': classify_reference_check,
            '_serialize_reference_items': lambda items: [],
            'config_manager': SimpleNamespace(APP_CONFIG={}),
            'constants': SimpleNamespace(
                CONFIG_OPTION_EMBY_PUBLIC_URL='public',
                CONFIG_OPTION_EMBY_SERVER_URL='server',
            ),
            'jsonify': lambda payload: payload,
        }
        exec(compile(ast.Module(body=[verify_node], type_ignores=[]), str(route_path), 'exec'), namespace)

        response = namespace['verify_person_cleanup_candidate']('1020094')

        self.assertTrue(response['candidate_removed'])
        self.assertTrue(response['verification_complete'])
        self.assertNotIn('Path', response)
        build_roots.assert_called_once_with()
        db.persist_protected_alias_and_remove_candidate.assert_called_once_with(
            self.candidate, 'protected', 'protected_library_alias', 'media1',
        )


if __name__ == '__main__':
    unittest.main()
