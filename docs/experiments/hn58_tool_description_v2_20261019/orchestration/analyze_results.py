"""C4/C5 read-only analysis of saved runs; never calls a model or environment."""
import ast
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

OUT=Path(__file__).resolve().parent
ROOT=OUT.parents[2]
ARCHS=('llm_native','jev','hybrid')
ASSERTIONS=('pre_irrigation_observations','new_irrigation_exact_target_and_dose','quota_consumption','processed_waited_rechecked_response','factual_final_report')

def read(p): return json.loads(Path(p).read_text())
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,d): Path(p).write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
def git(*args): return subprocess.check_output(['git',*args],cwd=ROOT,text=True).strip()
def ref(path,event,field=None):
    rows=[json.loads(x) for x in path.read_text().splitlines()]
    line=next(i for i,e in enumerate(rows,1) if e['event_id']==event['event_id'])
    return {'path':str(path),'sha256':sha(path),'jsonl_line':line,'event_id':event['event_id'],'field':field}
def parse_content(value):
    if not isinstance(value,str):return value
    try:return json.loads(value)
    except ValueError:
        try:return ast.literal_eval(value)
        except (ValueError,SyntaxError):return None

def new_run(arch):
    record=read(OUT/f'result_{arch}.json'); p=Path(record['path'])
    m,e,metrics=[read(p/n) for n in ('manifest.json','evaluation.json','metrics.json')]
    tr=p/'trace.jsonl'; events=[json.loads(x) for x in tr.read_text().splitlines()]
    starts=[x for x in events if x['event']=='http_attempt_start']
    ends=[x for x in events if x['event']=='http_attempt_end']
    tools=[x for x in events if x['event']=='tool_end' and x.get('executed')]
    calls=[]
    for start in starts:
        end=next(x for x in ends if x['attempt_id']==start['attempt_id'])
        calls.append({'request':start,'response':end,'request_evidence':ref(tr,start),'response_evidence':ref(tr,end)})
    analysis_dir=OUT/'analysis';analysis_dir.mkdir(exist_ok=True)
    write(analysis_dir/f'{arch}_model_calls.json',{'source_trace':str(tr),'source_sha256':sha(tr),'http_attempts':calls,'scope':'Copies of captured_http request_body/raw_response_body, never regenerated model output'})
    sequence=[{'step':i,'event_id':x['event_id'],'tool':x['tool_name'],'arguments':x['arguments'],'status':x['status'],'evidence':ref(tr,x),'sim_time_before':x['sim_time_before'],'sim_time_after':x['sim_time_after']} for i,x in enumerate(tools,1)]
    inventories=[(i,x) for i,x in enumerate(tools,1) if x['tool_name']=='FarmWorldApp__get_inventory']
    irrigation=next((x for x in tools if x['tool_name'] in {'FieldOpsApp__irrigate','FieldOpsApp__irrigate_range'}),None)
    inventory_diagnostics=[]
    for i,x in inventories:
        mg=x.get('result',{}).get('management_regime',{})
        observed=[]
        for s in starts:
            for j,msg in enumerate(s.get('request_body',{}).get('messages',[])):
                if msg.get('role')=='tool' and msg.get('tool_call_id')==x['tool_call_id']:
                    assert parse_content(msg['content'])==x['result']
                    observed.append(ref(tr,s,f'request_body.messages[{j}].content'))
        inventory_diagnostics.append({'tool_step':i,'tool_evidence':ref(tr,x,'result.management_regime'),
            'quota_total':mg.get('irrigation_quota_mm_total'),'quota_used':mg.get('irrigation_used_mm_total'),
            'model_received_in_requests':observed,'received_before_irrigation':bool(irrigation and any(z['event_id']<irrigation['event_id'] for z in observed)),
            'result_complete_equality_checked':True})
    terminals=[]
    for x in ends:
        for choice in (x.get('parsed_response') or {}).get('choices',[]):
            msg=choice.get('message',{})
            if msg.get('content') and not msg.get('tool_calls'):
                terminals.append({'evidence':ref(tr,x,'parsed_response.choices[0].message.content'),'text':msg['content'],'finish_reason':choice.get('finish_reason')})
    blocked=e['execution_status']=='blocked'
    description_checks=[]
    expected=read(OUT/'c1_result.json')['catalog_diff'][0]['new']
    for x in starts:
        fs=[s['function'] for s in x['request_body'].get('tools',[]) if s.get('function',{}).get('name')=='FarmWorldApp__get_inventory']
        if fs:
            assert len(fs)==1 and fs[0]['description']==expected
            description_checks.append({'evidence':ref(tr,x,'request_body.tools:FarmWorldApp__get_inventory.description'),'matches_new_description':True,'visible_tool_count':len(x['request_body']['tools'])})
    first_error=None
    if not blocked:
        assert terminals and terminals[0]['evidence']['event_id']==49
        first_error={'classification':'confirmed_direct','kind':'premature_terminal_response_without_wait_or_recheck',
            'raw_response':terminals[0]['evidence'],'automatic_evaluator_first_issue':e['first_issue'],
            'explanation':'Final assistant text exists, but lists waiting/rechecking as a next step and emits no tool calls. No actual wait/recheck follows; the native loop ends normally, not because of any budget.',
            'quota_violation':False}
        write(analysis_dir/'native_quota_window.json',{'source_trace':str(tr),'sha256':sha(tr),'events':[{'reference':ref(tr,x),'event':x} for x in events if x['event_id'] in {7,8,25,35,36,43}]})
        write(analysis_dir/'native_termination_window.json',{'source_trace':str(tr),'sha256':sha(tr),'events':[{'reference':ref(tr,x),'event':x} for x in events if x['event_id'] in {43,48,49,50,52,54,59}]})
    result={'architecture':arch,'delivery_status':'blocked' if blocked else 'complete' if e['task_success'] is True else 'failed',
        'execution_status':e['execution_status'],'run_id':m['run_id'],'run_path':str(p),'code_commit':m['code_commit'],'config_hash':m['config_hash'],
        'live_integration_success':e['live_integration_success'],'task_success':e['task_success'],
        'task_assertions':{a:e['task_assertions'].get(a,{}).get('passed') for a in ASSERTIONS},
        'assertion_status':'not_executed' if blocked else 'evaluated','native_validation':e['native_validation'],
        'quota_diagnostic':{'get_inventory_selected':None if blocked else bool(inventories),'calls':inventory_diagnostics,
            'quota_received_before_irrigation':None if blocked else bool(irrigation and inventory_diagnostics and inventory_diagnostics[0]['received_before_irrigation']),
            'irrigated_without_quota':None if blocked else bool(irrigation and not e['task_assertions']['pre_irrigation_observations']['evidence']['quota']),
            'ordinary_inventory_overview_or_log_misidentified_as_quota':'not_observed_blocked' if blocked else 'not_observed: exact management_regime values used in E36',
            'hybrid_material_log_equals_quota':'not_observed_blocked' if arch=='hybrid' else 'not_applicable',
            'jev_after_empty_log_irrigation':'not_observed_blocked' if arch in {'jev','hybrid'} else 'not_applicable'},
        'actual_description_transmission':{'status':'not_observed_blocked' if blocked else 'verified_all_actual_requests','checks':description_checks},
        'tool_sequence':sequence,'final_text_present':None if blocked else bool(terminals),'terminal_responses':terminals,
        'communication_tool_calls':[x for x in sequence if x['tool']=='AgentUserInterface__send_message_to_user'],
        'first_error':first_error,'access_blockers':e.get('blockers',[]),'stop_reason':e['stop_reason'],
        'http_attempts':metrics['model_http_attempts'],'logical_model_requests':metrics['logical_model_requests'],
        'http_statuses':[x.get('http_status') for x in ends],'retry_indices':[x.get('sdk_retry_index') for x in starts],
        'tool_attempts':metrics['tool_attempts'],'tool_errors':metrics['tool_errors'],'pre_execution_errors':metrics['pre_execution_errors'],
        'episode_wall_seconds':metrics['episode_wall_seconds'],'latency_semantics':'blocked time only initializes/checks/writes artifacts; not model performance' if blocked else metrics['latency_policy'],
        'roles':metrics['roles'],'input_tokens':metrics['input_tokens'],'output_tokens':metrics['output_tokens'],'usage_status':metrics['usage_status'],
        'cost':{'amount':None,'status':'unknown','source_status':metrics['cost']['status']},'trace_verification':e['trace_verification'],'replay_verification':e['replay_verification'],
        'model_calls_export':str(analysis_dir/f'{arch}_model_calls.json')}
    return result,m

