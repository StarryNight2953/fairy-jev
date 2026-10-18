"""Common identity, environment, evidence and evaluation around three real loops."""
from __future__ import annotations

import hashlib
import inspect
import importlib.metadata
import json
import os
import subprocess
import sys
import time
from collections import Counter
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fairy.agents.Jev.agent import JevAgent
from fairy.agents.Jev.candidates import CandidateResolver
from fairy.agents.Jev.client import OfficialBackend, ScriptedBackend
from fairy.agents.Jev.environment import create_fixture, snapshot_environment
from .evaluation import evaluate_task, apply_semantic_contract, semantic_first_issue
from fairy.agents.Jev.replay import replay_run
from fairy.agents.Jev.scripted import ScriptedPolicy
from fairy.agents.Jev.tools import ToolCatalog
from fairy.agents.Jev.trace import TraceWriter, digest, write_json
from fairy.scenarios.workflow import Workflow

from .budget import BudgetStop, EpisodeRuntime, LiveLedger

ROOT = Path(__file__).resolve().parents[2]
IMPLEMENTATIONS = {"llm_native": "fairy_native_observed_v1", "jev": "jev_mvp_v1_observed", "hybrid": "hybrid_sync_v1"}


def file_hash(path):
    p = Path(path)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def source_identity(trace=None):
    paths = [p for base in (ROOT/"fairy/agents/Jev", ROOT/"fairy/experiments") for p in base.rglob("*") if p.suffix in {".py", ".md"}]
    paths += [ROOT/"fairy/controllers/engine.py", ROOT/"uv.lock"]
    state = git("status", "--porcelain")
    result = {"code_commit": git("rev-parse", "HEAD"), "worktree_status": state,
              "source_clean": not bool(state), "module_path": str(ROOT/"fairy/__init__.py"),
              "python_executable": sys.executable, "worktree": str(ROOT),
              "implementation_hashes": {str(p.relative_to(ROOT)):file_hash(p) for p in sorted(paths)}}
    if state:
        result["debug_diff"] = git("diff", "HEAD", "--", "fairy", "configs", "tests", "pyproject.toml", "uv.lock")
    return trace.sanitize(result) if trace else result


def credential_requirements(config, architecture):
    from .credentials import required_credential_names
    requirements = required_credential_names(config, architecture)
    return [name for name in requirements if not os.environ.get(name)]


def summarize_metrics(events, runtime, *, live):
    starts = [e for e in events if e["event"] == "http_attempt_start"]
    ends = [e for e in events if e["event"] == "http_attempt_end"]
    tools = [e for e in events if e["event"] == "tool_end"]
    roles = {}
    for role in sorted({e["role"] for e in starts} | ({e["role"] for e in events if e["event"]=="model_request_start"} if live else set())):
        rs = [e for e in starts if e["role"] == role]
        es = [e for e in ends if e["role"] == role]
        usable = [e for e in es if isinstance(e.get("usage"), dict) and any(isinstance(e["usage"].get(k),(int,float)) for k in ("input_tokens","prompt_tokens","output_tokens","completion_tokens"))]
        def token(key, alternative):
            vals = [e["usage"].get(key, e["usage"].get(alternative)) for e in usable]
            vals = [v for v in vals if isinstance(v, (int,float))]
            return sum(vals) if vals else None
        roles[role] = {"http_attempts": len(rs), "logical_requests": sum(e["event"]=="model_request_start" and e.get("role")==role for e in events),
            "latency_seconds": sum(e.get("wall_elapsed_seconds",0) for e in es),
            "input_tokens": token("input_tokens","prompt_tokens"), "output_tokens": token("output_tokens","completion_tokens"),
            "usage_status": "reported" if usable and len(usable)==len(rs) and all(isinstance(e["usage"].get("input_tokens",e["usage"].get("prompt_tokens")),(int,float)) and isinstance(e["usage"].get("output_tokens",e["usage"].get("completion_tokens")),(int,float)) for e in usable) else "partially_reported" if usable else "unavailable",
            "response_models": sorted({str(e.get("response_model")) for e in es if e.get("response_model")})}
    def total(key):
        vals = [v[key] for v in roles.values() if v[key] is not None]
        return sum(vals) if vals else None
    usage_status = "reported" if roles and all(r["usage_status"]=="reported" for r in roles.values()) else "partially_reported" if any(r["usage_status"]!="unavailable" for r in roles.values()) else "unavailable"
    return {"model_http_attempts":len(starts), "logical_model_requests":sum(e["event"]=="model_request_start" for e in events) if live else 0,
        "roles":roles,"input_tokens":total("input_tokens"),"output_tokens":total("output_tokens"),"usage_status":usage_status,
        "tool_attempts":sum(bool(e.get("executed")) for e in tools),
        "tool_successes":sum(bool(e.get("executed")) and e.get("status")=="ok" for e in tools),
        "tool_errors":sum(bool(e.get("executed")) and e.get("status")=="error" for e in tools),
        "pre_execution_errors":sum(not e.get("executed") and e.get("status")=="error" and not e.get("interrupted") for e in tools),
        "decision_rounds":sum(e["event"]=="decision_start" and e.get("decision_stage")=="tool" for e in events) if any(e["event"]=="decision_start" for e in events) else len([e for e in events if e["event"]=="native_event" and e.get("native_event",{}).get("event_type")=="llm_call_start"]),
        "llm_updates":sum(e["event"]=="packet_accepted" for e in events),
        "cost":{"amount":None,"currency":None,"status":"unavailable","reason":"No verified price schedule or billed cost configured; no assumed caching discount"},
        "live":live,"budget_consumption":runtime.snapshot(),
        "latency_policy":"Episode monotonic wall time includes all roles, tool execution, state preparation and initialization; role durations are separate, not substituted for total."}


