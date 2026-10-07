"""Explicit account execution of the separately approved classification plan."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from netease_organizer.service import Organizer
from netease_organizer.classification_execution import execute_classification,validate_plan
from scripts.classification_control import ClassificationFileControl


def public_progress(event, jobs):
    if event.get('kind') == 'cli':
        return None
    view = {k: event[k] for k in ('phase', 'label', 'job_index', 'job_count', 'total_count',
                                 'completed_count', 'elapsed_seconds', 'stage', 'step') if k in event}
    if event.get('stage') == 'classification_preflight':
        index = event.get('job_index') if event.get('step') in ('playlist_start', 'playlist_verified') else None
    else:
        done = event.get('completed_count')
        index = done - 1 if type(done) is int and event.get('phase') == 'completed' else done
    if type(index) is int and 0 <= index < len(jobs):
        view['name'] = jobs[index]['name']
    return view


def main():
    parser=argparse.ArgumentParser()
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--execute-approved',action='store_true',help='Explicitly execute the approved default-visibility plan once.')
    group.add_argument('--resume-verified',action='store_true',help='Recheck the recorded complete prefix and execute only untouched remaining jobs.')
    args=parser.parse_args();root=Path(__file__).resolve().parents[1]
    plan=validate_plan(json.loads((root/'artifacts/分类整理计划.json').read_text(encoding='utf-8')))
    if not args.execute_approved and not args.resume_verified:
        print(json.dumps({'status':'preview','jobs':len(plan['jobs']),'source_count':plan['source_track_count'],'account_changed':False},ensure_ascii=False));return
    controller=Organizer(root)
    marker=controller._workspace_target(Path('.organizer/classification-pause.request'))
    marker.unlink(missing_ok=True)  # Only this explicit new account operation clears its previous pause.
    controller.control=ClassificationFileControl(marker)
    controller.prepare_operation('classification')
    last=[None]
    def progress(event):
        view=public_progress(event,plan['jobs'])
        if view is None:return
        if view!=last[0]:print(json.dumps(view,ensure_ascii=False),flush=True);last[0]=view
    controller.set_progress_listener(progress)
    if args.resume_verified:
        from netease_organizer.classification_execution import resume_verified_classification
        result=resume_verified_classification(controller,accept_default_visibility=True)
    else:
        result=execute_classification(controller,plan,accept_default_visibility=True)
    print(json.dumps({k:v for k,v in result.items() if k!='items'},ensure_ascii=False),flush=True)
    for item in result.get('items',[]):
        print(json.dumps({k:item.get(k) for k in ('name','status','count','expected_count','added_count','message')},ensure_ascii=False),flush=True)
    if result.get('status')!='completed':sys.exit(2)

if __name__=='__main__':main()
