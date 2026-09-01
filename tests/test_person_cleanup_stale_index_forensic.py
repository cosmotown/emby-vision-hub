import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from flask import Flask

import config_manager
from handler import emby
from routes.person_cleanup import person_cleanup_bp
from services.person_cleanup_safety import (
    candidate_fingerprint,
    classify_stale_index_forensic,
    is_explicit_verified_orphan,
)
from tasks import actors


class StaleIndexClassifierTests(unittest.TestCase):
    def candidate(self, name='Alias A'):
        return {
            'person_id': 'A', 'person_name': name,
            'provider_ids_json': {'Tmdb': '100'},
            'verification_status': 'unverified',
        }

    def detail_result(self, name='Alias A'):
        return {'status': 'ok', 'detail': {
            'Id': 'A', 'Name': name, 'Type': 'Person',
            'ProviderIds': {'Tmdb': '100'},
        }}

    def source(self):
        candidate = self.candidate()
        return {
            'person_id': 'A', 'person_name': 'Alias A',
            'provider_ids': {'Tmdb': '100'},
            'candidate_fingerprint': candidate_fingerprint(candidate),
            'source_proof_state': 'identity_not_found',
        }

    def relationship(self, people=None):
        if people is None:
            people = [('B', 'Live B')]
        return {
            'm1': {
                'item_id': 'm1', 'item_type': 'Movie', 'library_id': 'normal',
                'people': people,
            },
        }

    def classify(self, **changes):
        values = {
            'source_item': self.source(),
            'current_candidate': self.candidate(),
            'candidate_detail_result': self.detail_result(),
            'normal_referenced_person_ids': {'B'},
            'normal_item_people': self.relationship(),
            'identity_index': {('tmdb', '100'): {'A'}},
            'query_items': [{'Id': 'm1', 'Type': 'Movie', 'Path': '/normal/m1.mkv'}],
            'root_contract': {'complete': True, 'roots': (), 'selected_library_ids': frozenset()},
            'protection_reason': None,
        }
        values.update(changes)
        return classify_stale_index_forensic(**values)

    def test_case_a_complete_stale_signature(self):
        result = self.classify()
        self.assertEqual(result['forensic_state'], 'verified_stale_index_signature')
        self.assertEqual(result['identity_signal'], 'stale_index_no_identity_owner')
        self.assertEqual(result['people_signal'], 'stale_index_different_people')
        self.assertEqual(result['query_count'], 1)

    def test_case_b_actual_people_contains_candidate_is_linked(self):
        result = self.classify(normal_item_people=self.relationship([('A', 'Alias A')]))
        self.assertEqual(result['forensic_state'], 'linked')

    def test_case_c_incomplete_people_is_unavailable(self):
        broken = self.relationship()
        broken['m1']['people'] = None
        result = self.classify(normal_item_people=broken)
        self.assertEqual(result['forensic_state'], 'people_unavailable')

    def test_case_d_every_existing_protection_reason_rejects(self):
        for reason in (
            'protected_id', 'protected_name', 'protected_provider_identity',
            'protected_library_alias', 'protected_library_unverifiable',
        ):
            with self.subTest(reason=reason):
                self.assertEqual(
                    self.classify(protection_reason=reason)['forensic_state'],
                    'protected',
                )

    def test_case_d_query_hit_under_protected_root_rejects(self):
        contract = {
            'complete': True,
            'selected_library_ids': frozenset({'protected'}),
            'roots': ({
                'library_id': 'protected', 'library_name': 'Protected',
                'style': 'posix', 'path': '/protected',
            },),
        }
        result = self.classify(
            query_items=[{'Id': 'm1', 'Type': 'Movie', 'Path': '/protected/m1.mkv'}],
            root_contract=contract,
        )
        self.assertEqual(result['forensic_state'], 'protected')

    def test_case_e_query_disappeared(self):
        self.assertEqual(
            self.classify(query_items=[])['forensic_state'],
            'query_disappeared',
        )

    def test_case_f_candidate_fingerprint_changed(self):
        self.assertEqual(
            self.classify(current_candidate=self.candidate('Changed'))['forensic_state'],
            'candidate_changed',
        )

    def test_case_g_same_name_other_person_is_signal_only(self):
        result = self.classify(normal_item_people=self.relationship([('B', 'Alias A')]))
        self.assertEqual(result['forensic_state'], 'verified_stale_index_signature')
        self.assertEqual(result['people_signal'], 'stale_index_same_name_other_person')
        self.assertFalse(is_explicit_verified_orphan({
            'verification_status': result['forensic_state'],
        }, 1))

    def test_case_h_different_people_is_signal_only(self):
        result = self.classify(normal_item_people=self.relationship([('B', 'Different')]))
        self.assertEqual(result['people_signal'], 'stale_index_different_people')

    def test_identity_owner_not_live_is_separate_signal(self):
        result = self.classify(identity_index={('tmdb', '100'): {'A', 'C'}})
        self.assertEqual(result['forensic_state'], 'verified_stale_index_signature')
        self.assertEqual(result['identity_signal'], 'stale_index_identity_owner_not_live')

    def test_new_live_identity_owner_requires_alias_proof_again(self):
        result = self.classify(
            identity_index={('tmdb', '100'): {'A', 'B'}},
            normal_referenced_person_ids={'B'},
        )
        self.assertEqual(result['forensic_state'], 'identity_owner_live')

    def test_person_missing_is_distinct_from_detail_failure(self):
        self.assertEqual(self.classify(
            candidate_detail_result={'status': 'person_missing', 'detail': None},
        )['forensic_state'], 'person_missing')
        self.assertEqual(self.classify(
            candidate_detail_result={'status': 'failed_safe', 'detail': None},
        )['forensic_state'], 'failed_safe')

    def test_query_item_ownership_unknown_fails_closed(self):
        self.assertEqual(
            self.classify(normal_item_people={})['forensic_state'],
            'failed_safe',
        )

    def test_five_query_items_with_only_four_snapshot_bindings_fail_closed(self):
        relationships = {
            f'm{index}': {
                'item_id': f'm{index}', 'item_type': 'Movie', 'library_id': 'normal',
                'people': [('B', 'Live B')],
            }
            for index in range(1, 5)
        }
        query_items = [
            {'Id': f'm{index}', 'Type': 'Movie', 'Path': f'/normal/m{index}.mkv'}
            for index in range(1, 6)
        ]
        result = self.classify(
            normal_item_people=relationships,
            query_items=query_items,
        )
        self.assertEqual(result['forensic_state'], 'failed_safe')
        self.assertEqual(result['query_count'], 5)