def integration_coverage(events, architecture, live):
    if not live:
        return {"live_integration_success":None,"integration_coverage":{"live":"not_applicable"}}
    http_success = [e for e in events if e["event"]=="http_attempt_end" and e.get("status")=="ok"]
    tools = [e for e in events if e["event"]=="tool_end" and e.get("executed") and e.get("status")=="ok"]
    requests = [e for e in events if e["event"]=="http_attempt_start"]
    real_jev_requests = [e for e in requests if str(e.get("role","")).startswith("jev_")]
    successful_jev_ids = {e.get("logical_request_id") for e in http_success if str(e.get("role","")).startswith("jev_")}
    decision_ends = [e for e in events if e["event"]=="decision_end" and e.get("status")=="ok"]
    def status(ok): return "passed" if ok else "uncovered"
    coverage={"http_response":status(bool(http_success)),"native_execution":status(bool(tools)),
              "jev_tool_choice":"not_applicable","jev_parameter_choice":"not_applicable",
              "llm_material_in_jev":"not_applicable","cross_update_feedback":"not_applicable"}
    if architecture=="llm_native":
        feedback=any(t["event_id"]<r["event_id"] and any(m.get("role")=="tool" and m.get("tool_call_id")==t["tool_call_id"] for m in r.get("request_body",{}).get("messages",[])) for t in tools for r in requests)
    else:
        ds=[{**e,"state":e.get("request_body",{}).get("state",{})} for e in real_jev_requests]
        decision_ends=[e for e in decision_ends if e.get("request_id") in successful_jev_ids]
        coverage["jev_tool_choice"]=status(any(e.get("decision_stage")=="tool" for e in decision_ends))
        coverage["jev_parameter_choice"]=status(any(e.get("decision_stage")=="parameters" for e in decision_ends))
        def visible_ids(request):
            s=request.get("state",{})
            rows=s.get("observations",[])+s.get("recent_actions",[])+s.get("fresh_events",[])
            return {row.get("tool_call_id") or row.get("data",{}).get("tool_call_id") for row in rows}
        feedback=any(t["event_id"]<r["event_id"] and t["tool_call_id"] in visible_ids(r) for t in tools for r in ds)
        if architecture=="hybrid":
            packets=[e for e in events if e["event"]=="packet_accepted"]
            real_updates={e["logical_request_id"] for e in http_success if e.get("role")=="llm_context_update"}
            coverage["llm_material_in_jev"]=status(any(p.get("packet",{}).get("source_request_id") in real_updates and any(r["event_id"]>p["event_id"] and p.get("packet",{}).get("packet_id")==r.get("state",{}).get("decision_material",r.get("state",{}).get("packet",{})).get("packet_id") for r in ds) for p in packets))
            coverage["cross_update_feedback"]=status(any(p.get("packet",{}).get("packet_version",0)>1 and p.get("packet",{}).get("input_event_ids") and p.get("packet",{}).get("source_request_id") in real_updates for p in packets))
    coverage["tool_feedback_in_later_request"]=status(feedback)
    errors=any(e["event"] in {"run_error","runner_error","llm_update_error","hybrid_update_error","state_budget_exceeded","evaluation_error"} or (e["event"]=="decision_end" and e.get("status")=="error") for e in events)
    required=["http_response","native_execution","tool_feedback_in_later_request"]
    if architecture!="llm_native": required.append("jev_tool_choice")
    if architecture=="hybrid":required.append("llm_material_in_jev")
    # Parameter/cross-period branches may naturally remain uncovered. Report
    # them independently rather than forcing irrelevant live actions.
    success=False if errors else True if all(coverage[k]=="passed" for k in required) else None
    return {"live_integration_success":success,"integration_coverage":coverage}


