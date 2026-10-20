"""Reconstruct historical candidate inputs without replacing historical actions."""
from copy import deepcopy
import argparse
import hashlib
import json
from pathlib import Path
import socket

from fairy.agents.Jev.candidates import CandidateResolver
from fairy.agents.Jev.report_observations import VERSION, irrigation_recheck_evidence
from fairy.agents.Jev.state import VisibleState


def audit(run, output):
    def deny(*args, **kwargs):
        raise AssertionError("No network allowed in report candidate audit")
    socket.socket.connect = deny
    socket.socket.connect_ex = deny
    socket.create_connection = deny
    raw = (run / "trace.jsonl").read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    events = [json.loads(line) for line in raw.splitlines()]
    config = json.loads((run / "manifest.json").read_text())["config"]
    catalog = json.loads((run / "tool_catalog.json").read_text())["tools"]
    old = CandidateResolver(catalog, config)
    new = CandidateResolver(catalog, {**config, "report_observation_policy": VERSION})
    state = VisibleState(**config.get("history", {}))
    visible, comparisons = None, []
    for event in events:
        kind = event["event"]
        if kind == "run_start":
            state.task = event["task"]
        elif kind == "system_context":
            state.system_context.append(event["content"])
        elif kind == "tool_end":
            observation = {k: deepcopy(v) for k, v in event.items()
                           if k not in {"event", "event_id", "run_id", "wall_timestamp"}}
            state.add_observation(observation)
        elif kind == "environment_notification":
            state.add_observation({
                "kind": kind, "result": event["content"],
                "sim_time_after": event["sim_time"], "time": event.get("time"),
            })
        elif kind == "controller_feedback":
            state.add_feedback({k: deepcopy(v) for k, v in event.items()
                                if k not in {"event", "event_id", "run_id", "wall_timestamp", "step_id"}})
        elif kind == "decision_start" and event.get("decision_stage") == "tool":
            visible = state.snapshot(event["state"]["current_time"])
        elif kind == "parameter_candidates" and event["tool_name"] == "AgentUserInterface__send_message_to_user":
            before = old.generate(event["tool_name"], visible)
            after = new.generate(event["tool_name"], visible)
            before_content = next(iter(before["candidates"].values()))["arguments"]["content"]
            after_content = next(iter(after["candidates"].values()))["arguments"]["content"]
            comparisons.append({
                "generation_event_id": event["event_id"], "step_id": event["step_id"],
                "original_candidates_reconstructed_exactly": before["candidates"] == event["generation"]["candidates"],
                "original_candidate": before["candidates"],
                "proposed_candidate_not_executed": after["candidates"],
                "proposed_evidence": irrigation_recheck_evidence(visible, config["candidate_catalog"]["ridge_bounds"]["values"]),
                "original_content_utf8_bytes": len(before_content.encode()),
                "proposed_content_utf8_bytes": len(after_content.encode()),
            })
    assert comparisons, "No historical report candidates found"
    assert all(c["original_candidates_reconstructed_exactly"] for c in comparisons)
    assert hashlib.sha256((run / "trace.jsonl").read_bytes()).hexdigest() == digest
    output.mkdir(parents=True, exist_ok=False)
    (output / "candidate_comparisons.json").write_text(json.dumps(comparisons, ensure_ascii=False, indent=2) + "\n")
    summary = {
        "source_run": str(run), "source_trace_sha256": digest,
        "source_trace_unchanged": True, "report_candidates": len(comparisons),
        "original_candidates_reconstructed_exactly": True,
        "new_model_requests": 0, "new_tool_executions": 0,
        "new_policy": VERSION, "original_config_policy": config.get("report_observation_policy"),
        "report_bytes_before_after": [[c["original_content_utf8_bytes"], c["proposed_content_utf8_bytes"]] for c in comparisons],
        "interpretation": "Counterfactual candidate construction only. Original model choices, messages, trace and evaluator results remain unchanged. No prediction of new choices or task success.",
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    audit(args.run.resolve(), args.output.resolve())
