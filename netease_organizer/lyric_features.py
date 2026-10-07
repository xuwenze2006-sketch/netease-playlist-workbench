"""Derive classification clues from original lyrics without retaining lyric text."""

import re
import unicodedata
from collections import Counter

_STAMP = re.compile(r'\[[^\]]{0,120}\]|<\d[^>]{0,30}>')
_CREDIT = re.compile(
    r'^(作词|作曲|词|曲|编曲|制作|制作人|录音|混音|母带|监制|发行|出品|版权|'
    r'企划|统筹|和声|吉他|贝斯|鼓|键盘|演唱|歌手|专辑|歌词|翻译|译者|'
    r'composer|lyricist|arranger|producer|writer|written|vocal|lyrics|music|'
    r'mixed|mixing|mastered|recorded|recording|copyright|all rights)\s*[:：-]', re.I,
)
_YUE = set('嘅唔佢啲咗冇嘢喺嗰嚟攰嘥瞓睇俾畀咁係')
_FUNCTIONS = {
    'en': 'the and you your i my me we our it is are was were that this with for to of in on don can will what when how not love baby',
    'es': 'el los las una unos unas yo tu tus mis que por para con pero como muy eres estoy amor corazon corazón',
    'fr': 'le les une des je suis toi moi nous vous avec dans pour pas mon ma mes ton ta tes amour',
    'pt': 'eu você voce vocês voces não nao meu minha meus minhas pra isso estou tenho coração coracao amor saudade',
    'de': 'ich du dein deine dich mich wir nicht und ist bin das auf mit für fuer mein meine',
    'it': 'il gli io sei sono mio mia noi voi nella della questo questa cuore amore',
}
_FUNCTIONS = {k: set(v.split()) for k, v in _FUNCTIONS.items()}
_TOPICS = {
    'solitude': ('lonely', 'alone', 'miss', 'tears', 'cry', 'goodbye', '孤独', '寂寞', '眼泪', '想念', '离开', '再见'),
    'affection': ('love', 'heart', 'baby', 'darling', '喜欢', '爱情', '温柔', '拥抱', '陪伴'),
    'uplift': ('dream', 'hope', 'alive', 'shine', 'strong', '梦想', '希望', '明天', '勇敢', '阳光'),
    'party': ('dance', 'party', 'groove', 'funk', 'disco', '跳舞', '狂欢', '舞池'),
}


def derive_lyric_features(data):
    """Language/script/topic evidence only; absence of lyrics never means instrumental."""
    if type(data) is not dict:
        raise ValueError('歌词资料格式不可用。')
    lyric = data.get('lyric') or data.get('txtLyric') or ''
    if type(lyric) is not str or len(lyric) > 300000:
        raise ValueError('歌词资料格式不可用。')
    lines = []
    for raw in lyric.splitlines():
        text = _STAMP.sub('', raw).strip()
        if not text or _CREDIT.match(text):
            continue
        lines.append(text)
    instrumental_marker = data.get('pureMusic') is True or (
        len(lines) <= 3 and any('纯音乐' in x and ('欣赏' in x or '无歌词' in x) for x in lines)
    )
    text = unicodedata.normalize('NFKC', '\n'.join(lines)).casefold()
    scripts = Counter()
    for char in text:
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
    words = Counter(re.findall(r"[a-zÀ-ÖØ-öø-ÿ]+", text))
    scores = {language: sum(words[w] for w in terms) for language, terms in _FUNCTIONS.items()}
    hints = []
    if not instrumental_marker:
        if scripts['kana'] >= 6:
            hints.append('ja')
        if scripts['hangul'] >= 10:
            hints.append('ko')
        if scripts['han'] >= 20 and scripts['kana'] < 6:
            hints.append('zh_or_yue')
        if scripts['cyrillic'] >= 10:
            hints.append('ru_or_other_cyrillic')
        if scripts['arabic'] >= 10 or scripts['thai'] >= 10:
            hints.append('other_script')
        ranked = sorted(scores, key=scores.get, reverse=True)
        if scripts['latin'] >= 50:
            best = ranked[0]
            if scores[best] >= 8 and scores[best] >= scores[ranked[1]] * 1.4:
                hints.append(best)
            else:
                hints.append('latin_undetermined')
    return {
        'has_original_lyric_text': bool(lines) and not instrumental_marker,
        'provider_no_lyric': data.get('noLyric') is True,
        'instrumental_evidence': instrumental_marker,
        'original_line_count': len(lines),
        'script_counts': {k: scripts[k] for k in ('han', 'kana', 'hangul', 'cyrillic', 'latin', 'arabic', 'thai')},
        'latin_language_scores': scores,
        'language_hints': hints,
        'cantonese_specific_character_count': sum(text.count(c) for c in _YUE),
        'ukrainian_specific_character_count': sum(text.count(c) for c in 'іїєґ'),
        'topic_counts': {topic: sum(text.count(term) for term in terms) for topic, terms in _TOPICS.items()},
        'evidence_source': 'official_original_lyric_derived',
    }