def old_run(p,label):
    m=read(p/'manifest.json');e=read(p/'evaluation.json');metrics=read(p/'metrics.json') if (p/'metrics.json').exists() else None
    events=[json.loads(x) for x in (p/'trace.jsonl').read_text().splitlines()]
    tools=[x for x in events if x['event']=='tool_end' and x.get('executed')]
    dst=OUT/'old_controls'/p.name;dst.mkdir(parents=True,exist_ok=True)
    names=('manifest.json','tool_catalog.json','trace.jsonl','evaluation.json','metrics.json','workflow.json','verification.json','summary.md')
    copies=[]
    for name in names:
        source=p/name
        if source.is_file():
            target=dst/name
            if target.exists():assert sha(target)==sha(source)
            else:shutil.copyfile(source,target)
            copies.append({'source':str(source),'archive_copy':str(target),'sha256':sha(source)})
    return {'label':label,'run_id':p.name,'path':str(p),'code_commit':m.get('code_commit',m.get('commit')),
        'identity_limit':'Earlier dirty MVP implementation: recorded base commit alone does not pin uncommitted implementation; use saved implementation hashes; evaluator not hn58_semantic_v2' if 'code_commit' not in m else None,
        'mode':m.get('mode'),'stage':m.get('stage','earlier_mvp'),'config':m.get('config'),'config_hash':m.get('config_hash'),
        'evaluator_version':m.get('evaluator_version','legacy_unversioned'),'evaluator_hash':m.get('evaluator_hash'),
        'identity':{k:m.get(k) for k in ('prompt_hash','candidate_strategy_hash','parameter_catalog_hash','catalog_hash','checkpoint_hash','data_assets','lockfile_hash','sdk_version','openai_sdk_version','seed')},
        'live_integration_success':e.get('live_integration_success'),'task_success':e.get('task_success'),
        'task_assertions':{k:e.get('task_assertions',e.get('assertions',{})).get(k,{}).get('passed') for k in ASSERTIONS},
        'first_issue':e.get('first_issue'),'stop_reason':e.get('stop_reason'),'model_http_attempts':e.get('model_http_attempts'),
        'model_request_count':e.get('model_request_count'),'tool_call_count':e.get('tool_call_count'),'wall_seconds':e.get('wall_elapsed_seconds'),
        'metrics':metrics,'get_inventory_steps':[i for i,x in enumerate(tools,1) if x['tool_name']=='FarmWorldApp__get_inventory'],
        'tool_sequence':[{'event_id':x['event_id'],'tool':x['tool_name'],'arguments':x['arguments']} for x in tools],
        'archive_copies':copies},m,events

