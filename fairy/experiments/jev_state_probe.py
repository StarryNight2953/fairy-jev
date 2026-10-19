"""At most two frozen Jev requests; no Agent, environment or tools are executed.

The original failing request and an externally prepared state projection share
the exact question, candidate order, model and endpoint. This is a diagnostic,
not an episode or a measurement of task success.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from fairy.agents.Jev.client import DecisionError, OfficialBackend
from fairy.agents.Jev.trace import TraceWriter, digest, write_json
from .budget import BudgetStop, EpisodeRuntime, LiveLedger
from .transports import ObservedTransport


GROUPS = ("A_original", "B_task_history")
ACCESS_STATUSES = {401, 403, 429, 451}


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _path(value, parent):
    path = Path(value).expanduser()
    return (path if path.is_absolute() else parent / path).resolve()


def prepare_plan(plan_path):
    """Validate and materialize the two inputs locally; never access a service."""
    from typesafe_sdk import Choice
    from typesafe_sdk._core.json import serialize

    plan_path = Path(plan_path).resolve()
    plan = _read(plan_path)
    if tuple(plan.get("groups", GROUPS)) != GROUPS:
        raise ValueError("probe requires exactly A_original and B_task_history, once each")
    paths = {key: _path(plan[key], plan_path.parent) for key in
             ("source_trace", "source_manifest", "new_state_path", "output_dir", "ledger_path")}
    if "attempt_id" not in plan and "event_id" not in plan:
        raise ValueError("source attempt_id or event_id is required")
    starts, ends = [], []
    with paths["source_trace"].open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("event") == "http_attempt_start" and (
                    "attempt_id" not in plan or row.get("attempt_id") == plan["attempt_id"]) and (
                    "event_id" not in plan or row.get("event_id") == plan["event_id"]):
                starts.append(row)
            elif row.get("event") == "http_attempt_end" and starts and row.get("attempt_id") == starts[0]["attempt_id"]:
                ends.append(row)
    if len(starts) != 1:
        raise ValueError("source must identify exactly one HTTP request")
    source = starts[0]
    if len(ends) != 1 or ends[0].get("http_status") != 400 or "max_tokens_exceeded" not in json.dumps(ends[0]):
        raise ValueError("source must have a saved HTTP 400 max_tokens_exceeded response")
    request = source["request_body"]
    if not isinstance(request, dict) or set(request) != {"state", "model", "questions"}:
        raise ValueError("unsupported source request fields; cannot preserve with OfficialBackend")
    if not isinstance(request["state"], dict):
        raise ValueError("source state must be an object")
    questions = request["questions"]
    if not isinstance(questions, dict) or len(questions) != 1:
        raise ValueError("source must contain exactly one Choice question")
    stage, question = next(iter(questions.items()))
    if set(question) != {"type", "instructions", "criteria"} or question["type"] != "choice":
        raise ValueError("unsupported Choice fields")
    expected = json.loads(serialize({"state": request["state"], "model": request["model"],
        "questions": {stage: Choice(instructions=question["instructions"], criteria=question["criteria"])}}))
    if expected != request or list(expected["questions"][stage]["criteria"]) != list(question["criteria"]):
        raise ValueError("installed SDK cannot reproduce the frozen request exactly")
    manifest = _read(paths["source_manifest"])
    if manifest.get("run_id") != source.get("run_id"):
        raise ValueError("source manifest and trace run IDs differ")
    config = manifest.get("config", {})
    if config.get("model", request["model"]) != request["model"]:
        raise ValueError("source model differs from manifest")
    base_url = config.get("base_url", "https://api.typesafe.ai").rstrip("/")
    if source.get("endpoint") != base_url + "/v1/systemone":
        raise ValueError("source endpoint differs from manifest")
    if float(config.get("request_timeout_seconds", 30)) != 30:
        raise ValueError("this diagnostic requires the original 30-second Jev timeout")
    new_state = _read(paths["new_state_path"])
    if not isinstance(new_state, dict) or new_state.get("task") != request["state"].get("task"):
        raise ValueError("new state must preserve the exact source task")
    if "actions_so_far" not in new_state:
        raise ValueError("new state must contain actions_so_far")
    persisted = _read(paths["ledger_path"])
    for key in ("max_episodes", "max_http_attempts"):
        if type(persisted.get(key)) is not int or persisted[key] < 1:
            raise ValueError("existing ledger has invalid caps")
    requests = {GROUPS[0]: deepcopy(request), GROUPS[1]: {**deepcopy(request), "state": new_state}}
    backend_config = {"api_key_env": plan.get("api_key_env", config.get("api_key_env", "TYPESAFE_API_KEY")),
                      "base_url": base_url, "model": request["model"], "request_timeout_seconds": 30}
    preregistration = {"kind": "frozen_jev_state_probe_v1", "groups": list(GROUPS),
        "run_id": plan.get("run_id") or "jev_state_probe_" + uuid4().hex[:12],
        "paths": {key: str(path) for key, path in paths.items()},
        "source_attempt_id": source["attempt_id"], "source_event_id": source["event_id"],
        "source_response_event_id": ends[0]["event_id"], "source_run_id": source["run_id"],
        "source_hashes": {key: _sha(paths[key]) for key in ("source_trace", "source_manifest", "new_state_path")},
        "request_hashes": {group: digest(body) for group, body in requests.items()},
        "only_changed_request_field": "state", "question_id": stage,
        "candidate_order": list(question["criteria"]), "backend": backend_config,
        "limits": {"max_http_attempts": 2, "max_jev_http_attempts": 2, "wall_timeout_seconds": 120},
        "retry_max_retries": 0, "tools_executed": 0, "agent_runs": 0,
        "stop_remaining_on": sorted(ACCESS_STATUSES),
        "continue_after": "saved/live max_tokens_exceeded is the diagnostic condition, not a retry trigger",
        "live_integration_success": None, "task_success": None,
        "success_scope": "A valid Choice response only; returned tool calls are never executed"}
    return preregistration, requests, paths, persisted


class _ProbeRuntime(EpisodeRuntime):
    """Reserve caps before sends; allow parsing the last permitted response.

    ObservedTransport also checks after a response. The base implementation's
    count >= cap check would discard the second allowed parsed response, so this
    local diagnostic moves only that check to the reservation boundary.
    """
    def check(self):
        if self.remaining_seconds() <= 0:
            self.stop("wall_timeout")
        if self.counts["tool_attempts"] >= self.limits["max_tool_attempts"]:
            self.stop("max_tool_attempts")

    def reserve_http(self, role=None, logical_id=None):
        if self.counts["http_attempts"] >= self.limits["max_http_attempts"]:
            self.stop("max_http_attempts")
        return super().reserve_http(role, logical_id)


class _FrozenTransport(ObservedTransport):
    def handle_request(self, request):
        actual = json.loads(request.read())
        expected = self.runtime.expected_request
        if actual != expected or list(next(iter(actual["questions"].values()))["criteria"]) != list(
                next(iter(expected["questions"].values()))["criteria"]):
            raise ValueError("outgoing request differs from frozen input")
        return super().handle_request(request)


def _instrument(client, runtime):
    wrapped = {}
    def wrap(transport):
        if transport is None:
            return None
        if id(transport) not in wrapped:
            wrapped[id(transport)] = _FrozenTransport(transport, runtime)
        return wrapped[id(transport)]
    client._transport = wrap(client._transport)
    client._mounts = {pattern: wrap(transport) for pattern, transport in client._mounts.items()}


def run_probe(plan_path, *, transport=None):
    """Execute exactly one request per preregistered group unless blocked."""
    preregistration, requests, paths, persisted = prepare_plan(plan_path)
    output = paths["output_dir"]
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("probe output must be new or empty; evidence is never overwritten")
    output.mkdir(parents=True, exist_ok=True)
    key = os.environ.get(preregistration["backend"]["api_key_env"], "")
    ledger = LiveLedger(paths["ledger_path"], max_episodes=persisted["max_episodes"],
                        max_http_attempts=persisted["max_http_attempts"])
    summary = {"run_id": preregistration["run_id"], "groups": [], "status": "not_started",
               "tools_executed": 0, "agent_runs": 0, "task_success": None,
               "live_integration_success": None, "measurement": "frozen_request_acceptance_only"}
    reserved = False
    backend = None
    with TraceWriter(output / "trace.jsonl", summary["run_id"], secrets=[key]) as trace:
        def save(name, value):
            write_json(output / name, trace.sanitize(value))
        save("preregistration.json", preregistration)
        save("budget_before.json", ledger.snapshot())
        for group, body in requests.items():
            save(group + "_request.json", body)
        runtime = _ProbeRuntime(trace, preregistration["limits"], ledger=ledger)
        try:
            if not key:
                raise ValueError("missing_api_key")
            ledger.reserve_episode(summary["run_id"], "jev_state_probe", "diagnostic", summary["run_id"])
            reserved = True
            backend = OfficialBackend(preregistration["backend"], transport=transport)
            _instrument(backend.client._http_client, runtime)
            summary["sdk_version"] = backend.sdk_version
            with runtime.deadline_guard():
                for group, body in requests.items():
                    runtime.expected_request = body
                    stage, question = next(iter(body["questions"].items()))
                    role = "jev_tool_choice" if stage == "tool" else "jev_argument_choice"
                    result = {"group": group, "valid_choice": False}
                    summary["groups"].append(result)
                    trace.emit("probe_group_start", group=group, request_hash=digest(body))
                    try:
                        with runtime.logical(role):
                            decision = backend.decide(body["state"], stage, question["instructions"], question["criteria"])
                        result.update(status="accepted", valid_choice=True, selected_option=decision.selected_option,
                                      response=decision.record)
                    except DecisionError as exc:
                        result.update(status="decision_error", response=exc.record, error=str(exc))
                    except BudgetStop as exc:
                        result.update(status="budget_exhausted", reason=exc.reason)
                        raise
                    finally:
                        trace.emit("probe_group_end", **result)
                        save(group + "_result.json", result)
                    if result.get("response", {}).get("http_status") in ACCESS_STATUSES:
                        summary.update(status="access_blocked", stop_reason="http_" + str(result["response"]["http_status"]))
                        break
                else:
                    summary["status"] = "completed"
        except BudgetStop as exc:
            summary.update(status="budget_exhausted", stop_reason=exc.reason)
            trace.emit("probe_budget_stop", reason=exc.reason)
        except Exception as exc:
            summary.update(status="probe_error", error=f"{type(exc).__name__}: {exc}")
            trace.emit("probe_error", error=summary["error"])
        finally:
            if backend is not None:
                backend.close()
            if reserved:
                ledger.finish(summary["run_id"], summary["status"])
            summary["skipped_groups"] = [group for group in GROUPS if group not in {row["group"] for row in summary["groups"]}]
            summary["budget"] = runtime.snapshot()
            summary["http_attempts"] = runtime.counts["http_attempts"]
            summary["source_preserved"] = all(_sha(paths[key]) == value
                for key, value in preregistration["source_hashes"].items())
            save("budget_after.json", ledger.snapshot())
            save("summary.json", summary)
            trace.emit("probe_end", **summary)
        return trace.sanitize(summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    args = parser.parse_args()
    result = run_probe(args.plan)
    print(json.dumps({"status": result["status"], "http_attempts": result["http_attempts"]}))


if __name__ == "__main__":
    main()
