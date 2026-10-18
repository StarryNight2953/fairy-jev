"""One authorized diagnostic slot per architecture; existing gate and ledger intact.

This orchestration does not change any Agent or request construction logic.
No retries at experiment level. Existing SDK/loop limits remain in force.
"""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import importlib.metadata
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit
from urllib.request import getproxies

from fairy.agents.Jev.trace import write_json, digest
from fairy.experiments.access import service_blockers_from_smoke
from fairy.experiments.budget import LiveLedger
from fairy.experiments.episode import ROOT, IMPLEMENTATIONS, git, file_hash, run_episode, verify_and_replay, credential_requirements
from fairy.experiments.paths import resolve_saved_path

OUT = Path(__file__).resolve().parent
ID = 'hn58-tool-description-v2-20261019'
TAG = 'experiment/hn58-tool-description-v2-20261019'
LEDGER_ID = 'ba456f29add04993a04ac60636f1dc0f'
ARCHS = ('llm_native','jev','hybrid')
BASE = json.loads((ROOT/'configs/jev/hn58_hybrid_sync_k4_qwen.json').read_text())

def now(): return datetime.now(timezone(timedelta(hours=8))).isoformat()

def effective(arch):
    cfg=deepcopy(BASE)
    cfg.update(architecture=arch,implementation_version=IMPLEMENTATIONS[arch])
    return cfg

def get_ledger():
    path=resolve_saved_path(BASE['global_live_ledger'])
    assert path.is_file(), 'Never initialize an empty replacement ledger'
    data=json.loads(path.read_text())
    assert data['ledger_id']==LEDGER_ID and data['max_episodes']==12 and data['max_http_attempts']==1440
    assert sum(r['http_attempts'] for r in data['episodes'])==data['http_attempts']
    assert not any(r['status'] in {'running','reserved'} for r in data['episodes']), 'Unfinished reservation: stop, never retry'
    return LiveLedger(path,max_episodes=12,max_http_attempts=1440)

def access():
    paths=[ROOT/'runs/agent_comparison/hn58-three-agents-v1/smoke_index.json',ROOT/'runs/agent_comparison/hn58-three-agents-qwen-v1/smoke_index.json']
    records=[]
    sources=[]
    for p in paths:
        records.extend(json.loads(p.read_text())['runs'])
        sources.append({'path':str(p),'sha256':file_hash(p),'kind':'saved_smoke_index'})
    relocated=ROOT/'runs/agent_comparison/hybrid_relocated_20261018/smoke/20261018T140025_hybrid_live_9fd09d81'
    manifest=json.loads((relocated/'manifest.json').read_text())
    assert manifest['architecture']=='hybrid' and manifest['stage']=='smoke'
    records.append({'architecture':'hybrid','path':str(relocated)})
    sources.append({'path':str(relocated/'manifest.json'),'sha256':file_hash(relocated/'manifest.json'),'kind':'manifest_backed_derived_record_no_saved_index_exists'})
    result=service_blockers_from_smoke({'runs':records},target_configs={a:effective(a) for a in ARCHS},available_credentials={k for k,v in os.environ.items() if v})
    result['source_indices_and_record']=sources
    result['implementation_sha256']=file_hash(ROOT/'fairy/experiments/access.py')
    result['no_success_supersession']='Existing gate does not automatically clear historical same-service 451 with later success; no evidence filtered or rule relaxed.'
    return result

