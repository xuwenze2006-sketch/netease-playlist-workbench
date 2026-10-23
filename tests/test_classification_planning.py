import unittest
from netease_organizer.lyric_features import derive_lyric_features
from netease_organizer.classification_planning import infer_language, validate_rows


class ClassificationPlanningTests(unittest.TestCase):
    def test_no_lyric_does_not_invent_instrumental_or_english(self):
        f=derive_lyric_features({'noLyric':True})
        self.assertIsNone(infer_language(None,f)[0])

    def test_concrete_cantonese_record_is_kept(self):
        f=derive_lyric_features({'lyric':'天下苍生都在等待新的歌声'*20})
        self.assertEqual(infer_language('cantonese',f)[0],'cantonese')

    def test_japanese_small_english_refrain_does_not_become_multilingual(self):
        f=derive_lyric_features({'lyric':('私の心はあなたを待っています\n'*40)+'I love you baby'})
        self.assertEqual(infer_language(None,f)[0],'ja')

    def test_unreviewed_mixed_text_requires_language_review(self):
        f=derive_lyric_features({'lyric':('너를 사랑하고 기다리고 있어\n'*20)+('I love you and you are my baby\n'*20)})
        self.assertIsNone(infer_language(None,f)[0])

    def test_french_does_not_become_english(self):
        f=derive_lyric_features({'lyric':'je suis avec toi mon amour dans mes rêves\n'*30})
        self.assertEqual(infer_language(None,f)[0],'other')

    def test_known_instrumental_with_substantial_english_text_requires_review(self):
        f=derive_lyric_features({'lyric':'I love you and you are my baby\n'*30})
        lang,_,review=infer_language('instrumental',f)
        self.assertEqual(lang,'instrumental');self.assertTrue(review)

    def test_concrete_vietnamese_is_not_overwritten_by_english_word_coincidences(self):
        f=derive_lyric_features({'lyric':'I love you and you are my baby\n'*30})
        self.assertEqual(infer_language('other',f)[0],'other')

    def test_known_english_not_overwritten_by_embedded_chinese_translation(self):
        f=derive_lyric_features({'lyric':('天下苍生都在等待新的歌声\n'*30)+('I love you and you are my baby\n'*30)})
        self.assertEqual(infer_language('en',f)[0],'en')

    def test_provider_pure_music_conflict_does_not_discard_known_vocal_record(self):
        lang,_,review=infer_language('other',derive_lyric_features({'pureMusic':True}))
        self.assertEqual(lang,'other');self.assertTrue(review)

    def test_small_credit_script_does_not_override_chinese_record(self):
        f=derive_lyric_features({'lyric':('天下苍生都在等待新的歌声\n'*80)+'サンプルサンプル'})
        self.assertEqual(infer_language('mandarin',f)[0],'mandarin')

    def test_exact_positions_and_unknown_exclusivity(self):
        a=[1,['pop'],['commute'],'en',.8,'specific recording']
        self.assertEqual(validate_rows([a],1),[a])
        for rows in [[a,a], [[1,['pop','unknown'],[],'en',.8,'note']],[[2,['pop'],[],'en',.8,'note']]]:
            with self.assertRaises(ValueError):validate_rows(rows,1)

    def test_plain_han_without_a_recording_judgment_remains_unresolved(self):
        features = derive_lyric_features({'lyric': '天下苍生都在等待新的歌声\n' * 20})
        language, note, review = infer_language(None, features)
        self.assertIsNone(language)
        self.assertTrue(review)
        self.assertIn('汉字', note)

    def test_plain_cyrillic_does_not_prove_russian(self):
        features = derive_lyric_features({'lyric': 'Моя песня звучит под небом тихо\n' * 20})
        language, note, review = infer_language(None, features)
        self.assertIsNone(language)
        self.assertTrue(review)
        self.assertIn('西里尔', note)

    def test_ambiguous_han_still_flags_incompatible_known_language(self):
        features = derive_lyric_features({'lyric': '天下苍生都在等待新的歌声\n' * 20})
        for model in ('en', 'ko', 'ru'):
            with self.subTest(model=model):
                language, _, review = infer_language(model, features)
                self.assertEqual(language, model)
                self.assertTrue(review)

    def test_ambiguous_cyrillic_still_flags_incompatible_known_language(self):
        features = derive_lyric_features({'lyric': 'Моя песня звучит под небом тихо\n' * 20})
        for model in ('en', 'mandarin', 'ko', 'ja'):
            with self.subTest(model=model):
                language, _, review = infer_language(model, features)
                self.assertEqual(language, model)
                self.assertTrue(review)

    def test_ambiguous_scripts_accept_compatible_explicit_models(self):
        cases = [('天下苍生都在等待新的歌声\n' * 20, 'mandarin'),
                 ('天下苍生都在等待新的歌声\n' * 20, 'cantonese'),
                 ('天下苍生都在等待新的歌声\n' * 20, 'other'),
                 ('Моя песня звучит под небом тихо\n' * 20, 'ru'),
                 ('Моя песня звучит под небом тихо\n' * 20, 'other')]
        for lyric, model in cases:
            with self.subTest(model=model):
                language, _, review = infer_language(model, derive_lyric_features({'lyric': lyric}))
                self.assertEqual(language, model)
                self.assertFalse(review)

    def test_small_kana_cannot_turn_han_into_japanese_or_remove_conflict(self):
        features = derive_lyric_features({'lyric': ('天下苍生都在等待新的歌声\n' * 80) + 'サンプルサンプル'})
        self.assertEqual((infer_language(None, features)[0], infer_language(None, features)[2]), (None, True))
        self.assertEqual((infer_language('en', features)[0], infer_language('en', features)[2]), ('en', True))
        self.assertFalse(infer_language('mandarin', features)[2])

    def test_legacy_han_hints_hidden_by_small_kana_are_recovered(self):
        features = {'script_counts': {'han': 1000, 'kana': 8, 'latin': 0},
                    'language_hints': ['ja'], 'original_line_count': 80, 'has_original_lyric_text': True}
        language, _, review = infer_language(None, features)
        self.assertIsNone(language)
        self.assertTrue(review)
        self.assertTrue(infer_language('en', features)[2])

    def test_significant_unknown_latin_cannot_be_dropped_from_japanese(self):
        features = derive_lyric_features({'lyric': ('私の心はあなたを待っています\n' * 15)
                                         + ('aurinko paistaa hiljaa taivaan alla sydän laulaa\n' * 15)})
        self.assertIn('latin_undetermined', features['language_hints'])
        language, _, review = infer_language(None, features)
        self.assertIsNone(language)
        self.assertTrue(review)
        self.assertTrue(infer_language('ja', features)[2])

    def test_small_unknown_latin_refrain_preserves_japanese(self):
        features = derive_lyric_features({'lyric': ('私の心はあなたを待っています\n' * 80)
                                         + 'aurinko paistaa hiljaa taivaan alla sydän laulaa aurinko paistaa'})
        self.assertIn('latin_undetermined', features['language_hints'])
        language, _, review = infer_language(None, features)
        self.assertEqual(language, 'ja')
        self.assertFalse(review)

    def test_unknown_latin_alone_requests_review_without_guessing_english(self):
        features = derive_lyric_features({'lyric': 'aurinko paistaa hiljaa taivaan alla sydän laulaa\n' * 10})
        language, _, review = infer_language(None, features)
        self.assertIsNone(language)
        self.assertTrue(review)

    def test_platform_instrumental_marker_with_body_requires_review(self):
        for lyric in ('I love you and you are with me forever\n' * 10,
                      '天下苍生都在等待新的歌声\n' * 20):
            with self.subTest(lyric=lyric[:20]):
                features = derive_lyric_features({'pureMusic': True, 'lyric': lyric})
                language, _, review = infer_language(None, features)
                self.assertIsNone(language)
                self.assertTrue(review)
                language, _, review = infer_language('instrumental', features)
                self.assertEqual(language, 'instrumental')
                self.assertTrue(review)

    def test_short_body_with_marker_is_not_automatically_instrumental(self):
        features = derive_lyric_features({'lyric': '纯音乐，请欣赏\nhello world'})
        language, _, review = infer_language(None, features)
        self.assertIsNone(language)
        self.assertTrue(review)

    def test_legacy_platform_marker_does_not_hide_substantial_body(self):
        for scripts in ({'han': 320, 'latin': 0}, {'latin': 480, 'han': 0}):
            with self.subTest(scripts=scripts):
                features = {'instrumental_evidence': True, 'has_original_lyric_text': False,
                            'original_line_count': 12, 'script_counts': scripts, 'language_hints': []}
                language, _, review = infer_language(None, features)
                self.assertIsNone(language)
                self.assertTrue(review)
                self.assertTrue(infer_language('instrumental', features)[2])

    def test_legacy_short_marker_keeps_existing_instrumental_evidence(self):
        features = {'instrumental_evidence': True, 'has_original_lyric_text': False,
                    'original_line_count': 1, 'script_counts': {'han': 7}, 'language_hints': []}
        language, _, review = infer_language(None, features)
        self.assertEqual(language, 'instrumental')
        self.assertFalse(review)

    def test_known_instrumental_with_ambiguous_text_still_requires_review(self):
        features = {'script_counts': {'han': 320}, 'language_hints': ['zh_or_yue'],
                    'original_line_count': 12, 'has_original_lyric_text': False}
        language, _, review = infer_language('instrumental', features)
        self.assertEqual(language, 'instrumental')
        self.assertTrue(review)

    def test_supported_language_controls_still_use_positive_evidence(self):
        cases = [('私の心はあなたを待っています\n' * 20, 'ja'),
                 ('너를 사랑하고 내 마음에 있어\n' * 20, 'ko'),
                 ('I love you and you are with me forever\n' * 20, 'en'),
                 ('je suis avec toi dans mon amour pour toi nous pas moi\n' * 20, 'other'),
                 ('我係咁嘅人唔想瞓覺你嘅話我冇忘記\n' * 20, 'cantonese')]
        for lyric, expected in cases:
            with self.subTest(expected=expected):
                language, _, review = infer_language(None, derive_lyric_features({'lyric': lyric}))
                self.assertEqual(language, expected)
                self.assertFalse(review)

if __name__=='__main__':unittest.main()
