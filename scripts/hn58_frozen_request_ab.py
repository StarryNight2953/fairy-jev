"""Frozen-request diagnosis only: no Agent, Scenario, tool dispatch or evaluator.

Uses the existing LiveLedger/EpisodeRuntime/ObservedTransport accounting path.
Each sample consumes one ledger episode reservation and at most one actual HTTP.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import subprocess
import time

import httpx
from openai import OpenAI

from fairy.agents.Jev.trace import TraceWriter
from fairy.experiments.access import service_blockers_from_smoke
from fairy.experiments.budget import BudgetStop, EpisodeRuntime, LiveLedger
from fairy.experiments.paths import resolve_saved_path
from fairy.experiments.transports import instrument_http_client

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'runs/agent_comparison/hn58_tool_description_v2_20261019/live/20261018T165324_llm_native_live_3b5f3748'
SOURCE_COMMIT='bbe762c98a97e5f22cceb5f7b24fc3a67656fdb6'
LEDGER_ID='ba456f29add04993a04ac60636f1dc0f'
RULE='- If the task still requires a tool action, use the available tools to do that action now. Do not replace an unexecuted action with a plan for later. Give a final user response only after required actions are complete, or when a tool limitation prevents further action.'
LABELS=('progress_tool_call','other_or_invalid_tool_call','text_only_future_plan','text_only_claimed_complete','request_error')

def now():return datetime.now(timezone(timedelta(hours=8))).isoformat()
def read(p):return json.loads(Path(p).read_text())
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def encoded(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
def digest(value):return hashlib.sha256(encoded(value)).hexdigest()
def write(p,value):Path(p).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
def git(*args):return subprocess.check_output(['git',*args],cwd=ROOT,text=True).strip()

def variants(source):
    a=deepcopy(source);b=deepcopy(a)
    system=a['messages'][0]['content']
    assert a['messages'][0]['role']=='system'
    assert system.count('<agent_instructions>')==system.count('</agent_instructions>')==1
    assert RULE not in system
    b['messages'][0]['content']=system.replace('</agent_instructions>',RULE+'\n</agent_instructions>')
    restored=deepcopy(b);restored['messages'][0]['content']=restored['messages'][0]['content'].replace(RULE+'\n','',1)
    assert restored==a
    return a,b

def validate_history(body):
    pending={};seen=set();pairs=[]
    for i,m in enumerate(body['messages']):
        for call in m.get('tool_calls') or []:
            assert call['id'] not in seen and call['type']=='function'
            seen.add(call['id']);pending[call['id']]=i
        if m.get('role')=='tool':
            identifier=m['tool_call_id'];assert identifier in pending
            pairs.append({'tool_call_id':identifier,'assistant_message_index':pending.pop(identifier),'tool_message_index':i,
                'content_utf8_sha256':hashlib.sha256(m['content'].encode()).hexdigest()})
    assert not pending
    return pairs

def sdk_kwargs(body):
    assert set(body)=={'messages','model','max_tokens','parallel_tool_calls','temperature','tools','chat_template_kwargs'}
    kwargs=deepcopy(body)
    kwargs['extra_body']={'chat_template_kwargs':kwargs.pop('chat_template_kwargs')}
    return kwargs

def make_client(config, *, transport=None):
    # Source is explicitly unauthenticated local vLLM. Never read credentials.
    assert config['provider']=='vllm' and config['authentication_mode']=='none'
    kwargs={'api_key':'EMPTY','base_url':config['base_url'],'timeout':config['timeout'],'max_retries':0}
    if transport is not None:kwargs['http_client']=httpx.Client(transport=transport)
    return OpenAI(**kwargs)

def offline_wire(body,config):
    captured=[]
    def capture(request):
        captured.append(request.read())
        return httpx.Response(200,json={'id':'offline-serialization-only','object':'chat.completion','created':0,
            'model':body['model'],'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'offline fixture'}}]})
    with make_client(config,transport=httpx.MockTransport(capture)) as client:
        client.chat.completions.create(**sdk_kwargs(body))
    assert len(captured)==1 and json.loads(captured[0])==body
    return captured[0]

class OneShotFrozenTransport:
    """Reject drift or a second send before the existing accounting transport."""
    def __init__(self,inner,body,endpoint,shared,expected_wire_sha):
        self.inner,self.body,self.endpoint,self.shared,self.expected_wire_sha=inner,body,endpoint,shared,expected_wire_sha
    def handle_request(self,request):
        wire=request.read()
        if request.method!='POST' or str(request.url)!=self.endpoint or json.loads(wire)!=self.body:
            raise BudgetStop('frozen_request_mismatch')
        if hashlib.sha256(wire).hexdigest()!=self.expected_wire_sha:
            raise BudgetStop('sdk_wire_mismatch')
        if self.shared['attempted']>=1:raise BudgetStop('frozen_single_http_limit')
        self.shared['attempted']+=1
        return self.inner.handle_request(request)
    def close(self):return self.inner.close()

def guard_client(client,runtime,body,endpoint,wire_sha):
    instrument_http_client(client._client,runtime)
    shared={'attempted':0};wrapped={}
    def wrap(transport):
        if transport is None:return None
        if id(transport) not in wrapped:wrapped[id(transport)]=OneShotFrozenTransport(transport,body,endpoint,shared,wire_sha)
        return wrapped[id(transport)]
    client._client._transport=wrap(client._client._transport)
    client._client._mounts={p:wrap(t) for p,t in client._client._mounts.items()}
    return shared

def valid_arguments(function,args):
    if not isinstance(args,dict):return False,'arguments not object'
    schema=function['parameters'];properties=schema.get('properties',{})
    if set(args)-set(properties):return False,'unknown parameter'
    if not set(schema.get('required',[]))<=set(args):return False,'missing required parameter'
    for key,value in args.items():
        kind=properties[key].get('type')
        valid={'integer':type(value) is int,'number':type(value) in (int,float) and math.isfinite(value),
            'string':isinstance(value,str),'boolean':type(value) is bool}.get(kind,False)
        if not valid:return False,f'{key}: invalid {kind}'
    return True,None

def classify(response,body):
    """Predeclared local-action classification; never execute returned calls."""
    if not isinstance(response,dict) or response.get('model')!=body['model']:
        return {'label':'request_error','reason':'response model missing/mismatched','calls':[]}
    choices=response.get('choices')
    if not isinstance(choices,list) or len(choices)!=1:return {'label':'request_error','reason':'not exactly one readable choice','calls':[]}
    choice=choices[0];msg=choice.get('message')
    if not isinstance(msg,dict) or choice.get('finish_reason') in {'length','content_filter'}:
        return {'label':'request_error','reason':'missing message or truncated/filtered response','calls':[]}
    calls=msg.get('tool_calls') or [];content=msg.get('content') or '';checks=[]
    if not isinstance(calls,list) or not isinstance(content,str):return {'label':'request_error','reason':'invalid response protocol','calls':[]}
    schemas={x['function']['name']:x['function'] for x in body['tools']}
    advanced=False;bad=False
    allowed_after_wait={'SensorApp__read_soil_sensors','SensorApp__read_soil_sensor','FarmWorldApp__get_ridge_range_state','FarmWorldApp__get_ridge_state','FarmWorldApp__get_inventory','SystemApp__get_current_time'}
    for call in calls:
        f=call.get('function',{}) if isinstance(call,dict) else {};name=f.get('name');args=None;valid=False;reason=None;progress=False
        try:
            args=json.loads(f['arguments'])
            if call.get('type')!='function' or name not in schemas:reason='unknown tool/type'
            else:valid,reason=valid_arguments(schemas[name],args)
        except (KeyError,ValueError,TypeError):reason='unparseable function arguments'
        if valid and name=='SystemApp__advance_time':
            seconds=sum(args.get(k,0)*n for k,n in [('seconds',1),('minutes',60),('hours',3600),('days',86400)])
            valid=seconds>0;progress=valid;reason='positive simulation clock advance; no action executed' if valid else 'nonpositive duration rejected by real tool contract'
        elif valid and name=='SystemApp__wait_for_notification':
            valid=args.get('timeout',0)>=0
            progress=valid and args.get('timeout',0)>0
            reason='positive bounded wait: Engine advances to earlier event/timeout, may return before ETA; not equivalent to a guaranteed full response wait' if progress else 'zero/negative timeout does not establish forward wait'
        elif valid:
            reason='supporting read after a proposed wait, not an executed observation' if advanced and name in allowed_after_wait else 'does not advance the outstanding wait; repeated irrigation/unrelated operations are not progress'
            if not (advanced and name in allowed_after_wait):bad=True
        if not valid:bad=True
        if progress:advanced=True
        checks.append({'name':name,'arguments':args,'schema_and_runtime_argument_valid':valid,'progress_intent':progress,'reason':reason})
    if calls:
        label='progress_tool_call' if advanced and not bad else 'other_or_invalid_tool_call'
        reason='Classification by actual tool call list; text is ancillary. Multiple calls are only proposed serial actions, not executed.'
    elif not content.strip():label,reason='request_error','empty/unreadable response'
    else:
        import re
        future=bool(re.search(r'下一步|后续|还需|尚未|待.*(?:复查|确认)|(?:等待|复查).*(?:后|再)|\bnext\b|\bwill\b|\bneed to\b',content,re.I))
        complete=bool(re.search(r'任务.*(?:完成|结束)|已(?:经)?(?:完成.*)?(?:复查|验证|确认.*效果)|全部.*完成|\b(?:task complete|verified|all done)\b',content,re.I))
        label='text_only_future_plan' if future else 'text_only_claimed_complete' if complete else 'request_error'
        reason='Future/unexecuted action text' if future else 'Claims completion/verification without post-wait evidence' if complete else 'Text does not unambiguously fit preregistered terminal categories; no forced model-choice inference'
    return {'label':label,'reason':reason,'calls':checks,'content':content,'finish_reason':choice.get('finish_reason'),
        'has_tool_calls':bool(calls),'tools_executed':0,'task_success':None}

def prepare(out,instructions):
    out.mkdir(parents=True,exist_ok=False)
    manifest=read(SOURCE/'manifest.json');events=[json.loads(x) for x in (SOURCE/'trace.jsonl').read_text().splitlines()]
    assert manifest['code_commit']==SOURCE_COMMIT
    assert events[48]['event_id']==48 and events[48]['event']=='http_attempt_start'
    assert events[49]['event_id']==49 and events[49]['event']=='http_attempt_end'
    response=json.loads(events[49]['raw_response_body']);assert response==events[49]['parsed_response']
    assert not response['choices'][0]['message'].get('tool_calls') and response['choices'][0]['finish_reason']=='stop'
    a,b=variants(events[48]['request_body']);assert len(a['tools'])==58 and len(a['messages'])==9
    assert a.get('tool_choice')!='required' and a['model']==manifest['config']['llm']['model']
    assert a['temperature']==0.1 and a['max_tokens']==2048 and a['parallel_tool_calls'] is True and a['chat_template_kwargs']=={'enable_thinking':False}
    prior=read(SOURCE.parents[1]/'analysis/llm_native_model_calls.json')
    assert next(x for x in prior['http_attempts'] if x['request']['event_id']==48)['request']['request_body']==a
    assert importlib.metadata.version('openai')==manifest['openai_sdk_version']
    protected={str(p):sha(p) for p in (ROOT/'runs').rglob('*') if p.is_file() and not p.is_relative_to(out) and p.name!='live_budget_20261018.json'}
    protected.update({str(p):sha(p) for p in (ROOT/'fairy').rglob('*') if p.is_file() and p.suffix in {'.py','.md','.json','.xlsx'}})
    for p in [ROOT/'uv.lock',ROOT/'pyproject.toml',ROOT.parent/'workspace-paths.json']:protected[str(p)]=sha(p)
    paths=['fairy/agents/agent/agent.py','fairy/agents/agent/base_agent.py','fairy/controllers/engine.py','fairy/apps/system.py','fairy/time_manager.py','fairy/experiments/native.py']
    code={}
    (out/'source_code').mkdir()
    for name in paths:
        value=subprocess.check_output(['git','show',SOURCE_COMMIT+':'+name],cwd=ROOT)
        p=out/'source_code'/name.replace('/','__');p.write_bytes(value)
        code[name]={'execution_commit':SOURCE_COMMIT,'snapshot':str(p),'sha256':sha(p),'current_source_equal':(ROOT/name).read_bytes()==value}
    ledger_path=resolve_saved_path(manifest['config']['global_live_ledger']);assert ledger_path.is_file()
    ledger=read(ledger_path);assert ledger['ledger_id']==LEDGER_ID
    write(out/'budget_before.json',{'path':str(ledger_path),'sha256':sha(ledger_path),'ledger':ledger,'read_at':now()})
    identity={'prepared_at':now(),'source_run':str(SOURCE),'source_execution_commit':SOURCE_COMMIT,'diagnostic_base_commit':git('rev-parse','HEAD'),
        'worktree':str(ROOT),'git_status':git('status','--short'),'worktrees':git('worktree','list','--porcelain'),
        'frozen_tag':git('rev-parse','refs/tags/experiment/hn58-tool-description-v2-20261019'),
        'source_files':{n:{'path':str(SOURCE/n),'sha256':sha(SOURCE/n)} for n in ('trace.jsonl','manifest.json','evaluation.json','workflow.json','tool_catalog.json')},
        'events':{str(i):{'jsonl_line':i+1,'event':events[i]} for i in (43,48,49,50,52,55)},'source_code':code,
        'native_system_prompt_utf8_sha256':hashlib.sha256(a['messages'][0]['content'].encode()).hexdigest(),
        'manifest_prompt_hash':manifest['prompt_hash'],'manifest_prompt_hash_scope':'Hybrid prompt file; actual native system text hash recorded separately',
        'catalog_hash':manifest['catalog_hash'],'endpoint':events[48]['endpoint'],'llm_config':manifest['config']['llm'],
        'openai_sdk_version':manifest['openai_sdk_version'],'source_response_model':response['model'],'protected_files':protected,
        'historical_response_not_a_new_sample':True,'model_http_attempts':0,'environment_initializations':0}
    write(out/'source_identity.json',identity)
    write(out/'historical_E49.json',{'source_trace_sha256':sha(SOURCE/'trace.jsonl'),'event_id':49,'jsonl_line':50,'response':response,'new_sample':False})
    for group,body in [('A',a),('B',b)]:
        write(out/f'{group}_request.json',body)
        wire=offline_wire(body,identity['llm_config']);(out/f'{group}_sdk_wire.json').write_bytes(wire)
    write(out/'request_diff.json',{'passed':True,'changed_path':'messages[0].content','inserted_text':RULE+'\n','insertion':'Immediately before </agent_instructions>',
        'all_other_json_values_equal':True,'A_equals_E48':True,'historical_message_utf8_bytes_unchanged':True,
        'tool_order_and_all_schemas_unchanged':True,'tool_count':58,'history_pairs':validate_history(a),
        'source_prompt_already_requires_continuation':all(s in a['messages'][0]['content'] for s in ('Continue until the task is complete or impossible with available tools.','Only message the user when the full task is done or cannot be completed.')),
        'hashes':{g:{'file_sha256':sha(out/f'{g}_request.json'),'semantic_sha256':digest(body),'sdk_wire_sha256':sha(out/f'{g}_sdk_wire.json')} for g,body in [('A',a),('B',b)]},
        'offline_serialization':'Pinned OpenAI SDK with MockTransport only; no network, auth header saved, environment or Agent', 'model_http_attempts':0})
    (out/'instructions.md').write_bytes(Path(instructions).read_bytes())
    print(json.dumps({'T0':'PASS','T1':'PASS','out':str(out),'ledger_episodes':len(ledger['episodes']),'ledger_http':ledger['http_attempts']}))

def register(out):
    identity=read(out/'source_identity.json');before=read(out/'budget_before.json');assert not git('status','--porcelain')
    assert not (out/'preregistration.json').exists()
    current=read(before['path']);assert current==before['ledger']
    assert not any(x['status'] in {'running','reserved'} for x in current['episodes'])
    available=min(4,current['max_episodes']-len(current['episodes']),current['max_http_attempts']-current['http_attempts'])
    sequence=list('ABBA')[:available]
    target={'llm':identity['llm_config']}
    gate=service_blockers_from_smoke({'runs':[{'architecture':'llm_native','path':str(SOURCE)}]},target_configs={'llm_native':target})
    assert not gate['by_architecture']['llm_native']
    route={}
    with make_client(identity['llm_config']) as client:
        t=client._client._transport_for_url(httpx.URL(identity['endpoint']));pool=getattr(t,'_pool',None);proxy=getattr(pool,'_proxy_url',None)
        route={'transport':type(t).__name__,'pool':type(pool).__name__,'proxy':{'host':proxy.host.decode() if isinstance(proxy.host,bytes) else proxy.host,'port':proxy.port} if proxy else None}
    pre={'registered_at':now(),'diagnostic_id':out.name,'diagnostic_commit':git('rev-parse','HEAD'),'diagnostic_script_sha256':sha(__file__),
        'source_execution_commit':SOURCE_COMMIT,'source_identity_sha256':sha(out/'source_identity.json'),
        'conditions':{'A':'Exact saved E48 body','B':'A plus only the specified generic continuation rule within agent_instructions'},
        'sequence':sequence,'max_http_attempts':len(sequence),'sdk_retries':0,'max_http_per_sample':1,'http_timeout_seconds':identity['llm_config']['timeout'],
        'ledger_path':before['path'],'ledger_id':LEDGER_ID,'budget_before':current,'accounting':'Each frozen request reserves one existing LiveLedger episode slot and one HTTP via EpisodeRuntime/ObservedTransport. No Agent episode or environment runs.',
        'samples':[{'index':i,'condition':g,'request_id':f'{out.name}_{i}_{g}','slot':f'{out.name}_{i}_{g}','request_sha256':sha(out/f'{g}_request.json'),'wire_sha256':sha(out/f'{g}_sdk_wire.json')} for i,g in enumerate(sequence,1)],
        'access_review':gate,'route':route,'route_scope':'SDK selected transport; no connectivity probe or proxy changes',
        'request_diff_sha256':sha(out/'request_diff.json'),'classifications':list(LABELS),
        'classification_policy':{'version':'frozen_local_progress_v1','code_sha256':sha(__file__),
            'positive_advance_time':'progress when valid; shorter than ETA is only partial local progress, not completed waiting',
            'notification_wait':'positive timeout advances to earlier scheduled event or timeout under source Engine; may finish before irrigation ETA. Zero timeout is not progress.',
            'multiple_calls':'invalid/unrelated/repeated irrigation overrides progress; positive waiting plus subsequent relevant reads is local proposed progress only',
            'text_only':'future/unexecuted plans vs unsupported task completion; unreadable/unclassifiable output is request_error with evidence, not a forced normal model choice'},
        'stop_rules':['Stop batch on HTTP/network/SDK/protocol/model-identity error, serialization mismatch, budget error or missing source evidence. Never retry/replace.','Continue ABBA after normal text/tool outcomes without changing inputs.','A response containing tools is recorded and validated only; never executed.'],
        'sampling_seed':None,'temperature':0.1,'remote_determinism':'not guaranteed','cost':None,
        'status':'READY' if available>=2 else 'BLOCKED_ACCOUNTING_INTERFACE','tool_executions_allowed':0,'environment_initializations_allowed':0}
    write(out/'preregistration.json',pre)
    print(json.dumps({'T2':pre['status'],'sequence':sequence,'accounting_episodes':len(sequence),'route':route}))

def sample(out,index):
    pre=read(out/'preregistration.json');identity=read(out/'source_identity.json')
    assert pre['status']=='READY' and git('rev-parse','HEAD')==pre['diagnostic_commit'] and not git('status','--porcelain')
    assert sha(__file__)==pre['diagnostic_script_sha256'] and sha(out/'source_identity.json')==pre['source_identity_sha256']
    assert not (out/'batch_stopped.json').exists()
    spec=pre['samples'][index-1];assert spec['index']==index
    previous=[read(out/'samples'/f'{i:02d}'/'result.json') for i in range(1,index)]
    assert all(x['classification']['label']!='request_error' for x in previous)
    folder=out/'samples'/f'{index:02d}';folder.mkdir(parents=True,exist_ok=False)
    body=read(out/f"{spec['condition']}_request.json");assert sha(out/f"{spec['condition']}_request.json")==spec['request_sha256']
    for v in identity['source_files'].values():assert sha(v['path'])==v['sha256']
    assert Path(pre['ledger_path']).is_file()
    current=read(pre['ledger_path']);original=pre['budget_before']
    assert current['ledger_id']==LEDGER_ID and current['episodes'][:len(original['episodes'])]==original['episodes']
    assert len(current['episodes'])==len(original['episodes'])+index-1 and current['http_attempts']==original['http_attempts']+index-1
    assert not any(x['status'] in {'running','reserved'} for x in current['episodes'])
    ledger=LiveLedger(pre['ledger_path'],max_episodes=12,max_http_attempts=1440)
    write(folder/'budget_before.json',current)
    ledger.reserve_episode(spec['request_id'],'llm_native','frozen_request_diagnostic',spec['slot'])
    started=time.monotonic();error=None;classification=None
    with TraceWriter(folder/'trace.jsonl',spec['request_id']) as trace:
        # Retain the standard runtime caps; the outer guard imposes the stricter
        # one-send cap without interpreting a completed final HTTP as overflow.
        runtime=EpisodeRuntime(trace,{'wall_timeout_seconds':600},ledger=ledger)
        with make_client(identity['llm_config']) as client:
            guard_client(client,runtime,body,identity['endpoint'],spec['wire_sha256'])
            try:
                with runtime.deadline_guard(),runtime.logical('llm_native_decision'):
                    parsed=client.chat.completions.create(**sdk_kwargs(body))
                    trace.emit('sdk_response',parsed_response=parsed.model_dump(mode='json'))
            except (Exception,BudgetStop) as exc:
                error={'type':type(exc).__name__,'reason':str(exc)}
                trace.emit('request_error',**error)
        ends=[e for e in trace.events if e['event']=='http_attempt_end']
        end=ends[-1] if ends else {}
        response=end.get('parsed_response')
        if error or end.get('http_status')!=200:
            classification={'label':'request_error','reason':error or f"HTTP {end.get('http_status')}",'calls':[]}
        else:classification=classify(response,body)
        if end.get('raw_response_body') is not None:(folder/'raw_response.txt').write_text(end['raw_response_body'])
        write(folder/'response.json',response)
        usage=end.get('usage') or {}
        result={'index':index,'condition':spec['condition'],'request_id':spec['request_id'],'slot':spec['slot'],'completed_at':now(),
            'request_file':str(out/f"{spec['condition']}_request.json"),'request_sha256':spec['request_sha256'],
            'wire_sha256':spec['wire_sha256'],'response_file':str(folder/'response.json'),'response_sha256':sha(folder/'response.json'),
            'raw_response_file':str(folder/'raw_response.txt') if (folder/'raw_response.txt').exists() else None,
            'http_status':end.get('http_status'),'response_model':end.get('response_model'),'classification':classification,
            'http_attempts':runtime.counts['http_attempts'],'elapsed_seconds':time.monotonic()-started,'http_latency_seconds':end.get('wall_elapsed_seconds'),
            'usage':end.get('usage'),'input_tokens':usage.get('prompt_tokens'),'output_tokens':usage.get('completion_tokens'),
            'cached_tokens':(usage.get('prompt_tokens_details') or {}).get('cached_tokens'),'sampling_seed':response.get('seed') if isinstance(response,dict) else None,
            'system_fingerprint':response.get('system_fingerprint') if isinstance(response,dict) else None,
            'cost':None,'sdk_error':error,'task_success':None,'environment_initializations':0,'tool_executions':0}
    ledger.finish(spec['request_id'],'integration_error' if classification['label']=='request_error' else 'completed')
    write(folder/'budget_after.json',ledger.snapshot());write(folder/'result.json',result)
    result['trace_sha256']=sha(folder/'trace.jsonl');write(folder/'result.json',result)
    with (out/'samples.jsonl').open('a') as stream:stream.write(json.dumps(result,ensure_ascii=False)+'\n')
    if classification['label']=='request_error':write(out/'batch_stopped.json',{'index':index,'reason':classification['reason'],'no_replacement_samples':True})
    print(json.dumps({k:result[k] for k in ('index','condition','http_status','response_model','classification','http_attempts','elapsed_seconds')},ensure_ascii=False))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',choices=['prepare','register','sample']);parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--instructions',type=Path);parser.add_argument('--index',type=int)
    args=parser.parse_args();out=args.out.resolve()
    if args.stage=='prepare':prepare(out,args.instructions)
    elif args.stage=='register':register(out)
    else:sample(out,args.index)

if __name__=='__main__':main()
