"""One registered live episode per architecture, using unchanged production loops."""
from datetime import datetime, timezone
from pathlib import Path
import importlib.metadata
import json
import os
import subprocess
import sys

from fairy.agents.Jev.trace import digest, write_json
from fairy.experiments.access import service_blockers_from_smoke
from fairy.experiments.budget import LiveLedger
from fairy.experiments.episode import ROOT, file_hash, git, run_episode, verify_and_replay, credential_requirements
from fairy.experiments.paths import resolve_saved_path

OUT = Path(__file__).resolve().parent
ID = OUT.name
ARCHS = ('jev', 'hybrid')
OLD = ROOT/'runs/agent_comparison/hn58_tool_description_v2_20261019'
LEDGER_ID = 'ba456f29add04993a04ac60636f1dc0f'

def now():
    return datetime.now(timezone.utc).isoformat()

def cfg(arch):
    return json.loads((OUT/f'config_{arch}.json').read_text())

def budget():
    p = resolve_saved_path(cfg('jev')['global_live_ledger'])
    assert p.is_file(), 'Existing ledger required, never initialize a replacement'
    d = json.loads(p.read_text())
    assert d['ledger_id'] == LEDGER_ID and d['max_http_attempts'] == 1440
    assert d['max_episodes'] in (12, 13)
    assert sum(r['http_attempts'] for r in d['episodes']) == d['http_attempts']
    assert not any(r['status'] in ('reserved', 'running') for r in d['episodes'])
    if d['max_episodes'] == 13:
        a = json.loads((OUT/'budget_authorization.json').read_text())
        assert a['authorized'] is True and a['new_max_episodes'] == 13
    return LiveLedger(p, max_episodes=d['max_episodes'], max_http_attempts=1440)

def access():
    records = []
    for name in ('hn58-three-agents-v1', 'hn58-three-agents-qwen-v1'):
        records += json.loads((ROOT/'runs/agent_comparison'/name/'smoke_index.json').read_text())['runs']
    records.append({'architecture':'hybrid', 'path':str(ROOT/'runs/agent_comparison/hybrid_relocated_20261018/smoke/20261018T140025_hybrid_live_9fd09d81')})
    for arch in ARCHS:
        p = OUT/f'result_{arch}.json'
        if p.exists():
            r = json.loads(p.read_text())
            records.append({'architecture':arch, 'path':r['path']})
    return service_blockers_from_smoke({'runs':records}, target_configs={a:cfg(a) for a in ARCHS},
        available_credentials={k for k,v in os.environ.items() if v})

def route(client, url):
    # Local transport selection only; never send a probe or expose userinfo.
    transport = client._transport_for_url(type(client.base_url)(url))
    pool = getattr(transport, '_pool', None)
    proxy = getattr(pool, '_proxy_url', None)
    host = getattr(proxy, 'host', None)
    if isinstance(host, bytes):
        host = host.decode()
    return {'transport':type(transport).__name__, 'pool':type(pool).__name__,
        'proxy_host':host, 'proxy_port':getattr(proxy, 'port', None)}

