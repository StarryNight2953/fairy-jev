"""C1/C2 artifact extraction. No network, SDK client, or live ledger writes."""
import ast
from copy import deepcopy
import json
from pathlib import Path
import socket
import subprocess

from fairy.agents.Jev.environment import create_fixture
from fairy.agents.Jev.tools import ToolCatalog
from fairy.agents.Jev.trace import write_json, digest
from fairy.agents.Jev.hybrid.context import PacketContext
from fairy.agents.Jev.trace import TraceWriter
from fairy.experiments.episode import ROOT, IMPLEMENTATIONS, file_hash, run_episode, verify_and_replay
from fairy.experiments.scripted_native import NativeFixtureLLM
from typesafe_sdk import Choice
from typesafe_sdk._core.json import serialize

OUT = Path(__file__).resolve().parent
ARCHS = ('llm_native', 'jev', 'hybrid')
BASE = json.loads((ROOT/'configs/jev/hn58_hybrid_sync_k4_qwen.json').read_text())

def deny_network(*args, **kwargs):
    raise AssertionError('C1/C2 are offline: network connection prohibited')

socket.socket.connect = deny_network
socket.create_connection = deny_network

def effective(arch):
    config = deepcopy(BASE)
    config.update(architecture=arch, implementation_version=IMPLEMENTATIONS[arch])
    return config

def catalog_check():
    out = OUT/'offline_propagation'
    out.mkdir(exist_ok=False)
    scenario, engine, metadata = create_fixture(BASE)
    catalog = ToolCatalog(scenario.apps)
    old_path = ROOT/'runs/agent_comparison/hybrid_relocated_20261018/smoke/20261018T140025_hybrid_live_9fd09d81/tool_catalog.json'
    old = json.loads(old_path.read_text())
    new = catalog.to_dict()
    changes = []
    assert len(old['tools']) == len(new['tools'])
    for before, after in zip(old['tools'], new['tools']):
        assert before['name'] == after['name']
        for key in sorted(set(before)|set(after)):
            if before.get(key) != after.get(key):
                changes.append({'tool':after['name'], 'field':key, 'old':before.get(key), 'new':after.get(key)})
    assert len(changes) == 1 and changes[0]['tool'] == 'FarmWorldApp__get_inventory' and changes[0]['field'] == 'description'
    assert new['visible_count'] == 58 and new['choice_count'] == 59
    assert old['candidate_order'] == new['candidate_order']
    actual = new['tools'][[t['name'] for t in new['tools']].index('FarmWorldApp__get_inventory')]['description']
    assert 'management resource limits and usage (including irrigation quota)' in actual
    source = 'fairy/apps/farm_world/farm_world_app.py'
    before_source = subprocess.check_output(['git','show',f'334136928fdc62260fd49d3cad2b83cbb255880b:{source}'], cwd=ROOT, text=True)
    after_source = (ROOT/source).read_text()
    before_ast, after_ast = ast.parse(before_source), ast.parse(after_source)
    def strip_docstring(tree):
        method = next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='get_inventory')
        assert isinstance(method.body[0],ast.Expr) and isinstance(method.body[0].value,ast.Constant)
        method.body.pop(0)
        return ast.dump(tree,include_attributes=False)
    assert strip_docstring(before_ast) == strip_docstring(after_ast)
    write_json(out/'tool_catalog.json',new)
    write_json(out/'llm_native_function_tools.json',catalog.schemas)
    choice_wire = json.loads(serialize({'state':{},'model':BASE['model'],'questions':{'tool':Choice(instructions='Offline schema inspection only',criteria=catalog.options)}}))
    write_json(out/'jev_choice_serialization.json',choice_wire)
    with TraceWriter(out/'projection_trace.jsonl','offline_catalog_projection') as trace:
        context = PacketContext(trace, BASE)
        update = context.build_update_input(engine._resolve_agent_task(),[],catalog.options,0)
        write_json(out/'hybrid_update_input.json',update)
    summary = {'passed':True,'new_catalog_hash':catalog.hash,'old_catalog_path':str(old_path),'old_catalog_sha256':file_hash(old_path),
        'old_catalog_hash':old['catalog_hash'],'catalog_diff':changes,'candidate_order_unchanged':True,'non_docstring_ast_unchanged':True,
        'metadata':metadata,'native_schema_source':'ToolCatalog.schemas uses same build_toolset/exclusions as native controller; exact native path tested by regression',
        'jev_serialization_scope':'Public Choice + pinned SDK codec; state/instructions placeholders only for schema inspection. Actual unchanged agent instructions serialized from scripted traces in C2.',
        'hybrid_scope':'PacketContext.build_update_input exact projection; LLMUpdater message construction checked by regression with in-memory capture, no HTTP.',
        'real_transmission':'pending C3; blocked architectures must remain not_observed', 'model_http_attempts':0}
    write_json(OUT/'c1_result.json',summary)
    print(json.dumps({'checkpoint':'C1','passed':True,'catalog_hash':catalog.hash,'diff_count':len(changes)}),flush=True)

def scripted_check():
    assert not (OUT/'scripted_index.json').exists()
    rows=[]
    expected=json.loads((OUT/'c1_result.json').read_text())['new_catalog_hash']
    for arch in ARCHS:
        cfg=effective(arch)
        factory=(lambda runtime:NativeFixtureLLM(runtime)) if arch=='llm_native' else None
        path,_=run_episode(cfg,OUT/'scripted',architecture=arch,mode='scripted',stage='tool_description_v2_offline',
            experiment_id='hn58-tool-description-v2-20261019',slot=f'tdv2_offline_{arch}',require_clean=True,offline_native_llm=factory)
        result=verify_and_replay(path,cfg)
        manifest=json.loads((path/'manifest.json').read_text())
        events=[json.loads(x) for x in (path/'trace.jsonl').read_text().splitlines()]
        assert manifest['catalog_hash']==expected and manifest['source_clean']
        assert result['scripted_task_success'] is True and result['model_http_attempts']==0
        assert result['trace_verification']['passed'] and result['replay_verification']['passed']
        assert not any(e['event']=='http_attempt_start' for e in events)
        if arch!='llm_native':
            first=next(e for e in events if e['event']=='decision_start' and e['decision_stage']=='tool')
            wire=json.loads(serialize({'state':first['state'],'model':cfg['model'],'questions':{'tool':Choice(instructions=first['instructions'],criteria=first['criteria'])}}))
            write_json(OUT/'offline_propagation'/f'{arch}_actual_scripted_first_choice.json',{'source_trace':str(path/'trace.jsonl'),'event_id':first['event_id'],'wire_body':wire,'real_http_sent':False})
        rows.append({'architecture':arch,'path':str(path),'catalog_hash':manifest['catalog_hash'],**{k:result[k] for k in ('scripted_task_success','live_integration_success','task_success','model_http_attempts','tool_call_count','trace_verification','replay_verification')}})
        write_json(OUT/'scripted_index.json',{'runs':rows})
        print(json.dumps({'checkpoint':'C2','architecture':arch,'scripted_success':True,'http':0,'path':str(path)}),flush=True)
    write_json(OUT/'c2_scripted_result.json',{'passed':True,'runs':rows,'model_http_attempts':0,'network_guard':'socket connect denied during C1/C2','new_live_episodes':0})

if __name__=='__main__':
    import sys
    {'catalog':catalog_check,'scripted':scripted_check}[sys.argv[1]]()