def prepare():
    assert not (OUT/'preregistration.json').exists()
    assert not git('status','--porcelain')
    commit=git('rev-parse','HEAD')
    assert git('rev-parse',TAG)==commit
    ledger=get_ledger(); before=ledger.snapshot()
    assert len(before['episodes'])+3<=12 and before['http_attempts']+360<=1440
    gate=access()
    write_json(OUT/'access_review.json',gate)
    configs={a:effective(a) for a in ARCHS}
    hashes={a:digest(configs[a]) for a in ARCHS}
    for arch,cfg in configs.items(): write_json(OUT/f'config_{arch}.json',cfg)
    def safe_proxy(v):
        p=urlsplit(v)
        return {'scheme':p.scheme,'host':p.hostname,'port':p.port}
    # Construct only the already eligible local Qwen client, no HTTP/probe.
    from fairy.agents.llm.base_llm import BaseLLM
    from fairy.experiments.credentials import prepare_llm_config
    import httpx
    native=BaseLLM.llm_builder(prepare_llm_config(BASE['llm']))
    client=native.api_client._client
    selected=client._transport_for_url(httpx.URL(BASE['llm']['base_url']+'/chat/completions'))
    pool=getattr(selected,'_pool',None)
    proxy_url=getattr(pool,'_proxy_url',None)
    route={'configured_endpoint':BASE['llm']['base_url']+'/chat/completions','selected_transport_class':type(selected).__name__,'selected_pool_class':type(pool).__name__,
        'proxy':{'host':proxy_url.host.decode() if isinstance(proxy_url.host,bytes) else proxy_url.host,'port':proxy_url.port} if proxy_url else None,
        'scope':'Read selected unchanged SDK transport, no HTTP. Actual sends use same unmodified builder; trace records endpoints/statuses, not packet-level route capture.'}
    native.api_client.close()
    runtime={'timestamp':now(),'sdk_versions':{k:importlib.metadata.version(k) for k in ('typesafe-sdk','openai','httpx')},
        'missing_credentials':{a:credential_requirements(configs[a],a) for a in ARCHS},
        'system_proxies':{k:safe_proxy(v) for k,v in getproxies().items() if k in {'http','https','all'}},
        'native_transport':route,'jev_transport':'not constructed: historical access gate blocks TypeSafe; no request/no probe',
        'python_executable':sys.executable,'model_http_attempts':0}
    write_json(OUT/'runtime_preflight.json',runtime)
    slots=[{'architecture':a,'slot':f'{ID}_{a}_seed1904','seed':1904,'models':{'llm':BASE['llm']['model'] if a!='jev' else None,'jev':BASE['model'] if a!='llm_native' else None},
        'config_hash':hashes[a],'config_path':str(OUT/f'config_{a}.json'),'output_dir':str(OUT/'live'),
        'experiment_commit':commit,'implementation_version':configs[a]['implementation_version'],'run_allowance':configs[a]['experiment_limits'],
        'access_blockers':gate['by_architecture'][a]} for a in ARCHS]
    write_json(OUT/'preregistration.json',{'experiment_id':ID,'version':'tool_description_v2','purpose':'Single observed Docstring change; N=1 planned per architecture, no replacement samples',
        'registered_at':now(),'stage':'tool_description_v2','experiment_commit':commit,'experiment_tag':TAG,'slots':slots,
        'budget_before':before,'ledger_path':str(ledger.path),'ledger_id':LEDGER_ID,
        'max_new_live_episodes':3,'max_new_http_attempts':360,'access_gate_unchanged':True,
        'config_changes_from_existing_base':['architecture','implementation_version (identity metadata)'],
        'stop_policy':'No experiment retries, no model/route switching, no completion-forcing actions; existing access gate and all stricter loop limits preserved',
        'orchestrator_sha256':file_hash(Path(__file__))})
    write_json(OUT/'c0_identity.json',{'passed':True,'experiment_commit':commit,'experiment_tag':TAG,'source_clean':True,'base_commit':'334136928fdc62260fd49d3cad2b83cbb255880b',
        'diff_stat':git('diff','--stat','3341369',commit),'full_diff':git('diff','3341369',commit),
        'lockfile_sha256':file_hash(ROOT/'uv.lock'),'config_source_sha256':file_hash(ROOT/'configs/jev/hn58_hybrid_sync_k4_qwen.json'),
        'source_tree_hashes':{str(p.relative_to(ROOT)):file_hash(p) for p in sorted((ROOT/'fairy').rglob('*.py'))},
        'prompt_sha256':file_hash(ROOT/'fairy/agents/Jev/hybrid/prompt.md'),'budget':before,'remaining_episodes':12-len(before['episodes']),'remaining_http':1440-before['http_attempts'],
        'pending_reservations':[],'worktree_selection':'New experiment branch in existing hybrid worktree; other two worktrees not changed',
        'historical_preregistration_review':'local_qwen_preflight is completed configuration-only preparation (0 probe); relocated smoke_hybrid consumed by completed 9fd09d81; no outstanding reserved/running ledger records'})
    print(json.dumps({'prepared':ID,'commit':commit,'budget_before':{'episodes':len(before['episodes']),'http':before['http_attempts']},'blockers':{a:[x['reason'] for x in gate['by_architecture'][a]] for a in ARCHS}},ensure_ascii=False),flush=True)

def run_one(arch):
    prereg=json.loads((OUT/'preregistration.json').read_text())
    assert file_hash(Path(__file__))==prereg['orchestrator_sha256']
    assert not git('status','--porcelain') and git('rev-parse','HEAD')==prereg['experiment_commit']==git('rev-parse',TAG)
    assert json.loads((OUT/'c1_result.json').read_text())['passed']
    assert json.loads((OUT/'c2_scripted_result.json').read_text())['passed']
    assert json.loads((OUT/'regression_result.json').read_text())['exit_code']==0
    planned=next(x for x in prereg['slots'] if x['architecture']==arch)
    cfg=json.loads(Path(planned['config_path']).read_text())
    assert digest(cfg)==planned['config_hash']
    ledger=get_ledger(); before=ledger.snapshot()
    assert before['episodes'][:len(prereg['budget_before']['episodes'])]==prereg['budget_before']['episodes']
    assert not any(x['slot']==planned['slot'] for x in before['episodes'])
    gate=access()
    assert gate['by_architecture'][arch]==planned['access_blockers']
    blockers=gate['by_architecture'][arch]
    if not blockers:
        assert len(before['episodes'])<12 and before['http_attempts']+cfg['experiment_limits']['max_http_attempts']<=1440
    marker=OUT/f'started_{arch}.json'
    with marker.open('x') as f: json.dump({'at':now(),'slot':planned['slot'],'budget_before':before,'access_review':gate},f,ensure_ascii=False,indent=2)
    output,_=run_episode(cfg,OUT/'live',architecture=arch,mode='live',stage=prereg['stage'],experiment_id=ID,
        slot=planned['slot'],ledger=ledger,require_clean=True,external_blockers=blockers)
    result=verify_and_replay(output,cfg)
    after=ledger.snapshot()
    record={'architecture':arch,'path':str(output),'finished_at':now(),'budget_before':before,'budget_after':after,
        'new_live_episodes':len(after['episodes'])-len(before['episodes']),'new_http_attempts':after['http_attempts']-before['http_attempts'],
        **{k:result.get(k) for k in ('execution_status','live_integration_success','task_success','stop_reason','failure_layer','model_http_attempts','model_request_count','tool_call_count','wall_elapsed_seconds','first_issue','task_assertions','trace_verification','replay_verification')}}
    write_json(OUT/f'result_{arch}.json',record)
    print(json.dumps({k:record[k] for k in ('architecture','path','execution_status','live_integration_success','task_success','stop_reason','new_live_episodes','new_http_attempts','tool_call_count')},ensure_ascii=False),flush=True)

if __name__=='__main__':
    if sys.argv[1]=='prepare': prepare()
    else:
        assert sys.argv[1] in ARCHS
        run_one(sys.argv[1])
