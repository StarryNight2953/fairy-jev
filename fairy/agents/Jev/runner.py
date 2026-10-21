"""Standalone runner and artifact lifecycle for the independent Jev agent."""
from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
from datetime import datetime, timezone

from fairy.scenarios.workflow import Workflow

from .agent import DEFAULT_BUDGETS, JevAgent
from .candidates import CandidateResolver, extract_task_parameters
from .client import OfficialBackend, ScriptedBackend
from .environment import create_fixture, snapshot_environment
from .evaluation import evaluate_task
from .replay import replay_run
from .scripted import ScriptedPolicy
from .state import validate_state_strategy
from .parameter_contracts import validate_parameter_strategy
from .tools import ToolCatalog
from .trace import TraceWriter, digest, write_json


def _commit():
    return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False).stdout.strip() or None


def _sdk_version():
    try:
        return importlib.metadata.version("typesafe-sdk")
    except importlib.metadata.PackageNotFoundError:
        return None


def _implementation_hashes():
    import hashlib
    files = list(Path(__file__).parent.glob("*.py"))
    files.append(Path(__file__).parents[2] / "controllers" / "engine.py")
    return {str(path.relative_to(Path(__file__).parents[3])): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in files}


def _coverage(events, mode, attempted):
    required = ["real_tool_choice", "real_parameter_choice", "native_tool_execution", "result_in_later_real_request"]
    if mode != "live" or not attempted:
        return {"live_integration_success": None, "live_coverage_status": "not_run", "missing_stages": required}
    ends = {e["decision_id"]: e for e in events if e["event"] == "decision_end" and e.get("status") == "ok"
            and e.get("response", {}).get("backend") == "typesafe_sdk"}
    requests = [e for e in events if e["event"] == "decision_start"]
    tool_choices = [e for e in ends.values() if e["decision_stage"] == "tool" and e["selected_option"] != "FINISH"]
    param_choices = [e for e in ends.values() if e["decision_stage"] == "parameters"]
    native = [e for e in events if e["event"] == "tool_end" and e.get("executed") and all(d in ends for d in e.get("decision_ids", []))]
    correlated = []
    for event in native:
        stages = {ends[d]["decision_stage"] for d in event.get("decision_ids", [])}
        if not {"tool", "parameters"} <= stages:
            continue
        for request in requests:
            if request["event_id"] <= event["event_id"]:
                continue
            observations = request["state"].get("observations", []) + request["state"].get("recent_actions", [])
            if any(o.get("tool_call_id") == event["tool_call_id"] and o.get("result") == event.get("result") for o in observations):
                correlated.append({"tool_call_id": event["tool_call_id"], "decision_ids": event["decision_ids"],
                                   "feedback_decision_id": request["decision_id"]})
                break
    covered = dict(zip(required, [bool(tool_choices), bool(param_choices), bool(native), bool(correlated)]))
    implementation_error = any(e["event"] == "run_error" or (e["event"] == "decision_end" and e.get("status") == "error")
                               or (e["event"] == "tool_end" and e.get("error_stage") == "tool_args_parse") for e in events)
    complete = all(covered.values())
    return {"live_integration_success": False if implementation_error else (True if complete else None),
            "live_coverage_status": "failed" if implementation_error else ("complete" if complete else "incomplete"),
            "missing_stages": [name for name, present in covered.items() if not present], "coverage_chains": correlated}


