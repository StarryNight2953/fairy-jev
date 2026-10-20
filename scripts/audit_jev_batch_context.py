"""Read-only batch/context audit. No model, tool, environment or oracle execution."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def measure(value):
    text = canonical(value)
    return {"characters": len(text), "utf8_bytes": len(text.encode())}


def sha(value):
    return hashlib.sha256(value).hexdigest()


def request_profile(event):
    body = event.get("request_body", {})
    state = body.get("state")
    result = {
        "event_id": event["event_id"], "attempt_id": event["attempt_id"],
        "role": event.get("role"), "body": measure(body), "state": measure(state),
        "questions": measure(body.get("questions")),
        "state_components": {}, "action_count": 0, "actions_by_tool": [],
        "largest_actions": [], "identical_argument_repetitions": 0,
    }
    if not isinstance(state, dict):
        return result
    result["state_components"] = {key: measure(value) for key, value in state.items()}
    actions = state.get("actions_so_far", [])
    result["action_count"] = len(actions)
    by_tool = defaultdict(lambda: {"action_count": 0, "serialized_action_bytes": 0})
    largest = []
    repeated = Counter()
    for action in actions:
        name = action.get("action")
        size = measure(action)["utf8_bytes"]
        by_tool[name]["action_count"] += 1
        by_tool[name]["serialized_action_bytes"] += size
        repeated[(name, canonical(action.get("arguments")))] += 1
        largest.append({"observation_id": action.get("observation_id"), "tool_name": name,
                        "action_bytes": size, "result_bytes": measure(action.get("result"))["utf8_bytes"]})
    result["actions_by_tool"] = sorted(
        ({"tool_name": name, **data} for name, data in by_tool.items()),
        key=lambda item: item["serialized_action_bytes"], reverse=True,
    )
    result["largest_actions"] = sorted(largest, key=lambda item: item["action_bytes"], reverse=True)[:5]
    result["identical_argument_repetitions"] = sum(n - 1 for n in repeated.values())
    return result


def error_type(event):
    parsed = event.get("parsed_response")
    if not isinstance(parsed, dict):
        try:
            parsed = json.loads(event.get("raw_response_body", ""))
        except (ValueError, TypeError):
            return None
    detail = parsed.get("detail") if isinstance(parsed, dict) else None
    return detail.get("error_type") if isinstance(detail, dict) else None


def inspect_trace(path):
    raw = path.read_bytes()
    events = [json.loads(line) for line in raw.splitlines()]
    requests = {e["attempt_id"]: e for e in events if e["event"] == "http_attempt_start"}
    responses = [e for e in events if e["event"] == "http_attempt_end"]
    tools = [e for e in events if e["event"] == "tool_end" and e.get("executed")]
    errors = [e for e in responses if e.get("status") != "ok"]
    context = [e for e in errors if e.get("http_status") == 400 and error_type(e) == "max_tokens_exceeded"]
    dimensions = [request_profile(e) for e in requests.values()]
    profiles = {e["attempt_id"]: e for e in dimensions}
    failure = None
    if context:
        end = context[0]
        start = requests[end["attempt_id"]]
        earlier_success = [e for e in responses if e["event_id"] < start["event_id"] and e.get("status") == "ok"]
        prior_tool = [e for e in tools if e["event_id"] < start["event_id"]]
        failure = {
            "response_event_id": end["event_id"], "http_status": 400,
            "error_type": "max_tokens_exceeded", "raw_response_body": end.get("raw_response_body"),
            "request": profiles[end["attempt_id"]],
            "last_successful_request": profiles[earlier_success[-1]["attempt_id"]] if earlier_success else None,
            "last_tool": {
                "event_id": prior_tool[-1]["event_id"], "tool_name": prior_tool[-1]["tool_name"],
                "status": prior_tool[-1]["status"], "result_bytes": measure(prior_tool[-1].get("result"))["utf8_bytes"],
            } if prior_tool else None,
        }
    # Evidence of feedback transmission only; never substitute this for task success.
    feedback = None
    for tool in tools:
        if tool.get("status") != "ok":
            continue
        for request in requests.values():
            if request["event_id"] <= tool["event_id"]:
                continue
            state = request.get("request_body", {}).get("state")
            if not isinstance(state, dict):
                continue
            for action in state.get("actions_so_far", []):
                if all(action.get(a) == tool.get(b) for a, b in [
                    ("action", "tool_name"), ("arguments", "arguments"), ("result", "result"),
                    ("status", "status"), ("sim_time_before", "sim_time_before"),
                    ("sim_time_after", "sim_time_after"), ("executed", "executed"),
                ]):
                    feedback = {"tool_event_id": tool["event_id"], "request_event_id": request["event_id"],
                                "observation_id": action.get("observation_id")}
                    break
            if feedback:
                break
        if feedback:
            break
    tool_errors = [e for e in tools if e.get("status") == "error"]
    error_counts = Counter(
        (e["tool_name"], canonical(e.get("result"))) for e in tool_errors
    )
    return {
        "trace": str(path), "trace_sha256": sha(raw),
        "unchanged_after_read": sha(path.read_bytes()) == sha(raw),
        "http_attempts": len(requests), "http_responses": len(responses),
        "http_status_counts": dict(Counter(str(e.get("http_status")) for e in responses)),
        "unpaired_http_attempts": sorted(set(requests) - {e["attempt_id"] for e in responses}),
        "http_errors": [{"event_id": e["event_id"], "http_status": e.get("http_status"),
                         "error_type": error_type(e), "status": e.get("status")} for e in errors],
        "context_error": failure,
        "executed_tools": len(tools), "tool_errors": len(tool_errors),
        "tool_error_groups": [{"tool_name": name, "result": json.loads(value), "count": count}
                              for (name, value), count in error_counts.most_common()],
        "feedback_transmission_witness": feedback,
        "all_request_dimensions": dimensions,
        "cumulative_diagnostic_state_bytes": sum(d["state"]["utf8_bytes"] for d in dimensions),
        "maximum_diagnostic_body_bytes": max((d["body"]["utf8_bytes"] for d in dimensions), default=0),
        "maximum_action_count_in_request": max((d["action_count"] for d in dimensions), default=0),
    }


def audit(batch, output):
    batch = batch.resolve()
    progress_raw = (batch / "progress.json").read_bytes()
    prereg_raw = (batch / "preregistration.json").read_bytes()
    progress, prereg = json.loads(progress_raw), json.loads(prereg_raw)
    result_files = sorted((batch / "results").glob("*.json"))
    rows = []
    for path in result_files:
        record_raw = path.read_bytes()
        record = json.loads(record_raw)
        live = Path(record["path"])
        evaluation_raw = (live / "evaluation.json").read_bytes()
        evaluation = json.loads(evaluation_raw)
        manifest_raw = (live / "manifest.json").read_bytes()
        manifest = json.loads(manifest_raw)
        metrics_raw = (live / "metrics.json").read_bytes()
        metrics = json.loads(metrics_raw)
        trace = inspect_trace(live / "trace.jsonl")
        if trace["http_attempts"] != metrics["model_http_attempts"] or trace["executed_tools"] != metrics["tool_attempts"]:
            raise ValueError(f"Trace / metrics count mismatch: {live}")
        rows.append({
            "scenario_id": record["scenario_id"], "result_record": str(path),
            "result_record_sha256": sha(record_raw), "evaluation_sha256": sha(evaluation_raw),
            "run_id": manifest["run_id"], "seed": manifest["config"]["seed"],
            "source_commit": manifest["code_commit"], "manifest_sha256": sha(manifest_raw),
            "metrics_sha256": sha(metrics_raw),
            "usage": {key: metrics.get(key) for key in ("input_tokens", "output_tokens", "usage_status", "roles")},
            "stop_reason": evaluation["stop_reason"],
            "execution_status": evaluation["execution_status"],
            "live_integration_success_original": evaluation["live_integration_success"],
            "task_success_original": evaluation["task_success"],
            "native_validation_original": (evaluation.get("native_validation") or {}).get("success"),
            "wall_elapsed_seconds": evaluation["wall_elapsed_seconds"],
            "trace_verification_passed": evaluation.get("trace_verification", {}).get("passed"),
            "replay_verification_passed": evaluation.get("replay_verification", {}).get("passed"),
            **trace,
        })
    groups = {}
    for reason in sorted({r["stop_reason"] for r in rows}):
        subset = [r for r in rows if r["stop_reason"] == reason]
        groups[reason] = {
            "count": len(subset),
            "median_seconds_to_stop": median(r["wall_elapsed_seconds"] for r in subset),
            "median_executed_tools": median(r["executed_tools"] for r in subset),
            "context_error_count": sum(r["context_error"] is not None for r in subset),
            "semantic_completion_inferred": False,
        }
    failures = [r for r in rows if r["context_error"]]
    dominant = Counter()
    for row in failures:
        sources = row["context_error"]["request"]["actions_by_tool"]
        if sources:
            dominant[sources[0]["tool_name"]] += 1
    planned = prereg["planned_samples"]
    summary = {
        "snapshot_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "batch": str(batch), "planned_samples": planned, "saved_records_read": len(rows),
        "progress_snapshot": progress,
        "all_planned_records_present": len(rows) == planned,
        "batch_reports_finished": progress.get("finished") is True,
        "stop_reason_groups": groups,
        "context_error_runs": len(failures),
        "context_failure_dominant_history_tool": dict(dominant),
        "source_commits": sorted({r["source_commit"] for r in rows}),
        "scenario_seeds": sorted({r["seed"] for r in rows}),
        "total_http_attempts": sum(r["http_attempts"] for r in rows),
        "total_executed_tools": sum(r["executed_tools"] for r in rows),
        "total_tool_errors": sum(r["tool_errors"] for r in rows),
        "original_task_success_counts": dict(Counter(str(r["task_success_original"]) for r in rows)),
        "original_integration_counts": dict(Counter(str(r["live_integration_success_original"]) for r in rows)),
        "original_native_validation_counts": dict(Counter(str(r["native_validation_original"]) for r in rows)),
        "feedback_witness_runs": sum(r["feedback_transmission_witness"] is not None for r in rows),
        "trace_verifications_passed": sum(r["trace_verification_passed"] is True for r in rows),
        "replay_verifications_passed": sum(r["replay_verification_passed"] is True for r in rows),
        "all_trace_sources_unchanged": all(r["unchanged_after_read"] for r in rows),
        "all_http_attempts_paired": all(not r["unpaired_http_attempts"] for r in rows),
        "new_model_requests": 0, "new_tool_executions": 0,
        "measurement_scope": (
            "Characters/UTF-8 bytes are compact sorted JSON reserialization diagnostics, "
            "not wire sizes or model token counts. Per-action sums exclude list separators."
        ),
        "interpretation_limits": [
            "Time to failure is not time to task completion.",
            "model_finish does not establish semantic task completion.",
            "Native validation booleans and oracle path similarity are not common semantic grades.",
            "Repeated arguments may be appropriate after state/time changes.",
            "A feedback witness proves transmission of one actual observation, not comprehension.",
            "No paired Hybrid batch or matched-seed native causal comparison in this audit.",
            "No counterfactual performance prediction from request dimensions.",
        ],
    }
    output.mkdir(parents=True, exist_ok=False)
    (output / "progress_snapshot.json").write_bytes(progress_raw)
    (output / "preregistration_snapshot.json").write_bytes(prereg_raw)
    for name, value in [("summary.json", summary), ("cases.json", rows)]:
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    audit(args.batch, args.output)
