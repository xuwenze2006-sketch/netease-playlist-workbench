"""Behavioral tests for deterministic, cache-only organizing plans."""

import copy
import unittest

try:
    from netease_organizer.planning import build_plan
except ModuleNotFoundError as error:
    if error.name not in ("netease_organizer", "netease_organizer.planning"):
        raise
    build_plan = None


def playlist(ident, name, *, owned=True, special_type=0, members=(), complete=True):
    return {
        "id": str(ident), "name": name, "owned": owned,
        "special_type": special_type, "members": list(members),
        "membership_available": True, "snapshot_complete": complete,
        "overview_track_count": len(members), "issues": [],
    }


def song(ident, *artists):
    return {
        "id": str(ident), "name": f"歌曲 {ident}", "metadata_available": True,
        "artists": [{"id": str(aid), "name": name} for aid, name in artists],
        "album": {}, "duration_ms": None,
    }


def snapshot(playlists=(), tracks=None):
    return {
        "owner_id": "7",
        "source": {"kind": "local_desktop_cache", "online_account_verified": False, "overview_complete": True},
        "playlists": list(playlists), "tracks": dict(tracks or {}),
    }


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(build_plan, "build_plan has not been implemented")

    @staticmethod
    def jobs(plan, kind):
        return [job for job in plan["jobs"] if job["kind"] == kind]

    def test_renames_only_selected_owned_non_system_names(self):
        data = snapshot([
            playlist(100, "funk", special_type=5),
            playlist(101, "2026.4.23"), playlist(102, "funk"),
            playlist(103, "我喜欢的音乐-陶喆"), playlist(104, "Mill"),
            playlist(105, "已有歌单保持"), playlist(201, "funk", owned=False),
        ])
        plan = build_plan(data)
        renamed = {j["playlist_id"]: (j["old_name"], j["name"]) for j in self.jobs(plan, "rename_playlist")}
        self.assertEqual(renamed, {
            "101": ("2026.4.23", "2026-04-23"),
            "102": ("funk", "Funk"),
            "103": ("我喜欢的音乐-陶喆", "陶喆 · 喜欢的音乐"),
        })
        for job in self.jobs(plan, "reorder_playlists"):
            self.assertNotIn("100", job["playlist_ids"])
            self.assertNotIn("201", job["playlist_ids"])

    def test_valid_dot_dates_are_normalized_and_invalid_dates_remain(self):
        plan = build_plan(snapshot([
            playlist(1, "2026.4.23"), playlist(2, "2026.04.03"),
            playlist(3, "2026.2.30"), playlist(4, "旅行2026.4.23"),
        ]))
        self.assertEqual({j["playlist_id"]: j["name"] for j in self.jobs(plan, "rename_playlist")}, {
            "1": "2026-04-23", "2": "2026-04-03",
        })

    def test_reorder_groups_known_names_and_appends_unknown_in_original_order(self):
        names = [
            "未识别甲", "Mill", "韩语", "2026.4.23", "英语", "funk", "铃声", "日语",
            "用音乐找回过去的自己", "纯音乐", "中文歌", "俄语", "华语乐坛",
            "我喜欢的音乐-陶喆", "老音乐", "未识别乙",
        ]
        plan = build_plan(snapshot([playlist(i + 1, name) for i, name in enumerate(names)]))
        reorder = self.jobs(plan, "reorder_playlists")
        self.assertEqual(len(reorder), 1)
        self.assertEqual(reorder[0]["playlist_ids"], [
            "5", "8", "12", "3", "11", "13", "15", "9", "14", "6", "10", "7", "2", "4", "1", "16",
        ])

    def test_already_grouped_playlists_do_not_create_noop_reorder_job(self):
        plan = build_plan(snapshot([playlist(1, "英语"), playlist(2, "Funk"), playlist(3, "未知")]))
        self.assertEqual(self.jobs(plan, "reorder_playlists"), [])

    def test_artist_ranking_has_eight_song_threshold_and_five_job_cap(self):
        tracks, members = {}, []
        next_id = 1
        for artist_id, count in [(10, 10), (20, 9), (30, 8), (40, 8), (50, 8), (60, 8), (70, 7)]:
            for _ in range(count):
                tid = str(next_id)
                tracks[tid] = song(tid, (artist_id, f"歌手{artist_id}"))
                members.append(tid)
                next_id += 1
        members.append("1")
        tracks["1"]["artists"].append({"id": "10", "name": "歌手10"})
        plan = build_plan(snapshot([playlist(100, "我的喜欢", special_type=5, members=members)], tracks))
        drafts = self.jobs(plan, "create_artist_playlist")
        self.assertEqual([j["artist"]["id"] for j in drafts], ["10", "20", "30", "40", "50"])
        self.assertEqual([len(j["candidate_track_ids"]) for j in drafts], [10, 9, 8, 8, 8])

    def test_collaborations_count_for_each_artist_and_preserve_liked_song_order(self):
        members = ["8", "2", "6", "4", "1", "7", "3", "5", "8"]
        tracks = {str(i): song(i, (10, "歌手甲"), (20, "歌手乙"), (10, "歌手甲")) for i in range(1, 9)}
        plan = build_plan(snapshot([playlist(100, "我的喜欢", special_type=5, members=members)], tracks))
        drafts = self.jobs(plan, "create_artist_playlist")
        self.assertEqual([j["artist"]["id"] for j in drafts], ["10", "20"])
        for draft in drafts:
            self.assertEqual(draft["candidate_track_ids"], ["8", "2", "6", "4", "1", "7", "3", "5"])

    def test_same_name_artists_keep_separate_ids_and_disambiguated_draft_names(self):
        tracks = {str(i): song(i, (10, "同名歌手"), (20, "同名歌手")) for i in range(1, 9)}
        data = snapshot([playlist(100, "我的喜欢", special_type=5, members=list(tracks))], tracks)
        drafts = self.jobs(build_plan(data), "create_artist_playlist")
        self.assertEqual([j["artist"]["id"] for j in drafts], ["10", "20"])
        self.assertNotEqual(drafts[0]["name"], drafts[1]["name"])
        for draft in drafts:
            self.assertIn("同名歌手", draft["name"])
            self.assertIn(draft["artist"]["id"], draft["name"])

    def test_incomplete_liked_cache_keeps_known_candidate_and_lists_nine_unknown_ids(self):
        tracks = {str(i): song(i, (10, "歌手甲")) for i in range(1, 9)}
        tracks["9"] = {"id": "9", "name": "缺歌手", "metadata_available": False, "artists": []}
        members = [str(i) for i in range(1, 18)]
        data = snapshot([playlist(100, "我的喜欢", special_type=5, members=members, complete=False)], tracks)
        plan = build_plan(data)
        self.assertFalse(plan["summary"]["liked_snapshot_complete"])
        self.assertEqual(plan["summary"]["missing_metadata_track_ids"], [str(i) for i in range(9, 18)])
        self.assertIn("liked_snapshot_incomplete", plan["blocked_reasons"])
        self.assertEqual(self.jobs(plan, "create_artist_playlist")[0]["candidate_track_ids"], [str(i) for i in range(1, 9)])

    def test_unavailable_liked_membership_does_not_block_unrelated_rename(self):
        liked = playlist(100, "我的喜欢", special_type=5, complete=False)
        liked["membership_available"] = False
        plan = build_plan(snapshot([liked, playlist(101, "funk")]))
        self.assertEqual(self.jobs(plan, "create_artist_playlist"), [])
        self.assertEqual(self.jobs(plan, "rename_playlist")[0]["name"], "Funk")
        self.assertIn("liked_membership_unavailable", plan["blocked_reasons"])

    def test_plan_remains_offline_private_intent_with_raw_ids_and_pending_jobs(self):
        tracks = {str(i): song(i, (10, "歌手甲")) for i in range(1, 9)}
        data = snapshot([playlist(100, "我的喜欢", special_type=5, members=list(tracks)), playlist(101, "funk")], tracks)
        data["source"]["online_account_verified"] = True
        plan = build_plan(data)
        self.assertEqual(plan["owner_id"], "7")
        self.assertFalse(plan["source"]["online_account_verified"])
        self.assertFalse(plan["online_account_verified"])
        self.assertFalse(plan["applied_to_account"])
        self.assertIn("online_validation_required", plan["blocked_reasons"])
        self.assertIn("official_track_id_mapping_required", plan["blocked_reasons"])
        for job in plan["jobs"]:
            self.assertEqual(job["status"], "pending_online_validation")
        draft = self.jobs(plan, "create_artist_playlist")[0]
        self.assertEqual(draft["name"], "歌手甲 · 红心精选")
        self.assertEqual(draft["intended_visibility"], "private")
        self.assertEqual(draft["track_id_format"], "raw_decimal")
        self.assertTrue(draft["official_track_id_mapping_required"])
        self.assertNotIn("official_track_ids", draft)
        self.assertEqual(draft["candidate_track_ids"], [str(i) for i in range(1, 9)])

    def test_plan_is_deterministic_and_does_not_mutate_snapshot_or_share_source(self):
        data = snapshot([playlist(100, "我的喜欢", special_type=5), playlist(101, "funk")])
        before = copy.deepcopy(data)
        first, second = build_plan(data), build_plan(data)
        self.assertEqual(first, second)
        self.assertEqual(data, before)
        first["source"]["overview_complete"] = False
        self.assertTrue(data["source"]["overview_complete"])
        self.assertEqual(first["summary"]["job_count"], len(first["jobs"]))


if __name__ == "__main__":
    unittest.main()
