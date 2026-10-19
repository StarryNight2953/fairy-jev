"""Frozen suite controls and current-access handling, entirely offline."""
from copy import deepcopy
import json
from pathlib import Path
import socket

import pytest

from fairy.experiments import full_suite


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Full-suite unit tests must not perform network calls")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def base_config():
    return json.loads(Path("configs/jev/hn58_hybrid_sync_k4_qwen.json").read_text())


@pytest.mark.parametrize("architecture", full_suite.ARCHITECTURES)
def test_make_config_preserves_decision_controls_and_input(architecture):
    base = base_config()
    original = deepcopy(base)
    scenario = {"scenario_id": "scenario_l2_example", "clock_source": "checkpoint",
                "effective_start_time": 1786057131.7453332}
    config = full_suite.make_config(base, scenario, architecture)
    for key in ("model", "base_url", "api_key_env", "llm", "history", "hybrid",
                "candidate_catalog", "request_timeout_seconds"):
        assert config[key] == original[key]
    assert config["architecture"] == architecture and config["seed"] == 1904
    assert config["scenario_id"] == scenario["scenario_id"]
    assert config["evaluation_profile"] == config["evaluator_version"] == "scenario_native_v1"
    config["candidate_catalog"]["ranges"]["values"].append([1, 2])
    config["hybrid"]["update_interval"] = 100
    assert base == original


def test_hn58_keeps_its_existing_evaluator_and_all_limits_are_synced():
    config = full_suite.make_config(base_config(), {
        "scenario_id": full_suite.HN58, "clock_source": "checkpoint",
        "effective_start_time": 1786057131.7453332}, "hybrid")
    assert config["evaluation_profile"] == config["evaluator_version"] == "hn58_semantic_v2"
    limits = config["experiment_limits"]
    assert limits == full_suite.LIMITS
    assert config["budgets"]["max_steps"] == limits["max_decision_rounds"]
    assert config["budgets"]["max_tool_calls"] == limits["max_tool_attempts"]
    assert config["budgets"]["max_decision_calls"] == limits["max_jev_http_attempts"]
    assert config["budgets"]["wall_timeout_seconds"] == limits["wall_timeout_seconds"]
    assert limits["max_updater_attempts"] >= 2 * limits["max_updates"]
    assert limits["max_http_attempts"] >= limits["max_jev_http_attempts"] + limits["max_updater_attempts"]


def test_make_config_uses_current_inventory_clock_not_stale_hn58_time():
    base = base_config()
    base["effective_start_time"] = 1.0
    row = {"scenario_id": "scenario_full_season_example", "clock_source": "declared",
           "effective_start_time": 1777935600.0}
    config = full_suite.make_config(base, row, "jev")
    assert config["effective_start_time"] == row["effective_start_time"]
    assert config["clock_source"] == "declared"
    assert base["effective_start_time"] == 1.0


def event(event_id, role, http_status=None, error_type=None, *, kind="http_attempt_end"):
    return {"event": kind, "event_id": event_id, "role": role,
            "status": "ok" if http_status == 200 else "http_error" if http_status else "error",
            "http_status": http_status, "error_type": error_type,
            "endpoint": "https://example.invalid/model"}


@pytest.mark.parametrize("http_status", [401, 403, 429, 451, 500, 503])
def test_current_access_refusal_stops_only_affected_service(http_status):
    rows = [event(2, "jev_tool_choice", http_status), event(5, "llm_context_update", 200)]
    blockers = full_suite.service_blockers(rows)
    assert len(blockers) == 1
    assert blockers[0]["service"] == "typesafe"
    assert blockers[0]["http_status"] == http_status and blockers[0]["event_id"] == 2


@pytest.mark.parametrize("error_type", ["ConnectError", "ProxyError", "ConnectTimeout"])
def test_connection_failure_and_same_service_recovery(error_type):
    rows = [event(2, "llm_context_update", error_type=error_type)]
    assert full_suite.service_blockers(rows)[0]["service"] == "qwen"
    rows.append(event(3, "jev_tool_choice", 200))
    assert full_suite.service_blockers(rows)[0]["service"] == "qwen"
    rows.append(event(4, "llm_native_decision", 200))
    assert full_suite.service_blockers(rows) == []


def test_recovered_http_error_and_new_refusal_use_latest_actual_evidence():
    rows = [event(1, "jev_tool_choice", 503), event(2, "jev_parameter_choice", 200),
            event(3, "jev_tool_choice", 451)]
    assert full_suite.service_blockers(rows) == [{
        "service": "typesafe", "event_id": 3, "http_status": 451,
        "error_type": None, "endpoint": "https://example.invalid/model",
        "reason": "current_run_access_failure"}]


def test_non_http_history_and_request_specific_errors_do_not_block_service():
    rows = [event(1, "jev_tool_choice", 451, kind="historical_access_review"),
            event(2, "jev_tool_choice", 400),
            event(3, "llm_native_decision", error_type="BudgetStop")]
    assert full_suite.service_blockers(rows) == []


