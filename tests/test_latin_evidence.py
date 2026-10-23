import copy
import json
import unittest

from netease_organizer.lyric_features import derive_lyric_features


class LatinExtractionTests(unittest.TestCase):
    def features(self, text):
        return derive_lyric_features({'lyric': text})

    def test_vietnamese_word_parts_do_not_become_english_i(self):
        result = self.features('tôi ơi người trời đời lời\n' * 12)
        self.assertEqual(result['latin_language_scores']['en'], 0)
        self.assertEqual(result['language_hints'], ['latin_undetermined'])

    def test_combining_mark_and_mixed_script_words_are_not_sliced(self):
        result = self.features(('i\u0323 you\u0301 a你i aЖyou iअ\n') * 15)
        self.assertEqual(result['latin_language_scores']['en'], 0)
        self.assertNotIn('en', result['language_hints'])
        self.assertIn('latin_undetermined', result['language_hints'])

    def test_one_repeated_word_is_not_language_evidence(self):
        for word in ('baby', 'je'):
            with self.subTest(word=word):
                result = self.features((word + ' ') * 100)
                self.assertEqual(result['language_hints'], ['latin_undetermined'])

    def test_two_distinct_words_still_do_not_establish_language(self):
        result = self.features('baby love ' * 30)
        self.assertEqual(result['language_hints'], ['latin_undetermined'])

    def test_each_word_contributes_at_most_three_hits(self):
        result = self.features('baby ' * 50 + 'love you')
        self.assertEqual(result['language_hints'], ['latin_undetermined'])

    def test_sparse_familiar_tail_does_not_label_an_unknown_body(self):
        result = self.features('aurinko paistaa hiljaa taivaan alla sydän laulaa\n' * 100
                               + 'the and you your my me we our')
        self.assertEqual(result['language_hints'], ['latin_undetermined'])

    def test_diverse_and_substantial_english_still_matches(self):
        result = self.features('I love you and you are with me forever\n' * 3)
        self.assertEqual(result['language_hints'], ['en'])

    def test_diverse_french_still_matches(self):
        result = self.features('je suis avec toi dans mon amour pour toi nous pas moi\n' * 3)
        self.assertEqual(result['language_hints'], ['fr'])

    def test_substantial_english_and_french_are_both_retained(self):
        result = self.features('I love you and you are with me forever\n' * 3
                               + 'je suis avec toi dans mon amour pour toi nous pas moi\n' * 3)
        self.assertEqual(result['language_hints'], ['en', 'fr'])

    def test_no_lyric_has_no_latin_hint(self):
        self.assertEqual(self.features('')['language_hints'], [])

    def test_counts_store_no_original_words_or_text(self):
        result = self.features('privatecanarytext baby baby baby baby love love you you')
        evidence = result.get('latin_evidence')
        self.assertIsInstance(evidence, dict)
        self.assertEqual(set(evidence), {'version', 'letter_count', 'word_count', 'languages'})
        self.assertEqual(evidence['version'], 1)
        self.assertEqual(evidence['word_count'], 9)
        self.assertEqual(evidence['letter_count'], result['script_counts']['latin'])
        self.assertEqual(evidence['languages']['en'], {
            'distinct_words': 3, 'capped_hits': 7, 'matched_letters': 30,
        })
        self.assertEqual(result['latin_language_scores']['en'], 8)
        for language in evidence['languages'].values():
            self.assertEqual(set(language), {'distinct_words', 'capped_hits', 'matched_letters'})
            self.assertTrue(all(type(value) is int for value in language.values()))
        serialized = json.dumps(result)
        for original_word in ('privatecanarytext', 'baby', 'love', 'you'):
            self.assertNotIn(original_word, serialized)

    def test_nfkc_and_decomposed_diacritics_match_full_portuguese_words(self):
        result = self.features('eu voce\u0302 na\u0303o minha tenho corac\u0327a\u0303o saudade\n' * 3)
        self.assertEqual(result['language_hints'], ['pt'])

    def test_exact_coverage_boundary_is_inclusive(self):
        # Eight hits across three words contribute 29 matched letters.
        known = 'you you you love love love baby baby '
        self.assertEqual(self.features(known + 'x' * 261)['language_hints'], ['en'])
        self.assertEqual(self.features(known + 'x' * 262)['language_hints'], ['latin_undetermined'])

    def test_minimum_fifty_latin_letters_is_preserved(self):
        known = 'you you you love love love baby baby '
        self.assertEqual(self.features(known + 'x' * 20)['language_hints'], [])
        self.assertEqual(self.features(known + 'x' * 21)['language_hints'], ['en'])


