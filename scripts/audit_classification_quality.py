"""Read a saved report and create separate offline diagnostic drafts.

This script has no network or account-operation dependencies. Existing files
are never replaced, including an earlier audit or manually reviewed pilot.
"""

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from netease_organizer.classification_quality import (
    CONFLICT_REASON,
    INCOMPLETE_EVIDENCE_REASON,
    LOW_SCORE_REASON,
    SCENE_RULES,
    WEAK_EVIDENCE_REASON,
    diagnostic_strata,
    review_reasons,
    select_pilot,
    version_hints,
)


MAX_SOURCE_BYTES = 16 * 1024 * 1024
REPORT_NAME = "quality-report.json"
PILOT_NAME = "pilot-review.json"
_TEXT_FIELDS = ("name", "artists", "language", "evidence_note", "language_evidence_note")
_LIST_FIELDS = ("styles", "scenes", "pending_reasons")


def _read_source(source):
    if source.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("来源报告超过 16 MiB，未读取或生成任何报告。")
    with source.open("rb") as handle:
        raw = handle.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("来源报告超过 16 MiB，未生成任何报告。")
    return raw


def _invalid_constant(value):
    raise ValueError("来源报告含非有限数字。")


def _load_records(raw):
    try:
        document = json.loads(raw.decode("utf-8-sig"), parse_constant=_invalid_constant)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("来源不是兼容的 UTF-8 JSON 分类报告。") from exc
    if type(document) is not dict or document.get("kind") != "classification_report" or type(document.get("records")) is not list:
        raise ValueError("来源必须是已有的逐曲分类报告，不能是分类计划或质量草稿。")
    records = document["records"]
    for record in records:
        if type(record) is not dict:
            raise ValueError("来源逐曲记录格式不兼容。")
        if any(type(record.get(key)) is not str for key in _TEXT_FIELDS):
            raise ValueError("来源逐曲文字字段不兼容。")
        if any(type(record.get(key)) is not list or any(type(value) is not str for value in record[key]) for key in _LIST_FIELDS):
            raise ValueError("来源逐曲分类或待辨识字段不兼容。")
        score = record.get("style_judgment_score")
        if type(score) not in (int, float) or not 0 <= score <= 1 or not math.isfinite(score):
            raise ValueError("来源风格判断分数必须是 0 到 1 的有限数字。")
        if type(record.get("review_note")) is not bool:
            raise ValueError("来源复核标记必须是布尔值。")
    # Also reject duplicate, missing, boolean or non-positive positions.
    select_pilot(records, limit=0)
    return records


def _public_record(record):
    return {
        "position": record["position"],
        **{key: record[key] for key in _TEXT_FIELDS},
        **{key: list(record[key]) for key in _LIST_FIELDS},
        "style_judgment_score": record["style_judgment_score"],
        "review_note": record["review_note"],
    }


def _summary(records, selected):
    reason_counts = Counter(reason for record in records for reason in review_reasons(record))
    strata_counts = Counter(value for record in records for value in diagnostic_strata(record))
    return {
        "source_count": len(records),
        "needs_review_count": sum(bool(review_reasons(record)) for record in records),
        "low_score_without_pending_count": sum(LOW_SCORE_REASON in review_reasons(record) and not record["pending_reasons"] for record in records),
        "conflict_without_pending_count": sum(CONFLICT_REASON in review_reasons(record) and not record["pending_reasons"] for record in records),
        "generic_evidence_count": sum(WEAK_EVIDENCE_REASON in review_reasons(record) for record in records),
        "explicit_unverified_evidence_count": sum(INCOMPLETE_EVIDENCE_REASON in review_reasons(record) for record in records),
        "reason_counts": dict(sorted(reason_counts.items())),
        "diagnostic_layer_counts": dict(sorted(strata_counts.items())),
        "pilot_count": len(selected),
        "pilot_positions": selected,
        "classification_counts": {
            dimension: dict(sorted(Counter(
                value for record in records
                for value in set(record[key] if type(record[key]) is list else [record[key]])
            ).items()))
            for dimension, key in (("style", "styles"), ("scene", "scenes"), ("language", "language"))
        },
    }


