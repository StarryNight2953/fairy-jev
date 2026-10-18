"""Post-run evidence analysis only: no model, environment, or tool execution."""
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
import hashlib
import json
import shutil
import subprocess

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
DOCS = ROOT/'docs/experiments/hn58_jev_hybrid_continuation_20261019'
NATIVE = ROOT/'runs/agent_comparison/hn58_tool_description_v2_20261019/live/20261018T165324_llm_native_live_3b5f3748'
OLD_HYBRID = ROOT/'runs/agent_comparison/hybrid_relocated_20261018/smoke/20261018T140025_hybrid_live_9fd09d81'

def read(p):
    return json.loads(Path(p).read_text())

def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def write(p, obj):
    p = Path(p); p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n')

def events(p):
    return [json.loads(s) for s in (p/'trace.jsonl').read_text().splitlines() if s]

def compact_result(p, historical=False):
    e,m,manifest = (read(p/n) for n in ('evaluation.json','metrics.json','manifest.json'))
    return {'path':str(p),'run_id':manifest['run_id'],'historical_not_new_sample':historical,
        'code_commit':manifest['code_commit'],'config_hash':manifest['config_hash'],
        'execution_status':e['execution_status'],'live_integration_success':e['live_integration_success'],
        'task_success':e['task_success'],'stop_reason':e['stop_reason'],
        'assertions':{k:v['passed'] for k,v in e['assertions'].items()},
        'pre_irrigation_evidence':e['assertions']['pre_irrigation_observations']['evidence'],
        'first_automatic_issue':e.get('first_issue'),'metrics':m,
        'trace_verification_passed':e.get('trace_verification',{}).get('passed'),
        'replay_passed':e.get('replay_verification',{}).get('passed'),
        'replay_task_success':e.get('replay_verification',{}).get('task_success')}