def artifact_hashes(output):
    rows={str(p.relative_to(output)):file_hash(p) for p in sorted(output.rglob("*")) if p.is_file() and p.name!="files.sha256"}
    (output/"files.sha256").write_text("".join(f"{value}  {name}\n" for name,value in rows.items()),encoding="utf-8")
    return rows


def run_episode(config, output_dir, *, architecture=None, mode="live", stage="debug", experiment_id=None,
                slot=None, ledger=None, require_clean=False, offline_native_llm=None, external_blockers=None):
    episode_started=time.monotonic()
    architecture=architecture or config.get("architecture","jev")
    if architecture not in IMPLEMENTATIONS:raise ValueError("unknown architecture")
    run_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")+f"_{architecture}_{mode}_"+uuid4().hex[:8]
    output=Path(output_dir).resolve()/run_id
    output.mkdir(parents=True,exist_ok=False)
    secrets=[v for k,v in os.environ.items() if k.endswith(("API_KEY","API_TOKEN","ACCESS_TOKEN","SECRET")) and v]
    trace=TraceWriter(output/"trace.jsonl",run_id,secrets=secrets)
    if mode=="live" and ledger is None:
        ledger=LiveLedger(ROOT/config.get("global_live_ledger","runs/agent_comparison/live_budget_20261018.json"))
    runtime=EpisodeRuntime(trace,config.get("experiment_limits"),ledger=ledger)
    manifest={"run_id":run_id,"experiment_id":experiment_id,"slot":slot,"stage":stage,"mode":mode,
        "external_blockers":external_blockers or [],
        "architecture":architecture,"implementation_version":config.get("implementation_version",IMPLEMENTATIONS[architecture]),
        "backend":"scripted" if mode=="scripted" else "llm_native" if architecture=="llm_native" else "typesafe_sdk",
        **source_identity(trace),"config":trace.sanitize(config),"config_hash":digest(trace.sanitize(config)),
        "sdk_version":importlib.metadata.version("typesafe-sdk"),"openai_sdk_version":importlib.metadata.version("openai"),
        "lockfile_hash":file_hash(ROOT/"uv.lock"),"prompt_hash":file_hash(ROOT/"fairy/agents/Jev/hybrid/prompt.md"),
        "candidate_strategy_version":config.get("candidate_strategy_version","jev_mvp_v1"),
        "candidate_strategy_hash":file_hash(ROOT/"fairy/agents/Jev/candidates.py"),
        "parameter_catalog_hash":digest(config.get("candidate_catalog",{})),
        "evaluator_version":config.get("evaluator_version","hn58_semantic_v2"),"evaluator_hash":file_hash(ROOT/"fairy/experiments/evaluation.py"),
        "data_assets":{"tangyan5_workbook":file_hash(ROOT/"fairy/scenarios/farm_world_fullseason/tangyan5_table_fitted/data/糖研所5号地数据汇总-1206_副本.xlsx")},
        "randomness":{"scenario_seed":config.get("seed"),"remote_sampling_seed":"not supplied; cannot promise remote determinism","clock":"checkpoint initial time; wall-derived subsecond progression remains"},
        "global_live_ledger":str(ledger.path) if ledger else None,
        "tool_visibility_rule":"FARM_AGENT_HIDDEN_TOOL_NAMES","budgets":config.get("budgets",{}),"experiment_limits":runtime.limits}
    evaluation={"architecture":architecture,"implementation_version":manifest["implementation_version"],"execution_status":"blocked",
        "live_integration_success":None,"task_success":None,"scripted_task_success":None,"native_validation":None,
        "task_assertions":{},"assertions":{},"stop_reason":None,"failure_layer":None}
    scenario=engine=agent=backend=before=None
    workflow=Workflow()
    reserved=False
    deadline_scope=None
    try:
        runtime.check()
        deadline_scope=runtime.deadline_guard()
        deadline_scope.__enter__()
        if require_clean and not manifest["source_clean"]:
            raise ValueError("formal_source_not_clean")
        missing=credential_requirements(config,architecture) if mode=="live" else []
        scenario,engine,metadata=create_fixture(config)
        manifest.update(metadata)
        manifest["scenario_source"]={"path":inspect.getfile(type(scenario)),"sha256":file_hash(inspect.getfile(type(scenario)))}
        manifest["visibility_rule_hash"]=file_hash(ROOT/"fairy/controllers/agent_builder.py")
        before=snapshot_environment(scenario,engine)
        catalog=ToolCatalog(scenario.apps)
        resolver=CandidateResolver(catalog.entries,config)
        manifest["catalog_hash"]=catalog.hash
        write_json(output/"tool_catalog.json",catalog.to_dict())
        write_json(output/"parameter_support_matrix.json",resolver.support_matrix())
        trace.emit("fixture_ready",metadata=metadata,catalog_hash=catalog.hash)
        if external_blockers:
            evaluation.update(stop_reason="preflight_blocked",failure_layer="external_access",blockers=external_blockers)
            trace.emit("run_skipped",reason="preflight_blocked",blockers=external_blockers)
        elif missing:
            evaluation.update(stop_reason="missing_credentials",failure_layer="external_credentials",missing_credentials=missing)
            trace.emit("run_skipped",reason="missing_credentials",missing_credentials=missing)
        elif metadata["environment_check"]!="passed":
            evaluation.update(stop_reason="invalid_environment",failure_layer="environment")
        else:
            if mode=="live" and ledger:
                ledger.reserve_episode(run_id,architecture,stage,slot or run_id)
                reserved=True
            with nullcontext():
                if architecture=="llm_native":
                    from .native import run_native
                    native_llm=offline_native_llm(runtime) if offline_native_llm is not None and not hasattr(offline_native_llm,"chat_completion") else offline_native_llm
                    agent,workflow,reason=run_native(scenario,engine,config,runtime,llm=native_llm)
                else:
                    if mode=="scripted":
                        backend=ScriptedBackend(ScriptedPolicy())
                    else:
                        from .transports import instrument_http_client
                        backend=OfficialBackend(config)
                        instrument_http_client(backend.client._http_client,runtime)
                    cls=JevAgent
                    kwargs={"runtime":runtime}
                    if architecture=="hybrid":
                        from fairy.agents.Jev.hybrid.agent import HybridJevAgent
                        from fairy.agents.Jev.hybrid.updater import LLMUpdater,ScriptedUpdater
                        cls=HybridJevAgent
                        if mode=="scripted":updater=ScriptedUpdater(trace)
                        else:
                            from .transports import build_instrumented_llm
                            updater_llm=build_instrumented_llm(config["llm"],runtime,"llm_context_update")
                            updater=LLMUpdater(updater_llm,config,trace)
                        kwargs["updater"]=updater
                    agent=cls(catalog,backend,resolver,trace,config,**kwargs)
                    engine.agent=agent
                    agent.set_time_manager(engine.time_manager)
                    def poll():
                        event=engine._next_dynamic_event()
                        while event is not None and event.time_start<=engine.time_manager.time():
                            engine._trigger_dynamic_event(event)
                            event=engine._next_dynamic_event()
                    agent.poll_environment=poll
                    workflow=engine.run_scenario_agent(setup_scenario=False)
                    reason=agent.stop_reason
                evaluation.update(stop_reason=reason,failure_layer=getattr(agent,"failure_layer",None))
    except BudgetStop as exc:
        evaluation.update(stop_reason=exc.reason,execution_status="budget_exhausted",failure_layer="budget")
        trace.emit("episode_budget_stop",reason=exc.reason,side_effects=exc.side_effects)
    except Exception as exc:
        evaluation.update(stop_reason="runner_error",execution_status="integration_error",failure_layer="runner",error=f"{type(exc).__name__}: {exc}")
        trace.emit("runner_error",error=f"{type(exc).__name__}: {exc}")
    finally:
        if deadline_scope is not None:
            deadline_scope.__exit__(None,None,None)
        if agent is not None:
            workflow=agent.workflow
        executed=any(e["event"]=="tool_end" and e.get("executed") for e in trace.events)
        attempted=any((e["event"]=="decision_end" and e.get("status")=="ok") or (e["event"]=="native_event" and e.get("native_event",{}).get("event_type")=="llm_call_end" and e.get("native_event",{}).get("status")=="ok") for e in trace.events)
        if before is not None and (executed or attempted):
            try:
                strict=evaluate_task(scenario,engine,workflow,before,events=trace.events,environment_valid=manifest.get("environment_check")=="passed",output_dir=output)
                success=strict.pop("task_success")
                evaluation.update(strict)
                evaluation["task_assertions"]=strict["assertions"]
                evaluation["task_success"]=success if mode=="live" else None
                evaluation["scripted_task_success"]=success if mode=="scripted" else None
                evaluation["first_issue"]=semantic_first_issue(trace.events,engine._resolve_agent_task(),strict["assertions"])
                evaluation["first_violation_event_id"]=evaluation["first_issue"]["event_id"] if evaluation["first_issue"] else None
            except Exception as exc:
                evaluation.update(execution_status="integration_error",failure_layer="evaluation",evaluation_error=f"{type(exc).__name__}: {exc}")
                trace.emit("evaluation_error",error=f"{type(exc).__name__}: {exc}")
        evaluation["first_issue"]=semantic_first_issue(trace.events,engine._resolve_agent_task() if engine else "",evaluation.get("assertions"))
        evaluation["first_violation_event_id"]=evaluation["first_issue"]["event_id"] if evaluation["first_issue"] else None
        if evaluation.get("stop_reason") not in {"missing_credentials","invalid_environment","preflight_blocked"}:
            evaluation.update(integration_coverage(trace.events,architecture,mode=="live"))
            reason=evaluation.get("stop_reason") or "unknown_termination"
            if evaluation["execution_status"]=="integration_error":
                pass
            elif reason.startswith(("max_","total_")) or reason in {"wall_timeout","timeout"}:
                evaluation["execution_status"]="budget_exhausted"
            elif reason in {"model_finish","agent_finished","finish"}:
                evaluation["execution_status"]="completed"
            elif evaluation["execution_status"]!="integration_error":
                evaluation["execution_status"]="integration_error"
            if evaluation.get("first_issue"):
                evaluation["failure_layer"]=evaluation["first_issue"]["failure_layer"]
        metrics=summarize_metrics(trace.events,runtime,live=mode=="live")
        metrics["episode_wall_seconds"]=time.monotonic()-episode_started
        evaluation["decision_call_count"]=sum(e["event"]=="decision_start" for e in trace.events)
        if mode=="scripted":evaluation["scripted_loop_check"]="passed" if evaluation.get("scripted_task_success") else "failed"
        metrics["simulation_time_start"]=manifest.get("effective_start_time")
        metrics["simulation_time_end"]=engine.time_manager.time() if engine else None
        evaluation.update(budget_consumption=runtime.snapshot(),model_request_count=metrics["logical_model_requests"],
                          model_http_attempts=metrics["model_http_attempts"],tool_call_count=metrics["tool_attempts"],
                          wall_elapsed_seconds=metrics["episode_wall_seconds"])
        manifest.update(stop_reason=evaluation["stop_reason"],run_counts=metrics["budget_consumption"]["counts"])
        trace.emit("episode_end",evaluation=evaluation,metrics=metrics)
        if backend is not None:backend.close()
        if agent is not None and architecture=="llm_native" and hasattr(agent.llm,"api_client"):agent.llm.api_client.close()
        if "updater_llm" in locals():updater_llm.api_client.close()
        workflow.save_workflow(str(output/"workflow.json"))
        write_json(output/"manifest.json",trace.sanitize(manifest))
        write_json(output/"evaluation.json",trace.sanitize(evaluation))
        write_json(output/"metrics.json",trace.sanitize(metrics))
        trace.close()
        if reserved:ledger.finish(run_id,evaluation["execution_status"])
    return output,evaluation


