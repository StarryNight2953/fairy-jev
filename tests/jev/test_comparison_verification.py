from __future__ import annotations

import json
from copy import deepcopy

import pytest

from fairy.agents.Jev.hybrid.context import PacketContext
from fairy.agents.Jev.trace import TraceWriter, digest, write_json
from fairy.experiments.verification import verify_episode


def save_events(path, events):
    (path / "trace.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))


@pytest.fixture
def hybrid_run(tmp_path):
    entries = [{"name": "read", "description": "Read", "schema": {}, "parameters": [], "visible": True},
               {"name": "adjust", "description": "Adjust", "schema": {"value": "int"},
                "parameters": [{"name": "value", "type": "int", "required": True}], "visible": True}]
    options = {e["name"]: {"purpose": e["description"], "parameters": e["schema"],
                          "signature_parameters": e["parameters"]} for e in entries}
    options["FINISH"] = "Whole task finished"
    catalog_hash = digest(entries)
    workflow, decision = {}, 0
    with TraceWriter(tmp_path / "trace.jsonl", "fixture") as trace:
        context = PacketContext(trace)
        trace.emit("run_start", task="public task")
        for step in range(5):
            if step in (0, 4):
                data = context.build_update_input("public task", ["public system"], options, step)
                material = {"brief": f"Visible advice version {step//4+1}", "evidence_refs": data["input_event_ids"]}
                trace.emit("scripted_context_update", input=data, material=material)
                context.accept_packet(material, data, source_request_id=f"scripted_{step}")
            snapshot = context.freeze("public task", ["public system"], float(step), {"tools": step})
            step_id = f"step_{step:04d}"
            name = "read" if step < 4 else "adjust"
            decision += 1
            tool_decision = f"decision_{decision:04d}"
            trace.emit("decision_start", step_id=step_id, decision_id=tool_decision,
                       decision_stage="tool", state=snapshot, criteria=options,
                       candidate_order=list(options), candidate_hash=digest(options))
            trace.emit("decision_end", step_id=step_id, decision_id=tool_decision,
                       decision_stage="tool", status="ok", selected_option=name)
            arguments = {} if name == "read" else {"value": 1}
            candidates = {"args_000": {"arguments": arguments, "source_refs": ["tool_signature:test"]}}
            trace.emit("parameter_candidates", step_id=step_id, tool_decision_id=tool_decision,
                       tool_name=name, generation={"status": "ready", "candidates": candidates},
                       candidate_hash=digest(candidates), candidate_order=list(candidates))
            ids = [tool_decision]
            if name == "adjust":
                decision += 1
                parameter_decision = f"decision_{decision:04d}"
                trace.emit("decision_start", step_id=step_id, decision_id=parameter_decision,
                    decision_stage="parameters", state=snapshot, criteria=candidates,
                    candidate_order=list(candidates), candidate_hash=digest(candidates))
                trace.emit("decision_end", step_id=step_id, decision_id=parameter_decision,
                    decision_stage="parameters", status="ok", selected_option="args_000")
                ids.append(parameter_decision)
            call_id = f"tool_{step:04d}"
            fields = {"step_id": step_id, "tool_call_id": call_id, "tool_name": name,
                      "arguments": arguments, "decision_ids": ids, "sim_time_before": float(step)}
            trace.emit("tool_start", **fields)
            result = {"value": step}
            trace.emit("tool_end", **fields, sim_time_after=float(step+1), result=result,
                       executed=True, status="ok")
            workflow[call_id] = {"name": call_id, "tool_name": name, "tool_args": arguments,
                                 "content": result, "time_before": float(step), "time_after": float(step+1)}
        trace.emit("run_end", stop_reason="model_finish")
    write_json(tmp_path / "manifest.json", {"run_id": "fixture", "architecture": "hybrid", "mode": "scripted"})
    write_json(tmp_path / "tool_catalog.json", {"tools": entries, "visible_count": 2, "catalog_hash": catalog_hash})
    write_json(tmp_path / "workflow.json", workflow)
    write_json(tmp_path / "evaluation.json", {"task_success": None, "live_integration_success": None,
        "budget_consumption": {"counts": {"http_attempts": 0, "tool_attempts": 5}}})
    return tmp_path


def events(path):
    return [json.loads(line) for line in (path / "trace.jsonl").read_text().splitlines()]


def test_valid_material_prefix_replay_and_cross_update(hybrid_run):
    result = verify_episode(hybrid_run)
    assert result["passed"], result["issues"]
    assert result["coverage"]["hybrid_cross_update"] == "passed"
    assert result["counts"]["state_replays"] == 6


def test_modified_parameter_choice_snapshot_rejected(hybrid_run):
    rows = events(hybrid_run)
    parameter = next(e for e in rows if e["event"] == "decision_start" and e["decision_stage"] == "parameters")
    parameter["state"]["packet"]["brief"] = "silently changed advice"
    save_events(hybrid_run, rows)
    result = verify_episode(hybrid_run)
    assert not result["passed"]
    assert "frozen_two_stage_snapshot" in {i["check"] for i in result["issues"]}


def test_missing_new_feedback_rejected(hybrid_run):
    rows = events(hybrid_run)
    decision = next(e for e in rows if e["event"] == "decision_start" and e["state"]["fresh_events"])
    decision["state"]["fresh_events"] = []
    decision["state"]["fresh_event_ids"] = []
    save_events(hybrid_run, rows)
    result = verify_episode(hybrid_run)
    assert not result["passed"]
    assert "material_state_replay_equal" in {i["check"] for i in result["issues"]}


def test_future_evidence_citation_rejected(hybrid_run):
    rows = events(hybrid_run)
    packet = next(e for e in rows if e["event"] == "packet_accepted")
    packet["packet"]["evidence_refs"] = [len(rows)-1]
    save_events(hybrid_run, rows)
    result = verify_episode(hybrid_run)
    assert not result["passed"]
    assert "packet_citations_supplied" in {i["check"] for i in result["issues"]}


def test_packet_cannot_rewrite_saved_updater_material(hybrid_run):
    rows = events(hybrid_run)
    output = next(e for e in rows if e["event"] == "scripted_context_update")
    output["material"]["brief"] = "The actual updater said something else."
    save_events(hybrid_run, rows)
    result = verify_episode(hybrid_run)
    assert not result["passed"]
    assert "packet_preserves_model_material" in {i["check"] for i in result["issues"]}


def test_substituted_action_rejected(hybrid_run):
    rows = events(hybrid_run)
    action = next(e for e in rows if e["event"] == "tool_start")
    action["arguments"] = {"invented": 1}
    save_events(hybrid_run, rows)
    result = verify_episode(hybrid_run)
    assert not result["passed"]
    assert "execute_exact_selected_arguments" in {i["check"] for i in result["issues"]}


def test_removed_menu_option_rejected(hybrid_run):
    rows = events(hybrid_run)
    decision = next(e for e in rows if e["event"] == "decision_start")
    del decision["criteria"]["FINISH"]
    decision["candidate_order"] = list(decision["criteria"])
    decision["candidate_hash"] = digest(decision["criteria"])
    save_events(hybrid_run, rows)
    result = verify_episode(hybrid_run)
    assert not result["passed"]
    assert "full_visible_menu" in {i["check"] for i in result["issues"]}


def test_native_provider_id_is_linked_to_original_workflow_name(tmp_path):
    with TraceWriter(tmp_path / "trace.jsonl", "native") as trace:
        trace.emit("native_event", native_event={"event_type": "llm_call_end", "tool_calls": [
            {"id": "provider-call-id", "function": {"name": "Read", "arguments": "{}"}}]})
        args = {"tool_name": "Read", "tool_call_id": "provider-call-id", "arguments": {},
                "decision_ids": ["model_1"], "sim_time_before": 1.0}
        trace.emit("tool_start", **args)
        trace.emit("tool_end", **args, sim_time_after=2.0, result="actual", executed=True,
                   workflow_step_name="step17")
    write_json(tmp_path / "manifest.json", {"run_id": "native", "architecture": "llm_native", "mode": "scripted"})
    write_json(tmp_path / "evaluation.json", {"task_success": None, "live_integration_success": None})
    write_json(tmp_path / "workflow.json", {"step17": {"tool_name": "Read", "tool_args": {}, "content": "actual"}})
    result = verify_episode(tmp_path)
    assert result["passed"], result["issues"]
    assert result["coverage"]["jev_tool_choice"] == "not_applicable"


def test_unterminated_http_attempt_rejected(tmp_path):
    with TraceWriter(tmp_path / "trace.jsonl", "native") as trace:
        trace.emit("model_request_start", request_id="request", role="llm_native_decision")
        trace.emit("http_attempt_start", attempt_id="http_1", logical_request_id="request",
                   role="llm_native_decision", request_body={"model": "configured"})
        trace.emit("model_request_end", request_id="request", role="llm_native_decision")
    write_json(tmp_path / "manifest.json", {"run_id": "native", "architecture": "llm_native", "mode": "live"})
    write_json(tmp_path / "evaluation.json", {})
    write_json(tmp_path / "workflow.json", {})
    result = verify_episode(tmp_path)
    assert not result["passed"]
    assert "all_http_attempts_terminated" in {i["check"] for i in result["issues"]}


def test_interrupted_jev_tool_is_evidence_without_fabricated_workflow(hybrid_run):
    rows = events(hybrid_run)
    end = [e for e in rows if e["event"] == "tool_end"][-1]
    end.update(interrupted=True, side_effects="unknown", status="error", result=None,
               workflow_step_name=None)
    save_events(hybrid_run, rows)
    workflow = json.loads((hybrid_run / "workflow.json").read_text())
    del workflow[end["tool_call_id"]]
    write_json(hybrid_run / "workflow.json", workflow)
    result = verify_episode(hybrid_run)
    assert result["passed"], result["issues"]
    assert result["checks_performed"]["interrupted_effect_uncertainty"] == 1