class StaleIndexSnapshotTests(unittest.TestCase):
    @staticmethod
    def response(payload):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = payload
        return response

    def test_relationship_snapshot_captures_item_library_people_and_names(self):
        payload = {'TotalRecordCount': 1, 'Items': [{
            'Id': 'm1', 'Type': 'Movie',
            'People': [{'Id': 'A', 'Name': 'Alice'}],
        }]}
        with patch.object(emby.emby_client, 'get', return_value=self.response(payload)):
            result = emby.get_referenced_person_ids_strict(
                'http://emby', 'token', ['normal'],
                require_person_names=True, capture_item_people=True,
            )
        self.assertEqual(result['item_people']['m1'], {
            'item_id': 'm1', 'item_type': 'Movie', 'library_id': 'normal',
            'people': (('A', 'Alice'),),
        })

    def test_relationship_snapshot_duplicate_item_across_libraries_fails_closed(self):
        payload = {'TotalRecordCount': 1, 'Items': [{
            'Id': 'm1', 'Type': 'Movie', 'People': [{'Id': 'A', 'Name': 'Alice'}],
        }]}
        with patch.object(
            emby.emby_client, 'get',
            side_effect=[self.response(payload), self.response(payload)],
        ):
            self.assertIsNone(emby.get_referenced_person_ids_strict(
                'http://emby', 'token', ['one', 'two'],
                require_person_names=True, capture_item_people=True,
            ))

    def test_personids_query_requires_stable_total_and_unique_items(self):
        page1 = {'TotalRecordCount': 2, 'Items': [{'Id': 'm1', 'Type': 'Movie'}]}
        page2 = {'TotalRecordCount': 3, 'Items': [{'Id': 'm2', 'Type': 'Movie'}]}
        with patch.object(
            emby.emby_client, 'get',
            side_effect=[self.response(page1), self.response(page2)],
        ):
            self.assertIsNone(emby.get_person_media_query_items_strict(
                'http://emby', 'token', 'A', batch_size=1,
            ))

    def test_people_relationship_hash_changes_when_item_people_changes(self):
        first = actors._stale_index_relationship_hash({
            'm1': {
                'item_type': 'Movie', 'library_id': 'lib',
                'people': [('A', 'Alice')],
            },
        })
        second = actors._stale_index_relationship_hash({
            'm1': {
                'item_type': 'Movie', 'library_id': 'lib',
                'people': [('B', 'Bob')],
            },
        })
        self.assertNotEqual(first, second)

    def test_people_relationship_hash_is_order_independent_and_covers_all_facts(self):
        first = {
            'm2': {
                'item_type': 'Episode', 'library_id': 'tv',
                'people': [('B', 'Bob'), ('A', 'Alice')],
            },
            'm1': {
                'item_type': 'Movie', 'library_id': 'movies',
                'people': [('C', 'Carol')],
            },
        }
        reordered = {
            'm1': {
                'item_type': 'Movie', 'library_id': 'movies',
                'people': [('C', 'Carol')],
            },
            'm2': {
                'item_type': 'Episode', 'library_id': 'tv',
                'people': [('A', 'Alice'), ('B', 'Bob')],
            },
        }
        expected = actors._stale_index_relationship_hash(first)
        self.assertEqual(expected, actors._stale_index_relationship_hash(reordered))
        for field, changed in (
            ('library_id', 'other-library'),
            ('item_type', 'Series'),
        ):
            mutation = {key: {**value} for key, value in first.items()}
            mutation['m1'][field] = changed
            self.assertNotEqual(expected, actors._stale_index_relationship_hash(mutation))
        renamed = {key: {**value} for key, value in first.items()}
        renamed['m1']['people'] = [('C', 'Changed Name')]
        self.assertNotEqual(expected, actors._stale_index_relationship_hash(renamed))

    def test_forensic_task_contains_get_only_emby_path(self):
        source = (
            inspect.getsource(actors.task_stale_index_readonly_forensic)
            + inspect.getsource(actors._check_stale_index_forensic_candidate)
            + inspect.getsource(actors._build_stale_index_forensic_snapshots)
        )
        for forbidden in (
            '.post(', '.put(', '.patch(', '.delete(', 'DeletePerson',
            'PlaybackInfo', 'SyncMediaInfo', '/Refresh',
        ):
            self.assertNotIn(forbidden, source)

    def test_every_global_snapshot_drift_marks_run_stale_without_completion(self):
        first = {
            'generation': 7, 'protection_hash': 'guard',
            'normal_people_relationship_hash': 'people-1', 'person_hash': 'person',
        }
        for key, changed_value in (
            ('normal_people_relationship_hash', 'people-2'),
            ('protection_hash', 'guard-2'),
            ('person_hash', 'person-2'),
        ):
            with self.subTest(key=key):
                changed = {**first, key: changed_value}
                processor = SimpleNamespace(is_stop_requested=lambda: False)
                with patch.object(
                    actors.person_cleanup_db, 'get_latest_completed_alias_proof_source',
                    return_value={'proof_id': 'source'},
                ), patch.object(
                    actors, '_build_stale_index_forensic_snapshots', side_effect=[first, changed],
                ), patch.object(
                    actors.person_cleanup_db, 'create_stale_index_run', return_value={
                        'run_id': 'run', 'candidate_total': 0, 'checked_count': 0,
                    },
                ), patch.object(
                    actors.person_cleanup_db, 'stale_index_stop_requested', return_value=False,
                ), patch.object(
                    actors.person_cleanup_db, 'require_ready_protection_snapshot', return_value=7,
                ), patch.object(
                    actors.person_cleanup_db, 'claim_stale_index_items', return_value=[],
                ), patch.object(
                    actors.person_cleanup_db, 'fail_stale_index_run',
                ) as fail_run, patch.object(
                    actors.person_cleanup_db, 'complete_stale_index_run',
                ) as complete_run, patch.object(
                    actors.task_manager, 'update_status_from_thread',
                ):
                    actors.task_stale_index_readonly_forensic(processor)
                fail_run.assert_called_once()
                self.assertTrue(fail_run.call_args.kwargs['stale'])
                complete_run.assert_not_called()

    def test_task_worker_count_is_bounded_to_four(self):
        self.assertEqual(actors.PERSON_STALE_INDEX_WORKERS, 4)
        self.assertEqual(actors.PERSON_STALE_INDEX_CLAIM_LIMIT, 4)
        task_source = inspect.getsource(actors.task_stale_index_readonly_forensic)
        self.assertIn('max_workers=PERSON_STALE_INDEX_WORKERS', task_source)
        self.assertIn('limit=PERSON_STALE_INDEX_CLAIM_LIMIT', task_source)


