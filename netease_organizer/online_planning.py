"""Bind the existing organizing rules to a complete official account snapshot."""

from .planning import build_plan


def build_online_plan(snapshot):
    if snapshot.get('complete') is not True and snapshot.get('overview_complete') is not True:
        raise ValueError('在线快照不完整，不能生成可执行任务。')
    liked = snapshot['liked']
    tracks = liked['tracks']
    track_ids = {t['original_id']: t['id'] for t in tracks}
    playlist_ids = {p['original_id']: p['id'] for p in snapshot['playlists']}
    playlist_ids[liked['original_id']] = liked['id']
    compatible = {
        'owner_id': snapshot['account']['original_id'],
        'source': {'overview_complete': True, 'provider': 'official_ncm_cli'},
        'playlists': [dict(id=p['original_id'], name=p['name'], owned=True,
                           special_type=p['special_type'],
                           membership_available=p['id'] == liked['id'],
                           snapshot_complete=p['id'] == liked['id'] and snapshot.get('complete') is True,
                           members=[t['original_id'] for t in tracks] if p['id'] == liked['id'] else [])
                      for p in snapshot['playlists']],
        'tracks': {t['original_id']: {'metadata_available': t.get('metadata_available', True),
                                    'artists': [{'id': a['original_id'], 'name': a['name']}
                                                for a in t['artists']]} for t in tracks},
    }
    if not any(p['id'] == liked['original_id'] for p in compatible['playlists']):
        compatible['playlists'].append(dict(id=liked['original_id'], name=liked['name'], owned=True,
                                           special_type=5, membership_available=True, snapshot_complete=snapshot.get('complete') is True,
                                           members=[t['original_id'] for t in tracks]))
    plan = build_plan(compatible)
    plan.update(kind='official_online_organizing_plan', online_account_verified=True,
                account_id=snapshot['account']['id'], account_name=snapshot['account']['nickname'])
    plan['source']['online_account_verified'] = True
    plan['source']['liked_snapshot_complete'] = snapshot.get('complete') is True
    plan['summary']['online_liked_expected_count'] = liked['track_count']
    plan['summary']['online_liked_observed_count'] = len(tracks)
    plan['summary']['online_missing_record_count'] = max(0, liked['track_count'] - len(tracks))
    plan['blocked_reasons'] = []
    if snapshot.get('complete') is not True:
        plan['blocked_reasons'].append('liked_snapshot_incomplete')
    for job in plan['jobs']:
        if job['kind'] == 'rename_playlist':
            job['original_playlist_id'] = job['playlist_id']
            job['playlist_id'] = playlist_ids[job['playlist_id']]
            job['status'] = 'ready'
        elif job['kind'] == 'reorder_playlists':
            for key in ('current_playlist_ids', 'playlist_ids'):
                job[key] = [playlist_ids[i] for i in job[key]]
            job.update(status='blocked', blocked_reason='playlist_list_order_unsupported',
                       message='官方提供的是歌单内歌曲排序，未提供歌单列表排列接口。')
        else:
            job['original_candidate_track_ids'] = job['candidate_track_ids']
            job['candidate_track_ids'] = [track_ids[i] for i in job['candidate_track_ids']]
            job['source_playlist_id'] = liked['id']
            job.update(status='blocked', blocked_reason='private_playlist_creation_unavailable',
                       track_id_format='official_encrypted', official_track_id_mapping_required=False,
                       message='官方创建歌单接口未提供私密设置参数，尚不能按私密目标创建。')
        if job['status'] == 'blocked' and job['blocked_reason'] not in plan['blocked_reasons']:
            plan['blocked_reasons'].append(job['blocked_reason'])
    plan['summary']['ready_count'] = sum(j['status'] == 'ready' for j in plan['jobs'])
    plan['summary']['blocked_count'] = sum(j['status'] == 'blocked' for j in plan['jobs'])
    return plan
