"""Regression tests for normalized track metadata completeness."""

import unittest

from netease_bridge.cache import _track


class TrackMetadataTests(unittest.TestCase):
    def test_long_fields_preserve_unicode_and_complete_artist_metadata(self):
        normalized = _track({
            "id": 42,
            "name": "平凡之路 🎵",
            "artists": [{"id": 10, "name": "朴树"}, {"id": "20", "name": "共演者"}],
            "album": {"id": 91, "name": "猎户星座"},
            "duration": 180000,
            "private_extra": "not output",
        })
        self.assertEqual(normalized, {
            "id": "42",
            "name": "平凡之路 🎵",
            "artists": [{"id": "10", "name": "朴树"}, {"id": "20", "name": "共演者"}],
            "album": {"id": "91", "name": "猎户星座"},
            "duration_ms": 180000,
            "metadata_available": True,
        })

    def test_compact_fields_are_normalized_and_complete(self):
        normalized = _track({
            "id": "42",
            "name": "夜に駆ける",
            "ar": [{"id": "10", "name": "YOASOBI"}],
            "al": {"id": "91", "name": "THE BOOK"},
            "dt": 240000,
        })
        self.assertEqual(normalized, {
            "id": "42",
            "name": "夜に駆ける",
            "artists": [{"id": "10", "name": "YOASOBI"}],
            "album": {"id": "91", "name": "THE BOOK"},
            "duration_ms": 240000,
            "metadata_available": True,
        })

    def test_album_and_duration_are_optional_for_metadata_completeness(self):
        normalized = _track({"id": 42, "name": "一首歌", "ar": [{"id": 10, "name": "歌手甲"}]})
        self.assertTrue(normalized["metadata_available"])
        self.assertEqual(normalized["album"], {"id": None, "name": ""})
        self.assertIsNone(normalized["duration_ms"])

    def test_missing_empty_or_invalid_song_name_keeps_id_but_is_incomplete(self):
        for fields in ({}, {"name": ""}, {"name": " \t"}, {"name": None}, {"name": True}, {"name": 4}, {"name": []}):
            with self.subTest(fields=fields):
                normalized = _track({"id": 42, "artists": [{"id": 10, "name": "歌手甲"}], **fields})
                self.assertEqual(normalized["id"], "42")
                self.assertEqual(normalized["artists"], [{"id": "10", "name": "歌手甲"}])
                self.assertFalse(normalized["metadata_available"])

    def test_artist_list_must_be_nonempty_and_have_only_valid_entries(self):
        invalid = (
            {}, {"artists": []}, {"artists": {}},
            {"artists": {"id": 10, "name": "歌手甲"}},
            {"artists": None}, {"artists": "歌手甲"}, {"artists": False},
            {"artists": [None]}, {"artists": [4]},
            {"artists": [{"id": 10, "name": "歌手甲"}, False]},
            {"ar": []}, {"ar": {"id": 10, "name": "歌手甲"}},
        )
        for fields in invalid:
            with self.subTest(fields=fields):
                normalized = _track({"id": 42, "name": "一首歌", **fields})
                self.assertEqual(normalized["id"], "42")
                self.assertEqual(normalized["name"], "一首歌")
                self.assertFalse(normalized["metadata_available"])

    def test_partial_artist_fields_are_preserved_without_complete_metadata(self):
        cases = (
            ({"name": "歌手甲"}, {"id": None, "name": "歌手甲"}),
            ({"id": 10}, {"id": "10", "name": ""}),
            ({"id": 10, "name": ""}, {"id": "10", "name": ""}),
            ({"id": 10, "name": " \t"}, {"id": "10", "name": " \t"}),
            ({"id": 10, "name": True}, {"id": "10", "name": ""}),
            ({"id": True, "name": "歌手甲"}, {"id": None, "name": "歌手甲"}),
            ({"id": -1, "name": "歌手甲"}, {"id": None, "name": "歌手甲"}),
            ({"id": 1.5, "name": "歌手甲"}, {"id": None, "name": "歌手甲"}),
            ({"id": "wrong-id", "name": "歌手甲"}, {"id": None, "name": "歌手甲"}),
        )
        for artist, expected in cases:
            with self.subTest(artist=artist):
                normalized = _track({"id": 42, "name": "一首歌", "artists": [artist]})
                self.assertEqual(normalized["artists"], [expected])
                self.assertFalse(normalized["metadata_available"])

    def test_one_incomplete_artist_marks_collaboration_incomplete(self):
        normalized = _track({
            "id": 42, "name": "合作曲",
            "ar": [{"id": 10, "name": "歌手甲"}, {"id": 20}],
        })
        self.assertEqual(normalized["artists"], [{"id": "10", "name": "歌手甲"}, {"id": "20", "name": ""}])
        self.assertFalse(normalized["metadata_available"])

    def test_illegal_track_ids_and_nonobjects_are_not_tracks(self):
        for record in (None, [], True, "song", {}, {"id": None}, {"id": True}, {"id": False}, {"id": -1}, {"id": 1.5}, {"id": ""}, {"id": "wrong-id"}, {"id": {}}, {"id": []}):
            with self.subTest(record=record):
                self.assertIsNone(_track(record))


if __name__ == "__main__":
    unittest.main()
