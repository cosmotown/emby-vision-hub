import unittest
import inspect
import threading
import time
from unittest.mock import Mock, patch
from types import SimpleNamespace

from flask import Flask

import config_manager
from handler import emby
from routes.person_cleanup import person_cleanup_bp
from services.person_cleanup_safety import classify_alias_orphan_proof
from tasks import actors


class AliasOrphanProofSafetyTests(unittest.TestCase):
    def candidate(self, provider=None):
        return {
            'person_id': 'A',
            'person_name': 'Alias A',
            'provider_ids_json': {'Tmdb': '100'} if provider is None else provider,
            'verification_status': 'identity_alias_only',
        }

    def detail(self, person_id='A', provider=None, name=None):
        return {
            'Id': person_id,
            'Name': name or ('Alias A' if person_id == 'A' else 'Live B'),
            'Type': 'Person',
            'ProviderIds': provider if provider is not None else {'Tmdb': '100'},
        }

    def reference(self, **changes):
        result = {
            'status': 'identity_alias_only',
            'count': 0,
            'query_count': 5,
            'unverified_items': [],
        }
        result.update(changes)
        return result

    def classify(self, **changes):
        candidate = changes.pop('candidate', self.candidate())
        current = changes.pop('current_candidate', dict(candidate))
        detail = changes.pop('candidate_detail', self.detail())
        normal = changes.pop('normal_referenced_person_ids', {'B'})
        index = changes.pop('identity_index', {('tmdb', '100'): {'A', 'B'}})
        details = changes.pop('person_details', {'A': detail, 'B': self.detail('B')})
        references = changes.pop('reference_result', self.reference())
        return classify_alias_orphan_proof(
            candidate, current, detail, normal, index, details, references, **changes,
        )

    def test_case_a_exact_other_identity_is_live(self):
        self.assertEqual(self.classify()['proof_state'], 'verified_alias_orphan')

    def test_case_b_candidate_in_normal_snapshot_is_linked(self):
        self.assertEqual(
            self.classify(normal_referenced_person_ids={'A', 'B'})['proof_state'],
            'linked',
        )

    def test_case_c_no_other_identity(self):
        self.assertEqual(
            self.classify(identity_index={('tmdb', '100'): {'A'}})['proof_state'],
            'identity_not_found',
        )

    def test_case_d_multiple_other_identity_people_is_ambiguous(self):
        details = {'A': self.detail(), 'B': self.detail('B'), 'C': self.detail('C')}
        self.assertEqual(self.classify(
            identity_index={('tmdb', '100'): {'A', 'B', 'C'}},
            person_details=details,
        )['proof_state'], 'identity_ambiguous')

    def test_multi_provider_identities_pointing_to_different_people_are_ambiguous(self):
        candidate = self.candidate({'Tmdb': '100', 'Imdb': 'nm0200'})
        detail = self.detail(provider={'Tmdb': '100', 'Imdb': 'nm0200'})
        details = {
            'A': detail,
            'B': self.detail('B', provider={'Tmdb': '100'}),
            'C': self.detail('C', provider={'Imdb': 'nm0200'}),
        }
        result = self.classify(
            candidate=candidate,
            current_candidate=dict(candidate),
            candidate_detail=detail,
            normal_referenced_person_ids={'B', 'C'},
            identity_index={
                ('tmdb', '100'): {'A', 'B'},
                ('imdb', 'nm0200'): {'A', 'C'},
            },
            person_details=details,
        )
        self.assertEqual(result['proof_state'], 'identity_ambiguous')

    def test_case_e_identity_unavailable(self):
        candidate = self.candidate({})
        detail = self.detail(provider={})
        self.assertEqual(self.classify(
            candidate=candidate,
            current_candidate=dict(candidate),
            candidate_detail=detail,
            person_details={'A': detail},
            identity_index={},
        )['proof_state'], 'identity_unavailable')

    def test_case_f_unverified_people_fails_closed(self):
        self.assertEqual(self.classify(
            reference_result=self.reference(unverified_items=[{'Id': 'X'}]),
        )['proof_state'], 'people_unavailable')

    def test_case_g_every_protection_reason_rejects(self):
        for reason in (
            'protected_id', 'protected_name', 'protected_provider_identity',
            'protected_library_alias', 'protected_library_unverifiable',
            'protected_provider_invalid',
        ):
            with self.subTest(reason=reason):
                self.assertEqual(
                    self.classify(protection_reason=reason)['proof_state'],
                    'protected',
                )

    def test_case_j_candidate_fingerprint_change_fails_closed(self):
        current = self.candidate()
        current['person_name'] = 'Changed'
        self.assertEqual(
            self.classify(current_candidate=current)['proof_state'],
            'candidate_changed',
        )

    def test_live_match_must_be_in_normal_people_snapshot(self):
        self.assertEqual(
            self.classify(normal_referenced_person_ids=set())['proof_state'],
            'identity_not_found',
        )

    def test_existing_delete_contract_is_not_extended(self):
        source = __import__('inspect').getsource(
            __import__('services.person_cleanup_safety', fromlist=['is_explicit_verified_orphan'])
            .is_explicit_verified_orphan
        )
        self.assertIn("candidate.get('verification_status') == 'orphan'", source)
        self.assertNotIn('verified_alias_orphan', source)


