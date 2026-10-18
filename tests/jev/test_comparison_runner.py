"""Episode/experiment boundaries, without paid requests or real credentials."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from fairy.agents.Jev.trace import TraceWriter
from fairy.experiments import agent_comparison, episode
from fairy.experiments.budget import BudgetStop, EpisodeRuntime, LiveLedger


def config():
    return json.loads(Path("configs/jev/hn58_hybrid_sync_k4.json").read_text())


def test_missing_credentials_is_blocked_null_and_spends_no_live_allowance(tmp_path, monkeypatch):
    cfg = config()
    cfg["api_key_env"] = "ABSENT_JEV_TEST_KEY"
    cfg["llm"]["api_key_env"] = "ABSENT_LLM_TEST_KEY"
    monkeypatch.delenv("ABSENT_JEV_TEST_KEY", raising=False)
    monkeypatch.delenv("ABSENT_LLM_TEST_KEY", raising=False)
    ledger = LiveLedger(tmp_path / "ledger.json")
    output, result = episode.run_episode(cfg, tmp_path / "runs", architecture="hybrid", mode="live", ledger=ledger)
    assert result["execution_status"] == "blocked"
    assert result["stop_reason"] == "missing_credentials"
    assert result["task_success"] is result["live_integration_success"] is None
    assert result["model_http_attempts"] == result["tool_call_count"] == 0
    assert ledger.snapshot()["episodes"] == []
    metrics = json.loads((output / "metrics.json").read_text())
    assert metrics["input_tokens"] is metrics["output_tokens"] is None
    assert metrics["cost"]["amount"] is None
    assert metrics["cost"]["status"] == "unavailable"


def test_evaluator_exception_cannot_be_overwritten_by_model_finish(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError("injected evaluator defect")
    monkeypatch.setattr(episode, "evaluate_task", fail)
    output, result = episode.run_episode(config(), tmp_path, architecture="jev", mode="scripted")
    assert result["stop_reason"] == "model_finish"
    assert result["execution_status"] == "integration_error"
    assert result["failure_layer"] == "evaluation"
    assert result["scripted_task_success"] is None
    assert result["model_http_attempts"] == 0
    assert result["tool_call_count"] == 10
    assert "injected evaluator defect" in result["evaluation_error"]
    assert (output / "workflow.json").exists()


def test_ledger_caps_persist_across_instances_and_smoke_slot_rename(tmp_path):
    path = tmp_path / "ledger.json"
    first = LiveLedger(path, max_episodes=2, max_http_attempts=3)
    first.reserve_episode("one", "jev", "smoke", "smoke_original")
    first.reserve_http("one")
    second = LiveLedger(path, max_episodes=100, max_http_attempts=100)
    with pytest.raises(BudgetStop, match="smoke_slot_already_consumed"):
        second.reserve_episode("renamed", "jev", "smoke", "smoke_renamed")
    second.reserve_episode("two", "hybrid", "formal", "formal_hybrid")
    second.reserve_http("two")
    second.reserve_http("two")
    with pytest.raises(BudgetStop, match="total_model_http_attempts"):
        second.reserve_http("two")
    with pytest.raises(BudgetStop, match="total_live_episodes"):
        second.reserve_episode("three", "llm_native", "formal", "extra")
    assert first.snapshot()["http_attempts"] == 3


def comparison_fixture(tmp_path, monkeypatch, *, result_status="completed", verification=True):
    cfg = json.loads(Path("configs/experiments/hn58_three_agents_v1.json").read_text())
    cfg.update(output_dir=str(tmp_path / "out"), global_live_ledger=str(tmp_path / "ledger.json"),
               formal_order=[["llm_native", "jev", "hybrid"]], block_seeds=[1904])
    identity = {"commit": "R"}
    monkeypatch.setattr(agent_comparison, "git", lambda *args: "" if args[0] == "status" else identity["commit"])
    calls = []
    result = {"execution_status": result_status, "task_success": False,
        "scripted_task_success": None, "live_integration_success": result_status == "completed",
        "stop_reason": "finish" if result_status == "completed" else "runner_error", "failure_layer": None,
        "model_http_attempts": 0, "model_request_count": 0, "tool_call_count": 0, "wall_elapsed_seconds": 0.1,
        "trace_verification": {"passed": verification}, "replay_verification": {"status": "not_applicable"}}
    def run(cfg, output_dir, **kwargs):
        calls.append(kwargs)
        return tmp_path / kwargs["slot"], deepcopy(result)
    monkeypatch.setattr(agent_comparison, "run_episode", run)
    monkeypatch.setattr(agent_comparison, "verify_and_replay", lambda *args: deepcopy(result))
    return cfg, identity, calls


@pytest.mark.parametrize("status,verification", [("integration_error", True), ("completed", False)])
def test_formal_recording_failure_stops_batch_across_cli_restarts(tmp_path, monkeypatch, status, verification):
    cfg, _, calls = comparison_fixture(tmp_path, monkeypatch, result_status=status, verification=verification)
    agent_comparison.run_stage(cfg, config(), "formal")
    assert len(calls) == 1
    try:
        agent_comparison.run_stage(cfg, config(), "formal")
    except ValueError:
        pass  # Rejecting a stopped batch is also a valid explicit stop policy.
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["commit", "config"])
def test_formal_index_rejects_identity_drift_even_if_tag_moves(tmp_path, monkeypatch, change):
    cfg, identity, _ = comparison_fixture(tmp_path, monkeypatch)
    base = config()
    agent_comparison.run_stage(cfg, base, "formal")
    if change == "commit":
        identity["commit"] = "R2"
    else:
        base["hybrid"]["update_interval"] = 5
    with pytest.raises(ValueError):
        agent_comparison.run_stage(cfg, base, "formal")


def test_formal_dirty_commit_rejected_before_any_episode(tmp_path, monkeypatch):
    cfg, _, calls = comparison_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(agent_comparison, "git", lambda *args: " M fairy/file.py" if args[0] == "status" else "R")
    with pytest.raises(ValueError, match="clean"):
        agent_comparison.run_stage(cfg, config(), "formal")
    assert calls == []


def test_logical_request_without_http_and_empty_usage_remain_honest(tmp_path):
    with TraceWriter(tmp_path / "trace.jsonl", "offline") as trace:
        runtime = EpisodeRuntime(trace)
        with runtime.logical("jev_tool_choice"):
            pass
        events = list(trace.events)
        result = episode.summarize_metrics(events, runtime, live=True)
        assert result["logical_model_requests"] == 1
        assert result["model_http_attempts"] == 0
        assert result["input_tokens"] is None
        assert result["roles"]["jev_tool_choice"]["logical_requests"] == 1
        assert result["roles"]["jev_tool_choice"]["http_attempts"] == 0
        events += [
            {"event": "http_attempt_start", "role": "jev_tool_choice", "logical_request_id": "model_0001", "attempt_id": "http_0001"},
            {"event": "http_attempt_end", "role": "jev_tool_choice", "logical_request_id": "model_0001", "attempt_id": "http_0001", "usage": {}},
        ]
        result = episode.summarize_metrics(events, runtime, live=True)
        assert result["usage_status"] == "unavailable"
        assert result["input_tokens"] is result["output_tokens"] is None
        events[-1]["usage"] = {"input_tokens": 7, "output_tokens": None}
        partial = episode.summarize_metrics(events, runtime, live=True)
        assert partial["input_tokens"] == 7 and partial["output_tokens"] is None
        assert partial["usage_status"] == "partially_reported"


def test_material_coverage_requires_actual_jev_http_with_packet_not_only_decision_event():
    packet = {"packet_id": "packet_0001", "source_request_id": "model_update"}
    events = [
        {"event_id": 0, "event": "http_attempt_end", "role": "llm_context_update", "logical_request_id": "model_update", "status": "ok", "attempt_id": "http_update"},
        {"event_id": 1, "event": "packet_accepted", "packet": packet},
        {"event_id": 2, "event": "decision_end", "status": "ok", "decision_stage": "tool"},
        {"event_id": 3, "event": "tool_end", "tool_call_id": "tool_1", "executed": True, "status": "ok"},
        {"event_id": 4, "event": "decision_start", "state": {"packet": packet, "fresh_events": [{"tool_call_id": "tool_1"}]}},
    ]
    result = episode.integration_coverage(events, "hybrid", live=True)
    assert result["integration_coverage"]["llm_material_in_jev"] == "uncovered"
    assert result["live_integration_success"] is not True


def test_later_hybrid_updater_failure_is_an_integration_failure():
    result = episode.integration_coverage([{"event": "hybrid_update_error", "event_id": 0}], "hybrid", live=True)
    assert result["live_integration_success"] is False
