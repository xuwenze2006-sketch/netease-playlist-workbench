"""Explicitly read song lyrics and save only derived clues, with a resumable cache."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from netease_organizer.lyric_features import derive_lyric_features
from netease_organizer.official_cli import OfficialCli
from netease_organizer.service import Organizer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--read-lyrics', action='store_true', required=True)
    parser.add_argument('--workers', type=int, default=3, choices=(1, 2, 3))
    parser.add_argument('--retry-failed', action='store_true')
    options = parser.parse_args()
    baseline = json.loads((ROOT / 'artifacts/全库分类-账号基线.json').read_text(encoding='utf-8'))['snapshot']
    tracks = baseline['liked']['tracks']
    cache_path = ROOT / 'artifacts/全库分类-歌词特征.json'
    saved = json.loads(cache_path.read_text(encoding='utf-8')) if cache_path.exists() else {}
    cache = saved.get('tracks', {})
    controller = Organizer(ROOT)
    controller._require_credentials()
    pending = [t for t in tracks if t['original_id'] not in cache or
               (options.retry_failed and cache[t['original_id']].get('status') != 'ok')]
    started = time.monotonic()

    def stamp():
        return datetime.now(timezone(timedelta(hours=8))).isoformat()

    def save():
        controller._write_artifact('全库分类-歌词特征.json', json.dumps({
            'kind': 'classification_derived_lyric_cache', 'version': 1,
            'updated_at': stamp(), 'tracks': cache,
            'retains_lyric_text': False,
        }, ensure_ascii=False, indent=2, allow_nan=False))

    def read(track):
        try:
            response = OfficialCli(ROOT).run_json(['song', 'lyric', '--songId', track['id']])
            data = response.get('data')
            if (response.get('code') != 200 or type(data) is not dict or
                    str(data.get('originalId')) != track['original_id'] or
                    type(data.get('songId')) is not str or data['songId'].upper() != track['id'].upper()):
                return track['original_id'], {'status': 'unavailable', 'read_at': stamp()}
            return track['original_id'], {'status': 'ok', 'read_at': stamp(), **derive_lyric_features(data)}
        except Exception as error:
            return track['original_id'], {'status': 'failed', 'read_at': stamp(),
                'code': getattr(error, 'code', 'operation_failed')}

    print(json.dumps({'stage': 'lyric_enrichment', 'total': len(tracks), 'pending': len(pending),
                      'workers': options.workers}, ensure_ascii=False), flush=True)
    done = 0
    with ThreadPoolExecutor(max_workers=options.workers) as pool:
        futures = [pool.submit(read, track) for track in pending]
        for future in as_completed(futures):
            key, result = future.result()
            cache[key] = result
            done += 1
            if done % 25 == 0 or done == len(pending):
                save()
                print(json.dumps({'stage': 'lyric_progress', 'completed': done,
                    'total_pending': len(pending), 'cached': len(cache),
                    'ok': sum(v.get('status') == 'ok' for v in cache.values()),
                    'elapsed_seconds': round(time.monotonic() - started, 1)}, ensure_ascii=False), flush=True)
    save()
    print(json.dumps({'stage': 'lyric_enrichment_complete', 'records': len(cache),
        'failed': sum(v.get('status') != 'ok' for v in cache.values()),
        'retains_lyric_text': False}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