class AliasOrphanSnapshotTests(unittest.TestCase):
    @staticmethod
    def response(payload):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = payload
        return response

    def test_case_h_normal_snapshot_page_drift_fails_whole_snapshot(self):
        page1 = {'TotalRecordCount': 2, 'Items': [{'Id': 'm1', 'People': [{'Id': 'p1', 'Name': 'P1'}]}]}
        page2 = {'TotalRecordCount': 3, 'Items': [{'Id': 'm2', 'People': [{'Id': 'p2', 'Name': 'P2'}]}]}
        with patch.object(emby.emby_client, 'get', side_effect=[self.response(page1), self.response(page2)]):
            self.assertIsNone(emby.get_referenced_person_ids_strict(
                'http://emby', 'token', ['lib'], batch_size=1, require_person_names=True,
            ))

    def test_strict_library_snapshot_includes_mixed_library(self):
        payload = [{
            'ItemId': 'mixed', 'Name': 'Mixed', 'CollectionType': None,
            'Guid': 'g1', 'Locations': ['/media/mixed'],
        }]
        with patch.object(emby.emby_client, 'get', return_value=self.response(payload)):
            libraries = emby.get_all_libraries_with_paths_strict('http://emby', 'token')
        self.assertEqual([row['info']['Id'] for row in libraries], ['mixed'])

    def test_strict_library_snapshot_rejects_missing_locations_and_duplicate_ids(self):
        invalid_payloads = [
            [{'ItemId': 'missing', 'Name': 'Missing', 'Locations': []}],
            [
                {'ItemId': 'same', 'Name': 'One', 'Locations': ['/one']},
                {'ItemId': 'same', 'Name': 'Two', 'Locations': ['/two']},
            ],
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload), patch.object(
                emby.emby_client, 'get', return_value=self.response(payload),
            ):
                self.assertIsNone(
                    emby.get_all_libraries_with_paths_strict('http://emby', 'token')
                )

    def test_protection_hash_is_deterministic_and_alias_sensitive(self):
        contract = {
            'generation': 7,
            'person_ids': {'p2', 'p1'},
            'name_keys': {'b', 'a'},
            'provider_identities': {('tmdb', '2'), ('imdb', '1')},
            'alias_statuses': {'alias2': 'protected_library_alias'},
        }
        root_contract = {
            'complete': True,
            'selected_library_ids': frozenset({'l2', 'l1'}),
            'roots': (
                {'library_id': 'l2', 'library_name': 'Two', 'style': 'posix', 'path': '/two'},
                {'library_id': 'l1', 'library_name': 'One', 'style': 'posix', 'path': '/one'},
            ),
        }
        first = actors._alias_proof_protection_hash(contract, root_contract)
        reordered = actors._alias_proof_protection_hash(
            {**contract, 'person_ids': {'p1', 'p2'}, 'name_keys': {'a', 'b'}},
            {**root_contract, 'roots': tuple(reversed(root_contract['roots']))},
        )
        changed = actors._alias_proof_protection_hash(
            {**contract, 'alias_statuses': {
                **contract['alias_statuses'],
                'alias3': 'protected_library_unverifiable',
            }},
            root_contract,
        )
        self.assertEqual(first, reordered)
        self.assertNotEqual(first, changed)

    def test_normal_snapshot_requires_person_name_for_proof(self):
        payload = {'TotalRecordCount': 1, 'Items': [{'Id': 'm1', 'People': [{'Id': 'p1'}]}]}
        with patch.object(emby.emby_client, 'get', return_value=self.response(payload)):
            self.assertIsNone(emby.get_referenced_person_ids_strict(
                'http://emby', 'token', ['lib'], require_person_names=True,
            ))

    def test_normal_snapshot_rejects_every_incomplete_people_shape(self):
        invalid_items = [
            {'People': []},
            {'Id': 'm1'},
            {'Id': 'm1', 'People': {}},
            {'Id': 'm1', 'People': ['bad-row']},
            {'Id': 'm1', 'People': [{'Name': 'No ID'}]},
        ]
        for item in invalid_items:
            payload = {'TotalRecordCount': 1, 'Items': [item]}
            with self.subTest(item=item), patch.object(
                emby.emby_client, 'get', return_value=self.response(payload),
            ):
                self.assertIsNone(emby.get_referenced_person_ids_strict(
                    'http://emby', 'token', ['lib'], require_person_names=True,
                ))

    def test_normal_snapshot_rejects_duplicate_item_and_early_end(self):
        duplicate_pages = [
            self.response({'TotalRecordCount': 2, 'Items': [
                {'Id': 'm1', 'People': [{'Id': 'p1', 'Name': 'P1'}]},
            ]}),
            self.response({'TotalRecordCount': 2, 'Items': [
                {'Id': 'm1', 'People': [{'Id': 'p1', 'Name': 'P1'}]},
            ]}),
        ]
        with patch.object(emby.emby_client, 'get', side_effect=duplicate_pages):
            self.assertIsNone(emby.get_referenced_person_ids_strict(
                'http://emby', 'token', ['lib'], batch_size=1, require_person_names=True,
            ))
        early_end = [
            self.response({'TotalRecordCount': 2, 'Items': [
                {'Id': 'm1', 'People': [{'Id': 'p1', 'Name': 'P1'}]},
            ]}),
            self.response({'TotalRecordCount': 2, 'Items': []}),
        ]
        with patch.object(emby.emby_client, 'get', side_effect=early_end):
            self.assertIsNone(emby.get_referenced_person_ids_strict(
                'http://emby', 'token', ['lib'], batch_size=1, require_person_names=True,
            ))

    def test_all_person_snapshot_rejects_duplicate_id_and_total_drift(self):
        page1 = {'TotalRecordCount': 2, 'Items': [self._person('p1')]}
        page2 = {'TotalRecordCount': 2, 'Items': [self._person('p1')]}
        with patch.object(emby.emby_client, 'get', side_effect=[self.response(page1), self.response(page2)]):
            self.assertIsNone(emby.get_all_person_details_snapshot_strict(
                'http://emby', 'token', batch_size=1,
            ))

    def test_case_i_incomplete_protection_snapshot_creates_no_proof_results(self):
        processor = SimpleNamespace()
        with patch.object(
            actors, '_build_alias_proof_snapshots',
            side_effect=RuntimeError('保护快照未就绪'),
        ), patch.object(
            actors.person_cleanup_db, 'create_alias_proof_run',
        ) as create_run, patch.object(
            actors.task_manager, 'update_status_from_thread',
        ):
            with self.assertRaisesRegex(RuntimeError, '保护快照未就绪'):
                actors.task_alias_orphan_readonly_proof(processor)
        create_run.assert_not_called()

    def test_proof_task_contains_no_emby_mutation_path(self):
        task_source = inspect.getsource(actors.task_alias_orphan_readonly_proof)
        check_source = inspect.getsource(actors._check_alias_orphan_proof_candidate)
        combined = task_source + check_source
        for forbidden in (
            '.post(', '.delete(', 'DeletePerson', 'PlaybackInfo',
            'SyncMediaInfo', '/Refresh',
        ):
            self.assertNotIn(forbidden, combined)

    @staticmethod
    def _person(person_id):
        return {'Id': person_id, 'Name': person_id, 'Type': 'Person', 'ProviderIds': {}}


