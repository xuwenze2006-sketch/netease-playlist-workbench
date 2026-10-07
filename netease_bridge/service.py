"""Validated, paginated read tools and explicitly local playlist previews."""

import json
from pathlib import Path

from .cache import CacheError, CacheReader, default_data_dir, source_signature


class ToolFailure(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


_PAGING = {
    "offset": {"type": "integer", "minimum": 0, "default": 0},
    "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
}


def _definition(name, description, properties, required=()):
    return {
        "name": name, "description": description,
        "inputSchema": {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
    }


_TOOLS = [
    _definition("desktop_status", "读取本地网易云桌面缓存状态及能力；不证明在线账号当前状态。", {}),
    _definition("list_playlists", "分页读取缓存的自建/收藏歌单及完整性标记。", {"kind": {"type": "string", "enum": ["all", "owned", "collected"], "default": "all"}, **_PAGING}),
    _definition("get_playlist_tracks", "按缓存顺序分页读歌单成员；缺失或旧缓存会明确报告。", {"playlist_id": {"type": "string", "minLength": 1}, **_PAGING}, ("playlist_id",)),
    _definition("search_tracks", "在指定缓存歌单内搜索歌名、歌手、专辑，默认红心歌单。", {"playlist_id": {"type": "string"}, "query": {"type": "string", "minLength": 1}, **_PAGING}, ("query",)),
    _definition("preview_artist_playlist", "从完整的缓存歌单按精确歌手生成本地草案，包含合作曲；不写回账号。", {"playlist_id": {"type": "string"}, "artist_id": {"type": "string", "minLength": 1}, "artist_name": {"type": "string", "minLength": 1}, "name": {"type": "string", "minLength": 1}}),
]


class BridgeService:
    def __init__(self, data_dir: Path | None = None):
        self.data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
        self._cached = None
        self._signature = None

    def list_tools(self):
        return json.loads(json.dumps(_TOOLS))

    def _snapshot(self):
        signature = source_signature(self.data_dir)
        if self._cached is None or signature != self._signature:
            self._cached = CacheReader(self.data_dir).load()
            # Keep the pre-read signature. A source change during the read will
            # trigger a fresh snapshot on the next call, rather than pin old data.
            self._signature = signature
        return self._cached

    def _validate(self, tool, arguments):
        if not isinstance(arguments, dict):
            raise ValueError("arguments 必须是对象。")
        schema = tool["inputSchema"]
        if set(arguments) - set(schema["properties"]):
            raise ValueError("参数包含不支持的字段。")
        if any(key not in arguments for key in schema["required"]):
            raise ValueError("缺少必要参数。")
        for key, value in arguments.items():
            definition = schema["properties"][key]
            if definition["type"] == "integer":
                if isinstance(value, bool) or not isinstance(value, int) or value < definition.get("minimum", 0) or value > definition.get("maximum", value):
                    raise ValueError("分页参数范围不合法。")
            elif not isinstance(value, str) or not value.strip():
                raise ValueError("文本参数不能为空。")
            if "enum" in definition and value not in definition["enum"]:
                raise ValueError("筛选类别不合法。")
        if tool["name"] == "preview_artist_playlist" and ("artist_id" in arguments) == ("artist_name" in arguments):
            raise ValueError("artist_id 和 artist_name 必须且只能指定一个。")

    def call_tool(self, name, arguments):
        tool = next((tool for tool in _TOOLS if tool["name"] == name), None)
        if tool is None:
            raise KeyError("unknown tool")
        self._validate(tool, arguments)
        try:
            data = self._dispatch(name, arguments, self._snapshot())
            return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}], "structuredContent": data, "isError": False}
        except (CacheError, ToolFailure) as error:
            data = {"error": {"code": error.code if isinstance(error, ToolFailure) else "cache_unavailable", "message": str(error)}, "applied_to_account": False}
            return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}], "structuredContent": data, "isError": True}

    def _playlist(self, snapshot, requested=None):
        if requested is None:
            playlist = next((p for p in snapshot["playlists"] if p["owned"] and p["special_type"] == 5), None)
        else:
            playlist = next((p for p in snapshot["playlists"] if p["id"] == requested), None)
        if playlist is None:
            raise ToolFailure("playlist_not_found", "本地歌单快照中未找到指定歌单。")
        return playlist

    @staticmethod
    def _summary(playlist):
        return {key: value for key, value in playlist.items() if key != "members"}

    @staticmethod
    def _page(items, args):
        offset, limit = args.get("offset", 0), args.get("limit", 50)
        end = min(offset + limit, len(items))
        return {"total": len(items), "offset": offset, "limit": limit, "items": items[offset:end], "next_offset": end if end < len(items) else None}

    @staticmethod
    def _songs(snapshot, playlist):
        if not playlist["membership_available"]:
            raise ToolFailure("membership_unavailable", "本地缺少这个歌单的成员清单，不能把它视为空歌单。")
        return [snapshot["tracks"].get(tid, {"id": tid, "name": None, "artists": [], "album": {}, "metadata_available": False}) for tid in playlist["members"]]

    def _dispatch(self, name, args, snapshot):
        if name == "desktop_status":
            return {
                "source": snapshot["source"], "online_account_verified": False,
                "capabilities": {"local_playlist_read": True, "local_preview": True, "account_login": False, "account_writeback": False, "native_ui_control": False},
                "playlist_counts": {"all": len(snapshot["playlists"]), "owned": sum(p["owned"] for p in snapshot["playlists"]), "collected": sum(not p["owned"] for p in snapshot["playlists"])},
                "snapshot_complete_playlist_count": sum(p["snapshot_complete"] for p in snapshot["playlists"]),
                "writeback_reason": "当前原型没有受支持的官方账号操作接口。",
            }
        if name == "list_playlists":
            kind = args.get("kind", "all")
            items = [self._summary(p) for p in snapshot["playlists"] if kind == "all" or p["owned"] == (kind == "owned")]
            return {**self._page(items, args), "source": snapshot["source"]}
        playlist = self._playlist(snapshot, args.get("playlist_id"))
        songs = self._songs(snapshot, playlist)
        if name == "get_playlist_tracks":
            return {**self._page(songs, args), "playlist": self._summary(playlist), "source": snapshot["source"]}
        if name == "search_tracks":
            query = args["query"].casefold()
            matches = [song for song in songs if query in " ".join([song.get("name") or "", song.get("album", {}).get("name", ""), *(a["name"] for a in song["artists"])]).casefold()]
            return {**self._page(matches, args), "playlist": self._summary(playlist), "source": snapshot["source"]}
        if not playlist["snapshot_complete"]:
            raise ToolFailure("incomplete_source", "这个歌单的成员缓存不完整或已旧，不能据此生成完整歌手精选。")
        artists = {artist["id"]: artist["name"] for song in songs for artist in song["artists"] if artist["id"] is not None}
        artist_id = args.get("artist_id")
        if artist_id is None:
            candidates = {artist["id"] for song in songs for artist in song["artists"] if artist["name"] == args["artist_name"] and artist["id"] is not None}
            if len(candidates) > 1:
                raise ToolFailure("ambiguous_artist", "这个歌手名对应多个歌手 ID，请先搜索歌曲并指定 artist_id。")
            artist_id = next(iter(candidates), None)
        if artist_id not in artists:
            raise ToolFailure("artist_not_found", "指定歌手未出现在这个缓存歌单中。")
        chosen, seen = [], set()
        for song in songs:
            if song["id"] not in seen and any(a["id"] == artist_id for a in song["artists"]):
                chosen.append(song)
                seen.add(song["id"])
        return {
            "kind": "local_playlist_draft", "name": args.get("name", f"{artists[artist_id]} · 收藏精选"),
            "source_playlist": self._summary(playlist), "artist": {"id": artist_id, "name": artists[artist_id]},
            "track_count": len(chosen), "track_ids": [song["id"] for song in chosen], "tracks": chosen,
            "applied_to_account": False, "online_account_verified": False, "source": snapshot["source"],
        }
