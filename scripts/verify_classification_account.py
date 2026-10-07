"""Read-only final membership verification and a public classification handoff."""
import json
import sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from netease_organizer.classification_execution import validate_plan,plan_digest,classification_pending
from netease_organizer.create import ArtistExecutor
from netease_organizer.online import OnlineReader
from netease_organizer.service import Organizer


def main():
    root=Path(__file__).resolve().parents[1];a=root/'artifacts';controller=Organizer(root)
    plan=validate_plan(json.loads((a/'分类整理计划.json').read_text(encoding='utf-8')))
    receipt=json.loads((a/'分类整理执行结果.json').read_text(encoding='utf-8'))
    if (classification_pending(controller) or receipt.get('status')!='completed'
        or receipt.get('plan_digest')!=plan_digest(plan) or len(receipt.get('items',[]))!=len(plan['jobs'])):
        raise ValueError('分类写入尚未确认完成；此脚本不会重发任何写入。')
    controller.prepare_operation('classification_readback')
    read=OnlineReader(controller.cli,plan['account_original_id'],control=controller.control)
    live=read.read_snapshot()
    if ((live['account']['original_id'],live['account']['id'])!=(plan['account_original_id'],plan['account_id'])
        or (live['liked']['original_id'],live['liked']['id'],live['liked']['track_count'])!=(plan['original_source_playlist_id'],plan['source_playlist_id'],plan['source_track_count'])
        or [t['id'] for t in live['liked']['tracks']]!=plan['source_track_ids']):
        raise ValueError('当前账号或红心歌曲与执行基线不一致，保留已确认结果待核对。')
    executor=ArtistExecutor(controller.cli,plan['account_original_id'],lambda _:None,
                            expected_account_id=plan['account_id'])
    owner=executor._account();directory=executor._created();verified=[];covered=set()
    for index,(job,item) in enumerate(zip(plan['jobs'],receipt['items']),1):
        target=directory.get(item['playlist_id'])
        if target is None or target['original_id']!=item['original_playlist_id'] or target['name']!=job['name']:
            raise ValueError('新分类歌单身份未通过最终核对。')
        header,ids=executor._snapshot(target,owner)
        expected=job['candidate_track_ids']
        if header['count']!=len(expected) or len(ids)!=len(expected) or set(ids)!=set(expected):
            raise ValueError('分类歌曲集合在最终读取时不匹配。')
        covered.update(ids)
        verified.append({'name':job['name'],'dimension':job['dimension'],'count':len(ids),
                         'original_playlist_id':target['original_id'],
                         'url':f'https://music.163.com/#/playlist?id={target["original_id"]}',
                         'track_update_time':header['track_update_time']})
        print(json.dumps({'verified':index,'total':len(plan['jobs']),'name':job['name'],'count':len(ids)},ensure_ascii=False),flush=True)
    after=executor._created()
    if executor._account()!=owner or any(
        after.get(item['playlist_id'],{}).get('track_update_time')!=row['track_update_time']
        or after.get(item['playlist_id'],{}).get('count')!=row['count']
        or after.get(item['playlist_id'],{}).get('name')!=row['name']
        for item,row in zip(receipt['items'],verified)):
        raise ValueError('最终核对期间分类目录发生变化，需再次只读核验。')
    final=read.read_snapshot()
    if [t['id'] for t in final['liked']['tracks']]!=plan['source_track_ids'] or final['liked']['track_count']!=plan['source_track_count']:
        raise ValueError('最终读取期间红心歌单发生变化，保留结果待核对。')
    if covered!=set(plan['source_track_ids']):raise ValueError('最终分类集合没有覆盖全部红心歌曲。')
    summary=json.loads((a/'全库分类-逐曲结果.json').read_text(encoding='utf-8'))['summary']
    result={'kind':'classification_final_readback','status':'verified','verified_at':datetime.now().astimezone().isoformat(),
            'source_count':len(plan['source_track_ids']),'covered_count':len(covered),
            'original_favorites_membership_and_order_unchanged':True,'playlist_count':len(verified),
            'pending_count':summary['pending_count'],'unknown_style_count':summary['unknown_style_count'],
            'unknown_language_count':summary['unknown_language_count'],'playlists':verified}
    controller._write_artifact('分类整理最终核验.json',json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    controller._write_artifact('全库分类-账号完成快照.json',json.dumps({'kind':'classification_final_snapshot','snapshot':final},ensure_ascii=False,indent=2)+'\n')
    # Refresh the directory source used by the modern workbench; historical write journals remain untouched.
    controller._write_artifact('在线名称整理快照.json',json.dumps(final,ensure_ascii=False,indent=2)+'\n')
    lines=['# 喜欢的音乐：账号分类结果','',f'已新建并逐个核验 {len(verified)} 个分类歌单，覆盖当前 {len(covered)} 首红心歌曲。',
           '原始红心歌曲成员与顺序和执行前一致。按用户同意的平台默认可见性创建，可能公开。','',
           f'风格待辨识 {summary["unknown_style_count"]} 首、语言待辨识 {summary["unknown_language_count"]} 首；并集 {summary["pending_count"]} 首已归入“分类 · 待辨识”。',
           '同一首歌可进入多个维度。风格与场景是模型判断及听歌用途建议，未完成全库音频听辨；语言依据原始歌词及具体演唱版本，疑点保留。','',
           '| 账号中的新歌单 | 歌曲数 |','| --- | ---: |']
    lines.extend(f'| [{r["name"]}]({r["url"]}) | {r["count"]} |' for r in verified)
    lines+=['','逐曲分类与依据：[喜欢的音乐-分类明细.csv](喜欢的音乐-分类明细.csv)。','']
    controller._write_artifact('喜欢的音乐-账号分类结果.md','\n'.join(lines))
    print(json.dumps({k:v for k,v in result.items() if k!='playlists'},ensure_ascii=False),flush=True)

if __name__=='__main__':main()
