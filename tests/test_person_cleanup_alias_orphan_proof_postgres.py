import os
import logging
import unittest
from concurrent.futures import ThreadPoolExecutor

import config_manager
import constants
from database.connection import get_db_connection, init_db
from database import person_cleanup_db


POSTGRES_HOST = os.environ.get('EVH_TEST_POSTGRES_HOST')
if not hasattr(logging.Logger, 'trace'):
    logging.Logger.trace = logging.Logger.debug


@unittest.skipUnless(POSTGRES_HOST, 'isolated PostgreSQL is not configured')
class AliasOrphanProofPostgresTests(unittest.TestCase):
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
                cursor.execute("INSERT INTO person_cleanup_protection_state (singleton) VALUES (TRUE)")
        generation = person_cleanup_db.begin_protection_snapshot()
        person_cleanup_db.complete_protection_snapshot(generation)
        self.generation = generation

    def insert_candidates(self, count=8):
        person_cleanup_db.replace_candidates([
            {'Id': f'p{index}', 'Name': f'Person {index}', 'ProviderIds': {'Tmdb': str(1000 + index)}}
            for index in range(count)
        ])
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    UPDATE person_cleanup_candidates
                    SET verification_status = 'identity_alias_only',
                        last_checked_at = NOW(), last_error = 'fail closed'
                """)
        return person_cleanup_db.list_candidates_raw()

    def test_case_k_restart_resume_skips_completed_items(self):
        candidates = self.insert_candidates(6)
        run = person_cleanup_db.create_alias_proof_run(
            self.generation, 'protection-hash', 'normal-hash', 'person-hash', candidates,
        )
        claimed = person_cleanup_db.claim_alias_proof_items(run['proof_id'], 4)
        for item in claimed[:2]:
            self.assertTrue(person_cleanup_db.finish_alias_proof_item(
                run['proof_id'], item['person_id'], {'proof_state': 'identity_not_found'},
            ))
        person_cleanup_db.stop_alias_proof_run(run['proof_id'])
        resumed = person_cleanup_db.resume_alias_proof_run(
            run['proof_id'], self.generation, 'protection-hash', 'normal-hash', 'person-hash',
        )
        self.assertEqual(resumed['checked_count'], 2)
        remaining = []
        while True:
            batch = person_cleanup_db.claim_alias_proof_items(run['proof_id'], 4)
            if not batch:
                break
            remaining.extend(item['person_id'] for item in batch)
            for item in batch:
                person_cleanup_db.finish_alias_proof_item(
                    run['proof_id'], item['person_id'], {'proof_state': 'identity_not_found'},
                )
        self.assertEqual(len(remaining), 4)
        self.assertNotIn(claimed[0]['person_id'], remaining)
        final = person_cleanup_db.complete_alias_proof_run(run['proof_id'], self.generation)
        self.assertEqual(final['checked_count'], 6)
        self.assertEqual(final['state'], 'completed')

    def test_historical_preview_identity_alias_is_eligible_only_with_same_fingerprint(self):
        person_cleanup_db.replace_candidates([
            {'Id': 'historical', 'Name': 'Alias', 'ProviderIds': {'Tmdb': '42'}},
        ])
        candidate = person_cleanup_db.list_candidates_raw()[0]
        self.assertEqual(candidate['verification_status'], 'unverified')
        job_id = person_cleanup_db.create_cleanup_job()
        person_cleanup_db.initialize_cleanup_job_candidate_total(job_id, 1)
        person_cleanup_db.add_cleanup_job_item(job_id, candidate, 'identity_alias_only')
        person_cleanup_db.finish_cleanup_preview(job_id, self.generation)

        eligible = person_cleanup_db.list_alias_proof_candidates()
        self.assertEqual([row['person_id'] for row in eligible], ['historical'])
        self.assertEqual(eligible[0]['verification_status'], 'identity_alias_only')

        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE person_cleanup_candidates SET person_name = 'Changed' WHERE person_id = 'historical'"
                )
        self.assertEqual(person_cleanup_db.list_alias_proof_candidates(), [])

    def test_historical_preview_is_not_reused_after_current_linked_status(self):
        person_cleanup_db.replace_candidates([
            {'Id': 'historical', 'Name': 'Alias', 'ProviderIds': {'Tmdb': '42'}},
        ])
        candidate = person_cleanup_db.list_candidates_raw()[0]
        job_id = person_cleanup_db.create_cleanup_job()
        person_cleanup_db.initialize_cleanup_job_candidate_total(job_id, 1)
        person_cleanup_db.add_cleanup_job_item(job_id, candidate, 'identity_alias_only')
        person_cleanup_db.finish_cleanup_preview(job_id, self.generation)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    UPDATE person_cleanup_candidates
                    SET verification_status = 'linked'
                    WHERE person_id = 'historical'
                """)
        self.assertEqual(person_cleanup_db.list_alias_proof_candidates(), [])

    def test_case_l_skip_locked_claims_same_person_once(self):
        candidates = self.insert_candidates(8)
        run = person_cleanup_db.create_alias_proof_run(
            self.generation, 'protection-hash', 'normal-hash', 'person-hash', candidates,
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(person_cleanup_db.claim_alias_proof_items, run['proof_id'], 4)
                for _ in range(2)
            ]
            first, second = [future.result() for future in futures]
        self.assertEqual(len(first), 4)
        self.assertEqual(len(second), 4)
        self.assertTrue({row['person_id'] for row in first}.isdisjoint(
            {row['person_id'] for row in second}
        ))

    def test_resume_requeues_only_fingerprint_changed_completed_item(self):
        candidates = self.insert_candidates(2)
        run = person_cleanup_db.create_alias_proof_run(
            self.generation, 'protection-hash', 'normal-hash', 'person-hash', candidates,
        )
        claimed = person_cleanup_db.claim_alias_proof_items(run['proof_id'], 2)
        for item in claimed:
            person_cleanup_db.finish_alias_proof_item(
                run['proof_id'], item['person_id'], {'proof_state': 'identity_not_found'},
            )
        person_cleanup_db.stop_alias_proof_run(run['proof_id'])
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE person_cleanup_candidates SET person_name = 'Changed' WHERE person_id = 'p0'"
                )
        person_cleanup_db.resume_alias_proof_run(
            run['proof_id'], self.generation, 'protection-hash', 'normal-hash', 'person-hash',
        )
        changed = person_cleanup_db.requeue_changed_alias_proof_items(
            run['proof_id'], person_cleanup_db.list_candidates_raw(),
        )
        self.assertEqual(changed, 1)
        resumed = person_cleanup_db.get_alias_proof_run(run['proof_id'])
        self.assertEqual(resumed['checked_count'], 1)
        recheck = person_cleanup_db.claim_alias_proof_items(run['proof_id'], 4)
        self.assertEqual([item['person_id'] for item in recheck], ['p0'])

    def test_generation_drift_invalidates_prior_verified_results(self):
        candidates = self.insert_candidates(1)
        run = person_cleanup_db.create_alias_proof_run(
            self.generation, 'protection-hash', 'normal-hash', 'person-hash', candidates,
        )
        item = person_cleanup_db.claim_alias_proof_items(run['proof_id'], 1)[0]
        person_cleanup_db.finish_alias_proof_item(
            run['proof_id'], item['person_id'], {'proof_state': 'verified_alias_orphan'},
        )
        person_cleanup_db.fail_alias_proof_run(run['proof_id'], 'generation drift', stale=True)
        summary = person_cleanup_db.get_alias_proof_summary(run['proof_id'])
        self.assertEqual(summary['state'], 'stale')
        self.assertEqual(summary['verified_alias_orphan_count'], 0)
        self.assertEqual(summary['states'], [{'proof_state': 'failed_safe', 'count': 1}])

    def test_resume_rejects_protection_contract_hash_drift(self):
        candidates = self.insert_candidates(1)
        run = person_cleanup_db.create_alias_proof_run(
            self.generation, 'protection-hash', 'normal-hash', 'person-hash', candidates,
        )
        person_cleanup_db.stop_alias_proof_run(run['proof_id'])
        with self.assertRaisesRegex(RuntimeError, 'snapshot/generation'):
            person_cleanup_db.resume_alias_proof_run(
                run['proof_id'], self.generation, 'changed-protection-hash',
                'normal-hash', 'person-hash',
            )

    def test_completed_rejects_unknown_item_state_even_when_counts_match(self):
        candidates = self.insert_candidates(1)
        run = person_cleanup_db.create_alias_proof_run(
            self.generation, 'protection-hash', 'normal-hash', 'person-hash', candidates,
        )
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    UPDATE person_cleanup_alias_proof_items
                    SET proof_state = 'unexpected_state', checked_at = NOW()
                    WHERE proof_id = %s
                """, (run['proof_id'],))
                cursor.execute("""
                    UPDATE person_cleanup_alias_proof_runs
                    SET checked_count = candidate_total
                    WHERE proof_id = %s
                """, (run['proof_id'],))
        with self.assertRaisesRegex(RuntimeError, '状态或计数不完整'):
            person_cleanup_db.complete_alias_proof_run(run['proof_id'], self.generation)

    def test_alias_proof_migration_is_idempotent_and_additive(self):
        init_db()
        init_db()
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = 'person_cleanup_alias_proof_runs'
                """)
                columns = {row['column_name'] for row in cursor.fetchall()}
        self.assertIn('protection_snapshot_hash', columns)
        self.assertIn('normal_snapshot_hash', columns)
        self.assertIn('person_snapshot_hash', columns)

    def test_production_scale_fixture_all_items_are_terminal(self):
        candidates = self.insert_candidates(22002)
        run = person_cleanup_db.create_alias_proof_run(
            self.generation, 'protection-hash', 'normal-hash', 'person-hash', candidates[:21975],
        )
        distribution = [
            ('verified_alias_orphan', 20000),
            ('identity_unavailable', 900),
            ('identity_ambiguous', 500),
            ('identity_not_found', 400),
            ('protected', 100),
            ('failed_safe', 75),
        ]
        assigned = 0
        ordered_ids = [candidate['person_id'] for candidate in candidates[:21975]]
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                for state, count in distribution:
                    state_ids = ordered_ids[assigned:assigned + count]
                    cursor.execute(
                        """
                        UPDATE person_cleanup_alias_proof_items
                        SET proof_state = %s, checked_at = NOW()
                        WHERE proof_id = %s AND person_id = ANY(%s)
                        """,
                        (state, run['proof_id'], state_ids),
                    )
                    self.assertEqual(cursor.rowcount, count)
                    assigned += count
                cursor.execute(
                    """
                    UPDATE person_cleanup_alias_proof_runs
                    SET checked_count = candidate_total,
                        verified_alias_orphan_count = 20000,
                        protected_count = 100,
                        rejected_count = 1800,
                        failed_count = 75
                    WHERE proof_id = %s
                    """,
                    (run['proof_id'],),
                )
        self.assertEqual(assigned, 21975)
        final = person_cleanup_db.complete_alias_proof_run(run['proof_id'], self.generation)
        self.assertEqual(final['checked_count'], 21975)
        self.assertEqual(final['pending_count'], 0)
        self.assertEqual(sum(row['count'] for row in person_cleanup_db.get_alias_proof_summary(run['proof_id'])['states']), 21975)
        # The 27 people_unavailable candidates are intentionally not part of this proof run.
        self.assertEqual(22002 - final['candidate_total'], 27)


if __name__ == '__main__':
    unittest.main()