class AliasOrphanProofApiTests(unittest.TestCase):
    def setUp(self):
        self.previous_auth = config_manager.APP_CONFIG.get('auth_enabled')
        config_manager.APP_CONFIG['auth_enabled'] = False
        self.app = Flask(__name__)
        self.app.config['TESTING'] = True
        self.app.register_blueprint(person_cleanup_bp)
        self.client = self.app.test_client()

    def tearDown(self):
        if self.previous_auth is None:
            config_manager.APP_CONFIG.pop('auth_enabled', None)
        else:
            config_manager.APP_CONFIG['auth_enabled'] = self.previous_auth

    def test_status_and_samples_are_persisted_db_reads_without_emby(self):
        run = {'proof_id': 'proof-1', 'state': 'completed', 'states': []}
        sample = {
            'items': [{'person_id': 'A', 'proof_state': 'identity_not_found'}],
            'total': 1, 'page': 1, 'page_size': 20,
        }
        with patch(
            'routes.person_cleanup.person_cleanup_db.get_alias_proof_summary',
            return_value=run,
        ), patch(
            'routes.person_cleanup.person_cleanup_db.get_alias_proof_run',
            return_value=run,
        ), patch(
            'routes.person_cleanup.person_cleanup_db.list_alias_proof_items',
            return_value=sample,
        ), patch(
            'routes.person_cleanup.emby.get_person_media_references',
        ) as emby_lookup:
            detail = self.client.get('/api/person-cleanup/alias-proof-runs/proof-1')
            items = self.client.get(
                '/api/person-cleanup/alias-proof-runs/proof-1/items?state=identity_not_found'
            )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(items.status_code, 200)
        self.assertEqual(items.get_json()['items'][0]['person_id'], 'A')
        emby_lookup.assert_not_called()


