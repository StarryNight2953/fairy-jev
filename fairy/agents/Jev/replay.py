"""Replay native actions with FAIRY's recorded-time/pre-execution-error contract."""
from __future__ import annotations

import json
from pathlib import Path

from fairy.scenarios.workflow import Workflow, WorkflowStep
from scripts.rebatch_fos_from_traces import (
    ReplayDivergenceError,
    _align_replay_time,
    _tool_error_happened_before_execution,
    _workflow_step_times,
)

from .environment import create_fixture, snapshot_environment
from .evaluation import evaluate_task
from .tools import ToolCatalog, ToolExecutor
from .trace import TraceWriter, digest, write_json


def _action_effects(snapshot, prefix_length):
    return [{"action_type": a["action_type"], "actor_app": a["actor_app"],
             "ridge_ids": a["ridge_ids"], "parameters": a["parameters"]}
            for a in snapshot["actions"][prefix_length:]]


def replay_run(workflow_path, config, output_dir):
    """No model calls. Resolve fixture from sibling manifest; reject conflicts."""
    source_path, output = Path(workflow_path).resolve(), Path(output_dir)
    manifest_path = source_path.parent / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"Replay requires source manifest: {manifest_path}")
    source_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_config = dict(source_manifest.get("config") or {})
    for key in ("scenario_id", "seed", "scenario_kwargs", "init_kwargs", "clock_source"):
        if key not in source_config and key in source_manifest:
            source_config[key] = source_manifest[key]
    for key in ("scenario_id", "seed", "scenario_kwargs", "init_kwargs", "clock_source", "candidate_catalog"):
        if key in config and key in source_config and config[key] != source_config[key]:
            raise ValueError(f"Replay configuration conflict for {key}; source manifest is authoritative")
    source_config["effective_start_time"] = source_manifest["effective_start_time"]
    scenario, engine, metadata = create_fixture(source_config)
    if metadata["checkpoint_hash"] != source_manifest.get("checkpoint_hash"):
        raise ValueError("Replay checkpoint_hash conflicts with source manifest")
    catalog = ToolCatalog(scenario.apps)
    if source_manifest.get("catalog_hash") and source_manifest["catalog_hash"] != catalog.hash:
        raise ValueError("Replay tool catalog hash conflicts with source manifest")
    if source_manifest.get("parameter_catalog_hash") and source_manifest["parameter_catalog_hash"] != digest(source_config.get("candidate_catalog", {})):
        raise ValueError("Replay parameter catalog hash conflicts with source manifest")
    before = snapshot_environment(scenario, engine)
    source_workflow = Workflow.load_workflow(str(source_path))
    workflow, executed_workflow = Workflow(), Workflow()
    output.mkdir(parents=True, exist_ok=True)
    trace = TraceWriter(output / "trace.jsonl", output.name)
    executor = ToolExecutor(catalog, engine.time_manager, executed_workflow, trace)
    info = {"source_workflow": str(source_path), "source_manifest": str(manifest_path),
            "n_executed": 0, "n_restored_error_events": 0, "exact_timing_steps": 0,
            "legacy_timing_steps": 0, "divergences": [], "model_request_count": 0}
    previous_post_time = metadata["effective_start_time"]
    try:
        trace.emit("replay_start", source_workflow=str(source_path), source_manifest=str(manifest_path), metadata=metadata)
        for name, source_step in source_workflow.dag.items():
            step = source_step.to_dict()
            if not step.get("tool_name"):
                workflow.add_node(WorkflowStep(**step))
                continue
            tool_name = step["tool_name"]
            recorded_error = step.get("op_type") == "TOOL_ERROR"
            method = catalog.tools_map.get(tool_name)
            failed_before_execution = recorded_error and _tool_error_happened_before_execution(step)
            if method is None and not failed_before_execution:
                raise ReplayDivergenceError(f"{name}: missing source tool {tool_name}")
            pre_time, post_time, exact = _workflow_step_times(step, method, previous_post_time)
            if post_time < pre_time:
                raise ReplayDivergenceError(f"{name}: time_after precedes time_before")
            info["exact_timing_steps" if exact else "legacy_timing_steps"] += 1
            _align_replay_time(scenario, pre_time)
            if failed_before_execution:
                if not str(step.get("content") or "").strip():
                    raise ReplayDivergenceError(f"{name}: pre-execution error has no recorded error")
                info["n_restored_error_events"] += 1
                trace.emit("replay_error_restored", tool_call_id=name, tool_name=tool_name,
                           arguments=step["tool_args"], error_stage=step["error_stage"],
                           result=step["content"], executed=False, sim_time_before=pre_time, sim_time_after=post_time)
            else:
                row = executor.execute(tool_name, step.get("tool_args") or {}, tool_call_id=name)
                info["n_executed"] += int(row["executed"])
                if (row["status"] == "error") != recorded_error:
                    info["divergences"].append({"step": name, "kind": "execution_status", "source_error": recorded_error,
                                                "actual_error": row["error"], "actual_result": row["result"]})
                if row["arguments"] != step.get("tool_args"):
                    info["divergences"].append({"step": name, "kind": "arguments_changed", "source": step.get("tool_args"), "actual": row["arguments"]})
                # Sensor noise/time stamps differ. Compare meaningful action results.
                source_result = step.get("content")
                if isinstance(source_result, dict) and isinstance(row["result"], dict):
                    keys = ("status", "irrigated_ridges", "duration_hours_per_ridge", "field_equivalent_irrigation_mm", "advanced_seconds")
                    for key in keys:
                        if key in source_result and source_result.get(key) != row["result"].get(key):
                            info["divergences"].append({"step": name, "kind": "result_field", "field": key,
                                                       "source": source_result.get(key), "actual": row["result"].get(key)})
            _align_replay_time(scenario, post_time)
            # Preserve exactly what the agent saw; actual replay returns remain in trace.
            workflow.add_node(WorkflowStep(**step))
            previous_post_time = post_time
        result = evaluate_task(scenario, engine, workflow, before,
                               environment_valid=metadata["environment_check"] == "passed", output_dir=output)
        source_eval_path = source_path.parent / "evaluation.json"
        if source_eval_path.is_file():
            source_eval = json.loads(source_eval_path.read_text(encoding="utf-8"))
            source_before = source_eval.get("private_evidence_before")
            source_after = source_eval.get("private_evidence_after")
            if source_before is not None and source_after is not None:
                comparisons = {
                    "new_action_effects": _action_effects(source_after, len(source_before["actions"])) == _action_effects(result["private_evidence_after"], len(before["actions"])),
                    "quota": abs(float(source_after["management_regime"]["irrigation_used_mm_total"]) - float(result["private_evidence_after"]["management_regime"]["irrigation_used_mm_total"])) <= 2e-6,
                    "pending_irrigation": source_after["pending_irrigation_ridges"] == result["private_evidence_after"]["pending_irrigation_ridges"],
                }
                info["effect_comparisons"] = comparisons
                for key, matched in comparisons.items():
                    if not matched:
                        info["divergences"].append({"kind": "effect_mismatch", "field": key})
        info["replay_success"] = not info["divergences"]
        replay_task_success = result["task_success"]
        source_backend = source_manifest.get("backend")
        result.update({"replay": info, "backend": "replay", "live_integration_success": None,
                       "source_backend": source_manifest.get("backend"), "model_request_count": 0,
                       "tool_call_count": executor.call_count, "replay_task_success": replay_task_success,
                       "task_success": None if source_backend in {"scripted", "direct_native_control"} else replay_task_success,
                       "scripted_task_success": replay_task_success if source_backend == "scripted" else None})
        trace.emit("replay_end", **info)
        write_json(output / "evaluation.json", result)
        write_json(output / "manifest.json", {**source_manifest, **metadata, "backend": "replay", "mode": "replay",
                   "source_manifest": str(manifest_path), "source_workflow": str(source_path),
                   "run_id": output.name, "config": source_config, "catalog_hash": catalog.hash})
        write_json(output / "tool_catalog.json", catalog.to_dict())
        support = source_path.parent / "parameter_support_matrix.json"
        write_json(output / "parameter_support_matrix.json", json.loads(support.read_text()) if support.exists() else [])
        workflow.save_workflow(str(output / "workflow.json"))
        executed_workflow.save_workflow(str(output / "actual_replay_workflow.json"))
        return result
    except Exception as exc:
        trace.emit("replay_stop", reason="exception", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        trace.close()
