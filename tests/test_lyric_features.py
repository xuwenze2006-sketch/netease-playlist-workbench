import unittest
from netease_organizer.lyric_features import derive_lyric_features


class LyricFeatureTests(unittest.TestCase):
    def test_translation_and_credits_do_not_change_original_language(self):
        result = derive_lyric_features({'lyric': '[00:00]作词：张三\n[00:01]I love you and you are with me forever\n' * 3,
                                       'transLyric': '这是中文翻译，不是原文' * 20})
        self.assertEqual(result['script_counts']['han'], 0)
        self.assertIn('en', result['language_hints'])
        self.assertNotIn('lyric', result)

    def test_no_lyrics_does_not_prove_instrumental(self):
        result = derive_lyric_features({'noLyric': True, 'pureMusic': None, 'lyric': ''})
        self.assertFalse(result['instrumental_evidence'])
        self.assertEqual(result['language_hints'], [])

    def test_explicit_provider_instrumental_marker(self):
        result = derive_lyric_features({'lyric': '[00:00]纯音乐，请欣赏'})
        self.assertTrue(result['instrumental_evidence'])
        self.assertFalse(result['has_original_lyric_text'])
        self.assertEqual(result['language_hints'], [])

    def test_japanese_han_does_not_become_chinese(self):
        result = derive_lyric_features({'lyric': '[00:01]君のことを思い出していつも歌います世界未来' * 5})
        self.assertIn('ja', result['language_hints'])
        self.assertNotIn('zh_or_yue', result['language_hints'])

    def test_korean_and_english_are_both_retained(self):
        result = derive_lyric_features({'lyric': '[00:01]너를 사랑하고 내 마음에 있어\nI love you and you are with me forever\n' * 5})
        self.assertIn('ko', result['language_hints'])
        self.assertIn('en', result['language_hints'])

    def test_cantonese_specific_words_are_evidence_not_an_automatic_label(self):
        result = derive_lyric_features({'lyric': '我係咁嘅人唔想瞓覺你嘅話我冇忘記' * 5})
        self.assertGreater(result['cantonese_specific_character_count'], 10)
        self.assertIn('zh_or_yue', result['language_hints'])

    def test_latin_non_english_is_distinguished(self):
        result = derive_lyric_features({'lyric': 'je suis avec toi dans mon amour pour toi nous pas moi\n' * 5})
        self.assertIn('fr', result['language_hints'])
        self.assertNotIn('en', result['language_hints'])

    def test_nontext_and_unbounded_inputs_rejected(self):
        for raw in ({'lyric': 123}, {'lyric': 'a' * 300001}, []):
            with self.subTest(raw_type=type(raw).__name__), self.assertRaises(ValueError):
                derive_lyric_features(raw)


if __name__ == '__main__':
    unittest.main()
