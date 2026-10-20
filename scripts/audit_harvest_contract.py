"""Re-evaluate frozen saved harvest selections; no Agent, tools or model calls."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import socket

from fairy.agents.Jev.harvest_contract import HARVEST, harvest_range_menu


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def run(index_path: Path, output: Path):
    def deny(*args, **kwargs):
        raise RuntimeError("Network disabled for offline harvest audit")
    socket.socket.connect = deny
    socket.socket.connect_ex = deny
    socket.create_connection = deny
    index_bytes = index_path.read_bytes()
    index = json.loads(index_bytes)
    output.mkdir(parents=True, exist_ok=False)
    rows, sources = [], []
    for source in index["rows"]:
        path = Path(source["trace"])
        raw = path.read_bytes()
        if sha(raw) != source["trace_sha256"]:
            raise ValueError(f"Frozen trace hash changed: {path}")
        events = [json.loads(line) for line in raw.splitlines()]
        manifest_path = path.with_name("manifest.json")
        manifest_raw = manifest_path.read_bytes()
        manifest = json.loads(manifest_raw)
        catalog = manifest["config"]["candidate_catalog"]
        bounds = catalog["ridge_bounds"]["values"]
        width = catalog["max_pass_width"][HARVEST]
        by_id = {e["event_id"]: e for e in events}
        decisions = {e["decision_id"]: e for e in events if e["event"] == "decision_start"}
        requests = {e["logical_request_id"]: e for e in events
                    if e["event"] == "http_attempt_start" and "request_body" in e}
        for event in events:
            if event["event"] != "tool_end" or event.get("tool_name") != HARVEST:
                continue
            decision = decisions[event["decision_ids"][0]]
            request = requests[decision["request_id"]]
            state = request["request_body"]["state"]
            if state != decision["state"]:
                raise ValueError("Saved actual request and decision state differ")
            prior = [e for e in events if e["event_id"] < decision["event_id"]]
            menu = harvest_range_menu(prior, bounds, width)
            args = event["arguments"]
            start, end = args["start_ridge"], args["end_ridge"]
            allowed = end in menu["end_candidates_by_start"].get(str(start), [])
            targeted = set(range(start, end + 1))
            blocked = targeted & set(menu["facts"]["excluded_ridges"])
            proofs = []
            for ridge in sorted(blocked):
                evidence = menu["facts"]["evidence"][str(ridge)]
                earlier = by_id[evidence["event_id"]]
                matches = [
                    a["observation_id"] for a in state.get("actions_so_far", [])
                    if a.get("action") == earlier.get("tool_name")
                    and a.get("arguments") == earlier.get("arguments")
                    and a.get("result") == earlier.get("result")
                    and a.get("status") == earlier.get("status")
                    and a.get("sim_time_before") == earlier.get("sim_time_before")
                    and a.get("sim_time_after") == earlier.get("sim_time_after")
                    and a.get("executed") == earlier.get("executed")
                ]
                proofs.append({"ridge_id": ridge, **evidence,
                               "exact_observation_ids_in_actual_request": matches})
            value = event.get("result") or {}
            success = event.get("status") == "ok" and value.get("status") == "ok"
            known_error = value.get("error") == "All ridges must be planted before harvest"
            rows.append({
                "scenario_id": source["scenario_id"], "trace": str(path),
                "tool_event_id": event["event_id"],
                "tool_choice_event_id": decision["event_id"],
                "http_request_event_id": request["event_id"],
                "arguments": args, "original_success": success,
                "original_unplanted_error": known_error,
                "would_retain_original_range": allowed,
                "blocked_ridges": sorted(blocked),
                "allowed_start_count": len(menu["start_candidates"]),
                "allowed_range_count": sum(map(len, menu["end_candidates_by_start"].values())),
                "evidence_visible_in_actual_request": all(
                    p["exact_observation_ids_in_actual_request"] for p in proofs
                ),
                "exclusion_evidence": proofs,
            })
        sources.append({
            "trace": str(path), "sha256": sha(raw),
            "unchanged_after_read": sha(path.read_bytes()) == sha(raw),
            "manifest": str(manifest_path), "manifest_sha256": sha(manifest_raw),
            "ridge_bounds": bounds, "max_pass_width": width,
        })
    errors = [r for r in rows if r["original_unplanted_error"]]
    successes = [r for r in rows if r["original_success"]]
    summary = {
        "frozen_scenario_count": len(sources), "evaluated_harvest_calls": len(rows),
        "original_unplanted_errors": len(errors),
        "original_unplanted_errors_excluded": sum(not r["would_retain_original_range"] for r in errors),
        "original_successful_harvests": len(successes),
        "original_successful_harvests_retained": sum(r["would_retain_original_range"] for r in successes),
        "all_exclusion_evidence_exactly_in_original_http_input": all(
            r["evidence_visible_in_actual_request"] for r in rows
        ),
        "all_original_traces_unchanged": all(s["unchanged_after_read"] for s in sources),
        "input_index": str(index_path), "input_index_sha256": sha(index_bytes),
        "new_model_requests": 0, "new_tool_executions": 0,
        "runtime_enabled": False,
        "inference_limit": (
            "Retrospective menu eligibility only. No counterfactual model actions, "
            "saved-call estimate, latency improvement or task-success claim."
        ),
    }
    for name, value in [("summary.json", summary), ("calls.json", rows), ("sources.json", sources)]:
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    assert errors and all(not r["would_retain_original_range"] for r in errors)
    assert successes and all(r["would_retain_original_range"] for r in successes)
    assert summary["all_exclusion_evidence_exactly_in_original_http_input"]
    assert summary["all_original_traces_unchanged"]
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    run(args.index, args.output)
