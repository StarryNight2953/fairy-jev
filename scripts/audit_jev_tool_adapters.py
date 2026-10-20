"""Offline initial-menu/conditional-branch coverage for all formal scenarios.

Run from repository root with PYTHONPATH=.; output must be a new directory.
This is structural coverage, not task success or an exhaustive resource proof.
"""
from pathlib import Path
from collections import Counter
import argparse,json,socket,hashlib

def deny(*a,**kw):raise AssertionError('No network in Jev adapter coverage audit')
socket.socket.connect=deny;socket.socket.connect_ex=deny;socket.create_connection=deny
from fairy.experiments.suite_inventory import discover_scenarios,config_for_scenario
from fairy.agents.Jev.environment import create_fixture,public_task
from fairy.agents.Jev.tools import ToolCatalog,validate_arguments
from fairy.agents.Jev.candidates import CandidateResolver
from fairy.agents.Jev.parameter_contracts import range_contract,range_values,completion_ranges,validate_contract_arguments
from fairy.agents.Jev.trace import digest

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    cfg=json.loads(Path('configs/jev/hn58_jev_tool_adapters_v2.json').read_text());inventory=discover_scenarios();rows=[];tools={};failures=[];levels=Counter()
    for item in inventory:
        levels[item['level']]+=1;config=config_for_scenario(item,cfg);scenario,engine,metadata=create_fixture(config);cat=ToolCatalog(scenario.apps);resolver=CandidateResolver(cat.entries,config);state={'task':public_task(engine),'current_time':engine.time_manager.time(),'observations':[]}
        scenario_rows=[]
        for entry in cat.entries:
            if not entry.get('visible',True):continue
            name=entry['name'];contract=range_contract(entry,config['candidate_catalog'],config['parameter_strategy']);tools[name]={'parameters':entry['parameters'],'contract':contract};samples=[]
            if contract and contract.get('kind')=='single_ridge':
                for ridge in range_values(contract,'ridge_id',{}):
                    bound={'ridge_id':ridge};samples.append((bound,resolver.generate(name,state,selected_arguments=bound,prefix_decision_ids=['audit-prefix'])))
            elif contract:
                start,end=contract['start_parameter'],contract['end_parameter'];lo,hi=contract['ridge_bounds']
                for first in sorted({lo,min(lo+8,hi),min(lo+20,hi),hi}):
                    bound={start:first}
                    if contract['joint_remaining_parameters']:bound[end]=max(range_values(contract,end,bound))
                    samples.append((bound,resolver.generate(name,state,selected_ranges=completion_ranges(contract,bound,['audit-prefix']))))
            else:samples=[({},resolver.generate(name,state))]
            checked=[]
            for bound,g in samples:
                errors=[]
                for option in g.get('candidates',{}).values():
                    try:
                        validate_arguments(entry,option['arguments'])
                        if contract:validate_contract_arguments(contract,entry,option['arguments'])
                    except Exception as exc:errors.append(str(exc))
                sample={'bound':bound,'status':g['status'],'candidate_count':len(g.get('candidates',{})),'candidate_hash':digest(g.get('candidates',{})),'errors':errors}
                checked.append(sample)
                if errors or g['status']!='ready':failures.append({'scenario':item['scenario_id'],'tool':name,**sample,'reason':g.get('reason')})
            scenario_rows.append({'scenario':item['scenario_id'],'tool':name,'samples':checked})
        rows.extend(scenario_rows)
        (args.output/(item['scenario_id']+'.json')).write_text(json.dumps({'metadata':metadata,'catalog_hash':cat.hash,'public_task':state['task'],'rows':scenario_rows},ensure_ascii=False,indent=2))
    summary={'scenarios':len(inventory),'levels':dict(levels),'unique_tools':len(tools),'scenario_tool_contexts':len(rows),'conditional_samples':sum(len(r['samples']) for r in rows),'failures':failures,'model_calls':0,'native_tool_executions':0,'scope':'Initial public tasks; sampled range branches, all single-ridge prefix values; no exhaustive resource or task-completion guarantee','tool_categories':dict(Counter('single_ridge_contract' if x['contract'] and x['contract'].get('kind')=='single_ridge' else 'range_contract' if x['contract'] else 'legacy_parameter_values' if x['parameters'] else 'no_parameters' for x in tools.values()))}
    (args.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2));(args.output/'tools.json').write_text(json.dumps(tools,ensure_ascii=False,indent=2));print(json.dumps(summary,ensure_ascii=False));return bool(failures)
if __name__=='__main__':raise SystemExit(main())
