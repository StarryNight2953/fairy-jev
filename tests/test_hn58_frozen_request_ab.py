"""Offline frozen-input/accounting checks; no scenarios, agents or real HTTP."""
from copy import deepcopy
import hashlib
import json
import socket

import httpx
from openai import APIStatusError
import pytest

from scripts import hn58_frozen_request_ab as diag


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def deny(*args,**kwargs):raise AssertionError('Offline test network forbidden')
    monkeypatch.setattr(socket.socket,'connect',deny)
    monkeypatch.setattr(socket.socket,'connect_ex',deny)
    monkeypatch.setattr(socket,'create_connection',deny)


@pytest.fixture
def frozen():
    events=[json.loads(x) for x in (diag.SOURCE/'trace.jsonl').read_text().splitlines()]
    assert events[48]['event_id']==48
    return events[48]['request_body']


def config():return diag.read(diag.SOURCE/'manifest.json')['config']['llm']


def response(body,calls=None,text='下一步等待两小时后复查。'):
    return {'id':'offline','object':'chat.completion','created':0,'model':body['model'],
        'choices':[{'index':0,'finish_reason':'tool_calls' if calls else 'stop','message':{'role':'assistant','content':text,'tool_calls':calls or []}}]}


def call(name,args):return {'id':'offline-call','type':'function','function':{'name':name,'arguments':json.dumps(args)}}


def test_A_exact_and_B_only_the_one_system_insertion(frozen):
    original=deepcopy(frozen);a,b=diag.variants(frozen)
    assert a==original==frozen
    restored=deepcopy(b);restored['messages'][0]['content']=restored['messages'][0]['content'].replace(diag.RULE+'\n','')
    assert restored==a
    assert a['messages'][0]['content'].encode()==original['messages'][0]['content'].encode()
    assert b['messages'][1:]==a['messages'][1:]
    assert b['tools']==a['tools'] and len(a['tools'])==58
    assert a.get('tool_choice')!='required' and b.get('tool_choice')!='required'
    assert 'Continue until the task is complete or impossible with available tools.' in a['messages'][0]['content']
    assert b['messages'][0]['content'].count(diag.RULE)==1
    for forbidden in ('HN58','irrigation','quota','advance_time','S5','S6','hour'):
        assert forbidden not in diag.RULE
    assert len(diag.validate_history(a))==5
    # Historical tool content is a Python-style dict string; preserve its bytes.
    assert a['messages'][-1]['content'].startswith("{'status':")
    assert a['messages'][-1]['content'].encode()==b['messages'][-1]['content'].encode()


def test_missing_historical_call_pair_is_rejected(frozen):
    frozen['messages'][-1]['tool_call_id']='missing-id'
    with pytest.raises(AssertionError):diag.validate_history(frozen)


@pytest.mark.parametrize('group',[0,1])
def test_real_sdk_offline_serialization_is_exact(frozen,group):
    body=diag.variants(frozen)[group]
    assert json.loads(diag.offline_wire(body,config()))==body


def test_unknown_request_field_is_not_silently_dropped(frozen):
    frozen['unknown']='preserve-or-stop'
    with pytest.raises(AssertionError):diag.sdk_kwargs(frozen)


@pytest.mark.parametrize('status',[200,503])
def test_existing_ledger_counts_actual_send_and_second_send_is_blocked(tmp_path,frozen,status):
    ledger=diag.LiveLedger(tmp_path/'unit_test_only_ledger.json')
    ledger.reserve_episode('unit','llm_native','unit_test','unit')
    sent=[]
    def handle(request):
        sent.append(request.read())
        return httpx.Response(status,json=response(frozen))
    with diag.TraceWriter(tmp_path/'trace.jsonl','unit') as trace:
        runtime=diag.EpisodeRuntime(trace,ledger=ledger)
        with diag.make_client(config(),transport=httpx.MockTransport(handle)) as client:
            wire=diag.offline_wire(frozen,config())
            diag.guard_client(client,runtime,frozen,config()['base_url']+'/chat/completions',hashlib.sha256(wire).hexdigest())
            assert client.max_retries==0
            with runtime.logical('llm_native_decision'):
                if status==200:client.chat.completions.create(**diag.sdk_kwargs(frozen))
                else:
                    with pytest.raises(APIStatusError):client.chat.completions.create(**diag.sdk_kwargs(frozen))
                with pytest.raises(diag.BudgetStop,match='frozen_single_http_limit'):
                    client.chat.completions.create(**diag.sdk_kwargs(frozen))
    assert len(sent)==1 and ledger.snapshot()['http_attempts']==1


def test_request_drift_rejected_before_transport_or_accounting(tmp_path,frozen):
    sent=[]
    wire=diag.offline_wire(frozen,config())
    inner=httpx.MockTransport(lambda r:sent.append(r))
    guarded=diag.OneShotFrozenTransport(inner,frozen,'http://fixture',{'attempted':0},hashlib.sha256(wire).hexdigest())
    altered=deepcopy(frozen);altered['temperature']=0.2
    with pytest.raises(diag.BudgetStop,match='frozen_request_mismatch'):
        guarded.handle_request(httpx.Request('POST','http://fixture',json=altered))
    assert sent==[]


@pytest.mark.parametrize('name,args,label',[
    ('SystemApp__advance_time',{'hours':2},'progress_tool_call'),
    ('SystemApp__advance_time',{'seconds':0},'other_or_invalid_tool_call'),
    ('SystemApp__advance_time',{'hours':True},'other_or_invalid_tool_call'),
    ('SystemApp__advance_time',{'unknown':2},'other_or_invalid_tool_call'),
    ('SystemApp__wait_for_notification',{'timeout':7200},'progress_tool_call'),
    ('SystemApp__wait_for_notification',{'timeout':0},'other_or_invalid_tool_call'),
    ('FieldOpsApp__irrigate_range',{'start':43,'end':63,'duration_hours':0.8},'other_or_invalid_tool_call'),
    ('SensorApp__read_soil_sensors',{},'other_or_invalid_tool_call'),
    ('nonexistent',{},'other_or_invalid_tool_call'),
])
def test_tool_classifications_do_not_execute_calls(frozen,name,args,label):
    result=diag.classify(response(frozen,[call(name,args)]),frozen)
    assert result['label']==label and result['tools_executed']==0 and result['task_success'] is None


def test_unrelated_call_overrides_positive_advance(frozen):
    calls=[call('SystemApp__advance_time',{'hours':2}),call('FieldOpsApp__irrigate_range',{'start':43,'end':63,'duration_hours':0.8})]
    assert diag.classify(response(frozen,calls),frozen)['label']=='other_or_invalid_tool_call'


@pytest.mark.parametrize('text,label',[
    ('下一步等待约2小时后复查。','text_only_future_plan'),
    ('任务已完成，已复查验证。','text_only_claimed_complete'),
    ('','request_error'),
])
def test_text_categories(frozen,text,label):
    assert diag.classify(response(frozen,text=text),frozen)['label']==label


def test_model_mismatch_is_request_error(frozen):
    r=response(frozen);r['model']='other-model'
    assert diag.classify(r,frozen)['label']=='request_error'