def main():
    pre=read(OUT/'preregistration.json')
    before=read(OUT/'budget_before.json'); after=read(OUT/'budget_after.json')
    assert after['episodes'][:11]==before['episodes']
    assert len(after['episodes'])-len(before['episodes'])==2
    assert after['http_attempts']-before['http_attempts']==29
    assert after['ledger_id']==before['ledger_id'] and after['max_http_attempts']==1440
    assert read(pre['ledger_path'])==after
    paths={a:Path(read(OUT/f'result_{a}.json')['path']) for a in ('jev','hybrid')}
    es={a:events(p) for a,p in paths.items()}
    rows={a:compact_result(p) for a,p in paths.items()}
    manifests={a:read(p/'manifest.json') for a,p in paths.items()}
    native=compact_result(NATIVE,True); oldhybrid=compact_result(OLD_HYBRID,True)
    for name,p in (('native_v2',NATIVE),('hybrid_v1',OLD_HYBRID)):
        dest=OUT/'old_controls'/name
        if not dest.exists():
            shutil.copytree(p,dest)
    native_manifest=read(NATIVE/'manifest.json')
    identity_fields=['catalog_hash','checkpoint_hash','effective_start_time','checkpoint_sim_time',
        'prompt_hash','candidate_strategy_hash','parameter_catalog_hash','evaluator_hash',
        'lockfile_hash','data_assets','scenario_source','visibility_rule_hash','sdk_version','openai_sdk_version','seed']
    identity_comparison={k:{'llm_native_old':native_manifest.get(k),**{a:m.get(k) for a,m in manifests.items()}}
        for k in identity_fields}
    assert all(len({json.dumps(v,sort_keys=True) for v in row.values()})==1 for row in identity_comparison.values())
    wire_propagation={}
    for a,p in paths.items():
        e=es[a]; row=rows[a]; evaluation=read(p/'evaluation.json')
        assert row['trace_verification_passed'] and row['replay_passed']
        assert row['replay_task_success'] is False
        assert manifests[a]['code_commit']==pre['experiment_commit'] and manifests[a]['source_clean']
        assert (OUT/f'config_{a}.json').read_bytes()==Path(next(s for s in pre['slots'] if s['architecture']==a)['source_config']).read_bytes()
        starts={x['attempt_id']:x for x in e if x['event']=='http_attempt_start'}
        ends={x['attempt_id']:x for x in e if x['event']=='http_attempt_end'}
        assert starts.keys()==ends.keys() and len(starts)==row['metrics']['model_http_attempts']
        assert all(x['http_status']==200 and x['status']=='ok' for x in ends.values())
        assert all(json.loads(x['raw_response_body'])==x['parsed_response'] for x in ends.values())
        calls=[{'attempt_id':key,'request_event_id':x['event_id'],'response_event_id':ends[key]['event_id'],
            'role':x['role'],'logical_request_id':x['logical_request_id'],'endpoint':x['endpoint'],
            'request_body':x['request_body'],'raw_response_body':ends[key]['raw_response_body'],
            'response_model':ends[key]['response_model'],'http_status':ends[key]['http_status'],
            'latency_seconds':ends[key]['wall_elapsed_seconds'],'usage':ends[key]['usage']}
            for key,x in starts.items()]
        dest=OUT/'analysis'/a;dest.mkdir(parents=True,exist_ok=True)
        (dest/'model_calls.jsonl').write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in calls))
        tools=[x for x in e if x['event']=='tool_end']
        timeline=[{k:x.get(k) for k in ('event_id','step_id','tool_call_id','tool_name','arguments','status','executed',
            'sim_time_before','sim_time_after','wall_elapsed_seconds')} for x in tools]
        write(dest/'tool_timeline.json',timeline)
        write(dest/'packets.json',[x for x in e if x['event']=='packet_accepted'])
        irrigation=next(x for x in tools if isinstance(x.get('result'),dict) and x['result'].get('status')=='irrigation_started')
        inventory=[x for x in tools if x['tool_name']=='FarmWorldApp__get_inventory']
        reports=[x for x in tools if x['tool_name']=='AgentUserInterface__send_message_to_user']
        waits=[x for x in tools if x['tool_name'] in ('SystemApp__advance_time','SystemApp__wait_for_notification')]
        finish=next(x for x in e if x['event']=='model_finish')
        decision=next(x for x in e if x['event']=='decision_end' and x.get('decision_id')==finish['decision_id'])
        response=next(x for x in ends.values() if x['logical_request_id']==decision['request_id'])
        request=starts[response['attempt_id']]
        assert response['parsed_response']['answers']['tool']['choice']=='FINISH'
        row['quota_behavior']={'read_before_irrigation':bool(inventory and inventory[0]['event_id']<irrigation['event_id']),
            'inventory_events':[x['event_id'] for x in inventory],'quota_total':inventory[0]['result']['management_regime']['irrigation_quota_mm_total'],
            'used_before':inventory[0]['result']['management_regime']['irrigation_used_mm_total'],
            'quota_consumption':evaluation['assertions']['quota_consumption']['evidence'],
            'irrigation_log_selected':any(x['tool_name']=='FieldOpsApp__get_irrigation_log' for x in tools)}
        row['continuation']={'irrigation_event_id':irrigation['event_id'],'effect_ready_at':irrigation['result']['effect_ready_at'],
            'waits':[{k:x.get(k) for k in ('event_id','tool_name','arguments','sim_time_before','sim_time_after')} for x in waits],
            'wait_recheck_assertion_passed':row['assertions']['processed_waited_rechecked_response'],
            'recheck_evidence':evaluation['assertions']['processed_waited_rechecked_response']['evidence']['semantic_rechecks'],
            'report_event_ids':[x['event_id'] for x in reports],
            'report_assertion_passed':row['assertions']['factual_final_report'],
            'finish_request_event_id':request['event_id'],'finish_raw_response_event_id':response['event_id'],
            'finish_decision_event_id':decision['event_id'],'model_finish_event_id':finish['event_id'],
            'finish_confidence':response['parsed_response']['answers']['tool']['confidence'],
            'native_missing_wait_recheck_reproduced':False,
            'finish_without_report':not bool(reports),'budget_or_service_stop':False}
        row['http_status_counts']=dict(Counter(x['http_status'] for x in ends.values()))
        row['http_retries']=len(starts)-row['metrics']['logical_model_requests']
        choices=[x for x in starts.values() if x['role']=='jev_tool_choice']
        assert all(len(x['request_body']['questions']['tool']['criteria'])==59 for x in choices)
        assert all('including irrigation quota' in x['request_body']['questions']['tool']['criteria']['FarmWorldApp__get_inventory']['purpose'] for x in choices)
        wire_propagation[a]={'all_real_tool_choice_menus_have_58_tools_plus_FINISH':True,
            'tool_choice_requests':len(choices),'new_inventory_description_in_all_real_tool_choice_requests':True,
            'first_request_event_id':choices[0]['event_id']}
        write(dest/'finish_window.json',[x for x in e if request['event_id']-3<=x['event_id']<=finish['event_id']+1])
        write(dest/'irrigation_window.json',[x for x in e if irrigation['event_id']-12<=x['event_id']<=irrigation['event_id']+2])
    h=es['hybrid']; updater=json.loads(h[68]['request_body']['messages'][1]['content'])
    fact=next(x for x in updater['fresh_events'] if x['event_id']==49)
    assert fact==h[49] and fact['result']['management_regime']['irrigation_quota_mm_total']==1.4
    material=json.loads(h[69]['parsed_response']['choices'][0]['message']['content'])
    assert 'Quota is unlimited.' in material['brief']
    assert h[74]['packet']['brief']==material['brief']==h[79]['request_body']['state']['packet']['brief']
    assert h[79]['request_body']['state']['fresh_events']==[]
    for x in h:
        if x['event']=='http_attempt_start' and x['role']=='llm_context_update':
            capabilities=json.loads(x['request_body']['messages'][1]['content'])['tool_capabilities']
            assert len(capabilities)==59 and 'including irrigation quota' in capabilities['FarmWorldApp__get_inventory']['purpose']
    wire_propagation['hybrid']['new_inventory_description_in_both_real_updater_requests']=True
    contradiction={'classification':'DIRECT_OBSERVATION','first_clear_material_factual_contradiction_event_id':69,
        'actual_inventory_event_id':49,'actual_quota':1.4,'actual_used':0.0,
        'updater_http_request_event_id':68,'inventory_evidence_in_http_input_exactly_equal_to_tool_end':True,
        'raw_updater_response_event_id':69,'model_material_quote':'irrigation quota null/unlimited; Quota is unlimited.',
        'accepted_packet_event_id':74,'first_jev_http_request_carrying_wrong_material_event_id':79,
        'first_jev_input_has_packet_and_empty_fresh_events':True,'packet_source_kind':'llm_interpretation_and_advice',
        'not_a_tool_return_or_serialization_loss':True,
        'automatic_first_task_issue_event_id':92,
        'causation':'Unknown whether the false quota claim caused the irrigation choice or FINISH. Actual dose remained within the true quota.',
        'later_visible_correction':'E92 actual irrigation return again contains quota=1.4 and used=1.3125; retained as fresh evidence for later choices. No updater refresh after tool 7 because K=4.'}
    rows['hybrid']['material_contradiction']=contradiction
    write(OUT/'analysis/hybrid/quota_contradiction.json',contradiction)
    write(OUT/'analysis/hybrid/quota_contradiction_window.json',[h[i] for i in (49,65,68,69,70,71,74,79,80,91,92)])
    write(OUT/'wire_propagation.json',wire_propagation)
    write(OUT/'identity_comparison.json',identity_comparison)
    summary={'experiment_id':pre['experiment_id'],'execution_commit':pre['experiment_commit'],
        'new_live_episodes':2,'new_http_attempts':29,'new_native_runs':0,'replacement_samples':0,
        'rows':rows,'historical_controls':{'llm_native_v2':native,'hybrid_previous':oldhybrid},
        'comparison_scope':'Two independent full runs, not frozen E48 requests or causal isolation of architecture. Same HN58 checkpoint, seed1904, catalog/evaluator; all decision code unchanged.',
        'direct_observations':['Both waited and rechecked after irrigation ETA; native missing-wait/recheck behavior not observed in either new sample.',
            'Both omitted target-state evidence before irrigation. Both read actual remaining quota before irrigation.',
            'Pure Jev later sent a factual report and then FINISH; Hybrid FINISH after recheck without any communication tool.',
            'Hybrid updater misrepresented observed quota1.4 as unlimited in raw model output; unchanged wrong material reached Jev.'],
        'supported_inference':'The shared Jev FINISH handler can accept model completion while independent task requirements remain unmet. Hybrid showed this at the report stage.',
        'unknown':['Why a particular model selected FINISH','Whether quota misunderstanding caused later choices',
            'Stability or frequency of any failure','Would the generic B-rule improve these architectures','General comparative efficiency or success rate'],
        'semantic_limitations':['Public task asks for target status without naming growth_stage/irrigation_pending evaluator proxy fields.',
            'The public task text does not explicitly state the evaluator final-report scoring vocabulary; report omission is an existing-contract failure.',
            'Native terminal text and Jev FINISH are different mechanisms. One sample each cannot prove absence of a structural risk.'],
        'budget':{'before_slots':11,'after_slots':13,'before_http':37,'after_http':66,'max_episodes_after_user_override':13,
            'max_http_attempts':1440,'ledger_id':after['ledger_id'],'historical_rows_unchanged':True,'authorization_file':'budget_authorization.json'},
        'validation':{'regression':'302 passed in 3.81s','regression_network_denied':True,'both_trace_verifications_passed':True,
            'both_action_replays_passed':True,'replays_model_requests':0,'true_task_failure_reproduced':True,
            'all_29_http_responses_status200':True,'http_retries':0,'production_changes_this_experiment':[]}}
    write(OUT/'comparison.json',summary)
    original=read(OUT/'preservation_before.json')
    changed=[p for p,digest in original['files'].items() if (sha(p) if Path(p).is_file() else None)!=digest]
    assert not changed
    assert not subprocess.check_output(['git','diff','--name-only',pre['experiment_commit'],'--','fairy','configs','tests','uv.lock','pyproject.toml'],cwd=ROOT,text=True).strip()
    for tree in original['worktrees']:
        if Path(tree['path']) != ROOT:
            assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=tree['path'],text=True).strip()==tree['head']
            assert subprocess.check_output(['git','status','--porcelain'],cwd=tree['path'],text=True).strip()==tree['status']
    assert subprocess.check_output(['git','rev-parse','refs/tags/experiment/hn58-tool-description-v2-20261019'],cwd=ROOT,text=True).strip()==native_manifest['code_commit']
    preservation={'checked_paths':len(original['files']),'checked_existing_files':sum(v is not None for v in original['files'].values()),
        'checked_prior_absences':sum(v is None for v in original['files'].values()),'changed':changed,'passed':True,
        'original_ledger_rows_unchanged':after['episodes'][:11]==before['episodes'],
        'ledger_id_unchanged':after['ledger_id']==before['ledger_id'],'old_tags_not_moved':True}
    write(OUT/'preservation.json',preservation)
    source_files=['fairy/agents/Jev/agent.py','fairy/agents/Jev/state.py','fairy/agents/Jev/tools.py',
        'fairy/agents/Jev/hybrid/agent.py','fairy/agents/Jev/hybrid/context.py','fairy/agents/Jev/hybrid/prompt.md',
        'fairy/agents/Jev/hybrid/updater.py','fairy/agents/Jev/candidates.py','fairy/experiments/evaluation.py',
        'fairy/experiments/episode.py','fairy/experiments/access.py','fairy/experiments/budget.py',
        'fairy/agents/agent/agent.py','fairy/apps/system.py','fairy/controllers/engine.py']
    for name in source_files:
        dest=OUT/'source_code'/name;dest.parent.mkdir(parents=True,exist_ok=True)
        dest.write_bytes(subprocess.check_output(['git','show',pre['experiment_commit']+':'+name],cwd=ROOT))
    write(OUT/'source_identity.json',{'execution_commit':pre['experiment_commit'],'files':{n:sha(ROOT/n) for n in source_files},
        'source_clean_at_both_live_runs':True,'only_runtime_change_since_native_bbe762c':'access.py historical451 supersession, previously committed; no model decision changes'})
    command_records=[
        {'phase':'inspection','command':'git worktree list --porcelain; git diff --name-only bbe762c HEAD -- fairy configs uv.lock pyproject.toml requirements.txt','result':'Three worktrees recorded; only pre-existing access.py differs in production scope'},
        {'phase':'offline','command':f'PYTHONPATH=. .venv/bin/python {OUT.relative_to(ROOT)}/run_regression.py','result':'302 passed in 3.81s; socket network denied'},
        {'phase':'prepare','command':f'set +x\nset -a\nsource ../jev-mvp/env.sh >/dev/null 2>&1\nset +a\nPYTHONPATH=. .venv/bin/python {OUT.relative_to(ROOT)}/run_supplement.py prepare',
         'result':'Success, zero HTTP. Earlier local preparation attempts stopped before HTTP: env assignments needed export; route introspection fixed to use client.base_url URL class. Only ignored orchestration file edited before preregistration.'},
        {'phase':'authorization','command':'Python fcntl-locked same-ledger edit max_episodes12->13 after explicit user override; historical rows/counts unchanged','record':'budget_authorization.json'},
        *[{'phase':'live','command':f'set +x\nset -a\nsource ../jev-mvp/env.sh >/dev/null 2>&1\nset +a\nPYTHONPATH=. .venv/bin/python {OUT.relative_to(ROOT)}/run_supplement.py {a} > {OUT.relative_to(ROOT)}/console_{a}.log 2>&1',
            'result':{'exit_code':0,'live_integration_success':True,'task_success':False,'http_attempts':rows[a]['metrics']['model_http_attempts']}} for a in ('jev','hybrid')],
        {'phase':'analysis','command':f'.venv/bin/python {OUT.relative_to(ROOT)}/build_delivery.py','result':'Read-only source/trace analysis, preservation and payload checks; no model/environment calls'}]
    write(OUT/'commands.json',command_records)
    write(OUT/'validation.json',{'passed':True,**summary['validation'],'preservation':preservation,
        'config_and_common_initial_identity_equal':True,'quota_misstatement_verified_in_raw_request_response':True,
        'wire_propagation':wire_propagation})
    return summary

if __name__=='__main__':
    s=main()
    print(json.dumps({'analyzed':str(OUT),'runs':list(s['rows']),'http':s['new_http_attempts'],'preserved':read(OUT/'preservation.json')['checked_existing_files']},ensure_ascii=False))
