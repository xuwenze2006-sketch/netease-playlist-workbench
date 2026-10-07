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

if __name__=='__main__':unittest.main()
