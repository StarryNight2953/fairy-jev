"""Visible-only tool adapters: boundary and actual native execution tests."""
from copy import deepcopy
import json
from pathlib import Path
import socket
import pytest

from fairy.agents.Jev import tool_adapters as adapters
from fairy.agents.Jev.candidates import CandidateResolver
from fairy.agents.Jev.parameter_contracts import (ADAPTER_PARAMETER_STRATEGY, range_contract,
    completion_ranges, prefix_candidates, validate_contract_arguments, audit_parameter_contracts)
from fairy.agents.Jev.environment import create_fixture
from fairy.agents.Jev.tools import ToolCatalog
from fairy.agents.Jev.agent import JevAgent
from fairy.agents.Jev.client import ScriptedBackend
from fairy.agents.Jev.hybrid.agent import HybridJevAgent
from fairy.agents.Jev.trace import TraceWriter
from fairy.experiments.episode import run_episode,verify_and_replay

BASE=json.loads(Path('configs/jev/hn58_irrigation.json').read_text())

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**kw):raise AssertionError('No network in adapter tests')
    for name in ['connect','connect_ex']:monkeypatch.setattr(socket.socket,name,deny)
    monkeypatch.setattr(socket,'create_connection',deny)

@pytest.fixture(scope='module')
def entries():
    scenario,_,_=create_fixture(BASE)
    return ToolCatalog(scenario.apps).entries

def config(**kw):return {**deepcopy(BASE),'parameter_strategy':ADAPTER_PARAMETER_STRATEGY,**kw}
def row(name,result,at=10,call='t1',status='ok',arguments=None):
    return {'tool_name':name,'result':result,'status':status,'sim_time_before':at,'sim_time_after':at,
            'tool_call_id':call,'arguments':arguments or {}}
def state(*rows,now=100,task='Inspect emergence at ridges 0-63.'):
    return {'task':task,'current_time':now,'observations':list(rows)}

@pytest.mark.parametrize('app',['Robot0','Mavic3M','Matrice4T'])
def test_charge_eta_and_expiry_do_not_reuse_old_relative_time(app):
    charging=row(app+'__charge',{'status':'charging_started','eta_minutes_to_full':7},at=10)
    assert adapters.wait_values(state(charging,now=100))==[(330,'tool_result:t1:eta_minutes_to_full')]
    assert not adapters.wait_values(state(charging,now=500))
    done=row(app+'__check_status',{'charging':False,'eta_minutes_to_full':None},at=200,call='t2')
    assert not adapters.wait_values(state(charging,done,now=201))

@pytest.mark.parametrize('value',[True,float('nan'),float('inf'),-5])
def test_malformed_eta_not_offered(value):
    r=row('Robot0__charge',{'status':'charging_started','eta_minutes_to_full':value})
    assert not adapters.wait_values(state(r))

def test_missing_timestamp_unknown_not_infinite_block():
    r=row('Robot0__charge',{'status':'charging_started','eta_minutes_to_full':7});r.pop('sim_time_after')
    assert not adapters.wait_values(state(r))
    assert not adapters.precondition('Robot0__inspect_emergence',state(r))

def test_absolute_eta_precedence_and_other_effects_retained():
    a=row('Robot0__charge',{'status':'charging_started','eta_minutes_to_full':7,'charge_complete_at':500})
    b=row('FieldOpsApp__irrigate',{'effect_ready_at':600},at=20,call='t2')
    assert adapters.wait_values(state(a,b))==[(400,'tool_result:t1:charge_complete_at'),(500,'tool_result:t2:effect_ready_at')]

