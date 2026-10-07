import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from netease_organizer import web_state
from netease_organizer.service import _APPROVED_ARTIST_COUNTS


class PlaylistBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        (self.project/'artifacts').mkdir()
        self.live = {'account': {'id': 'A'*32, 'original_id': '42', 'nickname': '用户甲'},
                     'playlists': [{'id': 'B'*32, 'original_id': '7', 'name': 'Funk',
                                    'track_count': 10, 'special_type': 0}], 'liked': None}

    def write(self, name, value, timestamp):
        path = self.project/'artifacts'/name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        os.utime(path, (timestamp, timestamp))
        return path

    def artists(self):
        items = [{'name': name, 'playlist_id': f'{1000+i:032X}', 'original_playlist_id': str(1000+i),
                  'status': 'completed', 'count': count, 'expected_count': count}
                 for i, (name, count) in enumerate(_APPROVED_ARTIST_COUNTS.items())]
        self.write('歌手精选执行结果.json', {'status': 'completed', 'completed_count': 5,
            'outcome_known': True, 'applied_to_account': True, 'items': items}, 200)
        self.write('歌手精选执行进度.json', {'kind': 'artist_execution_journal', 'account_original_id': '42'}, 200)
        return items

    def read(self, key):
        return web_state.load_playlist_binding(self.project, key)

    def test_owned_directory_binding_retains_only_backend_ids_and_same_public_row(self):
        self.write('在线名称整理快照.json', self.live, 100)
        binding = self.read('7')
        self.assertEqual(binding['account'], {'id': 'A'*32, 'original_id': '42'})
        self.assertEqual(binding['playlist'], {'id': 'B'*32, 'original_id': '7'})
        self.assertEqual(binding['signatures'], web_state.directory_signatures(self.project))
        state = web_state.build_local_state(self.project)
        self.assertEqual(state['playlists'][0]['key'], '7')
        self.assertNotIn('B'*32, json.dumps(state))
        self.assertNotIn('_owner', state)

    def test_newer_confirmed_receipt_can_supply_exact_binding_but_not_resurrect_deleted_row(self):
        self.write('在线名称整理快照.json', self.live, 100)
        item = self.artists()[0]
        self.assertEqual(self.read(item['original_playlist_id'])['playlist'],
                         {'id': item['playlist_id'], 'original_id': item['original_playlist_id']})
        self.write('在线名称整理快照.json', self.live, 300)
        self.assertIsNone(self.read(item['original_playlist_id']))

    def test_conflicting_receipt_or_wrong_owner_does_not_supply_identity(self):
        self.write('在线名称整理快照.json', self.live, 100)
        item = self.artists()[0]
        self.write('歌手精选执行进度.json', {'kind': 'artist_execution_journal', 'account_original_id': '999'}, 200)
        self.assertIsNone(self.read(item['original_playlist_id']))
        self.write('歌手精选执行进度.json', {'kind': 'artist_execution_journal', 'account_original_id': '42'}, 200)
        self.live['playlists'][0]['original_id'] = item['original_playlist_id']
        self.write('在线名称整理快照.json', self.live, 100)
        self.assertEqual(self.read(item['original_playlist_id'])['playlist']['id'], 'B'*32)

    def test_source_changes_during_projection_do_not_bind_old_ids_to_new_directory(self):
        self.write('在线名称整理快照.json', self.live, 100)
        original = web_state._project_directory
        def switch(project, timestamp, snapshot):
            self.live['account'].update(id='E'*32, original_id='999')
            self.write('在线名称整理快照.json', self.live, 300)
            return original(project, timestamp, snapshot)
        with patch.object(web_state, '_project_directory', side_effect=switch):
            self.assertIsNone(self.read('7'))

    def test_invalid_or_missing_keys_do_not_guess_identity_by_name(self):
        self.assertIsNone(self.read('7'))
        self.write('在线名称整理快照.json', self.live, 100)
        for key in ('Funk', '../7', '07', '0', '1'*21, 7, True):
            with self.subTest(key=key):
                self.assertIsNone(self.read(key))
        self.assertIsNone(self.read('8'))
