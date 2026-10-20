"""Build the independent approved classification plan and public local report."""
import argparse
import csv
import io
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from netease_organizer.classification_planning import SCENES,STYLES,LANGUAGES,infer_language,validate_rows
from netease_organizer.classification_execution import validate_plan
from netease_organizer.service import Organizer
from netease_organizer.classification_generation import generation_lease,publish_exports,require_unexecuted


def build_exports(root):
    a=root/'artifacts'
    snap=json.loads((a/'全库分类-账号基线.json').read_text(encoding='utf-8'))['snapshot']
    source=snap['liked'];tracks=source['tracks']
    if source['track_count']!=len(tracks) or not source['membership_complete']:
        raise ValueError('当前分类要求完整歌曲全集。')
    rows=validate_rows(sum([json.loads((a/f'分类判断-分块{i:02}.json').read_text(encoding='utf-8')) for i in range(1,4)],[]),len(tracks))
    cache=json.loads((a/'全库分类-歌词特征.json').read_text(encoding='utf-8'))['tracks']
    historical=json.loads((a/'网易云本地歌单快照.json').read_text(encoding='utf-8')).get('tracks',{})
    reviews={}
    for i in range(1,4):
        path=a/f'分类语言核验{i:02}.json'
        if not path.exists():continue
        data=json.loads(path.read_text(encoding='utf-8'))
        entries=data if isinstance(data,list) else data.get('recommendations',[])
        for entry in entries:
            pos=entry['position']
            if type(pos) is not int or not 1<=pos<=len(tracks) or pos in reviews:raise ValueError('语言复核位置不兼容。')
            label=entry.get('language',entry.get('suggested_language'))
            if label is not None and label not in set(LANGUAGES)|{'instrumental'}:raise ValueError('语言复核标签不兼容。')
            if isinstance(data,dict):
                if label is not None and entry.get('confidence') not in ('high','medium'):continue
                if label is not None and rows[pos-1][3] is not None:continue
            if 'review_note' in entry and type(entry['review_note']) is not bool:raise ValueError('语言复核状态不兼容。')
            flags=entry.get('flags',[])
            if type(flags) is not list or any(type(flag) is not str for flag in flags):raise ValueError('语言复核标记不兼容。')
            needs_review=label is None or entry.get('review_note','possible_wrong_lyric' in flags)
            reason=next((entry[k] for k in ('notes','reason','evidence') if type(entry.get(k)) is str and entry[k]),'单曲语言复核')
            reviews[pos]=(label,reason,needs_review)
    jobs={(d,k):[] for d,labels in [('scene',SCENES),('style',STYLES),('language',LANGUAGES)] for k in labels}
    pending=[];records=[];review=[]
    for row,t in zip(rows,tracks):
        pos,styles,scenes,model,confidence,note=row
        f=cache[str(t['original_id'])]
        if f['status']!='ok':raise ValueError('歌词资料读取未完成。')
        language,language_note,needs_review=infer_language(model,f)
        if pos in reviews:
            language,language_note,needs_review=reviews[pos]
            language_note='单曲录音复核：'+language_note
        reasons=[]
        if styles==['unknown']:reasons.append('风格待辨识')
        if language is None:reasons.append('语言待辨识')
        if reasons:pending.append(t['id'])
        for k in styles:
            if k!='unknown':jobs['style',k].append(t['id'])
        for k in scenes:jobs['scene',k].append(t['id'])
        if language in LANGUAGES:jobs['language',language].append(t['id'])
        old=historical.get(str(t['original_id']),{}) if not t['metadata_available'] else {}
        public={'position':pos,'name':old.get('name') or t['name'],'artists':' / '.join(x['name'] for x in (old.get('artists') or t['artists'])),
                'styles':[STYLES.get(k,'待辨识') for k in styles],
                'scenes':[SCENES[k] for k in scenes],
                'language':LANGUAGES.get(language,'器乐或配乐录音' if language=='instrumental' else '待辨识'),
                'style_judgment_score':confidence,'evidence_note':note,
                'language_evidence_note':language_note,'pending_reasons':reasons,
                'review_note':needs_review,'live_metadata_available':t['metadata_available']}
        records.append(public)
        if needs_review:review.append({'position':pos,'name':t['name'],'model':model,'assigned':language,'hints':f['language_hints'],'scripts':f['script_counts'],'note':language_note})
    plan_jobs=[]
    for (dim,k),ids in jobs.items():
        if ids:
            label={'scene':SCENES,'style':STYLES,'language':LANGUAGES}[dim][k]
            prefix={'scene':'场景','style':'风格','language':'语言'}[dim]
            plan_jobs.append({'kind':'create_classification_playlist','name':f'{prefix} · {label}',
                              'dimension':dim,'candidate_track_ids':ids,'intended_visibility':'provider_default'})
    if pending:plan_jobs.append({'kind':'create_classification_playlist','name':'分类 · 待辨识',
                                'dimension':'style','candidate_track_ids':pending,'intended_visibility':'provider_default'})
    plan=validate_plan({'kind':'approved_classification_plan','version':1,
                       'account_original_id':snap['account']['original_id'],'account_id':snap['account']['id'],
                       'source_playlist_id':source['id'],'original_source_playlist_id':source['original_id'],
                       'source_track_count':source['track_count'],'source_track_ids':[t['id'] for t in tracks],
                       'jobs':plan_jobs})
    summary={'source_count':len(tracks),'covered_count':len(set(x for j in plan_jobs for x in j['candidate_track_ids'])),
             'playlist_count':len(plan_jobs),'pending_count':len(pending),'unknown_style_count':sum(r['styles']==['待辨识'] for r in records),
             'unknown_language_count':sum(r['language']=='待辨识' for r in records),'review_note_count':len(review),
             'dimensions':dict(Counter(j['dimension'] for j in plan_jobs)),
             'playlists':[{'name':j['name'],'count':len(j['candidate_track_ids'])} for j in plan_jobs]}
    now=datetime.now().astimezone().isoformat()
    def cell(x):
        s=str(x)
        # Retain the source text; only the spreadsheet export gains a literal prefix.
        start=s.lstrip()[:1]
        return "'"+s if s.startswith(('\t','\r','\n')) or (start and start in '=+-@＝＋－＠') else s
    out=io.StringIO(newline='');w=csv.writer(out,lineterminator='\n')
    w.writerow(['序号','歌曲','艺人','风格','场景','语言','待辨识项','风格依据','语言依据'])
    for r in records:w.writerow([cell(x) for x in [r['position'],r['name'],r['artists'],'；'.join(r['styles']),'；'.join(r['scenes']),r['language'],'；'.join(r['pending_reasons']),r['evidence_note'],r['language_evidence_note']]])
    csv_text='\ufeff'+out.getvalue().removesuffix('\n')
    md=['# 喜欢的音乐分类清单','',f'当前红心歌曲 {len(tracks)} 首，分类歌单 {len(plan_jobs)} 个，集合覆盖 {summary["covered_count"]} 首。',
        f'风格待辨识 {summary["unknown_style_count"]} 首，语言待辨识 {summary["unknown_language_count"]} 首；待辨识歌单并集 {len(pending)} 首。','',
        '同一首歌可以进入多个风格、场景或语言入口。分类不删除原始红心歌曲。',
        '风格与场景来自具体录音、艺人、专辑的模型判断，场景是推荐用途；尚未进行完整音频听辨。',
        '语言使用原始歌词文字及具体演唱版本判断；不使用译文，也不按标题或艺人国籍分语言。',
        '平台纯音乐标记与有词录音判断的冲突、采样、混语和资料不足项保留于复核明细。','',
        '用户已同意按平台默认可见性创建（可能公开）；当前此文件只代表本地计划，执行状态见独立执行结果。','',
        '| 分类歌单 | 歌曲数 |','| --- | ---: |']
    md.extend(f'| {j["name"]} | {len(j["candidate_track_ids"])} |' for j in plan_jobs)
    outputs=[
        ('全库分类-逐曲结果.json',json.dumps({'kind':'classification_report','created_at':now,'summary':summary,'records':records},ensure_ascii=False,indent=2)+'\n',None),
        ('全库分类-需复核录音.json',json.dumps(review,ensure_ascii=False,indent=2)+'\n',None),
        ('喜欢的音乐-分类明细.csv',csv_text,''),
        ('喜欢的音乐-分类清单.md','\n'.join(md)+'\n',None),
        ('分类整理计划.json',json.dumps(plan,ensure_ascii=False,indent=2)+'\n',None)]
    return plan,summary,outputs


def main(argv=()):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repair-missing',action='store_true',help='Reconstruct only missing local exports for the unchanged saved plan; never execute an account operation.')
    args=parser.parse_args(argv)
    root=Path(__file__).resolve().parents[1];controller=Organizer(root)
    with generation_lease(controller):
        if not args.repair_missing:require_unexecuted(controller)
        plan,summary,outputs=build_exports(root)
        written=publish_exports(controller,plan,outputs,repair_missing=args.repair_missing)
    if args.repair_missing:summary={**summary,'mode':'repair_missing','reconstructed_files':written}
    print(json.dumps(summary,ensure_ascii=False))

if __name__=='__main__':main(sys.argv[1:])
