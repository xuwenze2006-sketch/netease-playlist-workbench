"""Derive classification clues from original lyrics without retaining lyric text."""

import re
import unicodedata
from collections import Counter

from .latin_evidence import derive_latin_evidence

_STAMP = re.compile(r'\[[0-9]{2,}:[0-5][0-9](?:\.[0-9]+)?\]|<[0-9]{2,}:[0-5][0-9]\.[0-9]+>')
_METADATA = re.compile(r'\[\s*(?:ar|ti|al|by|offset|re|ve|length)\s*:[^\[\]]*\]', re.I)
_SECTION = re.compile(
    r'\[\s*(?:intro|outro|verse|chorus|bridge|pre-chorus|post-chorus|instrumental)'
    r'(?:\s+[0-9]+)?\s*\]', re.I,
)
_WRAPPED_LINE = re.compile(r'\[([^\[\]]*)\]|\(([^()]*)\)')
_CREDIT = re.compile(
    r'^(?:(?:作词|作曲|词|曲|编曲|制作|制作人|录音|混音|母带|监制|发行|出品|版权|'
    r'企划|统筹|和声|吉他|贝斯|鼓|键盘|演唱|歌手|专辑|歌词|翻译|译者'
    r')\s*[:-]|(?:composer|composed|lyricist|arranger|arranged|producer|produced|'
    r'writer|written|vocals?|lyrics|music|mixed|mixing|mastered|recorded|recording|'
    r'copyright|all rights)(?:\s+by)?\s*:)', re.I,
)
_INSTRUMENTAL_LINE = re.compile(r'纯音乐\s*[,，、:：;；。\-—]?\s*(?:请欣赏|无歌词)\s*[。.!！]*')
# These elongation, iteration, omission and filler characters are letters in
# Unicode, but cannot independently establish a lyric body or singing language.
_DECORATIVE_LETTERS = frozenset('ーゝゞヽヾـๆฯ\u115f\u1160\u3164\uffa0')
_YUE = set('嘅唔佢啲咗冇嘢喺嗰嚟攰嘥瞓睇俾畀咁係')
_TOPICS = {
    'solitude': ('lonely', 'alone', 'miss', 'tears', 'cry', 'goodbye', '孤独', '寂寞', '眼泪', '想念', '离开', '再见'),
    'affection': ('love', 'heart', 'baby', 'darling', '喜欢', '爱情', '温柔', '拥抱', '陪伴'),
    'uplift': ('dream', 'hope', 'alive', 'shine', 'strong', '梦想', '希望', '明天', '勇敢', '阳光'),
    'party': ('dance', 'party', 'groove', 'funk', 'disco', '跳舞', '狂欢', '舞池'),
}


def _is_substantive_letter(char):
    return unicodedata.category(char).startswith('L') and char not in _DECORATIVE_LETTERS


def _is_credit(text):
    wrapped = _WRAPPED_LINE.fullmatch(text)
    if wrapped:
        text = next(group for group in wrapped.groups() if group is not None).strip()
    return bool(_CREDIT.match(text))


def derive_lyric_features(data):
    """Language/script/topic evidence only; absence of lyrics never means instrumental."""
    if type(data) is not dict:
        raise ValueError('歌词资料格式不可用。')
    lyric = data.get('lyric') or data.get('txtLyric') or ''
    if type(lyric) is not str or len(lyric) > 300000:
        raise ValueError('歌词资料格式不可用。')
    lines = []
    instrumental_marker = data.get('pureMusic') is True
    for raw in lyric.splitlines():
        text = unicodedata.normalize('NFKC', raw)
        # Separators prevent timed word fragments from becoming familiar words.
        text = _STAMP.sub(' ', text).strip()
        if not text or _METADATA.fullmatch(text) or _SECTION.fullmatch(text) or _is_credit(text):
            continue
        # A provider description is evidence, not lyric text. A sentence that
        # merely contains those words must still be retained as body text.
        if _INSTRUMENTAL_LINE.fullmatch(text):
            instrumental_marker = True
            continue
        if any(_is_substantive_letter(char) for char in text):
            lines.append(text)
    text = '\n'.join(lines).casefold()
    scripts = Counter()
    for char in text:
        if not _is_substantive_letter(char):
            continue
        value = ord(char)
        if 0x3040 <= value <= 0x30ff or 0x31f0 <= value <= 0x31ff:
            scripts['kana'] += 1
        elif 0xac00 <= value <= 0xd7af or 0x1100 <= value <= 0x11ff:
            scripts['hangul'] += 1
        elif 0x4e00 <= value <= 0x9fff or 0x3400 <= value <= 0x4dbf:
            scripts['han'] += 1
        elif 0x0400 <= value <= 0x052f:
            scripts['cyrillic'] += 1
        elif 0x0600 <= value <= 0x06ff:
            scripts['arabic'] += 1
        elif 0x0e00 <= value <= 0x0e7f:
            scripts['thai'] += 1
        elif 'a' <= char <= 'z' or (char.isalpha() and 'LATIN' in unicodedata.name(char, '')):
            scripts['latin'] += 1
    latin = derive_latin_evidence(text, scripts['latin'])
    hints = []
    if scripts['kana'] >= 6:
        hints.append('ja')
    if scripts['hangul'] >= 10:
        hints.append('ko')
    if scripts['han'] >= 20 and scripts['kana'] < max(6, scripts['han'] * .04):
        hints.append('zh_or_yue')
    if scripts['cyrillic'] >= 10:
        hints.append('ru_or_other_cyrillic')
    if scripts['arabic'] >= 10 or scripts['thai'] >= 10:
        hints.append('other_script')
    hints.extend(latin['hints'])
    return {
        'has_original_lyric_text': bool(lines),
        'provider_no_lyric': data.get('noLyric') is True,
        'instrumental_evidence': instrumental_marker,
        'original_line_count': len(lines),
        'script_counts': {k: scripts[k] for k in ('han', 'kana', 'hangul', 'cyrillic', 'latin', 'arabic', 'thai')},
        'latin_language_scores': latin['scores'],
        'latin_evidence': latin['evidence'],
        'language_hints': hints,
        'cantonese_specific_character_count': sum(text.count(c) for c in _YUE),
        'ukrainian_specific_character_count': sum(text.count(c) for c in 'іїєґ'),
        'topic_counts': {topic: sum(text.count(term) for term in terms) for topic, terms in _TOPICS.items()},
        'evidence_source': 'official_original_lyric_derived',
    }