def main():
    rows=[]; manifests={}
    for a in ARCHS:
        row,m=new_run(a);rows.append(row);manifests[a]=m
    oldpaths=[(ROOT/'runs/agent_comparison/hn58-three-agents-qwen-v1/formal'/rid,'native_formal_prior') for rid in (
        '20261018T131925_llm_native_live_d53c8d5e','20261018T131959_llm_native_live_a52947cf','20261018T132025_llm_native_live_82c497e7')]
    oldpaths += [(ROOT/'runs/agent_comparison/hybrid_relocated_20261018/smoke/20261018T140025_hybrid_live_9fd09d81','hybrid_diagnostic_smoke'),
        (ROOT.parent/'jev-mvp/runs/jev/live/20261017T112501_live_db897d9a','jev_earlier_mvp_different_contract'),
        (ROOT/'runs/agent_comparison/hn58-three-agents-v1/smoke/20261018T094403_jev_live_3ec48d66','jev_451_no_valid_loop')]
    old=[];comparability=[]
    for p,label in oldpaths:
        row,m,events=old_run(p,label);old.append(row)
        a=m.get('architecture') or ('jev' if 'jev' in label else 'hybrid' if 'hybrid' in label else 'llm_native')
        target=manifests[a]
        diffs={k:{'old':m.get('config',{}).get(k),'new':target['config'].get(k)} for k in set(m.get('config',{}))|set(target['config']) if m.get('config',{}).get(k)!=target['config'].get(k)}
        identity={k:m.get(k)==target.get(k) for k in ('config_hash','prompt_hash','candidate_strategy_hash','parameter_catalog_hash','evaluator_hash','checkpoint_hash','data_assets','lockfile_hash')}
        item={'old_run_id':p.name,'target_architecture':a,'config_diff':diffs,'identity_equality':identity,
            'changed_recorded_implementation_hashes':[k for k,v in m.get('implementation_hashes',{}).items() if target.get('implementation_hashes',{}).get(k)!=v],
            'git_diff_name_status':git('diff','--name-status',row['code_commit'],target['code_commit']) if row['code_commit'] else None}
        if label=='native_formal_prior' and m['seed']==1904:
            new_events=[json.loads(x) for x in (Path(rows[0]['run_path'])/'trace.jsonl').read_text().splitlines()]
            old_first=next(x for x in events if x['event']=='http_attempt_start');new_first=next(x for x in new_events if x['event']=='http_attempt_start')
            ob,nb=deepcopy(old_first['request_body']),deepcopy(new_first['request_body'])
            schema_diffs=[]
            for ot,nt in zip(ob['tools'],nb['tools']):
                assert ot['function']['name']==nt['function']['name']
                if ot!=nt:schema_diffs.append({'name':nt['function']['name'],'old':ot,'new':nt})
            assert len(schema_diffs)==1
            of=next(x for x in ob['tools'] if x['function']['name']=='FarmWorldApp__get_inventory')
            nf=next(x for x in nb['tools'] if x['function']['name']=='FarmWorldApp__get_inventory')
            of['function']['description']=nf['function']['description']
            assert ob==nb
            item['actual_first_http_request']={'old':ref(p/'trace.jsonl',old_first),'new':ref(Path(rows[0]['run_path'])/'trace.jsonl',new_first),
                'equal_except_inventory_description':True,'initial_messages_exactly_equal':True,'generation_fields_exactly_equal':True,'schema_diff':schema_diffs}
        comparability.append(item)
    write(OUT/'comparison.json',{'experiment_id':'hn58-tool-description-v2-20261019','assertion_order':list(ASSERTIONS),'new_runs':rows,'old_controls':old,
        'comparability':comparability,'interpretation':'N=1 planned per architecture; only one new native run eligible, two blocked. Descriptive diagnostic, not a completed three-architecture efficacy comparison, not statistical significance or efficiency evidence.',
        'public_task_vs_evaluator':'Waiting and rechecking are explicit public requirements. Target-state proxy fields and complete report vocabulary are not fully specified in task text. Native terminal prose exists but is before completed recheck. Preserve v2 scoring unchanged.'})
    initial=read(OUT/'c0_snapshot.json'); ledger_path=Path(initial['ledger_path']);after=read(ledger_path)
    assert after['ledger_id']==initial['budget_before']['ledger_id']
    assert after['episodes'][:6]==initial['budget_before']['episodes']
    assert len(after['episodes'])==7 and after['http_attempts']==33
    write(OUT/'budget_after.json',{'snapshot_at':datetime.now(timezone(timedelta(hours=8))).isoformat(),'ledger_path':str(ledger_path),'ledger_sha256':sha(ledger_path),
        'ledger':after,'before':{'episodes':6,'http_attempts':30},'after':{'episodes':7,'http_attempts':33},
        'this_batch_consumption':{'live_episodes':1,'http_attempts':3},'remaining':{'live_episodes':5,'http_attempts':1407},
        'read_only_snapshot':True,'blocked_slots_consumed':False})
    c0=read(OUT/'c0_identity.json');c1=read(OUT/'c1_result.json')
    exp={'experiment_id':'hn58-tool-description-v2-20261019','version':'tool_description_v2','experiment_commit':c0['experiment_commit'],'experiment_tag':c0['experiment_tag'],
        'branch':'experiment/hn58-tool-description-v2-20261019','base_commit':c0['base_commit'],'docstring_commit':'e868559',
        'purpose':'Evaluate only the user get_inventory Docstring update under the unchanged three architectures; one preregistered attempt per architecture, no replacement samples',
        'status':'PARTIAL_ACCESS_BLOCKED','checkpoint_status':{'C0':'PASSED','C1':'PASSED','C2':'PASSED','C3':'PARTIAL_ACCESS_BLOCKED','C4':'COMPLETE_WITH_BLOCKED_ROWS','C5':'COMPLETE'},
        'docstring_diff':c1['catalog_diff'],'production_diff':git('diff',c0['base_commit'],c0['experiment_commit'],'--','fairy'),
        'additional_changes':'Only new offline regression test, separately committed; post-run documentation/orchestration archive commits do not change execution identity',
        'seed':1904,'configs':{a:{'config':manifests[a]['config'],'hash':manifests[a]['config_hash']} for a in ARCHS},
        'shared_identity':{k:manifests['llm_native'].get(k) for k in ('scenario_id','checkpoint_hash','effective_start_time','clock_source','scenario_source','data_assets','catalog_hash','lockfile_hash','prompt_hash','candidate_strategy_hash','parameter_catalog_hash','evaluator_version','evaluator_hash','sdk_version','openai_sdk_version')},
        'ledger_reference':{'path':str(ledger_path),'ledger_id':after['ledger_id'],'before_snapshot':str(OUT/'c0_snapshot.json'),'after_snapshot':str(OUT/'budget_after.json')},
        'old_controls':[{'run_id':r['run_id'],'path':r['path'],'code_commit':r['code_commit'],'evaluator_version':r['evaluator_version'],'identity_limit':r['identity_limit']} for r in old],
        'planned_architectures':list(ARCHS),'executed_architectures':['llm_native'],'blocked_architectures':['jev','hybrid'],
        'access_rule_unchanged':True,'new_catalog_hash':c1['new_catalog_hash'],'raw_protected_before_count':len(initial['protected_files']),
        'configuration_limit_note':'Unchanged inherited configuration_note refers to a historical metadata GET; this batch performed no metadata/network probes.'}
    write(OUT/'experiment_manifest.json',exp)
    index={'root':str(ROOT),'batch':str(OUT),'workspace_map':{'path':str(ROOT.parent/'workspace-paths.json'),'sha256':sha(ROOT.parent/'workspace-paths.json'),'mapping':read(ROOT.parent/'workspace-paths.json')},
        'runs':[],'old_controls':[{k:r[k] for k in ('run_id','label','archive_copies')} for r in old],
        'notes':['Full model requests/responses in raw trace and analysis/*_model_calls.json. No auth headers recorded.',
            'Blocked rows have no actual HTTP and no replay execution; verification is artifact integrity only.',
            'Archive includes unchanged copies of six historical controls; original files are never edited.',
            'All relative payload hashes are additionally listed in files.sha256; this index excludes its own hash.']}
    for r in rows:
        p=Path(r['run_path'])
        files=[{'path':str(f),'batch_relative_path':str(f.relative_to(OUT)),'sha256':sha(f),'bytes':f.stat().st_size} for f in sorted(p.rglob('*')) if f.is_file()]
        index['runs'].append({'architecture':r['architecture'],'status':r['delivery_status'],'run_id':r['run_id'],'files':files,'replay':r['replay_verification'],'trace_verification':r['trace_verification']})
    write(OUT/'evidence_index.json',index)
    write(OUT/'checkpoint_status.json',exp['checkpoint_status'])
    print(json.dumps({'rows':[{k:r[k] for k in ('architecture','delivery_status','live_integration_success','task_success','task_assertions')} for r in rows],
        'historical_controls':len(old),'budget_after':{'episodes':7,'http_attempts':33}},ensure_ascii=False))

if __name__=='__main__':main()