def prepare():
    assert not (OUT/'preregistration.json').exists()
    assert not git('status', '--porcelain')
    for arch in ARCHS:
        (OUT/f'config_{arch}.json').write_bytes((OLD/f'config_{arch}.json').read_bytes())
    ledger = budget()
    before = ledger.snapshot()
    assert len(before['episodes']) == 11 and before['http_attempts'] == 37
    gate = access()
    assert all(not gate['by_architecture'][a] for a in ARCHS)
    assert all(not credential_requirements(cfg(a), a) for a in ARCHS)
    from fairy.agents.Jev.client import OfficialBackend
    from fairy.agents.llm.base_llm import BaseLLM
    from fairy.experiments.credentials import prepare_llm_config
    backend = OfficialBackend(cfg('jev'))
    native = BaseLLM.llm_builder(prepare_llm_config(cfg('hybrid')['llm']))
    routes = {'jev':route(backend.client._http_client, cfg('jev')['base_url']+'/v1/systemone'),
        'qwen':route(native.api_client._client, cfg('hybrid')['llm']['base_url']+'/chat/completions')}
    backend.close(); native.api_client.close()
    hashes = {}
    for base in (ROOT/'fairy', ROOT/'configs', ROOT/'tests', ROOT/'runs/agent_comparison', ROOT/'runs/semantic_audit'):
        for p in sorted(base.rglob('*')):
            if p.is_file() and p.suffix != '.pyc' and '__pycache__' not in p.parts and OUT not in p.parents and p != ledger.path:
                hashes[str(p)] = file_hash(p)
    for p in (ROOT/'uv.lock', ROOT/'pyproject.toml', ROOT/'requirements.txt', ROOT.parent/'workspace-paths.json'):
        hashes[str(p)] = file_hash(p)
    trees = []
    for name in ('jev-mvp','llm-jev-hybrid','hn58-reproduce'):
        p = ROOT.parent/name
        trees.append({'path':str(p),'head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=p,text=True).strip(),
            'status':subprocess.check_output(['git','status','--porcelain'],cwd=p,text=True).strip()})
    prereg = {'experiment_id':ID, 'registered_at':now(), 'purpose':'N=1 each pure Jev and Hybrid; diagnose premature completion after irrigation',
        'experiment_commit':git('rev-parse','HEAD'), 'source_clean':True, 'order':list(ARCHS),
        'slots':[{'architecture':a,'seed':1904,'slot':f'{ID}_{a}_seed1904','config_hash':digest(cfg(a)),
            'source_config':str(OLD/f'config_{a}.json'),'source_config_sha256':file_hash(OLD/f'config_{a}.json'),
            'limits':cfg(a)['experiment_limits']} for a in ARCHS],
        'max_new_live_episodes':2, 'max_new_http_attempts':240,
        'ledger_path':str(ledger.path),'ledger_id':LEDGER_ID,'budget_before':before,
        'proposed_budget_amendment':{'max_episodes_before':12,'max_episodes_after':13,'max_http_attempts_unchanged':1440,'authorization_pending':True},
        'rules':['No changes to prompt, K=4, tools, candidates, loops, evaluator, model, route, task, checkpoint or seed',
            'Do not add the frozen-request B rule', 'No replacement samples regardless of task failure',
            'Retain SDK/updater retry policies and charge actual HTTP attempts',
            'Historical native E48/E49 is the reference; no new native run',
            'No manual actions; replay only after live capture with no model requests'],
        'outcome_dimensions':['five hn58_semantic_v2 assertions','quota observation before irrigation','positive wait reaching effect ETA',
            'post-ETA target recheck','report communication','FINISH selected while pending required actions',
            'first evidenced issue','live_integration_success separate from task_success'],
        'early_stop_interpretation':'Premature FINISH with unexecuted requirements is analogous outcome, not the native empty-tool-calls mechanism; max rounds/time/HTTP or integration errors are distinct stop mechanisms.',
        'sample_size_caveat':'N=1 per architecture, scenario seed only; no claim about prevalence or isolated causal contribution',
        'script_sha256':file_hash(Path(__file__)), 'worktrees':trees,
        'production_diff_from_native_v2':git('diff','--name-only','bbe762c','HEAD','--','fairy','configs','uv.lock','pyproject.toml'),
        'sdk_versions':{n:importlib.metadata.version(n) for n in ('typesafe-sdk','openai')},
        'route_selection':routes,'credential_present':{'TYPESAFE_API_KEY':True},'model_http_attempts_during_prepare':0}
    write_json(OUT/'preservation_before.json', {'files':hashes,'worktrees':trees})
    write_json(OUT/'budget_before.json', before)
    write_json(OUT/'access_preflight.json', gate)
    write_json(OUT/'preregistration.json', prereg)
    print(json.dumps({'prepared':str(OUT),'commit':prereg['experiment_commit'],'config_changed':False,
        'gate_blockers':{a:gate['by_architecture'][a] for a in ARCHS},'budget_used':[len(before['episodes']),before['http_attempts']],
        'budget_amendment_pending':True,'routes':routes,'historical_files_hashed':len(hashes)},ensure_ascii=False),flush=True)

def run_one(arch):
    pre = json.loads((OUT/'preregistration.json').read_text())
    assert not git('status','--porcelain') and git('rev-parse','HEAD') == pre['experiment_commit']
    assert pre['script_sha256'] == file_hash(Path(__file__))
    assert json.loads((OUT/'regression_result.json').read_text())['exit_code'] == 0
    plan = next(s for s in pre['slots'] if s['architecture'] == arch)
    assert digest(cfg(arch)) == plan['config_hash']
    ledger = budget(); before = ledger.snapshot()
    assert before['episodes'][:11] == pre['budget_before']['episodes']
    assert not any(r['slot'] == plan['slot'] for r in before['episodes'])
    remaining = [a for a in ARCHS if not (OUT/f'result_{a}.json').exists()]
    assert remaining[0] == arch
    assert len(before['episodes']) + len(remaining) <= before['max_episodes'], 'Both planned slots must fit before starting'
    assert before['http_attempts'] + 120*len(remaining) <= before['max_http_attempts']
    assert not credential_requirements(cfg(arch), arch)
    gate = access()
    with (OUT/f'started_{arch}.json').open('x') as f:
        json.dump({'at':now(),'slot':plan['slot'],'budget_before':before,'access':gate},f,ensure_ascii=False,indent=2)
    output, _ = run_episode(cfg(arch),OUT/'live',architecture=arch,mode='live',stage='supplemental_premature_stop',
        experiment_id=ID,slot=plan['slot'],ledger=ledger,require_clean=True,external_blockers=gate['by_architecture'][arch])
    result = verify_and_replay(output,cfg(arch))
    after = ledger.snapshot()
    record = {'architecture':arch,'path':str(output),'finished_at':now(),'budget_before':before,'budget_after':after,
        'new_live_episodes':len(after['episodes'])-len(before['episodes']),'new_http_attempts':after['http_attempts']-before['http_attempts'],
        'evaluation':result}
    write_json(OUT/f'result_{arch}.json',record)
    write_json(OUT/'budget_after.json',after)
    print(json.dumps({'architecture':arch,'path':str(output),**{k:result.get(k) for k in
        ('execution_status','live_integration_success','task_success','stop_reason','model_http_attempts','tool_call_count','first_issue')}},ensure_ascii=False),flush=True)

if __name__ == '__main__':
    if sys.argv[1] == 'prepare':
        prepare()
    else:
        assert sys.argv[1] in ARCHS
        run_one(sys.argv[1])