def test_summary_keeps_unknown_separate_from_failure(tmp_path):
    (tmp_path / "results").mkdir()
    full_suite.write_json(tmp_path / "preregistration.json", {"slots": [{"level": "L2"}] * 3})
    for index, success in enumerate((True, False, None)):
        full_suite.write_json(tmp_path / "results" / f"{index}.json", {
            "scenario_id": f"fixture_{index}", "level": "L2", "architecture": "jev",
            "evaluation": {"execution_status": "completed", "task_success": success,
                           "live_integration_success": True, "model_http_attempts": 2}})
    report = full_suite.summarize(tmp_path)
    group = report["groups"]["L2/jev"]
    assert (group["task_true"], group["task_false"], group["task_unknown"]) == (1, 1, 1)
    assert group["http_attempts"] == 6


def test_interrupted_slot_preserves_ledger_usage_and_partial_trace(tmp_path):
    from fairy.experiments.budget import LiveLedger
    ledger = LiveLedger(tmp_path / "ledger.json", max_episodes=3, max_http_attempts=10)
    ledger.reserve_episode("historical", "jev", "old", "old-slot")
    ledger.reserve_http("historical")
    ledger.finish("historical", "completed")
    old_row = deepcopy(ledger.snapshot()["episodes"][0])
    ledger.reserve_episode("partial", "hybrid", "full_suite_once", "batch-slot")
    ledger.reserve_http("partial")
    ledger.reserve_http("partial")
    path = tmp_path / "live" / "partial"
    path.mkdir(parents=True)
    trace = path / "trace.jsonl"
    trace.write_text(json.dumps(event(1, "jev_tool_choice", 451)) + "\n" +
                     json.dumps({"event": "tool_end", "executed": True}) + "\n{\"unfinished\":")
    original_trace = trace.read_bytes()
    row = {"slot": "slot", "ledger_slot": "batch-slot", "architecture": "hybrid"}
    record = full_suite.interrupted_record(tmp_path, row, ledger)
    assert record["model_http_attempts"] == 2 and record["tool_call_count"] == 1
    assert record["path"] == str(path) and record["task_success"] is None
    assert record["service_blockers"][0]["http_status"] == 451
    assert record["execution_status"] == "interrupted_not_retried"
    snapshot = ledger.snapshot()
    assert snapshot["http_attempts"] == 3 and snapshot["episodes"][0] == old_row
    assert snapshot["episodes"][1]["status"] == "interrupted_not_retried"
    assert trace.read_bytes() == original_trace
    assert full_suite.interrupted_record(tmp_path, row, ledger) == record
    assert ledger.snapshot() == snapshot


@pytest.mark.parametrize("already_exists", ["result", "claim"])
def test_child_cannot_replace_or_repeat_slot(tmp_path, monkeypatch, already_exists):
    slot = "once"
    config = {"fixture": True}
    row = {"slot": slot, "config_hash": full_suite.digest(config)}
    full_suite.write_json(tmp_path / "preregistration.json", {"code_commit": "frozen", "slots": [row]})
    full_suite.write_json(tmp_path / "configs" / f"{slot}.json", config)
    full_suite.write_json(tmp_path / "started" / f"{slot}.json", {"slot": slot})
    preserved = tmp_path / ("results" if already_exists == "result" else "worker_claims") / f"{slot}.json"
    full_suite.write_json(preserved, {"original": True})
    before = preserved.read_bytes()
    monkeypatch.setattr(full_suite, "_redact_streams", lambda: None)
    monkeypatch.setattr(full_suite, "git", lambda *args: "frozen" if args[0] == "rev-parse" else "")
    monkeypatch.setattr(full_suite, "run_episode", lambda *a, **k: pytest.fail("Repeated slot reached episode execution"))
    with pytest.raises((AssertionError, FileExistsError)):
        full_suite.child(tmp_path, slot)
    assert preserved.read_bytes() == before


def test_atomic_record_publishes_complete_json(tmp_path, monkeypatch):
    path = tmp_path / "record.json"
    old = {"version": 1}
    full_suite.write_json(path, old)
    original_replace = full_suite.os.replace
    observations = []
    def inspect_before_replace(source, destination):
        observations.append((full_suite.load(destination), full_suite.load(source)))
        original_replace(source, destination)
    monkeypatch.setattr(full_suite.os, "replace", inspect_before_replace)
    new = {"version": 2, "data": list(range(100))}
    full_suite.write_json(path, new)
    assert observations == [(old, new)] and full_suite.load(path) == new
    assert list(tmp_path.glob("*.tmp")) == []


def test_second_scheduler_cannot_enter_same_batch(tmp_path, monkeypatch):
    import fcntl
    monkeypatch.setattr(full_suite, "_run_locked", lambda *a: pytest.fail("Second scheduler entered batch"))
    with (tmp_path / "scheduler.lock").open("a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            full_suite.run(tmp_path)
