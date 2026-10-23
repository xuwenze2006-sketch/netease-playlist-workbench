import unittest

from netease_organizer.classification_planning import infer_language
from netease_organizer.latin_evidence import latin_hints
from netease_organizer.lyric_features import derive_lyric_features


class LyricTextEvidenceTests(unittest.TestCase):
    def features(self, text, **provider):
        return derive_lyric_features({'lyric': text, **provider})

    def test_square_bracketed_japanese_body_survives_with_or_without_timestamps(self):
        for prefix in ('', '[00:01.250]'):
            with self.subTest(prefix=prefix):
                result = self.features(prefix + '[まだ知らない道を君と歩いてゆく]\n'
                                       + prefix + '[風の音を聞いて明日へ歌おう]')
                self.assertEqual(result['original_line_count'], 2)
                self.assertEqual(result['script_counts']['han'], 10)
                self.assertEqual(result['script_counts']['kana'], 18)
                self.assertEqual(infer_language(None, result)[::2], ('ja', False))

    def test_embedded_unknown_brackets_preserve_english_words(self):
        result = self.features('We are [with you] and I can hear the wind\n'
                               'You and I will find [our way] in the night')
        self.assertEqual(result['script_counts']['latin'], 61)
        self.assertEqual(result['original_line_count'], 2)
        self.assertEqual(infer_language(None, result)[::2], ('en', False))

    def test_unknown_annotations_and_malformed_timestamps_are_retained(self):
        for text, letters in (
            ('[aside: love]', 9), ('[Verse of love]', 11),
            ('[Chorus forever]', 13), ('[ar: first] and love', 14),
            ('<1love>', 4), ('[00:love]', 4), ('[00:99 love]', 4),
            ('[00:01.bad love]', 7), ('[love', 4),
        ):
            with self.subTest(text=text):
                result = self.features(text)
                self.assertTrue(result['has_original_lyric_text'])
                self.assertEqual(result['script_counts']['latin'], letters)

    def test_numeric_timestamps_separate_word_fragments(self):
        result = self.features('t[00:01]he an<00:02.125>d yo[00:03.5]u\n' * 10)
        self.assertEqual(result['script_counts']['latin'], 90)
        self.assertEqual(result['latin_evidence']['word_count'], 60)
        self.assertEqual(result['latin_language_scores']['en'], 0)
        self.assertEqual(result['language_hints'], ['latin_undetermined'])

    def test_supported_timestamps_alone_add_no_body_or_marker(self):
        result = self.features('[00:01][12:59.123]\n<00:01.25>')
        self.assertEqual(result['original_line_count'], 0)
        self.assertFalse(result['has_original_lyric_text'])
        self.assertFalse(result['instrumental_evidence'])

    def test_complete_metadata_and_numbered_section_headers_add_no_evidence(self):
        metadata = ['[ar:The You And Me Band]', '[ti:You And I]', '[al:Love For You]',
                    '[by:The You And Me Band]', '[offset:-120]', '[re:Editor]',
                    '[ve:version]', '[length:03:45]']
        headings = ['[Intro]', '[Outro 2]', '[Verse 1]', '[Chorus 2]', '[Bridge]',
                    '[Pre-Chorus 1]', '[Post-Chorus]', '[Instrumental]']
        result = self.features('\n'.join(metadata + headings))
        self.assertEqual(result['original_line_count'], 0)
        self.assertEqual(sum(result['script_counts'].values()), 0)
        self.assertEqual(result['language_hints'], [])
        self.assertFalse(result['instrumental_evidence'])
        self.assertEqual(infer_language(None, result)[::2], (None, False))

    def test_explicit_english_credit_roles_require_colon_and_accept_by(self):
        roles = ('Lyrics by', 'Music by', 'Written by', 'Vocals', 'Vocal by',
                 'Composer by', 'Composed by', 'Arranger by', 'Arranged by',
                 'Producer by', 'Produced by', 'Recorded by', 'Mixed by',
                 'Mastered by', 'Lyrics', 'Music', 'Writer', 'Copyright', 'All rights')
        for role in roles:
            with self.subTest(role=role):
                result = self.features((role + ': The You And Me Band\n') * 4)
                self.assertFalse(result['has_original_lyric_text'])
                self.assertEqual(sum(result['script_counts'].values()), 0)
                self.assertEqual(infer_language(None, result)[::2], (None, False))

    def test_fullwidth_and_whole_line_wrapped_credits_are_recognized(self):
        result = self.features('Ｌｙｒｉｃｓ ｂｙ： The You And Me Band\n'
                               '[Vocals: The You And Me Band]\n'
                               '（Music by: The You And Me Band）\n'
                               '[00:01]作词：张三\n编曲-李四')
        self.assertEqual(result['original_line_count'], 0)
        self.assertEqual(sum(result['script_counts'].values()), 0)

    def test_music_dash_and_credit_words_without_colon_remain_lyrics(self):
        for text in ('Music - you are with me and I love you\n',
                     'Lyrics by the river are with you and me\n',
                     'Vocals in the night are with you and me\n'):
            with self.subTest(text=text):
                result = self.features(text * 4)
                self.assertEqual(result['original_line_count'], 4)
                self.assertEqual(infer_language(None, result)[::2], ('en', False))

    def test_leading_punctuation_does_not_turn_body_into_a_credit(self):
        for prefix in ('- ', ': '):
            with self.subTest(prefix=prefix):
                result = self.features((prefix + 'I love you and you are with me forever\n') * 3)
                self.assertEqual(result['original_line_count'], 3)
                self.assertEqual(infer_language(None, result)[::2], ('en', False))

    def test_metadata_or_credit_wrappers_must_cover_whole_line(self):
        result = self.features('[Vocals: me] and you are with me\n'
                               '[Chorus] I love you and you are with me\n'
                               'I sing of Lyrics by: the river\n')
        self.assertEqual(result['original_line_count'], 3)
        self.assertEqual(result['script_counts']['latin'], 75)

    def test_metadata_and_credits_do_not_erase_adjacent_body(self):
        result = self.features('[ar:The You And Me Band]\n[Chorus]\n'
                               'Lyrics by: The You And Me Band\n'
                               'まだ知らない道を君と歩いてゆく\n'
                               '風の音を聞いて明日へ歌おう\nVocals: The You And Me Band')
        self.assertEqual(result['original_line_count'], 2)
        self.assertEqual(result['script_counts']['latin'], 0)
        self.assertEqual(infer_language(None, result)[::2], ('ja', False))

    def test_nonletters_and_standalone_marks_do_not_create_script_or_body(self):
        for noise in ('・', '゠', '٠١٢٣٤٥٦٧٨٩', '๐๑๒๓๔๕๖๗๘๙', '฿',
                      '\u061c', '҂', '\u3099\u309a', '\u064e\u0651',
                      '\u0e31\u0e48\u0e49', '123!? 🎵'):
            with self.subTest(noise=repr(noise)):
                result = self.features((noise + '\n') * 20)
                self.assertFalse(result['has_original_lyric_text'])
                self.assertEqual(result['original_line_count'], 0)
                self.assertEqual(sum(result['script_counts'].values()), 0)
                self.assertEqual(infer_language(None, result)[::2], (None, False))

    def test_known_elongation_iteration_and_filler_letters_are_not_body(self):
        for decoration in ('ー', 'ｰ', 'ゝゞヽヾ', 'ـ', 'ๆฯ',
                           '\u115f\u1160\u3164\uffa0'):
            with self.subTest(decoration=repr(decoration)):
                result = self.features(decoration * 20)
                self.assertFalse(result['has_original_lyric_text'])
                self.assertEqual(sum(result['script_counts'].values()), 0)
                self.assertEqual(infer_language(None, result)[::2], (None, False))

    def test_foreign_attached_marks_cannot_supply_another_script(self):
        for marks, script in (('\u0e31\u0e48\u0e49', 'thai'),
                              ('\u064e\u0651', 'arabic'), ('\u3099', 'kana')):
            with self.subTest(script=script):
                result = self.features(('a' + marks + ' ') * 12)
                self.assertEqual(result['script_counts'][script], 0)
                self.assertEqual(result['script_counts']['latin'], 12)
                self.assertTrue(result['has_original_lyric_text'])
                self.assertEqual(infer_language(None, result)[::2], (None, False))

    def test_one_base_letter_cannot_be_inflated_past_a_script_threshold(self):
        for base, mark, script in (('ب', '\u064e', 'arabic'),
                                   ('ก', '\u0e48', 'thai'), ('あ', '\u3099', 'kana')):
            with self.subTest(script=script):
                result = self.features(base + mark * 100)
                self.assertEqual(result['script_counts'][script], 1)
                self.assertTrue(result['has_original_lyric_text'])
                self.assertEqual(infer_language(None, result)[::2], (None, False))

    def test_normal_kana_survives_long_vowels_halfwidth_and_decomposed_voicing(self):
        for text, kana in (('コーヒーをのみながらうたう', 11),
                           ('ｺｰﾋｰをのみながらうたう', 11),
                           ('か\u3099き\u3099く\u3099け\u3099こ\u3099あ', 6)):
            with self.subTest(text=text):
                result = self.features(text * 3)
                self.assertEqual(result['script_counts']['kana'], kana * 3)
                self.assertEqual(infer_language(None, result)[::2], ('ja', False))

    def test_normal_hangul_arabic_and_thai_keep_base_letter_evidence(self):
        for text, script, count, language in (
            ('너를 사랑하고', 'hangul', 6, 'ko'),
            ('ㄱㄴㄷㄹㅁㅂ', 'hangul', 6, 'ko'),
            ('بَتُثِجْحّخ', 'arabic', 6, 'other'),
            ('بـتـثـجـحـخ', 'arabic', 6, 'other'),
            ('ก่ข้ค๊ง๋จฉ', 'thai', 6, 'other'),
        ):
            with self.subTest(script=script, text=text):
                result = self.features(text * 3)
                self.assertEqual(result['script_counts'][script], count * 3)
                self.assertEqual(infer_language(None, result)[::2], (language, False))

    def test_combining_marks_remain_in_latin_words_and_do_not_expose_fragments(self):
        result = self.features('i\u0323 you\u0301 a你i aЖyou iअ\n' * 15)
        self.assertEqual(result['latin_language_scores']['en'], 0)
        self.assertNotIn('en', result['language_hints'])
        self.assertEqual(result['latin_evidence']['letter_count'], result['script_counts']['latin'])

    def test_latin_normalization_and_evidence_validation_remain_compatible(self):
        for text, language in (
            ('Ｉ love you and you are with me forever\n', 'en'),
            ('je suis avec toi dans mon amour pour toi nous pas moi\n', 'fr'),
            ('eu voce\u0302 na\u0303o minha tenho corac\u0327a\u0303o saudade\n', 'pt'),
        ):
            with self.subTest(language=language):
                result = self.features(text * 3)
                self.assertEqual(result['language_hints'], [language])
                self.assertEqual(latin_hints(result), [language])
                self.assertEqual(result['latin_evidence']['letter_count'], result['script_counts']['latin'])

    def test_only_substantive_lines_count_as_body(self):
        result = self.features('123!?\n🎵\nーーー\nhello\n\u064e\nγειά\n')
        self.assertEqual(result['original_line_count'], 2)
        self.assertTrue(result['has_original_lyric_text'])

    def test_noise_does_not_conflict_with_provider_instrumental_evidence(self):
        for marker, extra in (('', {'pureMusic': True}), ('[00:00]纯音乐，请欣赏\n', {})):
            with self.subTest(marker=marker):
                result = self.features(marker + '[Chorus]\nVocals: The You And Me Band\n'
                                       '123!? 🎵\nーーー\n٠١٢٣٤٥٦٧٨٩\n', **extra)
                self.assertFalse(result['has_original_lyric_text'])
                self.assertTrue(result['instrumental_evidence'])
                self.assertEqual(infer_language(None, result)[::2], ('instrumental', False))

    def test_unknown_script_letters_still_conflict_with_pure_music(self):
        for text in ('γειά σου', 'नमस्ते', '[まだ知らない道を君と歩いてゆく]'):
            with self.subTest(text=text):
                result = self.features(text, pureMusic=True)
                self.assertTrue(result['has_original_lyric_text'])
                self.assertEqual(result['original_line_count'], 1)
                self.assertEqual(infer_language(None, result)[::2], (None, True))

    def test_no_lyric_and_metadata_only_never_imply_instrumental(self):
        for text in ('', '[Instrumental]', '[ar:The Band]\n[Verse 1]', '123!? 🎵'):
            with self.subTest(text=text):
                result = self.features(text, noLyric=True)
                self.assertFalse(result['instrumental_evidence'])
                self.assertEqual(infer_language(None, result)[::2], (None, False))


if __name__ == '__main__':
    unittest.main()
