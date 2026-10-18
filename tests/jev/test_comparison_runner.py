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


def test_saved_access_block_prevents_backend_construction_even_when_key_present(tmp_path, monkeypatch):
    cfg = config()
    cfg["api_key_env"] = "PRESENT_OFFLINE_TEST_KEY"
    monkeypatch.setenv("PRESENT_OFFLINE_TEST_KEY", "offline-fixture-key")
    monkeypatch.setattr(episode, "OfficialBackend", lambda *args, **kwargs: pytest.fail("blocked run constructed model backend"))
    ledger = LiveLedger(tmp_path / "ledger.json")
    blockers = [{"reason": "service_access_restricted", "http_status": 451,
                 "source_run": "saved-offline-evidence", "event_id": 3}]
    _, result = episode.run_episode(cfg, tmp_path / "runs", architecture="jev", mode="live",
                                   ledger=ledger, external_blockers=blockers)
    assert result["execution_status"] == "blocked"
    assert result["stop_reason"] == "preflight_blocked"
    assert result["blockers"] == blockers
    assert result["task_success"] is result["live_integration_success"] is None
    assert result["model_http_attempts"] == result["tool_call_count"] == 0
    assert ledger.snapshot()["episodes"] == []
    assert ledger.snapshot()["http_attempts"] == 0


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


def test_episode_uses_semantic_diagnosis_for_valid_overview_route(tmp_path, monkeypatch):
    from fairy.agents.Jev.scripted import ScriptedPolicy
    class OverviewPolicy(ScriptedPolicy):
        sequence = list(ScriptedPolicy.sequence)
        sequence[2] = "FarmWorldApp__get_farm_overview"
    monkeypatch.setattr(episode, "ScriptedPolicy", OverviewPolicy)
    _, result = episode.run_episode(config(), tmp_path, architecture="jev", mode="scripted")
    assert result["scripted_task_success"] is True
    assert result["legacy_evaluation_v1"]["task_success"] is False
    assert result["first_issue"] is None
    assert result["first_violation_event_id"] is None


def test_native_early_finish_is_task_failure_with_specific_terminal_event(tmp_path):
    from openai.types.chat import ChatCompletion
    from fairy.experiments.scripted_native import NativeFixtureLLM
    class EarlyFinish(NativeFixtureLLM):
        def chat_completion(self, messages, tools=None):
            return ChatCompletion.model_validate({"id": "fixture", "object": "chat.completion", "created": 0,
                "model": self.model, "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "Done."}}]})
    _, result = episode.run_episode(config(), tmp_path, architecture="llm_native", mode="scripted",
                                   offline_native_llm=lambda runtime: EarlyFinish(runtime))
    assert result["execution_status"] == "completed"
    assert result["scripted_task_success"] is False
    assert result["tool_call_count"] == result["model_http_attempts"] == 0
    assert result["first_issue"]["failure_layer"] == "termination"
    assert type(result["first_violation_event_id"]) is int


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


def test_formal_missing_smoke_evidence_preserves_all_nine_blocked_slots_without_http(tmp_path, monkeypatch):
    cfg, _, calls = comparison_fixture(tmp_path, monkeypatch, result_status="blocked")
    actual = json.loads(Path("configs/experiments/hn58_three_agents_v1.json").read_text())
    cfg["formal_order"], cfg["block_seeds"] = actual["formal_order"], actual["block_seeds"]
    index = agent_comparison.run_stage(cfg, config(), "formal")
    assert len(index["planned_slots"]) == len(index["runs"]) == len(calls) == 9
    assert all(call["external_blockers"] for call in calls)
    assert [record["architecture"] for record in index["runs"]] == [a for block in actual["formal_order"] for a in block]
    assert all(record["execution_status"] == "blocked" for record in index["runs"])
    assert all(record["model_http_attempts"] == 0 for record in index["runs"])
    assert index["complete"] is False
    assert index["live_budget"]["episodes"] == []
    assert index["live_budget"]["http_attempts"] == 0


def test_new_smoke_uses_old_service_evidence_against_current_provider_config(tmp_path, monkeypatch):
    cfg, _, calls = comparison_fixture(tmp_path, monkeypatch, result_status='blocked')
    old_run=tmp_path/'saved_jev';old_run.mkdir()
    (old_run/'manifest.json').write_text(json.dumps({'config':config()}))
    (old_run/'evaluation.json').write_text(json.dumps({'execution_status':'integration_error'}))
    common={'attempt_id':'http_1','logical_request_id':'model_1','role':'jev_tool_choice'}
    events=[{'event_id':0,'event':'http_attempt_start',**common},
            {'event_id':1,'event':'http_attempt_end',**common,'http_status':451,'raw_response_body':'region refusal'}]
    (old_run/'trace.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
    prior=tmp_path/'old_smoke_index.json';prior.write_text(json.dumps({'runs':[{'architecture':'jev','path':str(old_run)}]}))
    cfg['prior_smoke_indices']=[str(prior)]
    base=config();base['llm']={'provider':'vllm','model':'local-qwen','base_url':'http://fixture/v1','authentication_mode':'none'}
    index=agent_comparison.run_stage(cfg,base,'smoke')
    by_arch={c['architecture']:c['external_blockers'] for c in calls}
    assert not by_arch['llm_native']
    assert by_arch['jev'][0]['http_status']==by_arch['hybrid'][0]['http_status']==451
    assert index['smoke_access_review']['source_indices']==[str(prior)]


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
