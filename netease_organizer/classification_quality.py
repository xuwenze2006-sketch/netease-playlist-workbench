"""Offline review signals and a reproducible diagnostic pilot.

Scores are uncalibrated model judgments, not probabilities of correctness.
Recording-version words are prompts to check identity, not verified metadata.
"""

import math
import re
from collections import Counter


GENERIC_EVIDENCE_NOTES = frozenset({
    "具体曲目、艺人与专辑的宽风格模型判断",
    "具体录音、艺人与专辑的宽风格模型判断；听歌场景为建议",
})
LOW_SCORE_REASON = "风格判断把握较低"
WEAK_EVIDENCE_REASON = "风格或场景依据过于宽泛"
INCOMPLETE_EVIDENCE_REASON = "分类依据明确标注待核对"
CONFLICT_REASON = "录音或语言证据存在冲突"

# These additions only discuss language, leaving the generic style/scene basis
# unchanged. Do not treat an arbitrary suffix as generic: it may add real
# recording evidence, which cannot be assessed from a keyword alone.
_LANGUAGE_ONLY_ADDENDA = (
    "语言按具体演唱版本判定，不按歌曲标题或艺人国籍",
    "吟唱使用构造语言，不能视为纯音乐",
)
_INCOMPLETE_NOTE = re.compile(r"待核(?:对)?[。.!！?？\s]*\Z")

# These are listening criteria to calibrate with the user, not audio classifiers.
SCENE_RULES = [
    {
        "scene": "学习专注",
        "include": [
            "试听时感知强度偏低或适中，节奏与动态变化较稳定",
            "没有持续抢占注意力的主唱、对白或明显音效；对人声的容忍度需个人校准",
            "曲内转折和与相邻歌曲的衔接不会频繁打断注意力",
        ],
        "exclude": [
            "反复出现突兀爆发、长段口白或强烈戏剧性停顿",
            "试听中持续让听者转移注意力去跟唱或追随复杂变化",
        ],
    },
    {
        "scene": "通勤散步",
        "include": [
            "试听时推进感适合个人步行习惯，感知强度适中",
            "主体段落的律动连续，曲间衔接不频繁打断行走听感",
            "人声与情绪可接受；同一歌单所需的明快或平静取向需个人校准",
        ],
        "exclude": [
            "长段演讲、对白或静默使本歌单的连续听歌体验中断",
            "曲内反复从极弱跳到极强，超出个人通勤时的舒适范围",
        ],
    },
    {
        "scene": "放松睡前",
        "include": [
            "试听整体感知强度偏低，峰值与段落转换平缓",
            "打击乐、主唱和高频声部不会持续造成紧张或惊醒感",
            "前奏、高潮和尾奏都适合放松；情绪取向需个人校准",
        ],
        "exclude": [
            "安静前奏之后出现强烈鼓点、嘶吼或突兀音效",
            "响度和编制反复剧烈变化，或试听时情绪持续被推向紧张",
        ],
    },
    {
        "scene": "运动提神",
        "include": [
            "试听有持续清晰的节奏支撑和足够的推进感",
            "主体段落的感知强度适合目标运动，不只依靠短暂高潮提神",
            "节奏与个人运动节律协调；不同运动类型需分别校准",
        ],
        "exclude": [
            "长段弱起、静默或慢速散板反复中断目标运动节律",
            "实际录音缺少持续推进感，仅标题或歌词提到运动、力量",
        ],
    },
    {
        "scene": "快乐律动",
        "include": [
            "试听能辨识连续律动，低音与节奏声部形成可跟随的节拍",
            "整体听感符合个人的轻快、愉悦或舞动偏好",
            "前后段落的律动足够一致，与相邻曲目衔接自然",
        ],
        "exclude": [
            "实际录音以沉重、紧张或悲伤听感为主，不符合这份歌单的情绪标准",
            "只有短暂舞曲片段，主体段落无法持续跟随律动",
        ],
    },
    {
        "scene": "独处释怀",
        "include": [
            "试听的演唱、编制与情绪推进符合个人独处时的表达需要",
            "全曲可形成可接受的情绪过程，高潮强度符合个人偏好",
            "歌词含义可辅助理解，但需同时核对具体录音的实际听感",
        ],
        "exclude": [
            "仅因为标题或歌词出现孤独、分手等词就收入，未核对录音听感",
            "实际录音的欢闹、戏谑或持续刺激感与这份歌单的情绪需求冲突",
        ],
    },
]

