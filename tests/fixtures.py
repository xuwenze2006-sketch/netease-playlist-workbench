import json
import sqlite3
from pathlib import Path


def make_cache(root: Path, *, wal: bool = False):
    folder = root / "Library"
    folder.mkdir(parents=True)
    connection = sqlite3.connect(folder / "webdb.dat")
    if wal:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.executescript(
        "CREATE TABLE dbTrack(id TEXT,jsonStr TEXT);"
        "CREATE TABLE playlistTrackIds(id TEXT,jsonStr TEXT);"
        "CREATE TABLE requestCache(id TEXT,jsonStr TEXT);"
    )
    songs = [
        {"id": 1, "name": "一首歌", "artists": [{"id": 10, "name": "歌手甲"}], "album": {"id": 91, "name": "原版"}, "duration": 180000},
        {"id": 2, "name": "Song B", "ar": [{"id": 20, "name": "Artist B"}], "al": {"id": 92, "name": "Album B"}, "dt": 210000},
        {"id": 3, "name": "合作曲", "artists": [{"id": 10, "name": "歌手甲"}, {"id": 20, "name": "Artist B"}], "album": {"id": 93, "name": "合作专辑"}},
        {"id": 4, "name": "一首歌", "artists": [{"id": 10, "name": "歌手甲"}], "album": {"id": 94, "name": "另一个版本"}},
    ]
    for song in songs:
        song["private_extra"] = "SECRET-METADATA-MUST-NOT-LEAK"
        connection.execute("INSERT INTO dbTrack VALUES(?,?)", (str(song["id"]), json.dumps(song)))
    playlists = [
        {"id": 100, "name": "我的喜欢", "userId": 7, "specialType": 5, "subscribed": False, "trackCount": 3, "trackUpdateTime": 200},
        {"id": 101, "name": "旧的歌单", "userId": 7, "specialType": 0, "subscribed": False, "trackCount": 1, "trackUpdateTime": 400},
        {"id": 102, "name": "缺少成员", "userId": 7, "specialType": 0, "subscribed": False, "trackCount": 2, "trackUpdateTime": 200},
        {"id": 103, "name": "收藏的歌单", "userId": 8, "specialType": 0, "subscribed": True, "trackCount": 2, "trackUpdateTime": 200},
    ]
    put_response(connection, "/xeapi/user/playlist", {"code": 200, "more": False, "playlist": playlists})
    for playlist, members, updated in [(playlists[0], [1, 2, 3], 200), (playlists[1], [4], 300)]:
        detail = {**playlist, "trackIds": [{"id": value} for value in members], "trackUpdateTime": updated, "tracks": []}
        put_response(connection, "/xeapi/v6/playlist/detail", {"code": 200, "playlist": detail})
    # An unrelated response must not be opened or exposed by the adapter.
    put_response(connection, "/eapi/login", {"token": "SECRET-LOGIN-MUST-NOT-LEAK"})
    connection.commit()
    return connection


def put_response(connection, endpoint, payload):
    # Real client cache IDs need not be parseable JSON; request bodies are opaque.
    key = json.dumps({"url": endpoint, "body": "SECRET-REQUEST-MUST-NOT-LEAK"}) + ":cache-key"
    connection.execute("INSERT INTO requestCache VALUES(?,?)", (key, json.dumps({"id": key, "cache": json.dumps(payload)})))