class StaleIndexApiTests(unittest.TestCase):
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

    def test_status_and_samples_are_persisted_reads_without_emby(self):
        run = {'run_id': 'run-1', 'state': 'completed', 'states': []}
        samples = {'items': [{'person_id': 'A'}], 'total': 1, 'page': 1, 'page_size': 20}
        with patch(
            'routes.person_cleanup.person_cleanup_db.get_stale_index_summary',
            return_value=run,
        ), patch(
            'routes.person_cleanup.person_cleanup_db.get_stale_index_run',
            return_value=run,
        ), patch(
            'routes.person_cleanup.person_cleanup_db.list_stale_index_items',
            return_value=samples,
        ), patch(
            'routes.person_cleanup.emby.get_person_media_query_items_strict',
        ) as emby_get:
            detail = self.client.get('/api/person-cleanup/stale-index-runs/run-1')
            rows = self.client.get(
                '/api/person-cleanup/stale-index-runs/run-1/items'
                '?state=verified_stale_index_signature&dimension=forensic_state'
            )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(rows.status_code, 200)
        self.assertEqual(rows.get_json()['total'], 1)
        emby_get.assert_not_called()

    def test_start_and_stop_routes_do_not_expose_delete_contract(self):
        source = inspect.getsource(__import__('routes.person_cleanup', fromlist=['x']))
        stale_section = source[source.index("@person_cleanup_bp.route('/stale-index-runs'"):]
        stale_section = stale_section[:stale_section.index("@person_cleanup_bp.route('/protected-libraries'")]
        self.assertNotIn('DeletePerson', stale_section)
        self.assertNotIn('task_delete_selected_ghost_actors', stale_section)


if __name__ == '__main__':
    unittest.main()
