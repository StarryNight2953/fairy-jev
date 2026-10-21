"""Offline three-architecture trace and material replay integrity checks.

This never calls a provider or tool and never feeds evaluator state to an agent.
Recorded model/task failures are valid evidence, not integrity failures.
"""
from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from fairy.agents.Jev.hybrid.context import PacketContext, VISIBLE_EVENTS
from fairy.agents.Jev.trace import digest

ROLES = {"llm_native_decision", "llm_native_summary", "llm_context_update",
         "jev_tool_choice", "jev_argument_choice"}


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def verify_episode(run_dir):
    root = Path(run_dir)
    issues, performed = [], Counter()

    def check(condition, code, event=None, detail=None):
        performed[code] += 1
        if not condition:
            issues.append({"check": code, "event_id": (event or {}).get("event_id"), "detail": detail})

    try:
        manifest = _read(root / "manifest.json")
        evaluation = _read(root / "evaluation.json")
        workflow = _read(root / "workflow.json")
        events = [json.loads(line) for line in (root / "trace.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        catalog = _read(root / "tool_catalog.json") if (root / "tool_catalog.json").exists() else None
        metrics = _read(root / "metrics.json") if (root / "metrics.json").exists() else {}
    except (OSError, ValueError) as error:
        return {"passed": False, "issues": [{"check": "artifact_read", "detail": str(error)}], "coverage": {}}
    architecture = manifest.get("architecture") or evaluation.get("architecture")
    mode = manifest.get("mode", "live")
    is_native, is_hybrid = architecture == "llm_native", architecture == "hybrid"
    check(architecture in {"llm_native", "jev", "hybrid"}, "architecture_identity")
    check([e.get("event_id") for e in events] == list(range(len(events))), "contiguous_event_ids")
    check(all(e.get("run_id") == manifest.get("run_id") for e in events), "run_identity")
    visible_entries = [e for e in (catalog or {}).get("tools", []) if e.get("visible", True)]
    by_name = {e["name"]: e for e in visible_entries}
    expected_menu = list(by_name) + ["FINISH"]
    if catalog:
        check(catalog.get("catalog_hash") == digest(visible_entries), "catalog_hash")
        check(catalog.get("visible_count") == len(visible_entries), "catalog_visible_count")

    http_starts, http_ends, logical, logical_ends = {}, {}, {}, {}
    starts, decisions, generations, tools, tool_starts, native_calls = {}, {}, {}, {}, {}, {}
    packet, packets, packet_input_events = None, [], {}
    update_starts, update_materials = {}, {}
    first_task, state_replays, tool_menus, parameter_choices = None, 0, 0, 0
    for event in events:
        kind, eid = event.get("event"), event.get("event_id")
        if kind == "model_request_start":
            key = event.get("request_id")
            check(bool(key) and key not in logical, "unique_logical_request", event)
            check(event.get("role") in ROLES, "known_model_role", event)
            logical[key] = event
        elif kind == "model_request_end":
            key = event.get("request_id")
            check(key in logical and key not in logical_ends, "logical_end_pair", event)
            logical_ends[key] = event
        elif kind == "http_attempt_start":
            key, rid = event.get("attempt_id"), event.get("logical_request_id")
            check(bool(key) and key not in http_starts, "unique_http_attempt", event)
            check(rid in logical and event.get("role") == logical.get(rid, {}).get("role"), "http_logical_role_link", event)
            check("request_body" in event, "http_request_preserved", event)
            http_starts[key] = event
        elif kind == "http_attempt_end":
            key = event.get("attempt_id")
            source = http_starts.get(key)
            check(source is not None and key not in http_ends, "http_end_pair", event)
            if source:
                check(all(event.get(k) == source.get(k) for k in ("role", "logical_request_id", "requested_model")), "http_identity", event)
            if event.get("http_status") is not None:
                check("raw_response_body" in event, "http_response_preserved", event)
            check("usage" in event and (event["usage"] is not None or event.get("usage_status") == "unavailable"), "unknown_usage_is_null", event)
            http_ends[key] = event
        elif kind == "native_event":
            native_event = event.get("native_event", {})
            if native_event.get("event_type") == "llm_call_end":
                for call in native_event.get("tool_calls", []):
                    native_calls[call.get("id")] = call
        elif kind in {"llm_update_start", "scripted_context_update"}:
            update_input = event.get("input", {})
            input_hash = digest(update_input)
            packet_input_events[input_hash] = event
            if kind == "llm_update_start":
                update_starts[event.get("update_id")] = input_hash
            else:
                update_materials[input_hash] = event.get("material", {})
            boundary = update_input.get("input_boundary_event_id", -1)
            check(boundary < eid, "update_input_is_prefix", event)
            raw_evidence = update_input.get("fresh_events", []) + update_input.get("prior_cited_evidence", [])
            exact_ids = sorted({row.get("event_id") for row in raw_evidence})
            check(exact_ids == update_input.get("input_event_ids"), "update_exact_evidence_set", event)
            for row in raw_evidence:
                index = row.get("event_id", -1)
                check(type(index) is int and 0 <= index <= boundary and index < len(events)
                      and row == events[index] and row.get("event") in VISIBLE_EVENTS,
                      "update_raw_evidence_matches_visible_prefix", event, index)
            check(update_input.get("previous_packet") == packet, "update_previous_packet", event)
        elif kind == "llm_update_attempt_end" and event.get("status") == "ok":
            try:
                parsed = json.loads(event.get("content"))
            except (TypeError, ValueError):
                parsed = None
            check(parsed == event.get("parsed"), "material_parsed_from_recorded_model_output", event)
        elif kind == "llm_update_end":
            input_hash = update_starts.get(event.get("update_id"))
            check(input_hash is not None, "update_output_follows_input", event)
            update_materials[input_hash] = event.get("material", {})
        elif kind == "packet_accepted":
            candidate = event.get("packet", {})
            version = len(packets) + 1
            check(candidate.get("packet_version") == version and candidate.get("packet_id") == f"packet_{version:04d}", "packet_version", event)
            source = packet_input_events.get(candidate.get("update_input_hash"))
            check(source is not None, "packet_has_exact_update_input", event)
            material = update_materials.get(candidate.get("update_input_hash"), {})
            check(all(candidate.get(key) == material.get(key) for key in ("brief", "evidence_refs")),
                  "packet_preserves_model_material", event)
            if source:
                data = source["input"]
                check(candidate.get("input_event_ids") == data.get("input_event_ids")
                      and candidate.get("covers_through_event_id") == data.get("input_boundary_event_id"), "packet_coverage_matches_request", event)
                check(set(candidate.get("evidence_refs", [])) <= set(data.get("input_event_ids", [])), "packet_citations_supplied", event)
                prior_count = sum(bool(e.get("executed")) for e in events[:source["event_id"]] if e.get("event") == "tool_end")
                check(candidate.get("created_after_tool_attempts") == prior_count, "packet_tool_attempt_count", event)
            packet = deepcopy(candidate)
            packets.append(event)
        elif kind == "hybrid_snapshot":
            evidence = event.get("candidate_evidence", {})
            check(event.get("candidate_evidence_hash") == digest(evidence), "candidate_evidence_hash", event)
            check(not set(evidence).intersection({"packet", "decision_material", "brief", "evidence_refs"}),
                  "material_excluded_from_candidate_evidence", event)
            observations = {row.get("observation_id"): row for row in evidence.get("observations", [])}
            for field in ("observations", "recent_actions", "action_facts"):
                for row in evidence.get(field, []):
                    call_id = row.get("tool_call_id")
                    if not call_id:
                        continue
                    source = tools.get(call_id, {})
                    result = row.get("result")
                    if isinstance(result, dict) and set(result) == {"same_as_observation"}:
                        result = observations.get(result["same_as_observation"], {}).get("result")
                    check(source and row.get("tool_name") == source.get("tool_name")
                          and row.get("arguments") == source.get("arguments") and result == source.get("result"),
                          "candidate_facts_from_actual_tool_prefix", event, call_id)
        elif kind == "decision_start":
            key, stage = event.get("decision_id"), event.get("decision_stage")
            check(bool(key) and key not in starts, "unique_decision_start", event)
            starts[key] = event
            criteria, state = event.get("criteria", {}), event.get("state", {})
            check(1 <= len(criteria) <= 255, "choice_option_limit", event)
            check(event.get("candidate_hash") == digest(criteria), "choice_candidate_hash", event)
            check(event.get("candidate_order") == list(criteria), "choice_candidate_order", event)
            if first_task is None:
                first_task = state.get("task")
            check(state.get("task") == first_task, "original_task_preserved", event)
            if stage == "tool":
                tool_menus += 1
                check(catalog is not None and list(criteria) == expected_menu, "full_visible_menu", event)
                for name, entry in by_name.items():
                    check(criteria.get(name) == {"purpose": entry["description"], "parameters": entry["schema"],
                        "signature_parameters": entry["parameters"]}, "catalog_option_content", event, name)
            elif stage == "parameters":
                parameter_choices += 1
                generation = generations.get(event.get("step_id"), {})
                check(criteria == generation.get("generation", {}).get("candidates"), "parameter_choice_matches_candidates", event)
                tool_request = starts.get(generation.get("tool_decision_id"), {})
                if is_hybrid:
                    check(state == tool_request.get("state"), "frozen_two_stage_snapshot", event)
            if is_hybrid:
                check(state.get("packet") == packet, "latest_packet_only", event)
                cutoff = state.get("observation_cutoff_event_id", -1)
                check(type(cutoff) is int and -1 <= cutoff < eid, "snapshot_prefix_boundary", event)
                if packet is not None and type(cutoff) is int and -1 <= cutoff < eid:
                    prefix = deepcopy(events[:cutoff+1])
                    replay_context = PacketContext(SimpleNamespace(events=prefix))
                    replay_context.packet = deepcopy(packet)
                    replay_context._covered_boundary = packet["covers_through_event_id"]
                    rebuilt = replay_context.freeze(state.get("task"), state.get("system_context"),
                        state.get("current_time"), state.get("budgets"))
                    check(rebuilt == state, "material_state_replay_equal", event)
                    state_replays += 1
                    if stage == "tool":
                        unseen = [row["event_id"] for row in events[cutoff+1:eid] if row.get("event") in VISIBLE_EVENTS]
                        check(not unseen, "fresh_feedback_immediate", event, unseen)
        elif kind == "decision_end":
            key = event.get("decision_id")
            source = starts.get(key)
            check(source is not None and key not in decisions, "decision_end_pair", event)
            if source and event.get("status") == "ok":
                check(event.get("selected_option") in source.get("criteria", {}), "selected_submitted_option", event)
            decisions[key] = event
        elif kind == "parameter_candidates":
            generation = event.get("generation", {})
            source = decisions.get(event.get("tool_decision_id"), {})
            check(source.get("status") == "ok" and source.get("selected_option") == event.get("tool_name"), "generate_selected_tool_only", event)
            check(event.get("step_id") not in generations, "one_generation_per_round", event)
            check(event.get("candidate_hash") == digest(generation.get("candidates", {})), "generated_candidate_hash", event)
            generations[event.get("step_id")] = event
        elif kind == "tool_start":
            key = event.get("tool_call_id")
            check(bool(key) and key not in tool_starts, "unique_tool_start", event)
            tool_starts[key] = event
            if is_native:
                call = native_calls.get(key, {})
                function = call.get("function", {})
                check(function.get("name") == event.get("tool_name"), "native_tool_selected_by_model", event)
                try:
                    raw_arguments = json.loads(function.get("arguments") or "{}")
                except (ValueError, TypeError):
                    raw_arguments = {}
                check(raw_arguments == event.get("arguments"), "native_arguments_selected_by_model", event)
            else:
                generation = generations.get(event.get("step_id"), {})
                choice_ids = event.get("decision_ids", [])
                check(bool(choice_ids) and choice_ids[0] == generation.get("tool_decision_id"), "tool_choice_link", event)
                check(event.get("tool_name") == generation.get("tool_name"), "execute_selected_tool", event)
                parameterized = bool(by_name.get(event.get("tool_name"), {}).get("parameters"))
                prefix_ids = generation.get("generation", {}).get("prefix_decision_ids", [])
                check(len(choice_ids) == (2 + len(prefix_ids) if parameterized else 1)
                      and (not parameterized or choice_ids[1:-1] == prefix_ids), "argument_choice_presence", event)
                candidate = decisions.get(choice_ids[-1] if choice_ids else None, {}).get("selected_option") if parameterized else "args_000"
                selected = generation.get("generation", {}).get("candidates", {}).get(candidate, {})
                check(event.get("arguments") == selected.get("arguments"), "execute_exact_selected_arguments", event)
        elif kind == "tool_end":
            key = event.get("tool_call_id")
            source = tool_starts.get(key)
            check(source is not None and key not in tools, "tool_end_pair", event)
            if source:
                check(all(event.get(k) == source.get(k) for k in ("tool_name", "arguments", "decision_ids")), "tool_identity", event)
            check(event.get("sim_time_after", 0) >= event.get("sim_time_before", 0), "forward_simulation_time", event)
            tools[key] = event

    check(set(http_starts) == set(http_ends), "all_http_attempts_terminated")
    check(set(logical) == set(logical_ends), "all_logical_requests_terminated")
    check(set(starts) == set(decisions), "all_decisions_terminated")
    check(set(tool_starts) == set(tools), "all_tool_calls_terminated")
    workflow_tools = {name: row for name, row in workflow.items() if isinstance(row, dict) and row.get("tool_name")}
    linked_workflow = set()
    for key, event in tools.items():
        workflow_name = event.get("workflow_step_name") if is_native else key
        if event.get("interrupted") and workflow_name not in workflow_tools:
            check(event.get("side_effects") in {"unknown", "not_started"}, "interrupted_effect_uncertainty", event)
            continue
        row = workflow_tools.get(workflow_name, {})
        linked_workflow.add(workflow_name)
        check(row.get("tool_name") == event.get("tool_name") and row.get("tool_args") == event.get("arguments")
              and row.get("content") == event.get("result"), "workflow_tool_result_parity", event)
    check(linked_workflow == set(workflow_tools), "no_untraced_workflow_tools")
    actual_tools = sum(bool(e.get("executed")) for e in tools.values())
    consumption = evaluation.get("budget_consumption") or metrics.get("budget_consumption") or metrics.get("budget") or {}
    counts = consumption.get("counts", consumption)
    if "http_attempts" in counts:
        check(counts["http_attempts"] == len(http_starts), "http_count_matches_metrics")
    if "tool_attempts" in counts:
        check(counts["tool_attempts"] == actual_tools, "tool_count_matches_metrics")
    if mode == "scripted":
        check(not http_starts, "scripted_has_no_http")
        check(evaluation.get("task_success") is None and evaluation.get("live_integration_success") is None, "scripted_not_live_success")
    if is_hybrid and packets:
        config = manifest.get("effective_config") or manifest.get("config") or {}
        interval = config.get("hybrid", {}).get("update_interval", config.get("update_interval", 4))
        attempts = [e["packet"]["created_after_tool_attempts"] for e in packets]
        check(attempts == list(range(0, len(attempts)*interval, interval)), "fixed_update_schedule")
        tool_limit = manifest.get("experiment_limits", {}).get("max_tool_attempts", 40)
        check(all(n < tool_limit for n in attempts), "no_update_after_tool_budget")
    coverage = {
        "http": "passed" if http_starts else ("not_applicable" if mode == "scripted" else "uncovered"),
        "native_execution": "passed" if actual_tools else "uncovered",
        "jev_tool_choice": "not_applicable" if is_native else ("passed" if tool_menus else "uncovered"),
        "jev_argument_choice": "not_applicable" if is_native else ("passed" if parameter_choices else "uncovered"),
        "llm_native_summary": "passed" if any(e.get("role") == "llm_native_summary" for e in logical.values()) else "not_applicable",
        "hybrid_material": "not_applicable" if not is_hybrid else ("passed" if packets and state_replays else "uncovered"),
        "hybrid_cross_update": "not_applicable" if not is_hybrid else ("passed" if len(packets) >= 2 else "uncovered"),
        "material_state_replay": "not_applicable" if not is_hybrid else ("passed" if state_replays else "uncovered"),
    }
    from fairy.agents.Jev.parameter_contracts import audit_parameter_contracts
    issues.extend(audit_parameter_contracts(events, by_name, manifest.get("effective_config") or manifest.get("config") or {}))
    return {"passed": not issues, "issues": issues, "coverage": coverage,
        "architecture": architecture, "run_id": manifest.get("run_id"),
        "checks_performed": dict(performed), "counts": {"http_attempts": len(http_starts),
            "logical_requests": len(logical), "native_tool_attempts": actual_tools,
            "decision_requests": len(starts), "packets": len(packets), "state_replays": state_replays},
        "scope": "Offline artifact/visible-prefix integrity and saved-material replay; no model or tool execution; task truth is independently evaluated."}