def _first_issue(events, task, assertions=None):
    """Locate evidenced mistakes without treating a partial action as a failed plan.

    This diagnoses the preserved trace after evaluation. It never changes the
    policy, candidates, actions, or task thresholds.
    """
    target = extract_task_parameters(task)
    region = target["target_range"]
    target_set = set(range(region[0], region[1] + 1)) if region else set()
    requested_hours = target["irrigation_hours"]
    observed_state, observed_soil, doses = set(), set(), {}
    weather, quota = False, False
    for event in events:
        kind = event["event"]
        reason, layer = None, None
        details = {}
        primary_decision = event.get("decision_id")
        if kind == "decision_end" and event.get("status") == "error":
            reason, layer = event.get("error"), "decision_backend"
        elif kind == "controller_feedback":
            reason, layer = event.get("reason"), "parameter_candidates"
        elif kind == "tool_end" and event.get("status") == "error":
            reason, layer = event.get("error"), "execution_adapter" if event.get("error_stage") == "tool_args_parse" else "native_tool"
            details = {"error_stage": event.get("error_stage"), "executed": event.get("executed"),
                       "native_error_form": {"tool_args_parse": "pre_execution_validation", "tool_return": "returned_error", "tool_call": "raised_exception"}.get(event.get("error_stage"), "unknown")}
        elif kind == "tool_end" and isinstance(event.get("result"), dict) and event["result"].get("status") == "irrigation_started":
            actual = set(event["result"].get("irrigated_ridges", []))
            dose = event["result"].get("duration_hours_per_ridge", event["result"].get("duration_hours"))
            for rid in actual:
                if isinstance(dose, (int, float)):
                    doses[rid] = doses.get(rid, 0.0) + float(dose)
            missing_pre = [name for name, present in {
                "current_weather": weather, "soil_moisture": target_set <= observed_soil,
                "target_state": target_set <= observed_state, "remaining_quota": quota}.items() if not present]
            outside = sorted(actual - target_set)
            overdose = sorted(rid for rid, total in doses.items() if requested_hours is not None and total > requested_hours + 1e-9)
            if missing_pre:
                reason, layer = "Irrigation executed before required visible observations were completed", "tool_selection"
                primary_decision = next(iter(event.get("decision_ids", [])), None)
            elif outside or overdose:
                reason, layer = "Executed irrigation extends outside visible target or cumulative dose exceeds the request", "parameter_selection"
                primary_decision = next(iter(event.get("decision_ids", [])[1:]), None)
            details = {"missing_pre_irrigation_observations": missing_pre, "outside_target_ridges": outside,
                       "overdosed_ridges": overdose, "cumulative_hours_by_ridge": dict(doses),
                       "irreversible_for_task_assertions": bool(missing_pre or outside or overdose)}
        elif kind == "model_finish":
            remaining = sorted(rid for rid in target_set if requested_hours is None or doses.get(rid, 0.0) < requested_hours - 1e-9)
            unmet = [name for name, assertion in (assertions or {}).items() if not assertion["passed"]]
            if remaining or unmet:
                reason, layer = "FINISH selected while the visible target or independently checked task requirements remained incomplete", "tool_selection"
                details = {"remaining_target_ridges": remaining, "cumulative_hours_by_ridge": dict(doses),
                           "required_hours_per_ridge": requested_hours, "unmet_task_assertions": unmet,
                           "partial_single_ridge_actions_are_not_individually_classified_as_wrong": True}
        elif kind == "run_end" and event.get("stop_reason") != "model_finish":
            unmet = [name for name, assertion in (assertions or {}).items() if not assertion["passed"]]
            if unmet:
                reason = f"Run stopped with unmet task requirements: {event.get('stop_reason')}"
                layer = "run_budget" if event.get("stop_reason") in {"max_steps", "max_tool_calls", "max_decision_calls", "wall_timeout"} else "controller"
                details = {"unmet_task_assertions": unmet}
        if reason:
            return {"event_id": event["event_id"], "step_id": event.get("step_id"),
                    "decision_id": primary_decision, "decision_ids": event.get("decision_ids"),
                    "tool_call_id": event.get("tool_call_id"), "failure_layer": layer, "reason": reason,
                    "details": details, "evidence": "trace.jsonl",
                    "assertion_evidence": "evaluation.json:assertions" if assertions else None}
        if kind == "tool_end" and event.get("status") == "ok":
            name, args, content = event.get("tool_name"), event.get("arguments", {}), event.get("result")
            weather = weather or name == "WeatherApp__get_current_weather"
            quota = quota or name == "FarmWorldApp__get_inventory"
            if name == "FarmWorldApp__get_ridge_range_state":
                observed_state.update(range(args["start"], args["end"] + 1))
            elif name == "FarmWorldApp__get_ridge_state":
                observed_state.add(args["ridge_id"])
            if name in {"SensorApp__read_soil_sensors", "SensorApp__read_soil_sensor"} and isinstance(content, dict):
                for reading in content.get("soil_sensors", [content]):
                    if "vwc" in reading and "ridge_start" in reading and "ridge_end" in reading:
                        observed_soil.update(range(reading["ridge_start"], reading["ridge_end"] + 1))
    return None


