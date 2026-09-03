import logging
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import config_manager
import constants
from database import person_cleanup_db
from database.connection import get_db_connection, init_db


POSTGRES_HOST = os.environ.get('EVH_TEST_POSTGRES_HOST')
if not hasattr(logging.Logger, 'trace'):
    logging.Logger.trace = logging.Logger.debug


@unittest.skipUnless(POSTGRES_HOST, 'isolated PostgreSQL is not configured')
class StaleIndexForensicPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config_manager.APP_CONFIG.update({
            constants.CONFIG_OPTION_DB_HOST: POSTGRES_HOST,
            constants.CONFIG_OPTION_DB_PORT: int(os.environ.get('EVH_TEST_POSTGRES_PORT', '5432')),
            constants.CONFIG_OPTION_DB_USER: os.environ.get('EVH_TEST_POSTGRES_USER', 'evh_test'),
            constants.CONFIG_OPTION_DB_PASSWORD: os.environ.get('EVH_TEST_POSTGRES_PASSWORD', 'evh_test'),
            constants.CONFIG_OPTION_DB_NAME: os.environ.get('EVH_TEST_POSTGRES_DB', 'evh_test'),
        })
        init_db()

    def setUp(self):
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    TRUNCATE TABLE
                        person_cleanup_stale_index_items,
                        person_cleanup_stale_index_runs,
                        person_cleanup_alias_proof_items,
                        person_cleanup_alias_proof_runs,
                        person_cleanup_candidates,
                        person_cleanup_protected_aliases,
                        person_cleanup_protected_identities,
                        person_cleanup_protected_names,
                        person_cleanup_protected_people,
                        person_cleanup_protected_libraries,
                        person_cleanup_protection_state
                    CASCADE
                """)
                cursor.execute(
                    "INSERT INTO person_cleanup_protection_state (singleton) VALUES (TRUE)"
                )
        self.generation = person_cleanup_db.begin_protection_snapshot()
        person_cleanup_db.complete_protection_snapshot(self.generation)

    def create_source(self, states=('identity_not_found',), count=None):
        count = int(count if count is not None else len(states))
        people = [
            {'Id': f'p{index}', 'Name': f'Person {index}', 'ProviderIds': {'Tmdb': str(1000 + index)}}
            for index in range(count)
        ]
        person_cleanup_db.replace_candidates(people)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    UPDATE person_cleanup_candidates
                    SET verification_status = 'identity_alias_only', last_checked_at = NOW()
                """)
        candidates = person_cleanup_db.list_candidates_raw()
        proof = person_cleanup_db.create_alias_proof_run(
            self.generation, 'protection', 'normal', 'persons', candidates,
        )
        claimed = []
        while True:
            batch = person_cleanup_db.claim_alias_proof_items(proof['proof_id'], 4)
            if not batch:
                break
            claimed.extend(batch)
        self.assertEqual(len(claimed), count)
        expanded_states = list(states) + ['identity_not_found'] * max(0, count - len(states))
        for item, state in zip(claimed, expanded_states):
            person_cleanup_db.finish_alias_proof_item(
                proof['proof_id'], item['person_id'], {'proof_state': state},
            )
        person_cleanup_db.complete_alias_proof_run(proof['proof_id'], self.generation)
        return proof, candidates

    def create_run(self, proof_id):
        return person_cleanup_db.create_stale_index_run(
            proof_id, self.generation, 'protection', 'relationships', 'persons',
        )

    def complete_run(self, run_id):
        return person_cleanup_db.complete_stale_index_run(
            run_id, self.generation, 'protection', 'relationships', 'persons',
        )

    def test_source_is_only_completed_identity_not_found(self):
        proof, _ = self.create_source(('identity_not_found', 'identity_unavailable'), count=2)
        run = self.create_run(proof['proof_id'])
        self.assertEqual(run['candidate_total'], 1)
        claimed = person_cleanup_db.claim_stale_index_items(run['run_id'], 4)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]['source_proof_state'], 'identity_not_found')

    def test_checkpoint_stop_resume_skips_completed_items(self):
        proof, _ = self.create_source(count=6)
        run = self.create_run(proof['proof_id'])
        claimed = person_cleanup_db.claim_stale_index_items(run['run_id'], 4)
        for item in claimed[:2]:
            person_cleanup_db.finish_stale_index_item(
                run['run_id'], item['person_id'], {
                    'forensic_state': 'verified_stale_index_signature',
                },
            )
        person_cleanup_db.stop_stale_index_run(run['run_id'])
        resumed = person_cleanup_db.resume_stale_index_run(
            run['run_id'], self.generation, 'protection', 'relationships', 'persons',
        )
        self.assertEqual(resumed['checked_count'], 2)
        remaining = []
        while True:
            batch = person_cleanup_db.claim_stale_index_items(run['run_id'], 4)
            if not batch:
                break
            remaining.extend(item['person_id'] for item in batch)
            for item in batch:
                person_cleanup_db.finish_stale_index_item(
                    run['run_id'], item['person_id'], {'forensic_state': 'linked'},
                )
        self.assertEqual(len(remaining), 4)
        final = self.complete_run(run['run_id'])
        self.assertEqual(final['state'], 'completed')
        self.assertEqual(final['checked_count'], 6)

    def test_two_independent_runs_are_required_for_stable_signature(self):
        proof, _ = self.create_source()
        first = self.create_run(proof['proof_id'])
        item = person_cleanup_db.claim_stale_index_items(first['run_id'], 1)[0]
        person_cleanup_db.finish_stale_index_item(
            first['run_id'], item['person_id'], {
                'forensic_state': 'verified_stale_index_signature',
                'identity_signal': 'stale_index_no_identity_owner',
                'people_signal': 'stale_index_different_people',
            },
        )
        self.complete_run(first['run_id'])
        first_item = person_cleanup_db.list_stale_index_items(
            first['run_id'], 'verified_stale_index_signature',
        )['items'][0]
        self.assertEqual(first_item['stable_pass_count'], 1)

        second = self.create_run(proof['proof_id'])
        item = person_cleanup_db.claim_stale_index_items(second['run_id'], 1)[0]
        person_cleanup_db.finish_stale_index_item(
            second['run_id'], item['person_id'], {
                'forensic_state': 'verified_stale_index_signature',
            },
        )
        before_complete = person_cleanup_db.list_stale_index_items(
            second['run_id'], 'verified_stale_index_signature',
        )['items'][0]
        self.assertEqual(before_complete['stable_pass_count'], 1)
        self.assertEqual(
            person_cleanup_db.get_stale_index_run(second['run_id'])['stable_signature_count'],
            0,
        )
        final = self.complete_run(second['run_id'])
        self.assertEqual(final['stable_signature_count'], 1)
        stable = person_cleanup_db.list_stale_index_items(
            second['run_id'], 'stable_stale_index_signature',
        )['items'][0]
        self.assertEqual(stable['stable_pass_count'], 2)

    def test_linked_second_run_resets_stability_chain(self):
        proof, _ = self.create_source()
        first = self.create_run(proof['proof_id'])
        person_cleanup_db.finish_stale_index_item(
            first['run_id'],
            person_cleanup_db.claim_stale_index_items(first['run_id'], 1)[0]['person_id'],
            {'forensic_state': 'verified_stale_index_signature'},
        )
        self.complete_run(first['run_id'])
        second = self.create_run(proof['proof_id'])
        person_cleanup_db.finish_stale_index_item(
            second['run_id'],
            person_cleanup_db.claim_stale_index_items(second['run_id'], 1)[0]['person_id'],
            {'forensic_state': 'linked'},
        )
        self.complete_run(second['run_id'])
        third = self.create_run(proof['proof_id'])
        person_cleanup_db.finish_stale_index_item(
            third['run_id'],
            person_cleanup_db.claim_stale_index_items(third['run_id'], 1)[0]['person_id'],
            {'forensic_state': 'verified_stale_index_signature'},
        )
        self.complete_run(third['run_id'])
        third_item = person_cleanup_db.list_stale_index_items(
            third['run_id'], 'verified_stale_index_signature',
        )['items'][0]
        self.assertEqual(third_item['stable_pass_count'], 1)

    def test_concurrent_claims_are_unique_and_bounded(self):
        proof, _ = self.create_source(count=12)
        run = self.create_run(proof['proof_id'])
        with ThreadPoolExecutor(max_workers=4) as executor:
            batches = list(executor.map(
                lambda _: person_cleanup_db.claim_stale_index_items(run['run_id'], 4),
                range(4),
            ))
        ids = [item['person_id'] for batch in batches for item in batch]
        self.assertEqual(len(ids), 12)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(len(batch) <= 4 for batch in batches))

    def test_stale_run_invalidates_signature_and_stable_counts(self):
        proof, _ = self.create_source()
        run = self.create_run(proof['proof_id'])
        item = person_cleanup_db.claim_stale_index_items(run['run_id'], 1)[0]
        person_cleanup_db.finish_stale_index_item(
            run['run_id'], item['person_id'], {
                'forensic_state': 'verified_stale_index_signature',
            },
        )
        person_cleanup_db.fail_stale_index_run(run['run_id'], 'snapshot drift', stale=True)
        summary = person_cleanup_db.get_stale_index_summary(run['run_id'])
        self.assertEqual(summary['state'], 'stale')
        self.assertEqual(summary['verified_signature_count'], 0)
        self.assertEqual(summary['stable_signature_count'], 0)
        self.assertEqual(summary['states'], [{'forensic_state': 'failed_safe', 'count': 1}])

    def test_summary_preserves_states_signals_and_distributions(self):
        proof, _ = self.create_source(count=2)
        run = self.create_run(proof['proof_id'])
        for index, item in enumerate(person_cleanup_db.claim_stale_index_items(run['run_id'], 2)):
            person_cleanup_db.finish_stale_index_item(
                run['run_id'], item['person_id'], {
                    'forensic_state': 'verified_stale_index_signature',
                    'identity_signal': 'stale_index_no_identity_owner',
                    'people_signal': 'stale_index_same_name_other_person',
                    'query_count': index + 1,
                    'actual_people_count': 1,
                    'same_name_other_count': 1,
                },
            )
        self.complete_run(run['run_id'])
        summary = person_cleanup_db.get_stale_index_summary(run['run_id'])
        self.assertTrue(summary['consistent'])
        self.assertEqual(summary['identity_signals'][0]['count'], 2)
        self.assertEqual(summary['people_signals'][0]['count'], 2)
        self.assertEqual(sum(row['count'] for row in summary['query_count_distribution']), 2)

    def test_stop_resume_ten_times_and_duplicate_finish_never_add_pass(self):
        proof, _ = self.create_source()
        run = self.create_run(proof['proof_id'])
        item = person_cleanup_db.claim_stale_index_items(run['run_id'], 1)[0]
        self.assertTrue(person_cleanup_db.finish_stale_index_item(
            run['run_id'], item['person_id'], {
                'forensic_state': 'verified_stale_index_signature',
            },
        ))
        self.assertFalse(person_cleanup_db.finish_stale_index_item(
            run['run_id'], item['person_id'], {
                'forensic_state': 'verified_stale_index_signature',
            },
        ))
        original_generation = run['forensic_generation']
        for _ in range(10):
            person_cleanup_db.stop_stale_index_run(run['run_id'])
            resumed = person_cleanup_db.resume_stale_index_run(
                run['run_id'], self.generation,
                'protection', 'relationships', 'persons',
            )
            self.assertEqual(resumed['forensic_generation'], original_generation)
        result = self.complete_run(run['run_id'])
        self.assertEqual(result['stable_signature_count'], 0)
        item = person_cleanup_db.list_stale_index_items(
            run['run_id'], 'verified_stale_index_signature',
        )['items'][0]
        self.assertEqual(item['stable_pass_count'], 1)

    def test_people_unavailable_second_run_breaks_stability_chain(self):
        proof, _ = self.create_source()
        first = self.create_run(proof['proof_id'])
        person_cleanup_db.finish_stale_index_item(
            first['run_id'],
            person_cleanup_db.claim_stale_index_items(first['run_id'], 1)[0]['person_id'],
            {'forensic_state': 'verified_stale_index_signature'},
        )
        self.complete_run(first['run_id'])
        second = self.create_run(proof['proof_id'])
        person_cleanup_db.finish_stale_index_item(
            second['run_id'],
            person_cleanup_db.claim_stale_index_items(second['run_id'], 1)[0]['person_id'],
            {'forensic_state': 'people_unavailable'},
        )
        second_done = self.complete_run(second['run_id'])
        self.assertEqual(second_done['stable_signature_count'], 0)
        third = self.create_run(proof['proof_id'])
        person_cleanup_db.finish_stale_index_item(
            third['run_id'],
            person_cleanup_db.claim_stale_index_items(third['run_id'], 1)[0]['person_id'],
            {'forensic_state': 'verified_stale_index_signature'},
        )
        self.complete_run(third['run_id'])
        third_item = person_cleanup_db.list_stale_index_items(
            third['run_id'], 'verified_stale_index_signature',
        )['items'][0]
        self.assertEqual(third_item['stable_pass_count'], 1)

    def test_every_non_signature_latest_run_invalidates_current_stable_state(self):
        proof, _ = self.create_source()
        for rejected_state in (
            'linked', 'protected', 'query_disappeared', 'people_unavailable',
            'candidate_changed', 'person_missing', 'identity_owner_live', 'failed_safe',
        ):
            with self.subTest(rejected_state=rejected_state):
                signature_run = self.create_run(proof['proof_id'])
                person_cleanup_db.finish_stale_index_item(
                    signature_run['run_id'],
                    person_cleanup_db.claim_stale_index_items(
                        signature_run['run_id'], 1,
                    )[0]['person_id'],
                    {'forensic_state': 'verified_stale_index_signature'},
                )
                signature_done = self.complete_run(signature_run['run_id'])
                self.assertEqual(signature_done['stable_signature_count'], 0)

                rejected_run = self.create_run(proof['proof_id'])
                person_cleanup_db.finish_stale_index_item(
                    rejected_run['run_id'],
                    person_cleanup_db.claim_stale_index_items(
                        rejected_run['run_id'], 1,
                    )[0]['person_id'],
                    {'forensic_state': rejected_state},
                )
                rejected_done = self.complete_run(rejected_run['run_id'])
                self.assertEqual(rejected_done['stable_signature_count'], 0)
                rejected_item = person_cleanup_db.list_stale_index_items(
                    rejected_run['run_id'], rejected_state,
                )['items'][0]
                self.assertEqual(rejected_item['stable_pass_count'], 0)

    def test_fingerprint_change_never_inherits_previous_pass(self):
        first_proof, _ = self.create_source()
        first = self.create_run(first_proof['proof_id'])
        person_cleanup_db.finish_stale_index_item(
            first['run_id'],
            person_cleanup_db.claim_stale_index_items(first['run_id'], 1)[0]['person_id'],
            {'forensic_state': 'verified_stale_index_signature'},
        )
        self.complete_run(first['run_id'])

        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE person_cleanup_candidates SET person_name = 'Changed Person'"
                )
        changed_candidates = person_cleanup_db.list_candidates_raw()
        second_proof = person_cleanup_db.create_alias_proof_run(
            self.generation, 'protection', 'normal', 'persons', changed_candidates,
        )
        claimed = person_cleanup_db.claim_alias_proof_items(second_proof['proof_id'], 1)[0]
        person_cleanup_db.finish_alias_proof_item(
            second_proof['proof_id'], claimed['person_id'],
            {'proof_state': 'identity_not_found'},
        )
        person_cleanup_db.complete_alias_proof_run(second_proof['proof_id'], self.generation)
        second = self.create_run(second_proof['proof_id'])
        person_cleanup_db.finish_stale_index_item(
            second['run_id'],
            person_cleanup_db.claim_stale_index_items(second['run_id'], 1)[0]['person_id'],
            {'forensic_state': 'verified_stale_index_signature'},
        )
        self.complete_run(second['run_id'])
        second_item = person_cleanup_db.list_stale_index_items(
            second['run_id'], 'verified_stale_index_signature',
        )['items'][0]
        self.assertEqual(second_item['stable_pass_count'], 1)

    def test_damaged_previous_source_cannot_contribute_stable_pass(self):
        first_proof, _ = self.create_source()
        first = self.create_run(first_proof['proof_id'])
        person_cleanup_db.finish_stale_index_item(
            first['run_id'],
            person_cleanup_db.claim_stale_index_items(first['run_id'], 1)[0]['person_id'],
            {'forensic_state': 'verified_stale_index_signature'},
        )
        self.complete_run(first['run_id'])

        second_proof, _ = self.create_source()
        second = self.create_run(second_proof['proof_id'])
        person_cleanup_db.finish_stale_index_item(
            second['run_id'],
            person_cleanup_db.claim_stale_index_items(second['run_id'], 1)[0]['person_id'],
            {'forensic_state': 'verified_stale_index_signature'},
        )
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE person_cleanup_alias_proof_runs SET state = 'failed' "
                    "WHERE proof_id = %s",
                    (first_proof['proof_id'],),
                )
        self.complete_run(second['run_id'])
        second_item = person_cleanup_db.list_stale_index_items(
            second['run_id'], 'verified_stale_index_signature',
        )['items'][0]
        self.assertEqual(second_item['stable_pass_count'], 1)

    def test_corrupt_or_incomplete_source_fails_closed_at_create_and_complete(self):
        proof, _ = self.create_source()
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE person_cleanup_alias_proof_items SET proof_state = 'pending' "
                    "WHERE proof_id = %s",
                    (proof['proof_id'],),
                )
        with self.assertRaisesRegex(RuntimeError, 'source summary'):
            self.create_run(proof['proof_id'])

        proof, _ = self.create_source()
        changed_source_run = self.create_run(proof['proof_id'])
        person_cleanup_db.finish_stale_index_item(
            changed_source_run['run_id'],
            person_cleanup_db.claim_stale_index_items(
                changed_source_run['run_id'], 1,
            )[0]['person_id'],
            {'forensic_state': 'verified_stale_index_signature'},
        )
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE person_cleanup_alias_proof_items SET proof_state = 'linked' "
                    "WHERE proof_id = %s",
                    (proof['proof_id'],),
                )
        with self.assertRaisesRegex(RuntimeError, 'source 已变化'):
            self.complete_run(changed_source_run['run_id'])

        proof, _ = self.create_source()
        run = self.create_run(proof['proof_id'])
        person_cleanup_db.finish_stale_index_item(
            run['run_id'],
            person_cleanup_db.claim_stale_index_items(run['run_id'], 1)[0]['person_id'],
            {'forensic_state': 'verified_stale_index_signature'},
        )
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE person_cleanup_alias_proof_runs SET state = 'failed' "
                    "WHERE proof_id = %s",
                    (proof['proof_id'],),
                )
        with self.assertRaisesRegex(RuntimeError, '不是 completed'):
            self.complete_run(run['run_id'])

    def test_completion_requires_all_persisted_snapshot_hashes(self):
        proof, _ = self.create_source()
        run = self.create_run(proof['proof_id'])
        person_cleanup_db.finish_stale_index_item(
            run['run_id'],
            person_cleanup_db.claim_stale_index_items(run['run_id'], 1)[0]['person_id'],
            {'forensic_state': 'verified_stale_index_signature'},
        )
        for changed in (
            ('changed', 'relationships', 'persons'),
            ('protection', 'changed', 'persons'),
            ('protection', 'relationships', 'changed'),
        ):
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(RuntimeError, 'snapshot hash'):
                    person_cleanup_db.complete_stale_index_run(
                        run['run_id'], self.generation, *changed,
                    )

    def test_stale_diagnostics_are_persisted_and_all_signatures_fail_closed(self):
        proof, _ = self.create_source(count=2)
        run = self.create_run(proof['proof_id'])
        for item in person_cleanup_db.claim_stale_index_items(run['run_id'], 4):
            person_cleanup_db.finish_stale_index_item(
                run['run_id'], item['person_id'],
                {'forensic_state': 'verified_stale_index_signature'},
            )
        diagnostics = {
            'final_snapshot_generation': self.generation,
            'final_protection_hash': 'protection',
            'final_normal_people_relationship_hash': 'changed-relationships',
            'final_person_snapshot_hash': 'changed-persons',
            'final_source_proof_hash': run['source_proof_hash'],
            'drift_generation': False,
            'drift_protection': False,
            'drift_normal_relationship': True,
            'drift_person': True,
            'drift_source_proof': False,
            'normal_relationship_drift_summary': {
                'added_item_count': 1,
                'samples': [{'item_id': 'm1', 'change_type': 'added'}],
            },
            'person_drift_summary': {
                'person_added_count': 1,
                'samples': [{'person_id': 'p-new', 'change_type': 'added'}],
            },
            'protection_drift_summary': {'protected_ids_changed': False},
            'source_proof_drift_summary': {'source_proof_changed': False},
        }
        person_cleanup_db.fail_stale_index_run(
            run['run_id'], 'precise drift', stale=True, diagnostics=diagnostics,
        )
        persisted = person_cleanup_db.get_stale_index_summary(run['run_id'])
        self.assertEqual(persisted['state'], 'stale')
        self.assertTrue(persisted['drift_normal_relationship'])
        self.assertTrue(persisted['drift_person'])
        self.assertEqual(
            persisted['normal_relationship_drift_summary']['added_item_count'], 1,
        )
        self.assertEqual(persisted['verified_signature_count'], 0)
        self.assertEqual(persisted['stable_signature_count'], 0)
        self.assertEqual(persisted['states'], [
            {'forensic_state': 'failed_safe', 'count': 2},
        ])
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*)::INTEGER AS count
                    FROM person_cleanup_stale_index_items
                    WHERE run_id = %s AND stable_pass_count <> 0
                    """,
                    (run['run_id'],),
                )
                self.assertEqual(cursor.fetchone()['count'], 0)

    def test_diagnostic_persistence_failure_cannot_block_stale_transition(self):
        proof, _ = self.create_source()
        run = self.create_run(proof['proof_id'])
        item = person_cleanup_db.claim_stale_index_items(run['run_id'], 1)[0]
        person_cleanup_db.finish_stale_index_item(
            run['run_id'], item['person_id'],
            {'forensic_state': 'verified_stale_index_signature'},
        )
        with patch.object(
            person_cleanup_db,
            '_persist_stale_index_diagnostics',
            side_effect=RuntimeError('diagnostic write failed'),
        ):
            person_cleanup_db.fail_stale_index_run(
                run['run_id'],
                'snapshot drift',
                stale=True,
                diagnostics={'drift_person': True},
            )
        persisted = person_cleanup_db.get_stale_index_summary(run['run_id'])
        self.assertEqual(persisted['state'], 'stale')
        self.assertEqual(persisted['verified_signature_count'], 0)
        self.assertEqual(persisted['stable_signature_count'], 0)
        self.assertEqual(persisted['states'], [
            {'forensic_state': 'failed_safe', 'count': 1},
        ])
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*)::INTEGER AS count
                    FROM person_cleanup_stale_index_items
                    WHERE run_id = %s AND stable_pass_count <> 0
                    """,
                    (run['run_id'],),
                )
                self.assertEqual(cursor.fetchone()['count'], 0)

    def test_unchanged_final_diagnostics_survive_completed_run(self):
        proof, _ = self.create_source()
        run = self.create_run(proof['proof_id'])
        item = person_cleanup_db.claim_stale_index_items(run['run_id'], 1)[0]
        person_cleanup_db.finish_stale_index_item(
            run['run_id'], item['person_id'], {'forensic_state': 'linked'},
        )
        diagnostics = {
            'final_snapshot_generation': self.generation,
            'final_protection_hash': 'protection',
            'final_normal_people_relationship_hash': 'relationships',
            'final_person_snapshot_hash': 'persons',
            'final_source_proof_hash': run['source_proof_hash'],
            'drift_generation': False,
            'drift_protection': False,
            'drift_normal_relationship': False,
            'drift_person': False,
            'drift_source_proof': False,
            'normal_relationship_drift_summary': {},
            'person_drift_summary': {},
            'protection_drift_summary': {},
            'source_proof_drift_summary': {'source_proof_changed': False},
        }
        person_cleanup_db.record_stale_index_final_diagnostics(
            run['run_id'], diagnostics,
        )
        completed = self.complete_run(run['run_id'])
        self.assertEqual(completed['state'], 'completed')
        self.assertEqual(completed['final_person_snapshot_hash'], 'persons')
        self.assertFalse(completed['drift_person'])

    def test_source_diagnostic_reports_changed_rows_and_incomplete_state(self):
        proof, _ = self.create_source()
        initial = person_cleanup_db.get_alias_proof_source_diagnostic(
            proof['proof_id'],
        )
        self.assertTrue(initial['complete'])
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE person_cleanup_alias_proof_items
                    SET person_name = 'Changed source row'
                    WHERE proof_id = %s
                    """,
                    (proof['proof_id'],),
                )
        changed = person_cleanup_db.get_alias_proof_source_diagnostic(
            proof['proof_id'],
        )
        self.assertTrue(changed['complete'])
        self.assertNotEqual(
            changed['source_proof_hash'], initial['source_proof_hash'],
        )
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE person_cleanup_alias_proof_runs
                    SET state = 'failed' WHERE proof_id = %s
                    """,
                    (proof['proof_id'],),
                )
        incomplete = person_cleanup_db.get_alias_proof_source_diagnostic(
            proof['proof_id'],
        )
        self.assertFalse(incomplete['complete'])
        self.assertIn('不是 completed', incomplete['error'])

    def test_migration_is_additive_idempotent_and_interrupts_running_work(self):
        proof, _ = self.create_source()
        run = self.create_run(proof['proof_id'])
        person_cleanup_db.claim_stale_index_items(run['run_id'], 1)
        init_db()
        init_db()
        interrupted = person_cleanup_db.get_stale_index_run(run['run_id'])
        self.assertEqual(interrupted['state'], 'interrupted')
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT forensic_state FROM person_cleanup_stale_index_items
                    WHERE run_id = %s
                """, (run['run_id'],))
                self.assertEqual(cursor.fetchone()['forensic_state'], 'pending')
                cursor.execute("""
                    SELECT COUNT(*)::INTEGER AS count FROM person_cleanup_candidates
                """)
                self.assertEqual(cursor.fetchone()['count'], 1)


if __name__ == '__main__':
    unittest.main()
