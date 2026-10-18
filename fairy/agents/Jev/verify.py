"""Offline integrity/provenance audit of saved Jev artifacts; never runs tools or APIs."""
from __future__ import annotations

import argparse
import json
import uuid
from collections import Counter
from pathlib import Path

from .trace import digest


_STATE_KEYS = {"task", "current_time", "observations", "recent_actions", "action_facts",
               "controller_feedback", "system_context", "history_retention", "report_history"}
_PRIVATE_KEYS = {"oracle", "is_oracle", "oracle_solution", "oracle_events", "evaluator",
                 "evaluation", "native_validation", "task_success", "scripted_task_success",
                 "checkpoint", "checkpoint_state", "private_state", "physics_state"}


def _read_json(path):
    def reject(value):
        raise ValueError(f"nonstandard JSON constant {value} in {path}")
    return json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=reject)


def _forbidden_paths(value, path="state"):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            item_path = f"{path}.{key}"
            if key.lower() in _PRIVATE_KEYS or key.startswith("_"):
                found.append(item_path)
            found.extend(_forbidden_paths(item, item_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_forbidden_paths(item, f"{path}[{index}]"))
    return found


def verify_run(run_dir: str | Path) -> dict:
    """Verify artifacts, treating recorded tool/model failures as valid outcomes.

    This audits integrity and visible-state provenance, not task correctness or
    secret values. Task assertions remain the independently saved evaluation.
    """
    root = Path(run_dir).resolve()
    issues, counts = [], Counter()

    def check(condition, code, event=None, detail=None):
        counts[code] += 1
        if not condition:
            issues.append({"check": code, "event_id": event.get("event_id") if event else None, "detail": detail})

    try:
        manifest = _read_json(root / "manifest.json")
        catalog = _read_json(root / "tool_catalog.json")
        evaluation = _read_json(root / "evaluation.json")
        workflow = _read_json(root / "workflow.json")
        events = [json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"nonstandard {value}")))
                  for line in (root / "trace.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError) as exc:
        return {"passed": False, "run_dir": str(root), "issues": [{"check": "artifact_read", "detail": str(exc)}]}
    mode, backend = manifest.get("mode"), manifest.get("backend")
    visible = [entry for entry in catalog.get("tools", []) if entry.get("visible", True)]
    by_name = {entry["name"]: entry for entry in visible}
    menu = list(by_name) + ["FINISH"]
    check(len(by_name) == len(visible), "unique_visible_tool_names")
    check(catalog.get("visible_count") == len(visible), "catalog_count")
    check(catalog.get("candidate_order") == menu, "catalog_order")
    check(catalog.get("catalog_hash") == digest(visible), "catalog_content_hash")
    check(manifest.get("catalog_hash") == catalog.get("catalog_hash"), "manifest_catalog_hash")
    check(len(menu) <= 255, "catalog_choice_limit")
    check(bool(events), "nonempty_trace")
    check([event.get("event_id") for event in events] == list(range(len(events))), "contiguous_event_ids")
    check(all(event.get("run_id") == manifest.get("run_id") for event in events), "event_run_id")

    starts, ends, decisions, generations, tool_starts, tool_ends = {}, {}, {}, {}, {}, {}
    step_tools, tool_menus, parameter_requests = Counter(), [], []
    feedback, contexts, notifications = [], [], []
    task, run_start = None, None
    end_events = []
    for event in events:
        kind = event.get("event")
        if kind == "run_start":
            task, run_start = event.get("task"), event
        if kind in {"decision_start", "tool_start"} and run_start:
            check(event.get("wall_timestamp", 0) - run_start["wall_timestamp"]
                  < run_start.get("budgets", {}).get("wall_timeout_seconds", float("inf")),
                  "no_new_operation_after_wall_budget", event)
        if kind == "system_context": contexts.append(event.get("content"))
        if kind == "environment_notification": notifications.append(event.get("content"))
        if kind == "controller_feedback": feedback.append(event)
        if kind == "run_end": end_events.append(event)
        if kind == "decision_start":
            decision_id, attempt_id = event.get("decision_id"), event.get("attempt_id")
            key = (decision_id, attempt_id)
            check(all(key) and key not in starts, "unique_decision_attempt_start", event)
            starts[key] = event
            options = event.get("criteria", {})
            check(1 <= len(options) <= 255, "decision_candidate_limit", event)
            check(event.get("candidate_order") == list(options), "decision_candidate_order", event)
            check(event.get("candidate_hash") == digest(options), "decision_candidate_hash", event)
            check(bool(event.get("instructions")), "explicit_decision_instructions", event)
            stage, state = event.get("decision_stage"), event.get("state", {})
            check(not _forbidden_paths(state), "no_private_state_fields", event, _forbidden_paths(state))
            if stage in {"tool", "parameters"}:
                check(set(state) <= _STATE_KEYS, "visible_state_shape", event, sorted(set(state)-_STATE_KEYS))
                check(state.get("task") == task, "visible_task_preserved", event)
                check(event.get("catalog_hash") == catalog.get("catalog_hash"), "decision_catalog_hash", event)
                observations = {row.get("observation_id"): row for row in state.get("observations", [])}
                for field in ["observations", "recent_actions", "action_facts"]:
                    for row in state.get(field, []):
                        call_id = row.get("tool_call_id")
                        if call_id:
                            source = tool_ends.get(call_id)
                            check(source is not None, "observation_follows_actual_execution", event, call_id)
                            if source:
                                result = row.get("result")
                                if isinstance(result, dict) and set(result) == {"same_as_observation"}:
                                    referenced = observations.get(result["same_as_observation"], {})
                                    check(referenced.get("tool_call_id") == call_id, "observation_reference_identity", event, call_id)
                                    result = referenced.get("result")
                                check(result == source.get("result") and row.get("arguments") == source.get("arguments")
                                      and row.get("tool_name") == source.get("tool_name") and row.get("status") == source.get("status"),
                                      "observation_matches_visible_tool_result", event, call_id)
                        elif row.get("kind") == "environment_notification":
                            check(row.get("result") in notifications, "notification_has_visible_source", event)
                        else:
                            check(False, "observation_has_visible_source", event, row.get("kind"))
                for item in state.get("controller_feedback", []):
                    check(any(all(source.get(k) == v for k, v in item.items()) for source in feedback), "feedback_has_recorded_source", event)
                for content in state.get("system_context", []):
                    check(content in contexts, "system_context_has_recorded_source", event)
                if "report_history" in state:
                    check(state["report_history"].get("sent_count") == sum(
                        row.get("tool_name") == "AgentUserInterface__send_message_to_user" for row in tool_ends.values()),
                        "report_count_matches_observed_calls", event)
            if stage == "tool":
                check(list(options) == menu, "full_native_tool_menu_plus_finish", event,
                      {"expected": len(menu), "actual": len(options)})
                tool_menus.append(list(options))
                for name, entry in by_name.items():
                    check(options.get(name) == {"purpose": entry["description"], "parameters": entry["schema"],
                                               "signature_parameters": entry["parameters"]}, "native_option_matches_catalog", event, name)
            elif stage == "parameters":
                generated = generations.get(event.get("step_id"))
                check(generated is not None, "parameters_after_generation", event)
                if generated:
                    check(generated.get("generation", {}).get("status") == "ready", "parameters_only_if_ready", event)
                    check(options == generated.get("generation", {}).get("candidates"), "parameters_match_generated_candidates", event)
                    entry = by_name.get(generated.get("tool_name"), {})
                    check(bool(entry.get("parameters")), "no_second_choice_for_noarg_tool", event)
                parameter_requests.append(event)
        elif kind == "decision_end":
            key = (event.get("decision_id"), event.get("attempt_id"))
            source = starts.get(key)
            check(source is not None and key not in ends, "unique_matched_decision_end", event)
            ends[key] = event
            if source:
                check(source.get("decision_stage") == event.get("decision_stage"), "decision_stage_identity", event)
                if event.get("status") == "ok":
                    check(event.get("selected_option") in source.get("criteria", {}), "successful_choice_in_submitted_set", event)
            decisions[event.get("decision_id")] = event
            response = event.get("response", {})
            if backend == "scripted":
                check(response.get("backend") == "scripted" and response.get("http_status") is None, "scripted_not_live", event)
            elif backend == "typesafe_sdk" and event.get("status") == "ok":
                check(response.get("backend") == "typesafe_sdk" and response.get("http_status") == 200
                      and response.get("raw_response_body") is not None and response.get("parsed_response") is not None,
                      "official_response_evidence", event)
                check(response.get("retry_max_retries") == 0, "sdk_automatic_retry_disabled", event)
                parsed = response.get("parsed_response") or {}
                answer = parsed.get("answers", {}).get(event.get("decision_stage"), {})
                check(answer.get("choice") == event.get("selected_option"), "parsed_response_matches_recorded_choice", event)
        elif kind == "parameter_candidates":
            source = decisions.get(event.get("tool_decision_id"))
            check(source is not None and source.get("status") == "ok" and source.get("decision_stage") == "tool"
                  and source.get("selected_option") == event.get("tool_name"), "generation_after_chosen_tool", event)
            check(event.get("step_id") not in generations, "one_generation_per_step", event)
            generations[event.get("step_id")] = event
            candidates = event.get("generation", {}).get("candidates", {})
            check(event.get("candidate_order") == list(candidates) and event.get("candidate_hash") == digest(candidates),
                  "generation_order_and_hash", event)
            for candidate in candidates.values():
                check(all(ref.startswith(("visible_task:", "tool_signature:", "tool_result:", "tool_arguments:",
                                          "declared_catalog:", "public_catalog:", "deterministic_template:"))
                          for ref in candidate.get("source_refs", [])), "candidate_sources_are_public_or_declared", event)
        elif kind == "tool_start":
            call_id, step_id = event.get("tool_call_id"), event.get("step_id")
            check(call_id and call_id not in tool_starts, "unique_tool_start", event)
            tool_starts[call_id] = event
            step_tools[step_id] += 1
            check(step_tools[step_id] == 1, "one_native_execution_per_step", event)
            generated = generations.get(step_id)
            check(generated is not None and generated.get("generation", {}).get("status") == "ready", "execution_after_ready_candidates", event)
            ids = event.get("decision_ids", [])
            if generated:
                selected = decisions.get(generated.get("tool_decision_id"), {})
                check(event.get("tool_name") == selected.get("selected_option"), "native_tool_is_selected_tool", event)
                entry = by_name.get(event.get("tool_name"), {})
                candidates = generated["generation"]["candidates"]
                if entry.get("parameters"):
                    check(len(ids) == 2 and all(i in decisions for i in ids), "two_decisions_for_parameterized_call", event)
                    answer = decisions.get(ids[-1] if ids else None, {})
                    check(answer.get("status") == "ok" and answer.get("decision_stage") == "parameters", "successful_parameter_selection_before_execution", event)
                    chosen = candidates.get(answer.get("selected_option"), {})
                else:
                    check(ids == [generated.get("tool_decision_id")], "one_decision_for_noarg_call", event)
                    chosen = candidates.get("args_000", {})
                check(ids and ids[0] == generated.get("tool_decision_id"), "call_links_tool_decision", event)
                check(event.get("arguments") == chosen.get("arguments"), "executed_args_equal_selected_candidate", event)
                check("source_refs" not in event.get("arguments", {}), "source_refs_not_native_kwargs", event)
        elif kind == "tool_end":
            call_id = event.get("tool_call_id")
            source = tool_starts.get(call_id)
            check(source is not None and call_id not in tool_ends, "unique_matched_tool_end", event)
            if source:
                check(all(event.get(k) == source.get(k) for k in ["tool_name", "arguments", "step_id", "decision_ids"]), "tool_identity_and_args_unchanged", event)
            check(event.get("sim_time_after", 0) >= event.get("sim_time_before", 0), "tool_clock_not_reversed", event)
            tool_ends[call_id] = event
    check(set(starts) == set(ends), "all_decision_attempts_terminated")
    check(set(tool_starts) == set(tool_ends), "all_tool_attempts_terminated")
    executed = sum(bool(event.get("executed")) for event in tool_ends.values())
    if mode in {"scripted", "live"} and starts:
        check(len(end_events) == 1, "one_run_termination")
        if end_events:
            terminal = end_events[-1]
            check(terminal.get("decision_count") == len({k[0] for k in starts}), "run_decision_count")
            check(terminal.get("tool_call_count") == executed, "run_executed_tool_count")
            budgets = manifest.get("budgets", {})
            check(terminal.get("step_count", 0) <= budgets.get("max_steps", float("inf")), "step_budget")
            check(len(starts) <= budgets.get("max_decision_calls", float("inf")), "decision_budget")
            check(executed <= budgets.get("max_tool_calls", float("inf")), "native_execution_budget")
    workflow_tools = {row.get("name"): row for row in workflow.values() if isinstance(row, dict) and row.get("tool_name")}
    check(set(workflow_tools) == set(tool_ends), "workflow_tool_identity_set")
    for call_id, event in tool_ends.items():
        row = workflow_tools.get(call_id, {})
        check(row.get("tool_name") == event.get("tool_name") and row.get("tool_args") == event.get("arguments")
              and row.get("content") == event.get("result"), "workflow_matches_tool_trace", event)
    check(evaluation.get("decision_call_count") == len(starts), "evaluation_decision_count")
    check(evaluation.get("tool_call_count") == executed, "evaluation_native_execution_count")
    check(evaluation.get("model_request_count") == (len(starts) if backend == "typesafe_sdk" else 0), "evaluation_live_request_count")
    check(any(event.get("event") == "evaluation" and event.get("result") == evaluation for event in events), "evaluation_saved_in_trace")
    if backend == "scripted":
        check(evaluation.get("live_integration_success") is None and evaluation.get("task_success") is None, "scripted_success_separate_from_jev")
    return {"passed": not issues, "run_dir": str(root), "run_id": manifest.get("run_id"), "mode": mode,
            "backend": backend, "scope": "Offline artifact integrity and visible-input provenance; does not re-evaluate task success or inspect secret values",
            "counts": {"events": len(events), "visible_native_tools": len(visible), "full_menu_choices": len(menu),
                       "tool_stage_requests": len(tool_menus), "parameter_stage_requests": len(parameter_requests),
                       "decision_attempts": len(starts), "native_tool_executions": executed,
                       "model_requests": len(starts) if backend == "typesafe_sdk" else 0},
            "stop_reason": evaluation.get("stop_reason"), "live_integration_success": evaluation.get("live_integration_success"),
            "scripted_task_success": evaluation.get("scripted_task_success"), "task_success": evaluation.get("task_success"),
            "checks_performed": dict(counts), "issues": issues}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output", type=Path, help="New audit file; existing evidence is never overwritten")
    args = parser.parse_args(argv)
    result = verify_run(args.run_dir)
    output = args.output or args.run_dir / f"verification_{uuid.uuid4().hex[:8]}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({"passed": result["passed"], "audit_path": str(output), "counts": result.get("counts"), "issues": result["issues"]}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