def verify_and_replay(output, config):
    output=Path(output)
    from .verification import verify_episode
    verification=verify_episode(output)
    write_json(output/"verification.json",verification)
    evaluation=json.loads((output/"evaluation.json").read_text())
    evaluation["trace_verification"]=verification
    if evaluation.get("tool_call_count",0):
        try:
            replay=replay_run(output/"workflow.json",config,output/"replay")
            source_events=[json.loads(line) for line in (output/"trace.jsonl").read_text().splitlines()]
            semantic=apply_semantic_contract(replay,Workflow.load_workflow(str(output/"workflow.json")),source_events)
            semantic["replay_task_success"]=semantic["task_success"]
            if replay.get("source_backend")=="scripted":semantic["task_success"]=None
            write_json(output/"replay"/"evaluation_semantic_v2.json",semantic)
            evaluation["replay_verification"]={"passed":replay["replay"]["replay_success"],"details":replay["replay"],"task_success":semantic.get("replay_task_success")}
        except Exception as exc:
            evaluation["replay_verification"]={"passed":False,"error":f"{type(exc).__name__}: {exc}"}
    else:evaluation["replay_verification"]={"status":"not_applicable","reason":"no actual native execution"}
    write_json(output/"evaluation.json",evaluation)
    artifact_hashes(output)
    return evaluation