def audit(source, output_dir, limit=50):
    """Create both drafts exclusively, after validating source and destinations."""
    source = Path(source).resolve()
    output_dir = Path(output_dir).resolve()
    if output_dir == source.parent:
        raise ValueError("请使用独立输出目录，不能直接写入来源成果目录。")
    targets = (output_dir / REPORT_NAME, output_dir / PILOT_NAME)
    if any(target.exists() or target.is_symlink() for target in targets):
        raise ValueError("输出报告已存在；请选新目录，现有报告和试点不会被覆盖。")
    if output_dir.exists() and not output_dir.is_dir():
        raise ValueError("输出路径必须是独立目录。")
    raw = _read_source(source)
    source_hash = hashlib.sha256(raw).hexdigest()
    records = _load_records(raw)
    selected = select_pilot(records, limit=limit)
    summary = _summary(records, selected)
    # A concurrent edit must not produce a draft claiming a coherent snapshot.
    if hashlib.sha256(_read_source(source)).hexdigest() != source_hash:
        raise ValueError("读取期间来源报告发生变化，未生成报告；请重新读取。")
    common = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_name": source.name,
        "source_sha256_before": source_hash,
        "source_sha256_after": source_hash,
        "source_size_bytes": len(raw),
        "status": "diagnostic_draft",
        "limitations": [
            "复核原因表示证据或判断需要检查，不表示歌曲已经分错。",
            "模型自评分未经校准，不能解释为分类准确率。",
            "这是覆盖不同问题的诊断试点，不是随机样本，不能据此估算全库错误率。",
            "标题里的版本词只是核对提示；风格、能量与场景适配需要核对具体录音并试听。",
            "本文件不是正式分类或账号执行计划；评估尚未完成时不应自动更改歌单。",
        ],
    }
    quality = {
        **common,
        "kind": "classification_quality_audit",
        "summary": summary,
        "flagged_records": [
            {**_public_record(record), "review_reasons": review_reasons(record)}
            for record in records if review_reasons(record)
        ],
    }
    selected_set = set(selected)
    pilot = {
        **common,
        "kind": "classification_quality_pilot",
        "selection_method": "rare_feature_coverage_then_layer_source_and_artist_spread",
        "scene_rules": SCENE_RULES,
        "records": [
            {
                **_public_record(record),
                "review_reasons": review_reasons(record),
                "diagnostic_layers": diagnostic_strata(record),
                "version_hints": version_hints(record),
                "evaluation": {
                    "evaluated": False,
                    "recording_version": None,
                    "evidence_sources": [],
                    "listened_segments": [],
                    "verified_styles": None,
                    "verified_language": None,
                    "scene_decisions": [],
                    "notes": "",
                },
            }
            for record in records if record["position"] in selected_set
        ],
    }
    payloads = [json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8") + b"\n" for document in (quality, pilot)]
    output_dir.mkdir(parents=True, exist_ok=True)
    # Reserve both files before writing. Exclusive creation also covers races.
    handles = []
    try:
        for target in targets:
            handles.append(target.open("xb"))
        for handle, payload in zip(handles, payloads):
            handle.write(payload)
            handle.flush()
    finally:
        for handle in handles:
            handle.close()
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="离线分类质量诊断，只生成独立草稿。")
    parser.add_argument("--input", type=Path, default=ROOT / "artifacts" / "全库分类-逐曲结果.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts" / "classification-quality-20261007")
    parser.add_argument("--limit", type=int, default=50, help="诊断试点数量，默认 50 首。")
    args = parser.parse_args(argv)
    try:
        summary = audit(args.input, args.output_dir, args.limit)
    except (OSError, ValueError) as exc:
        print(f"分类质量审计失败：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