def test_precondition_exact_device_and_latest_successful_feedback(entries):
    r=row('Robot0__charge',{'status':'charging_started','charge_complete_at':500})
    resolver=CandidateResolver(entries,config());s=state(r)
    g=resolver.generate('Robot0__inspect_emergence',s)
    assert g['status']=='needs_context' and g['precondition']=='device_charging'
    assert resolver.generate('Mavic3M__fly_survey',s)['status']=='ready'
    assert resolver.generate('Robot0__check_status',s)['status']=='ready'
    assert resolver.generate('Robot0__inspect_emergence',state(r,now=501))['status']=='ready'
    assert CandidateResolver(entries,BASE).generate('Robot0__inspect_emergence',s)['status']=='ready'
    done=row('Robot0__check_status',{'charging':False},at=101,call='t2')
    assert resolver.generate('Robot0__inspect_emergence',state(r,done,now=102))['status']=='ready'

def test_remaining_ranges_preserve_gaps_modality_and_repeats(entries):
    name='Robot0__inspect_emergence';resolver=CandidateResolver(entries,config())
    r=row(name,{'covered_ridges':[0,1,4,5],'uncovered_ridges':[2,3,6,7,10,11]})
    other=row('Robot0__inspect_pests',{'covered_ridges':[2,3,6,7]},at=12,call='t2')
    s=state(r,other);data=adapters.coverage(name,s)
    assert adapters.intervals(data['remaining'])==[[2,3],[6,7],[10,11]]
    menu=resolver.generate(name,s);args=[v['arguments'] for v in menu['candidates'].values()]
    assert {'start_ridge':2,'end_ridge':3} in args
    assert {'start_ridge':0,'end_ridge':63} in args
    assert {'start_ridge':2,'end_ridge':11} not in args
    repeat=adapters.annotate(name,{'start_ridge':0,'end_ridge':1},s)
    assert repeat['requested_new_count']==0
    assert 'Repeat observations remain allowed' in repeat['scope']

@pytest.mark.parametrize('tool,task',[('FieldOpsApp__irrigate_ridge','Irrigate ridges 0-20 for 0.7 hours; irrigate ridges 21-42 for 0.8 hours; irrigate ridges 43-63 for 0.9 hours.'),('FieldOpsApp__apply_pesticide_manual','Apply pesticide manually at ridges 0-63: pesticide 1 L per ridge or pesticide 2 L per ridge.')])
def test_previously_overflowing_tools_now_conditioned_without_losing_values(entries,tool,task):
    c=config();resolver=CandidateResolver(entries,c);s=state(task=task)
    assert CandidateResolver(entries,BASE).generate(tool,s)['status']=='candidate_limit_exceeded'
    entry=resolver.entries[tool];contract=range_contract(entry,c['candidate_catalog'],ADAPTER_PARAMETER_STRATEGY)
    assert len(prefix_candidates(contract,'ridge_id',{},[])['candidates'])==64
    for ridge in range(64):
        generated=resolver.generate(tool,s,selected_arguments={'ridge_id':ridge},prefix_decision_ids=['d2'])
        assert generated['status']=='ready' and len(generated['candidates'])<=255
        for item in generated['candidates'].values():validate_contract_arguments(contract,entry,item['arguments'])
        if tool.endswith('irrigate_ridge'):
            assert {.7,.8,.9} <= {x['arguments']['duration_hours'] for x in generated['candidates'].values()}
        else:
            assert {1.,2.} <= {x['arguments']['liters_per_ridge'] for x in generated['candidates'].values()}
            if ridge==63:assert all(x['arguments']['ridge_count']==1 for x in generated['candidates'].values())

@pytest.mark.parametrize('bad',[{}, {'ridge_id':True},{'ridge_id':-1},{'ridge_id':64},{'ridge_id':0,'duration_hours':1}])
def test_invalid_bound_prefix_not_repaired(entries,bad):
    assert CandidateResolver(entries,config()).generate('FieldOpsApp__irrigate_ridge',state(),selected_arguments=bad)['status']=='unsupported'