class LatinHintValidationTests(unittest.TestCase):
    def hints(self, features):
        from netease_organizer.latin_evidence import latin_hints
        return latin_hints(features)

    def good_features(self):
        return derive_lyric_features({'lyric': 'I love you and you are with me forever\n' * 3})

    def test_new_counts_override_conflicting_old_hint(self):
        result = self.good_features()
        result['language_hints'] = ['fr']
        self.assertEqual(self.hints(result), ['en'])

    def test_new_insufficient_counts_cannot_fall_back_to_old_hint(self):
        result = derive_lyric_features({'lyric': 'baby ' * 100})
        result['language_hints'] = ['en']
        self.assertEqual(self.hints(result), ['latin_undetermined'])

    def test_unknown_or_malformed_version_does_not_fall_back(self):
        for version in (2, '1', True, None):
            with self.subTest(version=version):
                result = self.good_features()
                result['latin_evidence']['version'] = version
                self.assertEqual(self.hints(result), ['latin_undetermined'])

    def test_present_but_malformed_evidence_does_not_fall_back(self):
        for evidence in (None, [], {}, 'broken'):
            with self.subTest(evidence=evidence):
                result = self.good_features()
                result['latin_evidence'] = evidence
                self.assertEqual(self.hints(result), ['latin_undetermined'])

    def test_count_types_bounds_and_cross_field_relations_are_checked(self):
        mutations = [
            ('negative letter count', lambda e: e.update(letter_count=-1)),
            ('bool letter count', lambda e: e.update(letter_count=True)),
            ('script mismatch', lambda e: e.update(letter_count=e['letter_count'] + 1)),
            ('too many words', lambda e: e.update(word_count=e['letter_count'] + 1)),
            ('float word count', lambda e: e.update(word_count=1.5)),
            ('missing language', lambda e: e['languages'].pop('pt')),
            ('invalid language body', lambda e: e['languages'].update(en=[])),
            ('missing metric', lambda e: e['languages']['en'].pop('capped_hits')),
            ('bool metric', lambda e: e['languages']['en'].update(distinct_words=True)),
            ('negative metric', lambda e: e['languages']['en'].update(matched_letters=-1)),
            ('too many distinct words', lambda e: e['languages']['en'].update(distinct_words=1000)),
            ('more distinct than hits', lambda e: e['languages']['en'].update(capped_hits=1)),
            ('cap above three per word', lambda e: e['languages']['en'].update(capped_hits=1000)),
            ('more matched letters than body', lambda e: e['languages']['en'].update(matched_letters=e['letter_count'] + 1)),
            ('hits without letters', lambda e: e['languages']['en'].update(matched_letters=0)),
            ('letters without hits', lambda e: e['languages']['fr'].update(matched_letters=1)),
        ]
        for name, mutate in mutations:
            with self.subTest(name=name):
                result = self.good_features()
                mutate(result['latin_evidence'])
                self.assertEqual(self.hints(result), ['latin_undetermined'])

    def test_mutated_script_count_cannot_authorize_specific_hint(self):
        for invalid in (None, True, '99', -1):
            with self.subTest(invalid=invalid):
                result = self.good_features()
                result['script_counts']['latin'] = invalid
                self.assertEqual(self.hints(result), ['latin_undetermined'])

    def test_uncapped_hits_cannot_claim_many_repeated_letters(self):
        result = self.good_features()
        result['latin_evidence']['languages']['fr'] = {
            'distinct_words': 1, 'capped_hits': 1, 'matched_letters': 30,
        }
        self.assertEqual(self.hints(result), ['latin_undetermined'])

    def test_distinct_word_lengths_bound_the_minimum_matched_letters(self):
        result = self.good_features()
        result['script_counts']['latin'] = 50
        result['latin_evidence']['letter_count'] = 50
        result['latin_evidence']['languages']['en'] = {
            'distinct_words': 3, 'capped_hits': 8, 'matched_letters': 8,
        }
        self.assertEqual(self.hints(result), ['latin_undetermined'])

    def test_new_language_map_order_does_not_change_candidate_order(self):
        result = derive_lyric_features({'lyric': 'I love you and you are with me forever\n' * 3
                                       + 'je suis avec toi dans mon amour pour toi nous pas moi\n' * 3})
        result['latin_evidence']['languages'] = dict(reversed(list(result['latin_evidence']['languages'].items())))
        self.assertEqual(self.hints(result), ['en', 'fr'])

    def test_legitimate_large_repetition_counts_remain_valid(self):
        result = derive_lyric_features({'lyric': 'baby love you ' * 100})
        self.assertEqual(self.hints(result), ['en'])

    def test_word_and_letter_totals_must_fit_the_distinct_matched_words(self):
        result = self.good_features()
        result['script_counts']['latin'] = 50
        result['latin_evidence'].update(letter_count=50, word_count=50)
        result['latin_evidence']['languages']['en'] = {
            'distinct_words': 3, 'capped_hits': 8, 'matched_letters': 29,
        }
        self.assertEqual(self.hints(result), ['latin_undetermined'])

    def test_joint_word_letter_lower_bound_accepts_a_real_boundary(self):
        result = derive_lyric_features({'lyric': 'i i i my my my me me ' + 'x ' * 42})
        self.assertEqual(result['latin_evidence']['word_count'], 50)
        self.assertEqual(result['latin_evidence']['letter_count'], 55)
        self.assertEqual(self.hints(result), ['en'])
        result['script_counts']['latin'] -= 1
        result['latin_evidence']['letter_count'] -= 1
        self.assertEqual(self.hints(result), ['latin_undetermined'])

    def test_joint_validation_preserves_derived_unicode_and_repeated_word_counts(self):
        cases = [
            ('I love you and you are with me forever\n' * 3, ['en']),
            ('eu voce\u0302 na\u0303o minha tenho corac\u0327a\u0303o saudade\n' * 3, ['pt']),
            ('i\u0323 you\u0301 a你i aЖyou iअ\n' * 15, ['latin_undetermined']),
            ('baby love you ' * 100, ['en']),
            ('tôi ơi người trời đời lời\n' * 12, ['latin_undetermined']),
        ]
        for text, expected in cases:
            with self.subTest(expected=expected, sample=text[:20]):
                self.assertEqual(self.hints(derive_lyric_features({'lyric': text})), expected)

    def test_validation_does_not_mutate_features(self):
        result = self.good_features()
        original = copy.deepcopy(result)
        self.assertEqual(self.hints(result), ['en'])
        self.assertEqual(result, original)

    def test_legacy_score_upper_bound_rejects_a_sparse_hint(self):
        result = {'script_counts': {'latin': 1000}, 'language_hints': ['en'],
                  'latin_language_scores': {'en': 8}}
        self.assertEqual(self.hints(result), ['latin_undetermined'])

    def test_legacy_score_upper_bound_does_not_reject_a_possible_hint(self):
        result = {'script_counts': {'latin': 320}, 'language_hints': ['en'],
                  'latin_language_scores': {'en': 8}}
        self.assertEqual(self.hints(result), ['en'])

    def test_legacy_without_score_preserves_minimal_hint_compatibility(self):
        self.assertEqual(self.hints({'language_hints': ['en']}), ['en'])
        self.assertEqual(self.hints({'language_hints': ['fr', 'en', 'fr', 'ko']}), ['en', 'fr'])

    def test_legacy_invalid_or_missing_specific_score_does_not_authorize_it(self):
        for scores in (None, [], {}, {'en': True}, {'en': -1}, {'en': '8'}, {'en': 0}):
            with self.subTest(scores=scores):
                self.assertEqual(self.hints({'script_counts': {'latin': 500},
                                             'language_hints': ['en'],
                                             'latin_language_scores': scores}), ['latin_undetermined'])

    def test_legacy_all_candidates_are_checked_independently(self):
        result = {'script_counts': {'latin': 1000}, 'language_hints': ['fr', 'en'],
                  'latin_language_scores': {'fr': 100, 'en': 8}}
        self.assertEqual(self.hints(result), ['fr'])

    def test_legacy_unspecified_latin_body_stays_undetermined(self):
        self.assertEqual(self.hints({'script_counts': {'latin': 50}, 'language_hints': []}),
                         ['latin_undetermined'])
        self.assertEqual(self.hints({'script_counts': {'latin': 49}, 'language_hints': []}), [])


if __name__ == '__main__':
    unittest.main()
