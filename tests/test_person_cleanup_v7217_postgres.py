import os
import logging
import unittest

import config_manager
import constants
from database import person_cleanup_db
from database.connection import get_db_connection, init_db
from services.person_cleanup_safety import candidate_fingerprint


POSTGRES_HOST = os.environ.get('EVH_TEST_POSTGRES_HOST')
if not hasattr(logging.Logger, 'trace'):
    logging.Logger.trace = logging.Logger.debug


@unittest.skipUnless(POSTGRES_HOST, 'isolated PostgreSQL is not configured')
class PersonCleanupV7217PostgresTests(unittest.TestCase):
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
                        person_cleanup_job_items,
                        person_cleanup_jobs,
                        person_cleanup_delete_attempts,
                        person_cleanup_candidates,
                        person_cleanup_protected_identities,
                        person_cleanup_protected_names,
                        person_cleanup_protected_people,
                        person_cleanup_protected_libraries,
                        person_cleanup_protection_state
                    CASCADE
                """)
                cursor.execute("""
                    INSERT INTO person_cleanup_protection_state (singleton)
                    VALUES (TRUE)
                """)

    def _ready_snapshot(self):
        person_cleanup_db.replace_protected_libraries([
            {'library_id': 'lib-protected', 'library_name': '保护库'},
        ])
        generation = person_cleanup_db.begin_protection_snapshot()
        people = [{
            'person_id': 'protected-person',
            'person_name': '保护人物',
            'provider_ids': {'Tmdb': '100', 'Imdb': 'nm0100', 'Douban': '200'},
        }]
        person_cleanup_db.merge_protected_people_for_library('lib-protected', people)
        person_cleanup_db.merge_protected_names_for_library('lib-protected', ['保护人物'])
        person_cleanup_db.merge_protected_identities_for_library('lib-protected', people)
        person_cleanup_db.complete_protection_snapshot(generation)
        return generation

    def test_snapshot_readiness_and_provider_identity_contract(self):
        person_cleanup_db.replace_protected_libraries([
            {'library_id': 'lib-protected', 'library_name': '保护库'},
        ])
        with self.assertRaisesRegex(RuntimeError, '未就绪'):
            person_cleanup_db.require_ready_protection_snapshot()

        generation = person_cleanup_db.begin_protection_snapshot()
        people = [{
            'person_id': 'p1',
            'person_name': '人物甲',
            'provider_ids': {'Tmdb': '00123', 'Imdb': 'NM0042', 'Douban': '0007'},
        }]
        person_cleanup_db.merge_protected_people_for_library('lib-protected', people)
        person_cleanup_db.merge_protected_names_for_library('lib-protected', ['人物甲'])
        person_cleanup_db.merge_protected_identities_for_library('lib-protected', people)
        person_cleanup_db.complete_protection_snapshot(generation)

        contract = person_cleanup_db.get_protection_contract()
        self.assertEqual(contract['generation'], generation)
        self.assertIn('p1', contract['person_ids'])
        self.assertIn('人物甲'.casefold(), contract['name_keys'])
        self.assertEqual(
            contract['provider_identities'],
            {('tmdb', '123'), ('imdb', 'nm0042'), ('douban', '7')},
        )
        same_identity = {
            'person_id': 'different-id',
            'person_name': '其他名字',
            'provider_ids_json': {'Tmdb': '123'},
        }
        self.assertEqual(
            person_cleanup_db.candidate_protection_reason(same_identity, contract),
            'protected_provider_identity',
        )

    def test_explicit_verification_status_replaces_legacy_timestamp_inference(self):
        generation = self._ready_snapshot()
        person_cleanup_db.replace_candidates([
            {'Id': 'orphan', 'Name': '孤儿人物', 'ProviderIds': {'Tmdb': '999'}},
            {'Id': 'alias', 'Name': '同身份人物', 'ProviderIds': {'Tmdb': '998'}},
        ])
        person_cleanup_db.mark_candidate_checked('orphan', 'orphan', generation)
        person_cleanup_db.mark_candidate_checked(
            'alias', 'identity_alias_only', generation, '仅命中其他 Person',
        )

        deletable = person_cleanup_db.list_explicit_verified_orphans(['orphan', 'alias'])
        self.assertEqual([row['person_id'] for row in deletable], ['orphan'])
        rows = {row['person_id']: row for row in person_cleanup_db.list_candidates_raw()}
        self.assertEqual(rows['orphan']['verification_status'], 'orphan')
        self.assertEqual(rows['alias']['verification_status'], 'identity_alias_only')

    def test_delete_attempt_is_at_most_once_across_jobs_and_restart(self):
        self._ready_snapshot()
        candidate = {
            'person_id': 'p-delete',
            'person_name': '待删人物',
            'provider_ids_json': {'Tmdb': '999'},
        }
        job1 = person_cleanup_db.create_cleanup_job()
        person_cleanup_db.add_cleanup_job_item(job1, candidate, 'verified_orphan')
        self.assertTrue(person_cleanup_db.mark_cleanup_job_item(
            job1, 'p-delete', 'deleting', submitted=True,
        ))
        person_cleanup_db.fail_cleanup_job(job1, '模拟进程中断')

        job2 = person_cleanup_db.create_cleanup_job()
        person_cleanup_db.add_cleanup_job_item(job2, candidate, 'verified_orphan')
        self.assertFalse(person_cleanup_db.mark_cleanup_job_item(
            job2, 'p-delete', 'deleting', submitted=True,
        ))

        init_db()
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT operation_id, state, post_attempts
                    FROM person_cleanup_delete_attempts
                    WHERE person_id = 'p-delete'
                """)
                attempt = dict(cursor.fetchone())
        self.assertEqual(attempt['operation_id'], f'job:{job1}')
        self.assertEqual(attempt['state'], 'submitting')
        self.assertEqual(attempt['post_attempts'], 1)

    def test_snapshot_refresh_invalidates_previous_orphan_verification(self):
        generation = self._ready_snapshot()
        person_cleanup_db.replace_candidates([
            {'Id': 'p1', 'Name': '人物甲', 'ProviderIds': {'Tmdb': '999'}},
        ])
        person_cleanup_db.mark_candidate_checked('p1', 'orphan', generation)
        before = person_cleanup_db.list_candidates_raw()[0]
        self.assertEqual(before['verification_fingerprint'], candidate_fingerprint(before))

        next_generation = person_cleanup_db.begin_protection_snapshot()
        after = person_cleanup_db.list_candidates_raw()[0]
        self.assertGreater(next_generation, generation)
        self.assertEqual(after['verification_status'], 'unverified')
        self.assertIsNone(after['verification_snapshot_generation'])
        self.assertIsNone(after['verification_fingerprint'])
        self.assertIsNone(after['last_checked_at'])

    def test_persistent_job_requires_preview_token_and_restart_fails_closed(self):
        generation = self._ready_snapshot()
        candidate = {
            'person_id': 'p-job',
            'person_name': '任务人物',
            'provider_ids_json': {'Tmdb': '997'},
        }
        job_id = person_cleanup_db.create_cleanup_job()
        person_cleanup_db.add_cleanup_job_item(job_id, candidate, 'verified_orphan')
        person_cleanup_db.finish_cleanup_preview(job_id, generation)
        token = person_cleanup_db.issue_cleanup_confirmation_token(job_id)
        with self.assertRaisesRegex(RuntimeError, '确认令牌无效'):
            person_cleanup_db.confirm_cleanup_job(job_id, 'wrong-token')
        person_cleanup_db.confirm_cleanup_job(job_id, token)
        person_cleanup_db.start_cleanup_job(job_id)
        self.assertTrue(person_cleanup_db.mark_cleanup_job_item(
            job_id, 'p-job', 'deleting', submitted=True,
        ))

        init_db()
        restarted = person_cleanup_db.get_cleanup_job(job_id, include_items=True)
        self.assertEqual(restarted['state'], 'interrupted_requires_repreview')
        self.assertEqual(restarted['items'][0]['execute_state'], 'deleting')
        self.assertEqual(restarted['items'][0]['post_attempts'], 1)
        self.assertFalse(person_cleanup_db.reserve_person_delete_attempt('p-job'))


if __name__ == '__main__':
    unittest.main()