def test_resource_filter_visible_evidence_rounding_and_staleness(entries):
    resolver=CandidateResolver(entries,config());name='TractorApp__apply_fertilizer'
    selected=[((0,9),'model_selection:d2,d3')]
    empty=row('TractorApp__get_status',{'fertilizer_spreader_kg':0,'fuel_tank_l':0})
    blocked=resolver.generate(name,state(empty),selected_ranges=selected)
    assert blocked['status']=='needs_context' and blocked['rejected_candidates']
    assert all(r['evidence']['source_refs']==['tool_result:t1:fertilizer_spreader_kg'] for r in blocked['rejected_candidates'])
    loaded=row('TractorApp__load_fertilizer',{'status':'ok'},at=20,call='t2')
    assert resolver.generate(name,state(empty,loaded),selected_ranges=selected)['status']=='ready'
    enough=row('TractorApp__get_status',{'fertilizer_spreader_kg':10})
    menu=resolver.generate(name,state(enough),selected_ranges=selected)
    assert {v['arguments']['kg_per_ridge'] for v in menu['candidates'].values()}=={1.0}
    # No fabricated hidden fuel consumption constant or certainty from rounded zero.
    assert adapters.resource_check(name,{'start_ridge':0,'end_ridge':0,'kg_per_ridge':.01},state(empty))['status']=='not_proven_insufficient'

@pytest.mark.parametrize('tool,result',[('TractorApp__harvest',{'status':'ok','grain_kg':1200}),('TractorApp__plant_seeds',{'status':'ok','planted_ridges':[0,1]}),('FarmWorldApp__store_grain',{'status':'ok','warehouse_grain_kg':1200})])
def test_generic_report_uses_actual_facts_not_irrigation_template(entries,tool,result):
    s=state(row(tool,result),task='Harvest, store and report actual results.')
    g=CandidateResolver(entries,config()).generate('AgentUserInterface__send_message_to_user',s)
    report=next(iter(g['candidates'].values()))['arguments']['content']
    assert tool in report and '成功启动灌溉' not in report
    assert 'not independent task validation' in report
    assert '1200' in report if 'grain_kg' in str(result) else 'planted_ridges' in report

def test_report_does_not_embed_previous_report_and_limits_detail():
    data={'field'+str(n):'x'*800 for n in range(100)}
    s=state(row('FarmWorldApp__get_inventory',data),row('AgentUserInterface__send_message_to_user','previous_report_secret_marker',at=12,call='t2'))
    text=adapters.factual_report(s)
    assert len(text)<12000 and 'previous_report_secret_marker' not in text and 'detail_omission_reason' in text

class Updater:
    def __init__(self,trace):self.trace=trace
    def update(self,data):
        material={'brief':'Use visible feedback; choose the next tool.','evidence_refs':data['input_event_ids']}
        self.trace.emit('scripted_context_update',input=data,material=material)
        return material

@pytest.mark.parametrize('arch,strategy',[('jev','visible_state_v1'),('jev','task_history_v1'),('hybrid','visible_state_v1')])
def test_actual_robot_blocked_charging_then_wait_and_continue(tmp_path,arch,strategy):
    cfg=config(state_strategy=strategy);scenario,engine,_=create_fixture(cfg);catalog=ToolCatalog(scenario.apps)
    resolver=CandidateResolver(catalog.entries,cfg);trace=TraceWriter(tmp_path/'trace.jsonl','v2-offline')
    turn=[0];tools=['Robot0__inspect_emergence','Robot0__inspect_emergence','Robot0__charge','Robot0__inspect_emergence','SystemApp__advance_time','Robot0__inspect_emergence','FINISH']
    def policy(state,stage,instructions,options):
        if stage=='tool':turn[0]+=1;return tools[turn[0]-1]
        if stage=='parameter_start_ridge':
            assert turn[0]!=4,'Blocked tool must not spend parameter choices'
            return 'value_'+str({1:0,2:8,6:16}[turn[0]])
        target={'seconds':3600,'minutes':0,'hours':0,'days':0} if turn[0]==5 else {'start_ridge':{1:0,2:8,6:16}[turn[0]],'end_ridge':{1:63,2:63,6:31}[turn[0]]}
        return next(k for k,v in options.items() if v['arguments']==target)
    cls=HybridJevAgent if arch=='hybrid' else JevAgent
    agent=cls(catalog,ScriptedBackend(policy),resolver,trace,cfg,**({'updater':Updater(trace)} if arch=='hybrid' else {}));agent.set_time_manager(engine.time_manager)
    agent.run('Inspect emergence at ridges 0-63.');trace.close()
    executed=[e for e in trace.events if e['event']=='tool_end']
    assert len(executed)==5 and all(e['status']=='ok' for e in executed)
    assert executed[-1]['result']['covered_ridges']==list(range(16,24))
    assert any(e['event']=='controller_feedback' and not e['executed'] and 'charging' in e['reason'] for e in trace.events)
    assert not audit_parameter_contracts(trace.events,catalog.by_name,cfg)
    assert agent.stop_reason=='model_finish'

