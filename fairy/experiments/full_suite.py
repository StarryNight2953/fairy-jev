"""One frozen live sample per architecture/scenario, with resumable evidence.

This orchestrates existing agents. It never repairs model actions, retries an
episode, changes candidates, or turns a weak native validator into task success.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import csv
import fcntl
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from fairy.agents.Jev.trace import digest, write_json as _write_json
from fairy.experiments.budget import LiveLedger
from fairy.experiments.episode import ROOT, artifact_hashes, git, run_episode, verify_and_replay

ARCHITECTURES = ("llm_native", "jev", "hybrid")
HN58 = "scenario_l1_three_cultivar_hn58_action_ready_irrigation"
LEDGER = ROOT / "runs/agent_comparison/live_budget_20261018.json"
LIMITS = {"max_tool_attempts": 300, "max_decision_rounds": 300,
          "max_http_attempts": 900, "max_jev_http_attempts": 600,
          "max_updater_attempts": 160, "max_updates": 80,
          "wall_timeout_seconds": 1800}


def now():
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


def load(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, data):
    """Publish whole records atomically while parent reads child progress."""
    path = Path(path)
    temporary = path.with_name(path.name + f".{os.getpid()}.{uuid4().hex}.tmp")
    _write_json(temporary, data)
    os.replace(temporary, path)


def make_config(base, scenario, architecture):
    from fairy.experiments.suite_inventory import config_for_scenario
    cfg = deepcopy(base)
    cfg.update(scenario_id=scenario["scenario_id"], architecture=architecture,
               seed=1904, scenario_kwargs={}, init_kwargs={},
               clock_source=scenario["clock_source"], global_live_ledger=str(LEDGER))
    profile = "hn58_semantic_v2" if scenario["scenario_id"] == HN58 else "scenario_native_v1"
    cfg.update(evaluation_profile=profile, evaluator_version=profile)
    cfg["experiment_limits"] = dict(LIMITS)
    cfg["budgets"].update(max_steps=300, max_tool_calls=300,
                          max_decision_calls=600, wall_timeout_seconds=1800)
    cfg["effective_limit_note"] = "User-authorized full-suite budget expansion; fixed before samples; no retry episodes."
    cfg["configuration_note"] = "Existing model, prompt, K, candidate space and agent loop preserved; new suite runner and evaluation applicability only."
    return config_for_scenario(scenario, cfg)


def service_blockers(events):
    """Only current real access failures stop a service, never old 451 rows."""
    failures = {}
    for e in events:
        if e.get("event") != "http_attempt_end":
            continue
        service = "typesafe" if str(e.get("role", "")).startswith("jev_") else "qwen"
        status = e.get("http_status")
        if e.get("status") == "ok":
            # A recovered transient error is not an outage.
            failures.pop(service, None)
        elif status in {401, 403, 429, 451} or (isinstance(status, int) and status >= 500) or e.get("error_type") in {"ConnectError", "ProxyError", "ConnectTimeout"}:
            failures[service] = {"service": service, "event_id": e["event_id"],
                                 "http_status": status, "error_type": e.get("error_type"),
                                 "endpoint": e.get("endpoint"), "reason": "current_run_access_failure"}
    return list(failures.values())


def reads_events(path):
    rows = []
    for line in Path(path).read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            break  # Retain an interrupted final line in the original file.
    return rows


def _redact_streams():
    secrets = [v for k, v in os.environ.items() if k.endswith(("API_KEY", "API_TOKEN", "ACCESS_TOKEN", "SECRET")) and v]

    class Redacted:
        def __init__(self, wrapped): self.wrapped = wrapped
        def write(self, value):
            for secret in secrets: value = value.replace(secret, "[REDACTED]")
            return self.wrapped.write(re.sub(r"(?i)Bearer\s+[^\s\"']+", "Bearer [REDACTED]", value))
        def flush(self): return self.wrapped.flush()
        def __getattr__(self, name): return getattr(self.wrapped, name)

    sys.stdout, sys.stderr = Redacted(sys.stdout), Redacted(sys.stderr)


def prepare(out, include_legacy=False):
    from fairy.experiments.suite_inventory import discover_scenarios
    assert not git("status", "--porcelain"), "Freeze a clean version before preparing"
    out.mkdir(parents=True, exist_ok=False)
    inventory = discover_scenarios(include_legacy=include_legacy)
    scenarios = sorted(inventory, key=lambda r: (r["scenario_id"] != HN58, r["level"], r["scenario_id"]))
    write_json(out / "scenario_inventory.json", scenarios)
    historical = {}
    for base in (ROOT / "runs/agent_comparison", ROOT / "runs/semantic_audit"):
        for p in base.rglob("*"):
            if p.is_file() and p != LEDGER and out not in p.parents and p.suffix != ".pyc":
                historical[str(p)] = sha(p)
    write_json(out / "preservation_before.json", {"files": historical})
    before = load(LEDGER)
    assert not any(e["status"] in {"reserved", "running"} for e in before["episodes"])
    assert sum(e["http_attempts"] for e in before["episodes"]) == before["http_attempts"]
    write_json(out / "budget_before.json", before)
    bases = {a: load(ROOT / "runs/agent_comparison/hn58_tool_description_v2_20261019" / f"config_{a}.json") for a in ARCHITECTURES}
    slots = []
    for index, row in enumerate(scenarios):
        # Rotate architecture order across scenarios to avoid one fixed ordering.
        order = ARCHITECTURES[index % 3:] + ARCHITECTURES[:index % 3]
        for architecture in order:
            slot = f"{row['scenario_id']}__{architecture}__seed1904"
            cfg = make_config(bases[architecture], row, architecture)
            write_json(out / "configs" / f"{slot}.json", cfg)
            slots.append({"slot": slot, "ledger_slot": f"{out.name}__{slot}",
                          "architecture": architecture, "scenario_id": row["scenario_id"],
                          "level": row["level"], "config_hash": digest(cfg)})
    prereg = {"experiment_id": out.name, "registered_at": now(), "code_commit": git("rev-parse", "HEAD"),
              "scope": "all current registered L1/L2 plus formal L3; legacy optional and labelled",
              "include_legacy": include_legacy, "scenario_counts": dict(Counter(r["level"] for r in scenarios)),
              "scenario_count": len(scenarios), "planned_samples": len(slots), "repeats": 1, "seed": 1904,
              "pythonhashseed": "1904", "remote_seed": "not supplied", "slots": slots,
              "limits": LIMITS, "maximum_concurrency": 3, "max_parallel_per_architecture": 1,
              "evaluation": "HN58 keeps exact semantic_v2; all other task_success unknown, native validator and workflow scores retained without invented thresholds",
              "rules": ["No replacement samples, including failures and interrupted slots", "No model action replacement or automatic repair",
                        "Keep prompt/K/candidates/report template/agent loop/model sampling unchanged",
                        "No oracle action execution; static oracle used only after execution for existing descriptive metrics",
                        "L1/L2 checkpoint clocks, L3 declared clocks", "Service access errors stop dependent future slots; no route changes"],
              "known_existing_limitations": ["Jev report candidate is irrigation-specific", "Finite candidate values can omit other tasks' required values",
                        "Default waits may be too short for full-season tasks", "Weak or absent validators do not establish task completion",
                        "Native round count is descriptive; its unchanged loop is bounded by HTTP/tools/wall time"],
              "cost": {"status": "unknown", "no_verified_price_schedule": True}}
    prereg["worktrees"] = [{"path": str(ROOT.parent/name),
                            "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT.parent/name, text=True).strip(),
                            "status": subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT.parent/name, text=True).strip()}
                           for name in ("jev-mvp", "llm-jev-hybrid", "hn58-reproduce")]
    write_json(out / "preregistration.json", prereg)
    write_json(out / "budget_authorization.json", {"authorized": True, "user_instruction": "放开预算执行一次覆盖三种实现以及所有L1,L2,L3场景的实验",
               "ledger_id": before["ledger_id"], "preserve_existing_rows_and_counts": True,
               "old_max_episodes": before["max_episodes"], "new_max_episodes": len(before["episodes"]) + len(slots),
               "old_max_http_attempts": before["max_http_attempts"],
               "new_max_http_attempts": max(before["max_http_attempts"], before["http_attempts"] + len(slots)*LIMITS["max_http_attempts"]),
               "single_episode_limits_before": bases["hybrid"]["experiment_limits"], "single_episode_limits_after": LIMITS})
    print(json.dumps({"prepared": str(out), "scenarios": len(scenarios), "slots": len(slots)}, ensure_ascii=False), flush=True)


def preflight(out):
    """Each fixture gets its own process; no model or oracle action is executed."""
    from concurrent.futures import ThreadPoolExecutor
    rows = load(out / "scenario_inventory.json")
    jobs = []
    def isolated(row):
        cmd = [sys.executable, "-m", "fairy.experiments.full_suite", "preflight-child", "--output", str(out), "--slot", row["scenario_id"]]
        try:
            proc = subprocess.run(cmd, cwd=ROOT, env={**os.environ, "PYTHONHASHSEED": "1904"}, capture_output=True, text=True, timeout=180)
            if proc.returncode:
                return {"scenario_id": row["scenario_id"], "passed": False, "error": f"fixture subprocess exit {proc.returncode}", "details": proc.stderr[-2000:]}
            return load(out / "preflight" / f"{row['scenario_id']}.json")
        except subprocess.TimeoutExpired:
            return {"scenario_id": row["scenario_id"], "passed": False, "error": "fixture timeout; subprocess terminated"}
    with ThreadPoolExecutor(max_workers=3) as pool:
        for row in rows:
            jobs.append((row, pool.submit(isolated, row)))
        results = []
        for row, future in jobs:
            try:
                result = future.result()
            except Exception as exc:
                result = {"scenario_id": row["scenario_id"], "passed": False, "error": f"{type(exc).__name__}: {exc}"}
            write_json(out / "preflight" / f"{row['scenario_id']}.json", result)
            results.append(result)
            if len(results) % 20 == 0:
                print(json.dumps({"preflight_completed": len(results), "planned": len(rows)}), flush=True)
    summary = {"scenarios": len(results), "passed": sum(r.get("passed") is True for r in results),
               "failed": [r for r in results if r.get("passed") is not True], "model_http_attempts": 0}
    write_json(out / "preflight_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


def preflight_child(out, scenario_id):
    import socket
    from fairy.experiments.suite_inventory import preflight_one
    def deny(*args, **kwargs): raise AssertionError("Offline fixture preflight: network denied")
    socket.socket.connect = deny; socket.socket.connect_ex = deny; socket.create_connection = deny
    row = next(r for r in load(out / "scenario_inventory.json") if r["scenario_id"] == scenario_id)
    cfg = load(out / "configs" / f"{scenario_id}__jev__seed1904.json")
    result = preflight_one(row, cfg)
    result["passed"] = result.get("status") == "passed"
    write_json(out / "preflight" / f"{scenario_id}.json", result)


def amend_budget(out):
    before, auth = load(out / "budget_before.json"), load(out / "budget_authorization.json")
    ledger = LiveLedger(LEDGER, max_episodes=auth["new_max_episodes"], max_http_attempts=auth["new_max_http_attempts"])
    with ledger._locked() as data:
        assert data["ledger_id"] == before["ledger_id"]
        assert data["episodes"][:len(before["episodes"])] == before["episodes"]
        data["max_episodes"] = max(data["max_episodes"], auth["new_max_episodes"])
        data["max_http_attempts"] = max(data["max_http_attempts"], auth["new_max_http_attempts"])
    write_json(out / "budget_authorized.json", ledger.snapshot())
    return ledger


def child(out, slot):
    _redact_streams()
    pre = load(out / "preregistration.json")
    row = next(r for r in pre["slots"] if r["slot"] == slot)
    cfg = load(out / "configs" / f"{slot}.json")
    assert digest(cfg) == row["config_hash"]
    assert git("rev-parse", "HEAD") == pre["code_commit"] and not git("status", "--porcelain")
    assert (out / "started" / f"{slot}.json").is_file(), "Worker must belong to registered scheduler start"
    assert not (out / "results" / f"{slot}.json").exists(), "Never replace an existing sample"
    (out / "worker_claims").mkdir(exist_ok=True)
    with (out / "worker_claims" / f"{slot}.json").open("x") as f:
        json.dump({"pid": os.getpid(), "started_at": now()}, f)
    identity = next(r for r in load(out / "scenario_inventory.json") if r["scenario_id"] == row["scenario_id"])
    for path_key, hash_key in (("class_source_path", "class_source_sha256"), ("checkpoint_path", "checkpoint_sha256")):
        if identity.get(path_key): assert sha(identity[path_key]) == identity[hash_key], "Frozen scenario input changed"
    auth = load(out / "budget_authorization.json")
    ledger = LiveLedger(LEDGER, max_episodes=auth["new_max_episodes"], max_http_attempts=auth["new_max_http_attempts"])
    output, evaluation = run_episode(cfg, out / "live", architecture=row["architecture"], stage="full_suite_once",
                                     experiment_id=out.name, slot=row["ledger_slot"], ledger=ledger, require_clean=True)
    evaluation = load(output / "evaluation.json")  # Disk copy is already secret-sanitized.
    # Write the live result before offline verification, so a replay defect never
    # discards or triggers a rerun of the real episode.
    record = {**row, "path": str(output), "finished_at": now(), "evaluation": evaluation,
              "service_blockers": service_blockers(reads_events(output / "trace.jsonl"))}
    manifest, fixture = load(output / "manifest.json"), load(out / "preflight" / f"{row['scenario_id']}.json")
    record["fixture_identity_matches_preflight"] = {
        "catalog": manifest.get("catalog_hash") == fixture.get("catalog_sha256"),
        "checkpoint": manifest.get("checkpoint_hash") == identity.get("checkpoint_sha256"),
        "initial_time": manifest.get("effective_start_time") == identity.get("effective_start_time")}
    write_json(out / "results" / f"{slot}.json", record)
    try:
        evaluation = verify_and_replay(output, cfg)
    except Exception as exc:
        record["verification_error"] = f"{type(exc).__name__}: {exc}"
    record["evaluation"] = evaluation
    record["service_blockers"] = service_blockers(reads_events(output / "trace.jsonl"))
    write_json(out / "results" / f"{slot}.json", record)


def run(out):
    with (out / "scheduler.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _run_locked(out)


def _run_locked(out):
    pre = load(out / "preregistration.json")
    assert pre["code_commit"] == git("rev-parse", "HEAD") and not git("status", "--porcelain")
    assert load(out / "regression_result.json")["exit_code"] == 0
    load(out / "preflight_summary.json")
    markers = list((out / "started").glob("*.json")) + list((out / "worker_claims").glob("*.json"))
    for marker in markers:
        data = load(marker)
        pid = data.get("worker_pid", data.get("pid"))
        if pid:
            command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
            if "fairy.experiments.full_suite child" in command and str(out) in command:
                raise RuntimeError("A prior worker is still running; preserve it and wait before resuming this scheduler")
    ledger = amend_budget(out)
    for name in ("started", "results", "logs"):
        (out / name).mkdir(exist_ok=True)
    active, blocked = {}, {}
    # Resume only unstarted slots. Existing records are never replaced.
    for p in sorted((out / "results").glob("*.json")):
        for blocker in load(p).get("service_blockers", []):
            blocked[blocker["service"]] = {**blocker, "source_result": str(p)}
    pending = [r for r in pre["slots"] if not (out / "results" / f"{r['slot']}.json").exists()]
    while pending or active:
        busy = {r["architecture"] for _, r, _, _ in active.values()}
        for row in list(pending):
            slot = row["slot"]
            if len(active) >= 3 or row["architecture"] in busy:
                continue
            pending.remove(row)
            result_path = out / "results" / f"{slot}.json"
            services = {"qwen"} if row["architecture"] == "llm_native" else {"typesafe"} if row["architecture"] == "jev" else {"qwen", "typesafe"}
            reasons = [blocked[s] for s in services if s in blocked]
            fixture = load(out / "preflight" / f"{row['scenario_id']}.json")
            started = out / "started" / f"{slot}.json"
            status = "interrupted_not_retried" if started.exists() else "blocked_external_service" if reasons else "blocked_environment" if fixture.get("passed") is not True else None
            if status:
                if started.exists():
                    record = interrupted_record(out, row, ledger)
                else:
                    record = {**row, "execution_status": status, "blockers": reasons,
                                         "preflight_error": fixture.get("error"), "task_success": None,
                                         "live_integration_success": None, "new_model_http_attempts": 0}
                write_json(result_path, record)
                for blocker in record.get("service_blockers", []):
                    blocked[blocker["service"]] = {**blocker, "source_result": str(result_path)}
                continue
            with started.open("x") as f:
                json.dump({"started_at": now(), **row}, f)
            stream = (out / "logs" / f"{slot}.log").open("x")
            env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONHASHSEED": "1904"}
            command = [sys.executable, "-m", "fairy.experiments.full_suite", "child", "--output", str(out), "--slot", slot]
            proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT)
            write_json(started, {**load(started), "worker_pid": proc.pid, "command": command})
            active[proc.pid] = (proc, row, stream, time.monotonic())
            busy.add(row["architecture"])
        for pid, (proc, row, stream, started_at) in list(active.items()):
            # Whole live episode is bounded internally. This outer deadline only
            # handles an initialization/finalization/replay hang.
            if proc.poll() is None and time.monotonic()-started_at > LIMITS["wall_timeout_seconds"] + 600:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if proc.poll() is None:
                continue
            stream.close()
            result_path = out / "results" / f"{row['slot']}.json"
            if not result_path.exists():
                record = interrupted_record(out, row, ledger)
                record.update(execution_status="worker_failed_not_retried", worker_exit_code=proc.returncode)
                write_json(result_path, record)
            record = load(result_path)
            for blocker in record.get("service_blockers", []):
                blocked[blocker["service"]] = {**blocker, "source_result": str(result_path)}
            del active[pid]
            e = record.get("evaluation", record)
            print(json.dumps({"slot": row["slot"], "status": e.get("execution_status"), "task_success": e.get("task_success"),
                              "http": e.get("model_http_attempts", 0), "remaining": len(pending), "active": len(active)}, ensure_ascii=False), flush=True)
            summarize(out, finished=False)
        write_json(out / "progress.json", {"at": now(), "pending": len(pending), "active": [r["slot"] for _, r, _, _ in active.values()],
                   "completed_records": len(list((out / "results").glob("*.json"))), "planned": len(pre["slots"]), "service_blockers": blocked})
        if active:
            time.sleep(1)
    write_json(out / "budget_after.json", ledger.snapshot())
    summarize(out, finished=True)
    original = load(out / "preservation_before.json")["files"]
    write_json(out / "preservation_after.json", {"files_checked": len(original), "changed": [p for p, h in original.items() if not Path(p).exists() or sha(p) != h]})
    artifact_hashes(out)


def interrupted_record(out, row, ledger):
    reserved = next((r for r in ledger.snapshot()["episodes"] if r["slot"] == row["ledger_slot"]), None)
    path = out / "live" / reserved["run_id"] if reserved else None
    events = reads_events(path / "trace.jsonl") if path and (path / "trace.jsonl").exists() else []
    if reserved and reserved["status"] in {"reserved", "running"}:
        ledger.finish(reserved["run_id"], "interrupted_not_retried")
    return {**row, "execution_status": "interrupted_not_retried", "path": str(path) if path else None,
            "model_http_attempts": reserved["http_attempts"] if reserved else 0,
            "tool_call_count": sum(e.get("event") == "tool_end" and bool(e.get("executed")) for e in events),
            "task_success": None, "live_integration_success": None,
            "service_blockers": service_blockers(events), "partial_evidence_preserved": True}


def summarize(out, finished=False):
    pre = load(out / "preregistration.json")
    rows = []
    for p in sorted((out / "results").glob("*.json")):
        r = load(p); e = r.get("evaluation", r)
        metrics = load(Path(r["path"]) / "metrics.json") if r.get("path") and (Path(r["path"]) / "metrics.json").exists() else {}
        rows.append({"scenario_id": r["scenario_id"], "level": r["level"], "architecture": r["architecture"],
                     "execution_status": e.get("execution_status"), "live_integration_success": e.get("live_integration_success"),
                     "task_success": e.get("task_success"), "native_contract_success": e.get("native_contract_success", (e.get("native_validation") or {}).get("success")),
                     "stop_reason": e.get("stop_reason"), "failure_layer": e.get("failure_layer"),
                     "model_http_attempts": e.get("model_http_attempts", 0), "tool_call_count": e.get("tool_call_count", 0),
                     "wall_seconds": e.get("wall_elapsed_seconds"), "input_tokens": metrics.get("input_tokens"), "output_tokens": metrics.get("output_tokens"),
                     "first_issue": e.get("first_issue"), "workflow_metrics": e.get("workflow_metrics"),
                     "trace_verification": (e.get("trace_verification") or {}).get("passed"),
                     "replay_verification": (e.get("replay_verification") or {}).get("passed"),
                     "path": r.get("path"), "record": str(p)})
    groups = {}
    for level in sorted({r["level"] for r in pre["slots"]}):
        for arch in ARCHITECTURES:
            selected = [r for r in rows if r["level"] == level and r["architecture"] == arch]
            groups[f"{level}/{arch}"] = {"recorded": len(selected), "status_counts": dict(Counter(r["execution_status"] for r in selected)),
                "task_true": sum(r["task_success"] is True for r in selected), "task_false": sum(r["task_success"] is False for r in selected),
                "task_unknown": sum(r["task_success"] is None for r in selected),
                "integration_true": sum(r["live_integration_success"] is True for r in selected),
                "http_attempts": sum(r["model_http_attempts"] for r in selected)}
    summary = {"experiment_id": out.name, "updated_at": now(), "finished": finished,
               "planned": len(pre["slots"]), "recorded": len(rows), "groups": groups, "rows": rows,
               "interpretation": "One sample per scenario/architecture. Unknown task_success is not failure or success. Bounded stops are censored runs. No reliable model-price estimate."}
    write_json(out / "comparison.json", summary)
    if rows:
        with (out / "comparison.csv").open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "preflight", "preflight-child", "run", "child", "report"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include-legacy", action="store_true")
    parser.add_argument("--slot")
    args = parser.parse_args(); out = args.output.resolve()
    if args.action == "prepare": prepare(out, args.include_legacy)
    elif args.action == "preflight": preflight(out)
    elif args.action == "preflight-child": preflight_child(out, args.slot)
    elif args.action == "run": run(out)
    elif args.action == "child": child(out, args.slot)
    else: print(json.dumps(summarize(out), ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
