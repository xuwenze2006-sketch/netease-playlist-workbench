"""Conservative Latin-language clues, retaining counts rather than lyric words."""

import unicodedata
from collections import Counter


_FUNCTIONS = {
    'en': 'the and you your i my me we our it is are was were that this with for to of in on don can will what when how not love baby',
    'es': 'el los las una unos unas yo tu tus mis que por para con pero como muy eres estoy amor corazon corazón',
    'fr': 'le les une des je suis toi moi nous vous avec dans pour pas mon ma mes ton ta tes amour',
    'pt': 'eu você voce vocês voces não nao meu minha meus minhas pra isso estou tenho coração coracao amor saudade',
    'de': 'ich du dein deine dich mich wir nicht und ist bin das auf mit für fuer mein meine',
    'it': 'il gli io sei sono mio mia noi voi nella della questo questa cuore amore',
}
_FUNCTIONS = {language: frozenset(terms.split()) for language, terms in _FUNCTIONS.items()}
_LENGTH_BOUNDS = {
    language: (min(map(len, terms)), max(map(len, terms)))
    for language, terms in _FUNCTIONS.items()
}
_METRICS = {'distinct_words', 'capped_hits', 'matched_letters'}


def _latin_words(text):
    # Keep complete Unicode letter/mark runs. In particular, neither Vietnamese
    # diacritics nor a non-Latin letter may expose a familiar word fragment.
    word = []
    for char in text:
        if unicodedata.category(char)[0] in ('L', 'M'):
            word.append(char)
        elif word:
            yield ''.join(word)
            word = []
    if word:
        yield ''.join(word)


def _is_latin_word(word):
    letters = [char for char in word if unicodedata.category(char).startswith('L')]
    return bool(letters) and all('LATIN' in unicodedata.name(char, '') for char in letters)


def _candidates(evidence):
    letters = evidence['letter_count']
    if letters < 50:
        return []
    hints = [language for language in _FUNCTIONS
             for metric in (evidence['languages'][language],)
             if metric['distinct_words'] >= 3 and metric['capped_hits'] >= 8
             and metric['matched_letters'] * 10 >= letters]
    return hints or ['latin_undetermined']


def derive_latin_evidence(normalized_text, latin_letter_count):
    """Derive raw compatibility scores and versioned, count-only evidence."""
    words = Counter(word for word in _latin_words(normalized_text) if _is_latin_word(word))
    scores = {}
    languages = {}
    for language, terms in _FUNCTIONS.items():
        matches = {word: words[word] for word in terms if words[word]}
        scores[language] = sum(matches.values())
        languages[language] = {
            'distinct_words': len(matches),
            'capped_hits': sum(min(count, 3) for count in matches.values()),
            'matched_letters': sum(len(word) * count for word, count in matches.items()),
        }
    evidence = {
        'version': 1,
        'letter_count': latin_letter_count,
        'word_count': sum(words.values()),
        'languages': languages,
    }
    return {'scores': scores, 'hints': _candidates(evidence), 'evidence': evidence}


def _count(value):
    return type(value) is int and value >= 0


def _matched_bounds(language, distinct, capped, word_count):
    """Bound letters using distinct terms and whether any term can repeat freely."""
    if not distinct:
        return 0, 0
    lengths = sorted(map(len, _FUNCTIONS[language]))

    def capped_letters(ordered):
        selected = ordered[:distinct]
        total = sum(selected)  # Each distinct term occurs at least once.
        extra = capped - distinct
        for length in selected:
            hits = min(2, extra)
            total += length * hits
            extra -= hits
        return total

    minimum = capped_letters(lengths)
    maximum = capped_letters(lengths[::-1])
    # More than three occurrences are possible only if a term reaches its cap.
    # Otherwise every occurrence is already represented by capped_hits.
    if capped >= distinct + 2:
        maximum += (word_count - capped) * lengths[-1]
    return minimum, maximum


def _valid_evidence(evidence, latin_count):
    if (type(evidence) is not dict
            or set(evidence) != {'version', 'letter_count', 'word_count', 'languages'}
            or type(evidence['version']) is not int or evidence['version'] != 1
            or not _count(latin_count) or not _count(evidence['letter_count'])
            or evidence['letter_count'] != latin_count
            or not _count(evidence['word_count'])
            or evidence['word_count'] > latin_count):
        return False
    languages = evidence['languages']
    if type(languages) is not dict or set(languages) != set(_FUNCTIONS):
        return False
    for language, metric in languages.items():
        if (type(metric) is not dict or set(metric) != _METRICS
                or not all(_count(value) for value in metric.values())):
            return False
        distinct = metric['distinct_words']
        capped = metric['capped_hits']
        matched = metric['matched_letters']
        if (distinct > len(_FUNCTIONS[language])
                or not distinct <= capped <= distinct * 3
                or capped > evidence['word_count']):
            return False
        minimum, maximum = _matched_bounds(language, distinct, capped, evidence['word_count'])
        if not minimum <= matched <= min(latin_count, maximum):
            return False
        # Every other Latin word also needs at least one letter. The minimum
        # letters represented by capped hits cannot consume that shared budget.
        if latin_count < evidence['word_count'] + minimum - capped:
            return False
    return True


def latin_hints(features):
    """Read new evidence or conservatively retain legacy, un-upgraded hints.

    A legacy score times the longest known word is only a coverage upper bound:
    failing it disproves sufficient coverage; passing it does not prove coverage
    or diversity. Missing/unknown new evidence must never bypass v1 validation.
    """
    scripts = features.get('script_counts', {})
    latin_count = scripts.get('latin') if type(scripts) is dict else None
    if 'latin_evidence' in features:
        evidence = features['latin_evidence']
        if not _valid_evidence(evidence, latin_count):
            return ['latin_undetermined']
        return _candidates(evidence)

    old_hints = features.get('language_hints', [])
    old_hints = {hint for hint in old_hints if type(hint) is str} if type(old_hints) is list else set()
    candidates = [language for language in _FUNCTIONS if language in old_hints]
    if 'latin_language_scores' not in features:
        accepted = candidates
    else:
        scores = features['latin_language_scores']
        accepted = [language for language in candidates
                    if type(scores) is dict and _count(scores.get(language))
                    and scores[language] > 0 and _count(latin_count)
                    and scores[language] * _LENGTH_BOUNDS[language][1] * 10 >= latin_count]
    if accepted:
        return accepted
    if candidates or 'latin_undetermined' in old_hints or (_count(latin_count) and latin_count >= 50):
        return ['latin_undetermined']
    return []