@pytest.mark.parametrize('arch',['jev','hybrid'])
def test_v2_hn58_full_offline_runner_verification_replay(tmp_path,arch):
    cfg=config();path,result=run_episode(cfg,tmp_path,architecture=arch,mode='scripted')
    assert result['scripted_task_success'] is True
    verified=verify_and_replay(path,cfg)
    assert verified['trace_verification']['passed'],verified['trace_verification']
    assert verified['replay_verification']['passed']

@pytest.mark.parametrize('tool,args',[('FieldOpsApp__irrigate_ridge',{'ridge_id':43,'duration_hours':.8}),('FieldOpsApp__apply_pesticide_manual',{'ridge_id':63,'liters_per_ridge':1.0,'ridge_count':1,'advance_time':True})])
def test_single_ridge_decision_chain_executes_exact_args_and_is_auditable(tmp_path,tool,args):
    cfg=config();scenario,engine,_=create_fixture(cfg);catalog=ToolCatalog(scenario.apps);trace=TraceWriter(tmp_path/'trace.jsonl','single-ridge-v2')
    turn=[0]
    def policy(state,stage,instructions,options):
        if stage=='tool':turn[0]+=1;return tool if turn[0]==1 else 'FINISH'
        if stage=='parameter_ridge_id':return 'value_'+str(args['ridge_id'])
        return next(k for k,v in options.items() if v['arguments']==args)
    agent=JevAgent(catalog,ScriptedBackend(policy),CandidateResolver(catalog.entries,cfg),trace,cfg);agent.set_time_manager(engine.time_manager)
    agent.run('Irrigate at ridges 43-63 for 0.8 hours; pesticide 1 L per ridge.');trace.close()
    executed=[e for e in trace.events if e['event']=='tool_end']
    assert len(executed)==1 and executed[0]['arguments']==args
    assert len(executed[0]['decision_ids'])==3
    assert not audit_parameter_contracts(trace.events,catalog.by_name,cfg)
    corrupted=deepcopy(trace.events)
    event=next(e for e in corrupted if e['event']=='parameter_candidates')
    next(iter(event['generation']['candidates'].values()))['arguments']['ridge_id']=64
    assert audit_parameter_contracts(corrupted,catalog.by_name,cfg)

def test_mixed_task_report_preserves_non_irrigation_facts(entries):
    s=state(row('FieldOpsApp__irrigate',{'status':'irrigation_started','irrigated_ridges':[0],'duration_hours_per_ridge':1}),row('TractorApp__harvest',{'grain_kg':1200},at=200,call='t2'))
    report=next(iter(CandidateResolver(entries,config()).generate('AgentUserInterface__send_message_to_user',s)['candidates'].values()))['arguments']['content']
    assert 'grain_kg' in report and '1200' in report and '灌溉专项记录' in report
