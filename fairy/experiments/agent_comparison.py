"""Fixed-order comparison CLI. Formal identities are immutable within a batch."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import statistics

from fairy.agents.Jev.trace import write_json, digest
from .budget import LiveLedger
from .episode import ROOT,artifact_hashes,credential_requirements,file_hash,git,run_episode,verify_and_replay


def load_configs(path):
    path=Path(path).resolve()
    cfg=json.loads(path.read_text())
    base=Path(cfg['base_config'])
    if not base.is_absolute():base=ROOT/base
    return cfg,json.loads(base.read_text())


def effective_config(base,comparison,architecture,seed):
    cfg=deepcopy(base)
    cfg.update(architecture=architecture,seed=seed,implementation_version=comparison['implementation_versions'][architecture])
    return cfg


def run_stage(comparison,base,stage):
    out=Path(comparison['output_dir'])
    if not out.is_absolute():out=ROOT/out
    out.mkdir(parents=True,exist_ok=True)
    index_path=out/f'{stage}_index.json'
    index=json.loads(index_path.read_text()) if index_path.exists() else {'experiment_id':comparison['experiment_id'],'stage':stage,'runs':[]}
    if stage=='formal' and index.get('batch_stopped'):
        raise ValueError('Formal batch is permanently stopped; preserve failed sample and remaining not_run slots')
    if stage=='formal':
        tag_commit=git('rev-parse',comparison['experiment_tag'])
        if tag_commit!=git('rev-parse','HEAD') or git('status','--porcelain'):
            raise ValueError('Formal runs require the clean frozen experiment tag as HEAD')
        identity={'code_commit':tag_commit,'experiment_tag':comparison['experiment_tag'],'comparison_config_hash':digest(comparison),'base_config_hash':digest(base)}
        if index.get('frozen_identity') and index['frozen_identity']!=identity:
            raise ValueError('Formal batch identity/config differs from frozen first invocation')
        index['frozen_identity']=identity
    ledger=None
    if stage in {'smoke','formal'}:
        ledger=LiveLedger(ROOT/comparison['global_live_ledger'],max_episodes=comparison.get('max_live_episodes',12),max_http_attempts=comparison.get('max_total_http_attempts',1440))
    if stage=='formal':
        plan=[(f'formal_block_{i+1}_{a}',a,comparison['block_seeds'][i]) for i,block in enumerate(comparison['formal_order']) for a in block]
    else:
        order=comparison.get('smoke_order',['llm_native','jev','hybrid'])
        plan=[(f'{stage}_{a}',a,comparison.get('smoke_seed',1904)) for a in order]
    index['planned_slots']=[slot for slot,_,_ in plan]
    stopped=False
    access_by_architecture={}
    if stage=='formal':
        from .access import service_blockers_from_smoke
        smoke_path=out/'smoke_index.json'
        if smoke_path.exists():
            access=service_blockers_from_smoke(json.loads(smoke_path.read_text()))
            access_by_architecture=access['by_architecture']
            index['smoke_access_review']=access
        else:
            access_by_architecture={a:[{'reason':'smoke_evidence_unavailable','failure_layer':'preflight','source_path':str(smoke_path)}] for a in ('llm_native','jev','hybrid')}
    for slot,architecture,seed in plan:
        previous=[r for r in index['runs'] if r['slot']==slot and r.get('execution_status')!='blocked']
        if previous:continue
        cfg=effective_config(base,comparison,architecture,seed)
        if stopped:
            index.setdefault('not_run',[]).append({'slot':slot,'reason':'batch_stopped_after_integration_or_recording_defect'})
            continue
        native=None
        if stage=='offline' and architecture=='llm_native':
            from .scripted_native import NativeFixtureLLM
            native=lambda runtime:NativeFixtureLLM(runtime)
        output,result=run_episode(cfg,out/stage,architecture=architecture,mode='scripted' if stage=='offline' else 'live',
            stage=stage,experiment_id=comparison['experiment_id'],slot=slot,ledger=ledger,
            require_clean=stage=='formal',offline_native_llm=native,external_blockers=access_by_architecture.get(architecture))
        result=verify_and_replay(output,cfg)
        summary={k:result.get(k) for k in ('execution_status','task_success','scripted_task_success','live_integration_success','stop_reason','failure_layer','model_http_attempts','model_request_count','tool_call_count','wall_elapsed_seconds')}
        record={'slot':slot,'architecture':architecture,'seed':seed,'path':str(output),**summary,'evaluation_sha256':file_hash(output/'evaluation.json'),'manifest_sha256':file_hash(output/'manifest.json'),'trace_sha256':file_hash(output/'trace.jsonl'),'verification_passed':result['trace_verification']['passed'],'replay_passed':result.get('replay_verification',{}).get('passed')}
        index['runs'].append(record)
        if ledger:index['live_budget']=ledger.snapshot()
        write_json(index_path,index)
        print(json.dumps(record,ensure_ascii=False),flush=True)
        if stage=='formal' and (result['execution_status']=='integration_error' or not result['trace_verification']['passed'] or result.get('replay_verification',{}).get('passed') is False):
            stopped=True
            index['batch_stopped']={'slot':slot,'reason':'integration_or_recording_defect','path':str(output)}
            write_json(index_path,index)
    index['complete']=all(any(r['slot']==slot and r.get('execution_status')!='blocked' for r in index['runs']) for slot,_,_ in plan)
    write_json(index_path,index)
    return index


def report(comparison):
    out=Path(comparison['output_dir'])
    if not out.is_absolute():out=ROOT/out
    indices={stage:json.loads((out/f'{stage}_index.json').read_text()) for stage in ('offline','smoke','formal') if (out/f'{stage}_index.json').exists()}
    rows=[]
    for record in indices.get('formal',{}).get('runs',[]):
        p=Path(record['path']);e=json.loads((p/'evaluation.json').read_text());m=json.loads((p/'metrics.json').read_text());manifest=json.loads((p/'manifest.json').read_text())
        rows.append({**record,'code_commit':manifest['code_commit'],'task_assertions':{k:v['passed'] for k,v in e['task_assertions'].items()},'native_validation':e['native_validation'],'first_issue':e.get('first_issue'),'metrics':m,'integration_coverage':e.get('integration_coverage',{}),'legacy_task_success':e.get('legacy_evaluation_v1',{}).get('task_success')})
    groups={}
    for architecture in ('llm_native','jev','hybrid'):
        group=[r for r in rows if r['architecture']==architecture]
        live=[r for r in group if r['execution_status']!='blocked']
        seconds=[r['wall_elapsed_seconds'] for r in live]
        groups[architecture]={'planned':3,'executed':len(live),'blocked':len(group)-len(live),'task_successes':sum(r['task_success'] is True for r in live),'integration_successes':sum(r['live_integration_success'] is True for r in live),'wall_seconds':{'median':statistics.median(seconds) if seconds else None,'min':min(seconds) if seconds else None,'max':max(seconds) if seconds else None},'total_http_attempts':sum(r['model_http_attempts'] for r in live),'total_native_attempts':sum(r['tool_call_count'] for r in live),'cost_status':'unavailable'}
    result={'experiment_id':comparison['experiment_id'],'formal_rows':rows,'groups':groups,'indices':{k:str(out/f'{k}_index.json') for k in indices},'interpretation':'Descriptive three-repetition whole-architecture comparison; no stable success-rate or isolated Jev causal contribution claim. Failed episodes are included.','price_status':'unavailable'}
    write_json(out/'comparison_report.json',result)
    lines=['# HN58 three-architecture comparison','',result['interpretation'],'','| Slot | Architecture | Task | Integration | Stop | HTTP | Tools | Wall s |','|---|---|---|---|---|---:|---:|---:|']
    for r in rows:lines.append(f"| {r['slot']} | {r['architecture']} | {r['task_success']} | {r['live_integration_success']} | {r['stop_reason']} | {r['model_http_attempts']} | {r['tool_call_count']} | {r['wall_elapsed_seconds']:.3f} |")
    lines+=['','Costs unavailable; see JSON for reported token usage and per-role latency.','',json.dumps(groups,ensure_ascii=False,indent=2)]
    (out/'comparison_report.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({'report':str(out/'comparison_report.json'),'groups':groups},ensure_ascii=False,indent=2))
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    parser.add_argument('--stage',choices=['offline','smoke','formal','report'],required=True)
    args=parser.parse_args()
    comparison,base=load_configs(args.config)
    if args.stage=='report':report(comparison)
    else:run_stage(comparison,base,args.stage)


if __name__=='__main__':main()
