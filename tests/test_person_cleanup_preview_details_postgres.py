import logging
import os
import unittest

import config_manager
import constants
from database import person_cleanup_db
from database.connection import get_db_connection, init_db


POSTGRES_HOST = os.environ.get('EVH_TEST_POSTGRES_HOST')
if not hasattr(logging.Logger, 'trace'):
    logging.Logger.trace = logging.Logger.debug


@unittest.skipUnless(POSTGRES_HOST, 'isolated PostgreSQL is not configured')
class PersonCleanupPreviewDetailsPostgresTests(unittest.TestCase):
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
                        person_cleanup_jobs
                    CASCADE
                """)

    def test_historical_preview_grouping_keeps_22714_persisted_items(self):
        distribution = {
            'verified_orphan': 712,
            'identity_alias_only': 18000,
            'people_unavailable': 2500,
            'invalid_response': 600,
            'connection_failed': 300,
            'linked': 200,
            'protected_library_alias': 150,
            'protected_library_unverifiable': 100,
            'protected_id': 50,
            'protected_name': 40,
            'protected_provider_identity': 30,
            'future_unknown_state': 32,
        }
        self.assertEqual(sum(distribution.values()), 22714)
        self.assertEqual(sum(distribution.values()) - distribution['verified_orphan'], 22002)

        job_id = 'historical-preview-22714'
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO person_cleanup_jobs (
                        job_id, state, candidate_total,
                        verification_failed_count, verified_orphan_count,
                        created_at
                    )
                    VALUES (%s, 'completed', 22714, 22002, 712,
                            '2026-08-28T00:00:00+08:00')
                    """,
                    (job_id,),
                )
                for status, count in distribution.items():
                    cursor.execute(
                        """
                        INSERT INTO person_cleanup_job_items (
                            job_id, person_id, person_name, provider_ids_json,
                            candidate_fingerprint, preview_state, execute_state,
                            last_error
                        )
                        SELECT
                            %s,
                            %s || '-' || item_number::TEXT,
                            %s || ' sample ' || item_number::TEXT,
                            CASE WHEN item_number = 1
                                 THEN jsonb_build_object('Tmdb', item_number::TEXT)
                                 ELSE '{}'::jsonb END,
                            md5(%s || item_number::TEXT),
                            %s,
                            CASE WHEN %s = 'verified_orphan'
                                 THEN 'deleted' ELSE 'pending' END,
                            'persisted reason: ' || %s
                        FROM generate_series(1, %s) AS item_number
                        """,
                        (
                            job_id, status, status, status, status,
                            status, status, count,
                        ),
                    )
                cursor.execute(
                    """
                    SELECT candidate_total, verified_orphan_count,
                           verification_failed_count, updated_at
                    FROM person_cleanup_jobs
                    WHERE job_id = %s
                    """,
                    (job_id,),
                )
                before_job = dict(cursor.fetchone())
                cursor.execute(
                    """
                    SELECT COUNT(*)::INTEGER AS count
                    FROM person_cleanup_job_items
                    WHERE job_id = %s
                    """,
                    (job_id,),
                )
                before_items = int(cursor.fetchone()['count'])

        summary = person_cleanup_db.get_cleanup_job_preview_summary(job_id)
        self.assertEqual(summary['candidate_total'], 22714)
        self.assertEqual(summary['items_total'], 22714)
        self.assertEqual(summary['verified_orphan'], 712)
        self.assertEqual(summary['non_verified_orphan'], 22002)
        self.assertEqual(sum(summary['counts'].values()), 22714)
        self.assertEqual(summary['counts'], distribution)
        self.assertEqual(summary['counts']['future_unknown_state'], 32)
        self.assertEqual(
            next(row['percentage'] for row in summary['states']
                 if row['status'] == 'verified_orphan'),
            3.13,
        )
        self.assertTrue(summary['consistent'])
        self.assertIsNone(summary['consistency_warning'])

        samples = person_cleanup_db.list_cleanup_job_preview_items(
            job_id, 'identity_alias_only', page=2, page_size=5,
        )
        self.assertEqual(samples['total'], 18000)
        self.assertEqual(len(samples['items']), 5)
        self.assertTrue(all(
            item['preview_state'] == 'identity_alias_only'
            for item in samples['items']
        ))
        first_page = person_cleanup_db.list_cleanup_job_preview_items(
            job_id, 'identity_alias_only', page=1, page_size=5,
        )
        self.assertIn('Tmdb', first_page['items'][0]['provider_ids_json'])

        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO person_cleanup_jobs (
                        job_id, state, candidate_total, created_at
                    ) VALUES (
                        'new-preview-22002', 'previewing', 22002,
                        '2026-08-29T00:00:00+08:00'
                    )
                    """
                )
                cursor.execute(
                    """
                    INSERT INTO person_cleanup_job_items (
                        job_id, person_id, person_name, provider_ids_json,
                        candidate_fingerprint, preview_state
                    )
                    SELECT
                        'new-preview-22002',
                        'alias-' || item_number::TEXT,
                        'Alias ' || item_number::TEXT,
                        '{}'::jsonb,
                        md5(item_number::TEXT),
                        'identity_alias_only'
                    FROM generate_series(1, 376) AS item_number
                    """
                )

        running = person_cleanup_db.get_cleanup_job_preview_summary(
            'new-preview-22002'
        )
        self.assertEqual(running['preview_progress_count'], 376)
        self.assertEqual(running['preview_expected_count'], 22002)
        self.assertFalse(running['preview_complete'])
        self.assertTrue(running['consistent'])
        self.assertIsNone(running['consistency_warning'])
        self.assertEqual(running['counts']['identity_alias_only'], 376)
        self.assertEqual(
            next(row['percentage'] for row in running['states']
                 if row['status'] == 'identity_alias_only'),
            1.71,
        )

        history = person_cleanup_db.list_cleanup_jobs(limit=20)
        self.assertEqual(history[0]['job_id'], 'new-preview-22002')
        self.assertIn(job_id, [job['job_id'] for job in history])
        historical = person_cleanup_db.get_cleanup_job_preview_summary(job_id)
        self.assertEqual(historical['candidate_total'], 22714)
        self.assertEqual(historical['verified_orphan'], 712)
        self.assertEqual(historical['non_verified_orphan'], 22002)

        init_db()
        restarted = person_cleanup_db.get_cleanup_job_preview_summary(job_id)
        self.assertEqual(restarted['counts'], distribution)

        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT candidate_total, verified_orphan_count,
                           verification_failed_count, updated_at
                    FROM person_cleanup_jobs
                    WHERE job_id = %s
                    """,
                    (job_id,),
                )
                after_job = dict(cursor.fetchone())
                cursor.execute(
                    """
                    SELECT COUNT(*)::INTEGER AS count
                    FROM person_cleanup_job_items
                    WHERE job_id = %s
                    """,
                    (job_id,),
                )
                after_items = int(cursor.fetchone()['count'])
        self.assertEqual(after_job, before_job)
        self.assertEqual(after_items, before_items)

    def test_stopped_partial_preview_is_progress_not_corruption(self):
        job_id = person_cleanup_db.create_cleanup_job()
        self.assertEqual(
            person_cleanup_db.initialize_cleanup_job_candidate_total(job_id, 22002),
            22002,
        )
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO person_cleanup_job_items (
                        job_id, person_id, person_name, provider_ids_json,
                        candidate_fingerprint, preview_state
                    )
                    SELECT
                        %s,
                        'stopped-' || item_number::TEXT,
                        'Stopped ' || item_number::TEXT,
                        '{}'::jsonb,
                        md5(item_number::TEXT),
                        'identity_alias_only'
                    FROM generate_series(1, 500) AS item_number
                    """,
                    (job_id,),
                )
        self.assertEqual(
            person_cleanup_db.initialize_cleanup_job_candidate_total(job_id, 22002),
            22002,
        )
        with self.assertRaisesRegex(RuntimeError, '候选总数已固定'):
            person_cleanup_db.initialize_cleanup_job_candidate_total(job_id, 22001)
        person_cleanup_db.finish_cleanup_job(job_id, stopped=True)

        job = person_cleanup_db.get_cleanup_job(job_id)
        summary = person_cleanup_db.get_cleanup_job_preview_summary(job_id)
        self.assertEqual(job['state'], 'stopped')
        self.assertEqual(job['candidate_total'], 22002)
        self.assertEqual(summary['preview_progress_count'], 500)
        self.assertEqual(summary['preview_expected_count'], 22002)
        self.assertFalse(summary['preview_complete'])
        self.assertTrue(summary['consistent'])
        self.assertIsNone(summary['consistency_warning'])

    def test_consistency_warning_never_hides_unknown_rows(self):
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO person_cleanup_jobs (job_id, state, candidate_total)
                    VALUES ('inconsistent-job', 'completed', 3)
                    """
                )
                cursor.execute(
                    """
                    INSERT INTO person_cleanup_job_items (
                        job_id, person_id, person_name, provider_ids_json,
                        candidate_fingerprint, preview_state
                    ) VALUES
                        ('inconsistent-job', 'p1', 'P1', '{}', 'f1', 'future-a'),
                        ('inconsistent-job', 'p2', 'P2', '{}', 'f2', 'future-b')
                    """
                )

        summary = person_cleanup_db.get_cleanup_job_preview_summary('inconsistent-job')
        self.assertEqual(summary['counts']['future-a'], 1)
        self.assertEqual(summary['counts']['future-b'], 1)
        self.assertEqual(sum(summary['counts'].values()), 2)
        self.assertEqual(summary['counts']['verified_orphan'], 0)
        self.assertEqual(summary['items_total'], 2)
        self.assertFalse(summary['consistent'])
        self.assertIn('job_items=2', summary['consistency_warning'])
        self.assertIn('candidate_total=3', summary['consistency_warning'])


if __name__ == '__main__':
    unittest.main()