_VERSION_PATTERNS = (
    ("现场录音", re.compile(r"(?<![a-z])live(?![a-z])|现场", re.I)),
    ("混音", re.compile(r"(?<![a-z])remix(?:ed)?(?![a-z])|混音", re.I)),
    ("翻唱", re.compile(r"(?<![a-z])cover(?![a-z])|翻唱", re.I)),
    ("伴奏或器乐版本", re.compile(r"(?<![a-z])instrumental(?![a-z])|伴奏", re.I)),
    ("不插电或原声版本", re.compile(r"(?<![a-z])(?:acoustic|unplugged)(?![a-z])|不插电", re.I)),
    ("EXPO 版本", re.compile(r"(?<![a-z])expo[\s_-]*ver(?:sion)?\.?(?![a-z])", re.I)),
    ("加速版本", re.compile(r"(?<![a-z])(?:speed|sped)[\s_-]*up(?![a-z])|加速", re.I)),
    ("减速版本", re.compile(r"(?<![a-z])(?:slowed|slow[\s_-]*down)(?![a-z])|减速|降速|慢速版", re.I)),
    ("剪辑或扩展版本", re.compile(r"(?<![a-z])(?:edit|extended)(?![a-z])|剪辑版|加长版", re.I)),
    ("短片段或电视截取版", re.compile(r"(?<![a-z])tv[\s_-]*(?:size|ver(?:sion)?\.?|版)(?![a-z])|片段|截取版|短版", re.I)),
    ("Demo 或试作版本", re.compile(r"(?<![a-z])demo(?![a-z])|试作版", re.I)),
)
_STRATA = ("conflict", "low_score_assigned", "unknown", "weak_evidence", "ordinary", "other")


def _score(record):
    value = record.get("style_judgment_score")
    if type(value) in (int, float) and 0 <= value <= 1 and math.isfinite(value):
        return value
    return None


def _pending(record):
    value = record.get("pending_reasons", [])
    if type(value) is not list:
        return []
    return [item for item in value if type(item) is str and item.strip()]


def review_reasons(record):
    """Explain review priority without changing the assigned classification."""
    reasons = list(dict.fromkeys(_pending(record)))
    if record.get("review_note") is True:
        reasons.append(CONFLICT_REASON)
    score = _score(record)
    if score is not None and score < 0.7:
        reasons.append(LOW_SCORE_REASON)
    note = record.get("evidence_note")
    if type(note) is str:
        normalized = note.strip().rstrip("。.!！;；")
        if (normalized in GENERIC_EVIDENCE_NOTES or any(
                normalized == template + "；" + suffix
                for template in GENERIC_EVIDENCE_NOTES for suffix in _LANGUAGE_ONLY_ADDENDA)):
            reasons.append(WEAK_EVIDENCE_REASON)
        if _INCOMPLETE_NOTE.search(note):
            reasons.append(INCOMPLETE_EVIDENCE_REASON)
    return list(dict.fromkeys(reasons))


def version_hints(record):
    """Find title cues that warrant checking the specific recording version."""
    name = record.get("name", "")
    if type(name) is not str:
        return []
    return [label for label, pattern in _VERSION_PATTERNS if pattern.search(name)]


