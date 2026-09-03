import copy
import unittest
from unittest.mock import patch

import config_manager
from tasks import actors


class StaleIndexDriftDiagnosticsTests(unittest.TestCase):
    def snapshots(self):
        contract = {
            'generation': 7,
            'person_ids': {'protected-person'},
            'name_keys': {'protected name'},
            'provider_identities': {('tmdb', '100')},
            'alias_statuses': {'alias-person': 'protected_library_alias'},
        }
        root_contract = {
            'complete': True,
            'selected_library_ids': {'protected-library'},
            'roots': ({
                'library_id': 'protected-library',
                'library_name': 'Protected',
                'style': 'posix',
                'path': '/secret/protected',
            },),
        }
        item_people = {
            'm1': {
                'item_id': 'm1', 'item_type': 'Movie', 'library_id': 'normal',
                'people': (('p1', 'Person One'),),
            },
        }
        person_details = {
            'p1': {'Id': 'p1', 'Name': 'Person One', 'ProviderIds': {'Tmdb': '1'}},
        }
        return self.rehash({
            'generation': 7,
            'contract': contract,
            'root_contract': root_contract,
            'item_people': item_people,
            'person_details': person_details,
            'media_count': 1,
        })

    @staticmethod
    def rehash(snapshot):
        snapshot['protection_hash'] = actors._alias_proof_protection_hash(
            snapshot['contract'], snapshot['root_contract'],
        )
        snapshot['normal_people_relationship_hash'] = actors._stale_index_relationship_hash(
            snapshot['item_people'],
        )
        snapshot['person_hash'] = actors._alias_proof_snapshot_hash([
            (
                person_id,
                str(detail.get('Name') or ''),
                detail.get('ProviderIds') or {},
            )
            for person_id, detail in sorted(snapshot['person_details'].items())
        ])
        return snapshot

    def diagnostics(self, final, *, source_hash='source', source_complete=True):
        start = self.snapshots()
        return actors._build_stale_index_drift_diagnostics(
            start,
            self.rehash(final),
            'source',
            {
                'complete': source_complete,
                'source_proof_hash': source_hash,
                'error': None if source_complete else 'source incomplete',
            },
        )

    def test_unrelated_person_addition_is_precise_person_drift_and_still_stale(self):
        final = copy.deepcopy(self.snapshots())
        final['person_details']['unrelated'] = {
            'Id': 'unrelated', 'Name': 'Unrelated', 'ProviderIds': {'Tmdb': '999'},
        }
        result = self.diagnostics(final)
        self.assertTrue(result['has_drift'])
        self.assertTrue(result['drift_person'])
        self.assertFalse(result['drift_normal_relationship'])
        self.assertEqual(result['person_drift_summary']['person_added_count'], 1)

    def test_person_name_change_is_counted(self):
        final = copy.deepcopy(self.snapshots())
        final['person_details']['p1']['Name'] = 'Renamed'
        result = self.diagnostics(final)
        self.assertEqual(result['person_drift_summary']['person_name_changed_count'], 1)
        self.assertEqual(result['person_drift_summary']['person_provider_ids_changed_count'], 0)

    def test_person_provider_ids_change_is_counted_with_canonical_sample(self):
        final = copy.deepcopy(self.snapshots())
        final['person_details']['p1']['ProviderIds'] = {'Tmdb': '2'}
        result = self.diagnostics(final)
        self.assertEqual(result['person_drift_summary']['person_provider_ids_changed_count'], 1)
        sample = result['person_drift_summary']['samples'][0]
        self.assertEqual(sample['old_provider_identities'], ['tmdb:1'])
        self.assertEqual(sample['new_provider_identities'], ['tmdb:2'])

    def test_new_movie_is_relationship_drift(self):
        final = copy.deepcopy(self.snapshots())
        final['item_people']['m2'] = {
            'item_id': 'm2', 'item_type': 'Movie', 'library_id': 'normal',
            'people': (('p1', 'Person One'),),
        }
        final['media_count'] = 2
        result = self.diagnostics(final)
        summary = result['normal_relationship_drift_summary']
        self.assertTrue(result['drift_normal_relationship'])
        self.assertEqual(summary['added_item_count'], 1)
        self.assertEqual(summary['start_media_count'], 1)
        self.assertEqual(summary['final_media_count'], 2)
        self.assertNotIn('/secret', str(summary))

    def test_existing_media_people_change_is_counted(self):
        final = copy.deepcopy(self.snapshots())
        final['item_people']['m1']['people'] = (('p2', 'Person Two'),)
        result = self.diagnostics(final)
        summary = result['normal_relationship_drift_summary']
        self.assertEqual(summary['changed_item_people_count'], 1)
        self.assertEqual(summary['people_added_count'], 1)
        self.assertEqual(summary['people_removed_count'], 1)

    def test_type_library_and_people_name_changes_are_separate_counts(self):
        final = copy.deepcopy(self.snapshots())
        final['item_people']['m1'].update({
            'item_type': 'Episode',
            'library_id': 'other-normal',
            'people': (('p1', 'Renamed Person'),),
        })
        summary = self.diagnostics(final)['normal_relationship_drift_summary']
        self.assertEqual(summary['changed_item_people_count'], 1)
        self.assertEqual(summary['changed_item_type_count'], 1)
        self.assertEqual(summary['changed_library_ownership_count'], 1)
        self.assertEqual(summary['people_name_changed_count'], 1)

    def test_only_protection_change_is_reported_by_component(self):
        final = copy.deepcopy(self.snapshots())
        final['contract']['person_ids'].add('new-protected')
        result = self.diagnostics(final)
        self.assertTrue(result['drift_protection'])
        self.assertTrue(
            result['protection_drift_summary']['protected_ids_changed'],
        )
        self.assertFalse(result['drift_person'])

    def test_only_source_proof_change_is_reported(self):
        result = self.diagnostics(copy.deepcopy(self.snapshots()), source_hash='changed')
        self.assertTrue(result['drift_source_proof'])
        self.assertTrue(result['source_proof_drift_summary']['source_proof_changed'])
        self.assertFalse(result['drift_person'])
        self.assertFalse(result['drift_normal_relationship'])

    def test_multiple_drifts_are_reported_independently(self):
        final = copy.deepcopy(self.snapshots())
        final['generation'] = 8
        final['contract']['generation'] = 8
        final['item_people']['m2'] = {
            'item_id': 'm2', 'item_type': 'Movie', 'library_id': 'normal', 'people': (),
        }
        final['person_details']['p1']['Name'] = 'Renamed'
        result = self.diagnostics(final, source_complete=False)
        self.assertTrue(result['drift_generation'])
        self.assertTrue(result['drift_protection'])
        self.assertTrue(result['drift_normal_relationship'])
        self.assertTrue(result['drift_person'])
        self.assertTrue(result['drift_source_proof'])

    def test_unchanged_snapshots_complete_diagnostic_without_drift(self):
        result = self.diagnostics(copy.deepcopy(self.snapshots()))
        self.assertFalse(result['has_drift'])
        self.assertFalse(any(
            result[key]
            for key in (
                'drift_generation', 'drift_protection', 'drift_normal_relationship',
                'drift_person', 'drift_source_proof',
            )
        ))

    def test_samples_are_bounded_to_twenty(self):
        final = copy.deepcopy(self.snapshots())
        for index in range(30):
            final['item_people'][f'new-{index:02d}'] = {
                'item_id': f'new-{index:02d}', 'item_type': 'Movie',
                'library_id': 'normal', 'people': (),
            }
            final['person_details'][f'new-{index:02d}'] = {
                'Name': f'New {index}', 'ProviderIds': {},
            }
        result = self.diagnostics(final)
        self.assertEqual(len(result['normal_relationship_drift_summary']['samples']), 20)
        self.assertEqual(len(result['person_drift_summary']['samples']), 20)

    def test_diagnostic_summary_failure_cannot_hide_exact_hash_drift(self):
        final = copy.deepcopy(self.snapshots())
        final['item_people']['m2'] = {
            'item_id': 'm2', 'item_type': 'Movie',
            'library_id': 'normal', 'people': (),
        }
        final = self.rehash(final)
        with patch.object(
            actors,
            '_stale_index_relationship_drift_summary',
            side_effect=RuntimeError('diagnostic-only failure'),
        ):
            result = self.diagnostics(final)
        self.assertTrue(result['has_drift'])
        self.assertTrue(result['drift_normal_relationship'])
        self.assertFalse(result['normal_relationship_drift_summary']['available'])
        self.assertEqual(
            result['normal_relationship_drift_summary']['error'],
            'RuntimeError',
        )


if __name__ == '__main__':
    unittest.main()
