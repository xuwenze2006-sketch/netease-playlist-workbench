"""Language evidence must remain conservative through the real export builder."""

import json
import tempfile
import unittest
from pathlib import Path

from netease_organizer.lyric_features import derive_lyric_features
from scripts.build_classification_plan import build_exports


def cached_features(scripts, hints, **changes):
    return {
        'status': 'ok', 'has_original_lyric_text': True,
        'original_line_count': 24, 'script_counts': scripts,
        'language_hints': hints, **changes,
    }


def lyric_features(text, **provider_fields):
    return {'status': 'ok', **derive_lyric_features({'lyric': text, **provider_fields})}


class LanguageEvidenceGenerationTests(unittest.TestCase):
    def build(self, features, *, models=None, reviews=None):
        """Use synthetic source files and retain every byte across in-memory generation."""
        directory = tempfile.TemporaryDirectory(prefix='language-evidence-generation-')
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        artifacts = root / 'artifacts'
        artifacts.mkdir()
        count = len(features)
        models = models if models is not None else [None] * count
        tracks = [
            {'id': f'{position:032X}', 'original_id': str(position),
             'name': f'合成录音{position}', 'artists': [{'name': '合成艺人'}],
             'metadata_available': True}
            for position in range(1, count + 1)
        ]
        inputs = {
            '全库分类-账号基线.json': {'snapshot': {
                'account': {'original_id': '9001', 'id': 'A' * 32},
                'liked': {'id': 'B' * 32, 'original_id': '9002',
                          'track_count': count, 'membership_complete': True, 'tracks': tracks}}},
            '分类判断-分块01.json': [
                [position, ['pop'], ['commute'], models[position - 1], .9, '合成风格场景依据']
                for position in range(1, count + 1)
            ],
            '分类判断-分块02.json': [], '分类判断-分块03.json': [],
            '全库分类-歌词特征.json': {'tracks': {
                str(position): feature for position, feature in enumerate(features, 1)}},
            '网易云本地歌单快照.json': {'tracks': {}},
        }
        if reviews is not None:
            inputs['分类语言核验01.json'] = reviews
        for name, value in inputs.items():
            (artifacts / name).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        for index in range(1, 4):
            (artifacts / f'分类输入-分块{index:02}.jsonl').write_text(
                ''.join(json.dumps([position, track['name'], ['合成艺人'], '', None, True],
                                   ensure_ascii=False) + '\n'
                        for position, track in enumerate(tracks, 1)) if index == 1 else '',
                encoding='utf-8')
        before = {path.name: path.read_bytes() for path in artifacts.iterdir()}
        plan, summary, outputs = build_exports(root)
        self.assertEqual({path.name: path.read_bytes() for path in artifacts.iterdir()}, before)
        self.assertEqual(plan['source_track_ids'], [track['id'] for track in tracks])
        self.assertEqual(summary['source_count'], count)
        self.assertEqual(summary['covered_count'], count)
        documents = {name: json.loads(content) for name, content, _ in outputs if name.endswith('.json')}
        records = documents['全库分类-逐曲结果.json']['records']
        self.assertEqual([record['styles'] for record in records], [['流行抒情']] * count)
        self.assertEqual([record['scenes'] for record in records], [['通勤散步']] * count)
        jobs = {job['name']: job['candidate_track_ids'] for job in plan['jobs']}
        self.assertEqual(jobs['风格 · 流行抒情'], [track['id'] for track in tracks])
        self.assertEqual(jobs['场景 · 通勤散步'], [track['id'] for track in tracks])
        return records, documents['全库分类-需复核录音.json'], jobs, summary

    def assert_unresolved(self, features):
        records, reviews, jobs, summary = self.build(features)
        count = len(features)
        self.assertEqual([record['language'] for record in records], ['待辨识'] * count)
        self.assertEqual([record['pending_reasons'] for record in records], [['语言待辨识']] * count)
        self.assertTrue(all(record['review_note'] for record in records))
        self.assertEqual([(review['position'], review['assigned']) for review in reviews],
                         [(position, None) for position in range(1, count + 1)])
        self.assertEqual(jobs['分类 · 待辨识'], [f'{position:032X}' for position in range(1, count + 1)])
        self.assertFalse(any(name.startswith('语言 · ') for name in jobs))
        self.assertEqual(summary['pending_count'], count)
        self.assertEqual(summary['unknown_language_count'], count)
        self.assertEqual(summary['review_note_count'], count)

    def test_cached_han_and_cyrillic_alone_do_not_create_specific_language_playlists(self):
        self.assert_unresolved([
            cached_features({'han': 400}, ['zh_or_yue']),
            cached_features({'cyrillic': 400}, ['ru_or_other_cyrillic']),
        ])

    def test_current_derivation_keeps_ambiguous_scripts_pending_through_exports(self):
        self.assert_unresolved([
            lyric_features(('今天星光照亮远方我们一起走过长路\n') * 12),
            lyric_features(('Свет над нами снова открывает дорогу\n') * 12),
        ])

    def test_legacy_pure_music_marker_with_body_is_reviewed_instead_of_instrumental(self):
        self.assert_unresolved([
            cached_features({'han': 400}, [], has_original_lyric_text=False,
                            instrumental_evidence=True),
            cached_features({'latin': 400}, [], has_original_lyric_text=False,
                            instrumental_evidence=True),
        ])

    def test_current_platform_marker_cannot_hide_body_during_generation(self):
        self.assert_unresolved([
            lyric_features(('今天星光照亮远方我们一起走过长路\n') * 12, pureMusic=True),
        ])

    def test_explicit_models_keep_labels_without_losing_incompatible_script_review(self):
        han = cached_features({'han': 400}, ['zh_or_yue'])
        cyrillic = cached_features({'cyrillic': 400}, ['ru_or_other_cyrillic'])
        records, reviews, jobs, summary = self.build(
            [han, han, cyrillic, han, cyrillic],
            models=['mandarin', 'cantonese', 'ru', 'en', 'mandarin'])
        self.assertEqual([record['language'] for record in records],
                         ['国语', '粤语', '俄语', '英语', '国语'])
        self.assertEqual([record['review_note'] for record in records], [False, False, False, True, True])
        self.assertEqual([review['position'] for review in reviews], [4, 5])
        self.assertNotIn('分类 · 待辨识', jobs)
        self.assertEqual(jobs['语言 · 国语'], [f'{position:032X}' for position in (1, 5)])
        self.assertEqual(jobs['语言 · 粤语'], [f'{2:032X}'])
        self.assertEqual(jobs['语言 · 俄语'], [f'{3:032X}'])
        self.assertEqual(jobs['语言 · 英语'], [f'{4:032X}'])
        self.assertEqual(summary['review_note_count'], 2)

    def test_explicit_instrumental_model_is_retained_with_body_conflict_review(self):
        records, reviews, jobs, _ = self.build([
            cached_features({'han': 400}, [], has_original_lyric_text=False,
                            instrumental_evidence=True),
        ], models=['instrumental'])
        self.assertEqual(records[0]['language'], '器乐或配乐录音')
        self.assertIs(records[0]['review_note'], True)
        self.assertEqual([(review['position'], review['assigned']) for review in reviews], [(1, 'instrumental')])
        self.assertFalse(any(name.startswith('语言 · ') for name in jobs))

    def test_accepted_per_song_resolution_remains_authoritative(self):
        records, reviews, jobs, summary = self.build([
            cached_features({'han': 400}, ['zh_or_yue']),
            cached_features({'cyrillic': 400}, ['ru_or_other_cyrillic']),
            cached_features({'han': 400}, [], has_original_lyric_text=False,
                            instrumental_evidence=True),
        ], models=['en', None, None], reviews=[
            {'position': position, 'language': language, 'review_note': False,
             'notes': '合成的逐曲版本核验结论'}
            for position, language in enumerate(('cantonese', 'ru', 'instrumental'), 1)
        ])
        self.assertEqual([record['language'] for record in records], ['粤语', '俄语', '器乐或配乐录音'])
        self.assertTrue(all(record['language_evidence_note'].startswith('单曲录音复核：') for record in records))
        self.assertFalse(any(record['review_note'] for record in records))
        self.assertEqual(reviews, [])
        self.assertEqual(summary['pending_count'], 0)
        self.assertEqual(jobs['语言 · 粤语'], [f'{1:032X}'])
        self.assertEqual(jobs['语言 · 俄语'], [f'{2:032X}'])
        self.assertNotIn('分类 · 待辨识', jobs)

    def test_normal_japanese_korean_and_english_still_receive_language_playlists(self):
        records, reviews, jobs, summary = self.build([
            lyric_features(('今日もあなたと星の光を見ながら歩いている\n') * 20),
            lyric_features(('오늘도 우리는 별빛 아래 함께 걸어갑니다\n') * 20),
            lyric_features(('I love you and you love me my baby we are together\n') * 20),
        ])
        self.assertEqual([record['language'] for record in records], ['日语', '韩语', '英语'])
        self.assertEqual(reviews, [])
        self.assertEqual(summary['pending_count'], 0)
        for position, label in enumerate(('日语', '韩语', '英语'), 1):
            self.assertEqual(jobs['语言 · ' + label], [f'{position:032X}'])

    def test_short_english_refrain_does_not_replace_dominant_japanese_or_korean(self):
        refrain = 'I love you and you love me my baby\n' * 2
        records, reviews, jobs, _ = self.build([
            lyric_features(('今日もあなたと星の光を見ながら歩いている\n') * 20 + refrain),
            lyric_features(('오늘도 우리는 별빛 아래 함께 걸어갑니다\n') * 20 + refrain),
        ])
        self.assertEqual([record['language'] for record in records], ['日语', '韩语'])
        self.assertEqual(reviews, [])
        self.assertNotIn('语言 · 英语', jobs)
        self.assertNotIn('分类 · 待辨识', jobs)

    def test_significant_unidentified_latin_body_is_not_silently_discarded(self):
        self.assert_unresolved([
            cached_features({'hangul': 300, 'latin': 400}, ['ko', 'latin_undetermined']),
        ])

    def test_small_kana_fragment_does_not_erase_dominant_ambiguous_han(self):
        self.assert_unresolved([
            lyric_features(('今天星光照亮远方我们一起走过长路\n') * 30 + 'あいうえおか'),
        ])

    def test_multiple_latin_hints_stay_unresolved_in_both_orders(self):
        self.assert_unresolved([
            cached_features({'latin': 400}, ['en', 'fr']),
            cached_features({'latin': 400}, ['fr', 'en']),
        ])

    def test_legacy_sparse_hint_moves_only_language_to_pending(self):
        self.assert_unresolved([
            cached_features({'latin': 4000}, ['en'], latin_language_scores={'en': 8}),
        ])

    def test_current_weak_latin_evidence_does_not_create_english_playlist(self):
        self.assert_unresolved([
            lyric_features('baby\n' * 30),
            lyric_features('tôi đợi người cuối trời\n' * 20),
            lyric_features('aurinko paistaa hiljaa taivaan alla sydän laulaa\n' * 60
                           + 'I love you and we are with my baby'),
        ])

    def test_multiple_non_english_hints_keep_the_supported_broad_category(self):
        records, reviews, jobs, summary = self.build([
            cached_features({'latin': 400}, ['fr', 'pt']),
            cached_features({'latin': 400}, ['pt', 'fr']),
        ])
        self.assertEqual([record['language'] for record in records], ['其他与多语'] * 2)
        self.assertEqual(reviews, [])
        self.assertEqual(summary['pending_count'], 0)
        self.assertEqual(jobs['语言 · 其他与多语'], [f'{position:032X}' for position in (1, 2)])

    def test_weak_new_and_legacy_latin_keep_explicit_models_and_accepted_reviews(self):
        weak = lyric_features('baby\n' * 30)
        sparse = cached_features({'latin': 4000}, ['en'], latin_language_scores={'en': 8})
        records, reviews, jobs, summary = self.build(
            [weak, sparse, weak, sparse], models=['en', 'other', 'en', None], reviews=[
                {'position': 3, 'language': 'other', 'review_note': False,
                 'notes': '合成逐曲核验优先于功能词线索'},
                {'position': 4, 'language': 'en', 'review_note': False,
                 'notes': '合成具体演唱版本已核验'},
            ])
        self.assertEqual([record['language'] for record in records],
                         ['英语', '其他与多语', '其他与多语', '英语'])
        self.assertFalse(any(record['review_note'] for record in records))
        self.assertTrue(all(record['language_evidence_note'].startswith('单曲录音复核：')
                            for record in records[2:]))
        self.assertEqual(reviews, [])
        self.assertEqual(summary['pending_count'], 0)
        self.assertNotIn('分类 · 待辨识', jobs)
        self.assertEqual(jobs['语言 · 英语'], [f'{position:032X}' for position in (1, 4)])
        self.assertEqual(jobs['语言 · 其他与多语'], [f'{position:032X}' for position in (2, 3)])


if __name__ == '__main__':
    unittest.main()