def _summary(output, evaluation):
    keys = ["environment_check", "scripted_loop_check", "live_integration_success", "live_coverage_status",
            "missing_stages", "scripted_task_success", "task_success", "stop_reason", "failure_layer",
            "model_request_count", "decision_call_count", "tool_call_count", "wall_elapsed_seconds"]
    lines = ["# Jev run result", "", f"Run artifacts: `{output.resolve()}`", ""]
    lines += [f"- {key}: `{json.dumps(evaluation.get(key), ensure_ascii=False)}`" for key in keys]
    if evaluation.get("first_issue"):
        lines += ["", "First evidenced issue:", "", json.dumps(evaluation["first_issue"], ensure_ascii=False, indent=2)]
    lines += ["", "Native validation is retained separately; strict task assertions determine task success.",
              "Full model requests/responses and actual tool results: trace.jsonl. Replayable calls: workflow.json."]
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_mode(mode, config, output_dir, *, workflow_path=None):
    state_strategy = validate_state_strategy(config)
    started = time.perf_counter()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + mode + "_" + uuid.uuid4().hex[:8]
    output = Path(output_dir).resolve() / run_id
    output.mkdir(parents=True, exist_ok=False)
    if mode == "replay":
        if workflow_path is None:
            raise ValueError("--workflow is required for replay")
        try:
            evaluation = replay_run(workflow_path, config, output)
        except Exception as exc:
            key = os.environ.get(config.get("api_key_env", "TYPESAFE_API_KEY"))
            trace_path = output / "trace.jsonl"
            trace = TraceWriter(trace_path if not trace_path.exists() else output / "runner_error_trace.jsonl", run_id, secrets=(key,))
            evaluation = {"task_success": None, "live_integration_success": None, "replay_task_success": None,
                          "stop_reason": "replay_error", "failure_layer": "replay", "error": f"{type(exc).__name__}: {exc}"}
            trace.emit("replay_terminal_error", result=evaluation)
            write_json(output / "manifest.json", trace.sanitize({"run_id": run_id, "mode": mode, "backend": "replay", "config": config,
                       "source_workflow": str(workflow_path), "commit": _commit(), "sdk_version": _sdk_version()}))
            for filename, empty in [("tool_catalog.json", {}), ("parameter_support_matrix.json", []), ("workflow.json", {})]:
                if not (output / filename).exists():
                    write_json(output / filename, empty)
            trace.close()
        evaluation.update(stop_reason=evaluation.get("stop_reason", "replay_complete"), wall_elapsed_seconds=time.perf_counter()-started,
                          live_coverage_status="not_run", missing_stages=[], scripted_loop_check=None,
                          decision_call_count=0)
        write_json(output / "evaluation.json", evaluation)
        _summary(output, evaluation)
        return output, evaluation
    key = os.environ.get(config.get("api_key_env", "TYPESAFE_API_KEY"))
    trace = TraceWriter(output / "trace.jsonl", run_id, secrets=(key,))
    backend, agent, scenario, engine, before = None, None, None, None, None
    workflow = Workflow()
    manifest = {"run_id": run_id, "mode": mode, "backend": "scripted" if mode == "scripted" else "typesafe_sdk" if mode in {"live", "sdk-smoke"} else "inventory",
                "config": config, "commit": _commit(), "sdk_version": _sdk_version(), "model": config.get("model"),
                "state_strategy": state_strategy,
                "parameter_strategy": validate_parameter_strategy(config),
                "config_hash": digest(config), "parameter_catalog_hash": digest(config.get("candidate_catalog", {})),
                "budgets": {**DEFAULT_BUDGETS, **config.get("budgets", {})},
                "implementation_hashes": _implementation_hashes(), "history_policy": config.get("history", {}),
                "timeout_semantics": "Run wall budget checked at decision/tool boundaries; in-flight SDK HTTP operations may overrun. request_timeout_seconds is not a hard run deadline.",
                "hash_provenance": {"config_hash": "SHA-256 canonical JSON of complete config", "parameter_catalog_hash": "SHA-256 canonical JSON of candidate_catalog",
                                    "catalog_hash": "SHA-256 canonical JSON of visible native tool entries", "checkpoint_hash": "SHA-256 of unmodified checkpoint file bytes"}}
    evaluation = {"environment_check": None, "scripted_loop_check": None, "live_integration_success": None,
                  "live_coverage_status": "not_run", "missing_stages": [], "scripted_task_success": None,
                  "task_success": None, "stop_reason": None, "failure_layer": None, "native_validation": None,
                  "model_request_count": 0, "decision_call_count": 0, "tool_call_count": 0, "assertions": {}}
    write_json(output / "tool_catalog.json", {})
    write_json(output / "parameter_support_matrix.json", [])
    try:
        scenario, engine, metadata = create_fixture(config)
        before = snapshot_environment(scenario, engine)
        catalog = ToolCatalog(scenario.apps)
        resolver = CandidateResolver(catalog.entries, config)
        manifest.update(**metadata, catalog_hash=catalog.hash)
        evaluation["environment_check"] = metadata["environment_check"]
        write_json(output / "manifest.json", trace.sanitize(manifest))
        write_json(output / "tool_catalog.json", catalog.to_dict())
        write_json(output / "parameter_support_matrix.json", resolver.support_matrix())
        trace.emit("fixture_ready", metadata=metadata, catalog_hash=catalog.hash)
        if mode == "inventory":
            from .inventory import audit_all_scenarios
            audit = audit_all_scenarios(output / "full_audit")
            evaluation.update(stop_reason="inventory_complete", inventory={
                k: audit[k] for k in ("registered_scenarios", "initialized_scenarios", "failed_initializations",
                                     "ordinary_mode_name_count", "agent_visible_name_count", "max_visible_tools_per_initialized_scenario")})
        elif mode in {"live", "sdk-smoke"} and not key:
            evaluation.update(stop_reason="missing_api_key", failure_layer="external_credentials")
            evaluation.update(_coverage(trace.events, "live", False))
            trace.emit("run_skipped", reason="missing_api_key", api_key_env=config.get("api_key_env", "TYPESAFE_API_KEY"))
        elif mode == "sdk-smoke":
            backend = OfficialBackend(config)
            state = {"task": "SDK connectivity check", "ready": True}
            options = {"ready": "The supplied state has ready=true", "blocked": "The supplied state has ready=false"}
            instructions = "Select the option that exactly matches the boolean ready in the state."
            trace.emit("decision_start", decision_id="smoke_1", attempt_id="smoke_1_attempt_1", decision_stage="smoke",
                       state=state, instructions=instructions, criteria=options, candidate_order=list(options), candidate_hash=digest(options),
                       requested_model=config.get("model"), sdk_version=_sdk_version())
            decision_started = time.perf_counter()
            try:
                result = backend.decide(state, "smoke", instructions, options)
                trace.emit("decision_end", decision_id="smoke_1", decision_stage="smoke", attempt_id="smoke_1_attempt_1",
                           status="ok", selected_option=result.selected_option, response=result.record,
                           wall_elapsed_seconds=time.perf_counter()-decision_started)
                evaluation.update(stop_reason="sdk_smoke_complete", sdk_smoke_success=True)
            except Exception as exc:
                trace.emit("decision_end", decision_id="smoke_1", decision_stage="smoke", attempt_id="smoke_1_attempt_1",
                           status="error", error=f"{type(exc).__name__}: {exc}", response=getattr(exc, "record", {}),
                           wall_elapsed_seconds=time.perf_counter()-decision_started)
                evaluation.update(stop_reason="sdk_smoke_error", sdk_smoke_success=False, failure_layer="decision_backend")
            evaluation.update(model_request_count=1, decision_call_count=1)
        elif mode in {"scripted", "live"}:
            if metadata["environment_check"] != "passed":
                evaluation.update(stop_reason="invalid_environment", failure_layer="environment")
            else:
                backend = ScriptedBackend(ScriptedPolicy()) if mode == "scripted" else OfficialBackend(config)
                agent = JevAgent(catalog, backend, resolver, trace, config)
                engine.agent = agent
                agent.set_time_manager(engine.time_manager)
                def poll():
                    event = engine._next_dynamic_event()
                    while event is not None and event.time_start <= engine.time_manager.time():
                        engine._trigger_dynamic_event(event)
                        event = engine._next_dynamic_event()
                agent.poll_environment = poll
                workflow = engine.run_scenario_agent(setup_scenario=False)
                strict = evaluate_task(scenario, engine, workflow, before, environment_valid=True, output_dir=output)
                strict_success = strict.pop("task_success")
                evaluation.update(strict)
                evaluation.update(stop_reason=agent.stop_reason, failure_layer=agent.failure_layer or strict.get("failure_layer"),
                    decision_call_count=agent.decision_count, model_request_count=agent.decision_count if mode == "live" else 0,
                    tool_call_count=agent.tool_call_count, scripted_task_success=strict_success if mode == "scripted" else None,
                    task_success=strict_success if mode == "live" else None,
                    scripted_loop_check=("passed" if strict_success and agent.stop_reason == "model_finish" else "failed") if mode == "scripted" else None,
                    first_issue=_first_issue(trace.events, agent.state.task, strict["assertions"]))
                evaluation.update(_coverage(trace.events, mode, agent.decision_count > 0))
                if evaluation.get("first_issue"):
                    evaluation["failure_layer"] = evaluation["first_issue"]["failure_layer"]
        else:
            raise ValueError(f"Unknown mode: {mode}")
    except Exception as exc:
        trace.emit("runner_error", error=f"{type(exc).__name__}: {exc}")
        evaluation.update(stop_reason="runner_error", failure_layer="runner", error=f"{type(exc).__name__}: {exc}")
        if agent is not None:
            workflow = agent.workflow
            evaluation.update(decision_call_count=agent.decision_count, model_request_count=agent.decision_count if mode == "live" else 0,
                              tool_call_count=agent.tool_call_count)
        if mode == "live" and evaluation["model_request_count"]:
            evaluation.update(live_integration_success=False, live_coverage_status="failed")
    finally:
        if backend is not None:
            try:
                backend.close()
            except Exception as exc:
                trace.emit("backend_close_error", error=f"{type(exc).__name__}: {exc}")
        evaluation["wall_elapsed_seconds"] = time.perf_counter()-started
        manifest["run_counts"] = {name: evaluation.get(name) for name in ("model_request_count", "decision_call_count", "tool_call_count", "wall_elapsed_seconds")}
        manifest["stop_reason"] = evaluation.get("stop_reason")
        trace.emit("evaluation", result=evaluation)
        write_json(output / "manifest.json", trace.sanitize(manifest))
        workflow.save_workflow(str(output / "workflow.json"))
        write_json(output / "evaluation.json", trace.sanitize(evaluation))
        _summary(output, trace.sanitize(evaluation))
        trace.close()
    return output, evaluation