class AliasOrphanProofTaskTests(unittest.TestCase):
    def test_candidate_uses_fresh_exact_person_detail(self):
        proof_item = {
            'person_id': 'A', 'person_name': 'Alias A',
            'candidate_provider_ids': {'Tmdb': '100'},
        }
        candidate = {
            'person_id': 'A', 'person_name': 'Alias A',
            'provider_ids_json': {'Tmdb': '100'},
            'verification_status': 'identity_alias_only',
        }
        snapshots = {
            'person_details': {
                'A': {'Id': 'A', 'Name': 'stale', 'Type': 'Person', 'ProviderIds': {'Tmdb': '100'}},
                'B': {'Id': 'B', 'Name': 'Live B', 'Type': 'Person', 'ProviderIds': {'Tmdb': '100'}},
            },
            'normal_ids': {'B'},
            'identity_index': {('tmdb', '100'): {'A', 'B'}},
            'root_contract': {'complete': True},
            'contract': {'generation': 1},
        }
        fresh = {'Id': 'A', 'Name': 'Alias A', 'Type': 'Person', 'ProviderIds': {'Tmdb': '100'}}
        processor = SimpleNamespace(emby_url='http://emby', emby_api_key='token', emby_user_id=None)
        with patch.object(
            actors.person_cleanup_db, 'get_candidates_by_ids',
            side_effect=[[candidate], [candidate]],
        ), patch.object(
            actors.person_cleanup_db, 'candidate_protection_reason', return_value=None,
        ), patch.object(
            actors.emby, 'get_person_details_strict', return_value={'A': fresh},
        ) as detail_get, patch.object(
            actors.emby, 'get_person_media_references', return_value={
                'status': 'identity_alias_only', 'count': 0,
                'query_count': 1, 'unverified_items': [],
            },
        ):
            outcome = actors._check_alias_orphan_proof_candidate(
                processor, proof_item, snapshots,
            )

        self.assertEqual(outcome['proof_state'], 'verified_alias_orphan')
        detail_get.assert_called_once_with('http://emby', 'token', ['A'], batch_size=1)

    def test_final_snapshot_drift_fails_run_without_completion(self):
        first = {
            'generation': 7, 'protection_hash': 'guard1',
            'normal_hash': 'n1', 'person_hash': 'p1',
        }
        changed = {
            'generation': 7, 'protection_hash': 'guard2',
            'normal_hash': 'n1', 'person_hash': 'p1',
        }
        processor = SimpleNamespace(is_stop_requested=lambda: False)
        with patch.object(
            actors, '_build_alias_proof_snapshots', side_effect=[first, changed],
        ), patch.object(
            actors.person_cleanup_db, 'list_alias_proof_candidates', return_value=[],
        ), patch.object(
            actors.person_cleanup_db, 'create_alias_proof_run', return_value={
                'proof_id': 'proof', 'candidate_total': 0, 'checked_count': 0,
            },
        ), patch.object(
            actors.person_cleanup_db, 'alias_proof_stop_requested', return_value=False,
        ), patch.object(
            actors.person_cleanup_db, 'require_ready_protection_snapshot', return_value=7,
        ), patch.object(
            actors.person_cleanup_db, 'claim_alias_proof_items', return_value=[],
        ), patch.object(
            actors.person_cleanup_db, 'fail_alias_proof_run',
        ) as fail_run, patch.object(
            actors.person_cleanup_db, 'complete_alias_proof_run',
        ) as complete_run, patch.object(
            actors.task_manager, 'update_status_from_thread',
        ):
            actors.task_alias_orphan_readonly_proof(processor)

        fail_run.assert_called_once_with(
            'proof',
            'Emby/保护快照在证明期间发生变化，全部证明结果已失败关闭',
            stale=True,
        )
        complete_run.assert_not_called()

    def test_four_workers_are_bounded_and_every_claim_is_terminalized(self):
        candidates = [
            {
                'person_id': f'p{index}', 'person_name': f'P{index}',
                'provider_ids_json': {'Tmdb': str(index + 1)},
                'verification_status': 'identity_alias_only',
            }
            for index in range(12)
        ]
        queue = [
            {
                'proof_id': 'proof', **candidate,
                'candidate_provider_ids': candidate['provider_ids_json'],
                'proof_state': 'checking',
            }
            for candidate in candidates
        ]
        lock = threading.Lock()
        active = 0
        maximum = 0
        checked = 0

        def claim(_proof_id, limit):
            batch = queue[:limit]
            del queue[:limit]
            return batch

        def check(*_args):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.01)
            with lock:
                active -= 1
            return {'proof_state': 'identity_not_found'}

        def finish(*_args):
            nonlocal checked
            checked += 1
            return True

        processor = SimpleNamespace(is_stop_requested=lambda: False)
        snapshots = {
            'generation': 7, 'protection_hash': 'guard',
            'normal_hash': 'n', 'person_hash': 'p',
        }
        with patch.object(actors, '_build_alias_proof_snapshots', side_effect=[snapshots, snapshots]), \
             patch.object(actors.person_cleanup_db, 'list_alias_proof_candidates', return_value=candidates), \
             patch.object(actors.person_cleanup_db, 'create_alias_proof_run', return_value={
                 'proof_id': 'proof', 'candidate_total': 12, 'checked_count': 0,
             }), \
             patch.object(actors.person_cleanup_db, 'alias_proof_stop_requested', return_value=False), \
             patch.object(actors.person_cleanup_db, 'require_ready_protection_snapshot', return_value=7), \
             patch.object(actors.person_cleanup_db, 'claim_alias_proof_items', side_effect=claim), \
             patch.object(actors, '_check_alias_orphan_proof_candidate', side_effect=check), \
             patch.object(actors.person_cleanup_db, 'finish_alias_proof_item', side_effect=finish), \
             patch.object(actors.person_cleanup_db, 'get_alias_proof_run', side_effect=lambda *_: {
                 'candidate_total': 12, 'checked_count': checked,
                 'verified_alias_orphan_count': 0,
             }), \
             patch.object(actors.person_cleanup_db, 'complete_alias_proof_run', return_value={
                 'checked_count': 12, 'verified_alias_orphan_count': 0,
             }), \
             patch.object(actors.task_manager, 'update_status_from_thread'):
            actors.task_alias_orphan_readonly_proof(processor)

        self.assertEqual(checked, 12)
        self.assertGreater(maximum, 1)
        self.assertLessEqual(maximum, 4)


if __name__ == '__main__':
    unittest.main()
