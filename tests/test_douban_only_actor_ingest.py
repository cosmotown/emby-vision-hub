import unittest
import os
import tempfile
from services.person_write_safety import PersonOperationJournal
from unittest.mock import MagicMock, patch

from core_processor import MediaProcessor
from database.actor_db import ActorDBManager


class DoubanOnlyActorIngestTests(unittest.TestCase):
    def test_database_accepts_douban_identity_without_fake_tmdb_id(self):
        with patch('database.actor_db.logger.trace', create=True):
            manager = ActorDBManager()
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            None,
            {'map_id': 7, 'action': 'INSERTED'},
        ]

        map_id, action = manager.upsert_person(
            cursor,
            {
                'id': None,
                'douban_id': '12345',
                'name': '豆瓣独有演员',
                'profile_path': 'https://img1.doubanio.com/p12345.jpg',
            },
            {'url': 'http://emby.invalid', 'api_key': 'hidden', 'user_id': 'u1'},
        )

        self.assertEqual((map_id, action), (7, 'INSERTED'))
        executed_sql = '\n'.join(call.args[0] for call in cursor.execute.call_args_list)
        self.assertIn('ON CONFLICT (douban_celebrity_id)', executed_sql)
        self.assertNotIn("'None'", executed_sql)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, APP_DATA_DIR=self.temp.name)
        env.start()
        self.addCleanup(env.stop)

    def processor(self):
        processor = MediaProcessor.__new__(MediaProcessor)
        processor.config = {}
        processor.emby_url = 'http://emby.invalid'
        processor.emby_api_key = 'hidden'
        processor.emby_user_id = 'u1'
        processor.actor_db_manager = MagicMock()
        processor.actor_db_manager.upsert_person.return_value = (9, 'UPDATED')
        return processor

    def actor(self):
        return {'douban_id': '5678', 'name': 'Different display name', 'character': 'Role'}

    def person(self):
        return {'Id': 'person-1', 'Name': 'Existing', 'Type': 'Person',
                'ProviderIds': {'Douban': '5678'}}

    def test_existing_exact_identity_reused_without_rename_or_creation(self):
        processor = self.processor()
        person = self.person()
        with patch('core_processor.emby.resolve_exact_person_identity', return_value=[person]), \
             patch('core_processor.emby.get_emby_item_details', side_effect=[
                 person, {'Id': 'm', 'Type': 'Movie', 'People': []},
                 {'Id': 'm', 'People': [{'Id': 'person-1'}]},
             ]), patch('core_processor.emby.update_emby_item_details', return_value=True) as update, \
             patch('core_processor.emby.update_person_details') as rename:
            self.assertEqual(processor._sync_douban_only_cast_to_emby('m', [self.actor()], MagicMock()), 1)
        self.assertEqual(update.call_count, 1)
        self.assertEqual(update.call_args.args[1]['People'][0]['Id'], 'person-1')
        rename.assert_not_called()

    def test_absent_person_is_durably_quarantined_without_name_create(self):
        processor = self.processor()
        with patch('core_processor.emby.resolve_exact_person_identity', return_value=[]) as resolve, \
             patch('core_processor.emby.update_emby_item_details') as update, \
             patch('core_processor.emby.update_person_details') as person_write:
            for _ in range(2):
                self.assertEqual(processor._sync_douban_only_cast_to_emby('m', [self.actor()], MagicMock()), 0)
        resolve.assert_called_once()
        update.assert_not_called()
        person_write.assert_not_called()
        state = PersonOperationJournal(processor.emby_url, 'douban', '5678').read()
        self.assertEqual(state['failure_stage'], 'name_only_create_blocked')
        self.assertIsNone(state['created_person_id'])
        self.assertTrue(state['operation_id'])

    def test_failed_attach_is_not_detached_or_replayed(self):
        processor = self.processor()
        person = self.person()
        with patch('core_processor.emby.resolve_exact_person_identity', return_value=[person]), \
             patch('core_processor.emby.get_emby_item_details', side_effect=[
                 person, {'Id': 'm', 'Type': 'Movie', 'People': [{'Id': 'existing'}]},
             ]), patch('core_processor.emby.update_emby_item_details', return_value=False) as update:
            self.assertEqual(processor._sync_douban_only_cast_to_emby('m', [self.actor()], MagicMock()), 0)
            self.assertEqual(processor._sync_douban_only_cast_to_emby('m', [self.actor()], MagicMock()), 0)
        update.assert_called_once()
        self.assertEqual(update.call_args.args[1]['People'][0]['Id'], 'existing')
        state = PersonOperationJournal(processor.emby_url, 'douban', '5678').read()
        self.assertEqual(state['failure_stage'], 'association_result_unknown')
        self.assertEqual(state['target_person_id'], 'person-1')

    def test_ambiguous_provider_blocks_all_writes(self):
        processor = self.processor()
        people = [self.person(), dict(self.person(), Id='second')]
        with patch('core_processor.emby.resolve_exact_person_identity', return_value=people), \
             patch('core_processor.emby.update_emby_item_details') as update:
            self.assertEqual(processor._sync_douban_only_cast_to_emby('m', [self.actor()], MagicMock()), 0)
        update.assert_not_called()

    def test_two_workers_cannot_create_duplicate(self):
        from concurrent.futures import ThreadPoolExecutor
        processor = self.processor()
        with patch('core_processor.emby.resolve_exact_person_identity', return_value=[]) as resolve, \
             patch('core_processor.emby.update_emby_item_details') as update:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: processor._sync_douban_only_cast_to_emby(
                    'm', [self.actor()], MagicMock()), range(2)))
        self.assertEqual(results, [0, 0])
        resolve.assert_called_once()
        update.assert_not_called()  # Unsupported creation is zero, not a claimed get-or-create.


if __name__ == '__main__':
    unittest.main()
