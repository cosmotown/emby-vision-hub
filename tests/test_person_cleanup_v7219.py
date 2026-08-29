import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import config_manager  # Initialize settings before database.connection is imported.
from tasks import actors


class ReadonlyAliasScanTests(unittest.TestCase):
    @staticmethod
    def processor(stop_requested=lambda: False):
        return SimpleNamespace(
            emby_url='http://emby',
            emby_api_key='secret',
            emby_user_id='user',
            is_stop_requested=stop_requested,
        )

    def test_candidate_check_uses_get_only_helper_with_serial_detail_fallback(self):
        result = {
            'status': 'identity_alias_only',
            'count': 0,
            'query_count': 1,
        }
        candidate = {
            'person_id': '1020094',
            'person_name': '1田中',
        }
        with patch.object(
            actors.emby, 'get_person_media_references', return_value=result,
        ) as helper:
            references, status = actors._check_readonly_alias_candidate(
                self.processor(), candidate, {'complete': True},
            )

        self.assertIs(references, result)
        self.assertEqual(status, 'identity_alias_only')
        helper.assert_called_once_with(
            'http://emby',
            'secret',
            '1020094',
            limit=1,
            person_name='1田中',
            protected_root_contract={'complete': True},
            user_id='user',
            detail_workers=1,
        )

    def test_production_style_aliases_are_persisted_and_removed(self):
        candidates = [
            {'person_id': '1020094', 'person_name': '1田中', 'provider_ids_json': {}},
            {'person_id': '619579', 'person_name': '2◎ゆうか', 'provider_ids_json': {}},
            {'person_id': '619697', 'person_name': '----', 'provider_ids_json': {}},
        ]
        references = {
            '1020094': ({
                'status': 'protected_library_alias',
                'protected_library_id': 'protected',
                'evidence_item_id': 'media-a',
            }, 'protected_library_alias'),
            '619579': ({
                'status': 'protected_library_alias',
                'protected_library_id': 'protected',
                'evidence_item_id': 'media-b',
            }, 'protected_library_alias'),
            '619697': ({
                'status': 'protected_library_unverifiable',
                'protected_library_id': 'protected',
                'evidence_item_id': 'media-c',
            }, 'protected_library_unverifiable'),
        }
        claimed = [False]
        counters = {'checked_count': 0, 'protected_count': 0}
        finished = []

        def claim(_scan_id, limit):
            self.assertEqual(limit, 4)
            if claimed[0]:
                return []
            claimed[0] = True
            return candidates

        def finish(_scan_id, candidate, outcome, **kwargs):
            finished.append((candidate['person_id'], outcome, kwargs))
            counters['checked_count'] += 1
            counters['protected_count'] += int(outcome.startswith('protected_library_'))
            return {
                'candidate_total': 3,
                **counters,
            }

        with patch.object(
            actors.person_cleanup_db, 'claim_readonly_alias_candidates', side_effect=claim,
        ), patch.object(
            actors.person_cleanup_db, 'finish_readonly_alias_candidate', side_effect=finish,
        ), patch.object(
            actors.person_cleanup_db, 'complete_readonly_scan', return_value={
                'state': 'completed', 'candidate_total': 3,
                'checked_count': 3, 'protected_count': 3,
            },
        ), patch.object(
            actors, '_check_readonly_alias_candidate',
            side_effect=lambda _processor, candidate, _contract: references[candidate['person_id']],
        ) as reference_check, patch.object(
            actors.task_manager, 'update_status_from_thread',
        ):
            result = actors._run_readonly_alias_scan(
                self.processor(), 'scan-1', {'complete': True},
            )

        self.assertEqual(result['state'], 'completed')
        self.assertEqual(reference_check.call_count, 3)
        self.assertEqual(
            {person_id: outcome for person_id, outcome, _kwargs in finished},
            {
                '1020094': 'protected_library_alias',
                '619579': 'protected_library_alias',
                '619697': 'protected_library_unverifiable',
            },
        )
        self.assertTrue(all(item[2]['library_id'] == 'protected' for item in finished))

    def test_worker_concurrency_is_bounded_to_four(self):
        candidates = [
            {'person_id': str(index), 'person_name': f'p{index}', 'provider_ids_json': {}}
            for index in range(12)
        ]
        queue = list(candidates)
        lock = threading.Lock()
        active = 0
        maximum = 0
        checked = 0

        def claim(_scan_id, limit):
            batch = queue[:limit]
            del queue[:limit]
            return batch

        def check(_processor, _candidate, _contract):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return {'status': 'orphan'}, 'orphan'

        def finish(_scan_id, _candidate, _outcome, **_kwargs):
            nonlocal checked
            checked += 1
            return {
                'candidate_total': len(candidates),
                'checked_count': checked,
                'protected_count': 0,
            }

        with patch.object(
            actors.person_cleanup_db, 'claim_readonly_alias_candidates', side_effect=claim,
        ), patch.object(
            actors.person_cleanup_db, 'finish_readonly_alias_candidate', side_effect=finish,
        ), patch.object(
            actors.person_cleanup_db, 'complete_readonly_scan', return_value={
                'state': 'completed', 'candidate_total': len(candidates),
                'checked_count': len(candidates), 'protected_count': 0,
            },
        ), patch.object(
            actors, '_check_readonly_alias_candidate', side_effect=check,
        ), patch.object(
            actors.task_manager, 'update_status_from_thread',
        ):
            actors._run_readonly_alias_scan(
                self.processor(), 'scan-2', {'complete': True},
            )

        self.assertEqual(checked, len(candidates))
        self.assertGreater(maximum, 1)
        self.assertLessEqual(maximum, 4)

    def test_stop_preserves_unstarted_candidates_for_resume(self):
        candidates = [
            {'person_id': str(index), 'person_name': f'p{index}', 'provider_ids_json': {}}
            for index in range(8)
        ]
        queue = list(candidates)
        stop = {'requested': False}
        checked = []

        def claim(_scan_id, limit):
            batch = queue[:limit]
            del queue[:limit]
            return batch

        def finish(_scan_id, candidate, _outcome, **_kwargs):
            checked.append(candidate['person_id'])
            if len(checked) == 4:
                stop['requested'] = True
            return {
                'candidate_total': 8,
                'checked_count': len(checked),
                'protected_count': 0,
            }

        with patch.object(
            actors.person_cleanup_db, 'claim_readonly_alias_candidates', side_effect=claim,
        ), patch.object(
            actors.person_cleanup_db, 'finish_readonly_alias_candidate', side_effect=finish,
        ), patch.object(
            actors.person_cleanup_db, 'stop_readonly_scan',
        ) as stop_scan, patch.object(
            actors.person_cleanup_db, 'get_readonly_scan', return_value={
                'state': 'stopped', 'candidate_total': 8,
                'checked_count': 4, 'pending_count': 4,
            },
        ), patch.object(
            actors, '_check_readonly_alias_candidate',
            return_value=({'status': 'orphan'}, 'orphan'),
        ), patch.object(
            actors.task_manager, 'update_status_from_thread',
        ):
            result = actors._run_readonly_alias_scan(
                self.processor(lambda: stop['requested']),
                'scan-3', {'complete': True},
            )

        self.assertEqual(result['state'], 'stopped')
        self.assertEqual(len(checked), 4)
        self.assertEqual(len(queue), 4)
        stop_scan.assert_called_once_with('scan-3')


if __name__ == '__main__':
    unittest.main()
