"""Build deterministic offline organizing candidates from a cache snapshot.

Raw decimal cache IDs are evidence for later official online mapping. They are
never converted into an invented official/encrypted ID by this module.
"""

import copy
import re
from datetime import date


_PENDING = "pending_online_validation"
_NAMES = {"funk": "Funk", "我喜欢的音乐-陶喆": "陶喆 · 喜欢的音乐"}
_ORDER = {
    "英语": 0, "日语": 1, "俄语": 2, "韩语": 3, "中文歌": 4, "华语乐坛": 5,
    "老音乐": 6, "用音乐找回过去的自己": 7,
    "陶喆": 8, "陶喆 · 喜欢的音乐": 8,
    "Funk": 9, "纯音乐": 10, "铃声": 11, "Mill": 12,
}


def _decimal_id(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value >= 0 else None
    return value if isinstance(value, str) and value.isascii() and value.isdecimal() else None


def _target_name(name):
    if name in _NAMES:
        return _NAMES[name]
    match = re.fullmatch(r"([0-9]{4})\.([0-9]{1,2})\.([0-9]{1,2})", name)
    if match:
        try:
            return date(*(int(part) for part in match.groups())).isoformat()
        except ValueError:
            pass
    return name


def _order_key(entry):
    index, playlist = entry
    name = _target_name(playlist["name"])
    if name in _ORDER:
        return (_ORDER[name], "", index)
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", name):
        try:
            date.fromisoformat(name)
        except ValueError:
            pass
        else:
            return (13, name, index)
    return (99, "", index)


def build_plan(snapshot) -> dict:
    """Return candidates pending online validation, without mutating input."""
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot 必须是缓存快照对象。")
    source = copy.deepcopy(snapshot.get("source", {}))
    source["online_account_verified"] = False
    playlists = snapshot.get("playlists", [])
    tracks = snapshot.get("tracks", {})
    reasons = ["online_validation_required"]
    if source.get("overview_complete") is not True:
        reasons.append("overview_incomplete")

    eligible, seen = [], set()
    for record in playlists:
        if record.get("owned") is not True or record.get("special_type") == 5:
            continue
        ident = _decimal_id(record.get("id"))
        if ident is None or not isinstance(record.get("name"), str):
            continue
        if ident in seen:
            if "duplicate_cached_playlist_ids" not in reasons:
                reasons.append("duplicate_cached_playlist_ids")
            continue
        seen.add(ident)
        eligible.append({"id": ident, "name": record["name"]})

    jobs = []
    for record in eligible:
        new_name = _target_name(record["name"])
        if new_name != record["name"]:
            jobs.append({
                "id": f"rename:{record['id']}", "kind": "rename_playlist", "status": _PENDING,
                "playlist_id": record["id"], "old_name": record["name"], "name": new_name,
            })
    current = [record["id"] for record in eligible]
    ordered = [record["id"] for _, record in sorted(enumerate(eligible), key=_order_key)]
    if ordered != current:
        jobs.append({
            "id": "reorder:owned", "kind": "reorder_playlists", "status": _PENDING,
            "current_playlist_ids": current, "playlist_ids": ordered,
            "coverage": "cached_owned_non_system_playlists", "append_unrecognized": True,
        })

    liked_candidates = [p for p in playlists if p.get("owned") is True and p.get("special_type") == 5]
    liked = liked_candidates[0] if len(liked_candidates) == 1 else None
    if not liked_candidates:
        reasons.append("liked_playlist_missing")
    elif len(liked_candidates) > 1:
        reasons.append("liked_playlist_ambiguous")
    missing = []
    artists, names_to_ids = {}, {}
    members = []
    membership_available = bool(liked and liked.get("membership_available") is True)
    liked_complete = bool(liked and liked.get("snapshot_complete") is True)
    if liked:
        if not liked_complete:
            reasons.append("liked_snapshot_incomplete")
        if not membership_available:
            reasons.append("liked_membership_unavailable")
        else:
            seen_members = set()
            for raw_id in liked.get("members", []):
                tid = _decimal_id(raw_id)
                if tid is None or tid in seen_members:
                    continue
                seen_members.add(tid)
                members.append(tid)
                track = tracks.get(tid)
                if not isinstance(track, dict) or track.get("metadata_available") is not True:
                    missing.append(tid)
                if not isinstance(track, dict) or not isinstance(track.get("artists"), list):
                    continue
                credited = set()
                for artist in track["artists"]:
                    if not isinstance(artist, dict):
                        continue
                    aid = _decimal_id(artist.get("id"))
                    name = artist.get("name")
                    if aid is None or not isinstance(name, str) or not name.strip():
                        continue
                    name = name.strip()
                    names_to_ids.setdefault(name, set()).add(aid)
                    if aid in credited:
                        continue
                    credited.add(aid)
                    candidate = artists.setdefault(aid, {"id": aid, "name": name, "track_ids": [], "first_seen": len(artists)})
                    candidate["track_ids"].append(tid)
    if missing:
        reasons.append("liked_metadata_incomplete")
    ranked = sorted(artists.values(), key=lambda artist: (-len(artist["track_ids"]), artist["first_seen"]))
    for artist in [a for a in ranked if len(a["track_ids"]) >= 8][:5]:
        name = f"{artist['name']} · 红心精选"
        if len(names_to_ids[artist["name"]]) > 1:
            name += f"（ID {artist['id']}）"
        jobs.append({
            "id": f"artist:{artist['id']}", "kind": "create_artist_playlist", "status": _PENDING,
            "name": name, "artist": {"id": artist["id"], "name": artist["name"]},
            "source_playlist_id": _decimal_id(liked.get("id")),
            "candidate_track_ids": list(artist["track_ids"]),
            "track_id_format": "raw_decimal", "official_track_id_mapping_required": True,
            "intended_visibility": "private", "liked_snapshot_complete": liked_complete,
        })
    if any(job["kind"] == "create_artist_playlist" for job in jobs):
        reasons.append("official_track_id_mapping_required")

    summary = {
        "cached_playlist_count": len(playlists), "organizable_owned_playlist_count": len(eligible),
        "liked_playlist_id": _decimal_id(liked.get("id")) if liked else None,
        "liked_snapshot_complete": liked_complete, "liked_membership_available": membership_available,
        "cached_liked_unique_track_count": len(members), "missing_metadata_track_ids": missing,
        "rename_count": sum(j["kind"] == "rename_playlist" for j in jobs),
        "reorder_count": sum(j["kind"] == "reorder_playlists" for j in jobs),
        "artist_playlist_count": sum(j["kind"] == "create_artist_playlist" for j in jobs),
        "job_count": len(jobs),
    }
    return {
        "kind": "offline_organizing_plan", "schema_version": 1,
        "owner_id": snapshot.get("owner_id"), "source": source, "summary": summary,
        "jobs": jobs, "blocked_reasons": reasons,
        "online_account_verified": False, "applied_to_account": False,
    }
