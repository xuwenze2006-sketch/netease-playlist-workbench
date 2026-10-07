import unittest

from netease_organizer.online_planning import build_online_plan


def live_snapshot():
    return {
        'complete': True, 'account': {'id': 'A' * 32, 'original_id': '7', 'nickname': '用户'},
        'playlists': [
            {'id': 'B' * 32, 'original_id': '11', 'name': 'funk', 'track_count': 1, 'special_type': 0},
            {'id': 'C' * 32, 'original_id': '12', 'name': '喜欢', 'track_count': 8, 'special_type': 5},
        ],
        'liked': {'id': 'C' * 32, 'original_id': '12', 'name': '喜欢', 'track_count': 8, 'special_type': 5,
                  'tracks': [{'id': f'{i:032X}', 'original_id': str(i), 'name': f'歌曲{i}',
                              'artists': [{'id': 'D' * 32, 'original_id': '9', 'name': '歌手'}]}
                             for i in range(1, 9)]},
    }


class OnlinePlanningTests(unittest.TestCase):
    def test_official_ids_are_bound_but_private_creation_remains_blocked(self):
        plan = build_online_plan(live_snapshot())
        rename = next(j for j in plan['jobs'] if j['kind'] == 'rename_playlist')
        artist = next(j for j in plan['jobs'] if j['kind'] == 'create_artist_playlist')
        self.assertEqual(rename['playlist_id'], 'B' * 32)
        self.assertEqual(rename['original_playlist_id'], '11')
        self.assertEqual(rename['status'], 'ready')
        self.assertEqual(artist['candidate_track_ids'], [f'{i:032X}' for i in range(1, 9)])
        self.assertFalse(artist['official_track_id_mapping_required'])
        self.assertEqual(artist['status'], 'blocked')
        self.assertEqual(artist['blocked_reason'], 'private_playlist_creation_unavailable')
        self.assertTrue(plan['online_account_verified'])
        self.assertFalse(plan['applied_to_account'])

    def test_song_reorder_is_not_used_for_playlist_list_order(self):
        snap = live_snapshot()
        snap['playlists'].insert(0, {'id': 'E' * 32, 'original_id': '13', 'name': 'Mill',
                                    'track_count': 1, 'special_type': 0})
        plan = build_online_plan(snap)
        reorder = next(j for j in plan['jobs'] if j['kind'] == 'reorder_playlists')
        self.assertEqual(reorder['status'], 'blocked')
        self.assertEqual(reorder['blocked_reason'], 'playlist_list_order_unsupported')
        self.assertEqual(reorder['playlist_ids'], ['B' * 32, 'E' * 32])

    def test_incomplete_online_snapshot_never_becomes_executable(self):
        snap = live_snapshot()
        snap['complete'] = False
        with self.assertRaises(ValueError):
            build_online_plan(snap)

    def test_complete_overview_keeps_independent_renames_ready_when_songs_are_filtered(self):
        snap = live_snapshot()
        snap.update(complete=False, overview_complete=True)
        snap['liked']['track_count'] = 10
        plan = build_online_plan(snap)
        self.assertEqual(plan['summary']['ready_count'], 1)
        self.assertEqual(plan['summary']['online_missing_record_count'], 2)
        self.assertIn('liked_snapshot_incomplete', plan['blocked_reasons'])
        artist = next(j for j in plan['jobs'] if j['kind'] == 'create_artist_playlist')
        self.assertFalse(artist['liked_snapshot_complete'])
        self.assertEqual(artist['status'], 'blocked')


if __name__ == '__main__':
    unittest.main()