def diagnostic_strata(record):
    """Return overlapping diagnostic layers; none labels a song as wrong."""
    strata = []
    reasons = review_reasons(record)
    if record.get("review_note") is True:
        strata.append("conflict")
    score = _score(record)
    if score is not None and score < 0.7 and not _pending(record):
        strata.append("low_score_assigned")
    if _pending(record):
        strata.append("unknown")
    if WEAK_EVIDENCE_REASON in reasons or INCOMPLETE_EVIDENCE_REASON in reasons:
        strata.append("weak_evidence")
    if not reasons and score is not None and score >= 0.8:
        strata.append("ordinary")
    return strata or ["other"]


def _features(record):
    features = {("stratum", value) for value in diagnostic_strata(record)}
    features.update(("version", value) for value in version_hints(record))
    for dimension, key in (("style", "styles"), ("scene", "scenes")):
        values = record.get(key, [])
        if type(values) is list:
            features.update((dimension, value) for value in values if type(value) is str and value)
    language = record.get("language")
    if type(language) is str and language:
        features.add(("language", language))
    return features


def select_pilot(records, limit=50):
    """Select a coverage-first diagnostic pilot, returned in source order.

Rare label, review-layer and recording-version features get first coverage.
    Remaining slots balance diagnostic layers, source ranges and artists.
    This is not a random sample and cannot estimate the full library's error
    rate. Inputs remain untouched.
"""
    if type(limit) is not int or limit < 0:
        raise ValueError("试点数量必须是非负整数。")
    records = list(records)
    positions = [record.get("position") for record in records]
    if any(type(position) is not int or position < 1 for position in positions) or len(set(positions)) != len(positions):
        raise ValueError("试点来源需要唯一的正整数歌曲序号。")
    target = min(len(records), limit)
    if not target:
        return []
    features = [_features(record) for record in records]
    frequencies = Counter(feature for values in features for feature in values)
    uncovered = set(frequencies)
    selected = set()
    primary = [diagnostic_strata(record)[0] for record in records]
    artists = [str(record.get("artists", "")).strip().casefold() for record in records]
    bucket_count = min(5, target)
    buckets = [index * bucket_count // len(records) for index in range(len(records))]
    layer_counts = Counter()
    range_counts = Counter()
    layer_range_counts = Counter()
    artist_counts = Counter()

    def spread_key(index):
        # Spread within each available layer before favoring familiar prefixes.
        same_layer = [old for old in selected if primary[old] == primary[index]]
        return (
            -layer_range_counts[primary[index], buckets[index]],
            -range_counts[buckets[index]],
            -artist_counts[artists[index]],
            min((abs(index - old) for old in same_layer), default=len(records)),
            min((abs(index - old) for old in selected), default=len(records)),
            -index,
        )

    def choose(index):
        selected.add(index)
        layer_counts[primary[index]] += 1
        range_counts[buckets[index]] += 1
        layer_range_counts[primary[index], buckets[index]] += 1
        artist_counts[artists[index]] += 1

    while uncovered and len(selected) < target:
        rarest = min(uncovered, key=lambda feature: (frequencies[feature], feature))
        candidates = [index for index, values in enumerate(features) if index not in selected and rarest in values]
        index = max(candidates, key=lambda candidate: (
            sum(1 / frequencies[feature] for feature in sorted(features[candidate] & uncovered)),
            len(features[candidate] & uncovered),
            *spread_key(candidate),
        ))
        choose(index)
        uncovered.difference_update(features[index])

    groups = {stratum: set() for stratum in _STRATA}
    for index, stratum in enumerate(primary):
        if index not in selected:
            groups[stratum].add(index)
    while len(selected) < target:
        available = [stratum for stratum in _STRATA if groups[stratum]]
        stratum = min(available, key=lambda value: (layer_counts[value], _STRATA.index(value)))
        index = max(groups[stratum], key=spread_key)
        groups[stratum].remove(index)
        choose(index)
    return [record["position"] for index, record in enumerate(records) if index in selected]
