"""Conditional parameter coverage and execution checks; all providers offline."""
from copy import deepcopy
import json
from pathlib import Path
import socket

import pytest

from fairy.agents.Jev.agent import JevAgent
from fairy.agents.Jev.candidates import CandidateResolver
from fairy.agents.Jev.client import ScriptedBackend
from fairy.agents.Jev.environment import create_fixture
from fairy.agents.Jev.hybrid.agent import HybridJevAgent
from fairy.agents.Jev.parameter_contracts import (
    CONTRACT_PARAMETER_STRATEGY, audit_parameter_contracts, completion_ranges,
    prefix_candidates, range_contract, range_values, validate_parameter_strategy,
)
from fairy.agents.Jev.runner import run_mode
from fairy.agents.Jev.tools import ToolCatalog
from fairy.agents.Jev.trace import TraceWriter
from fairy.agents.Jev.verify import verify_run
from fairy.experiments.episode import run_episode
from fairy.experiments.verification import verify_episode

BASE = json.loads(Path("configs/jev/hn58_irrigation.json").read_text())


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Network forbidden in parameter-contract tests")
    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)
    monkeypatch.setattr(socket, "create_connection", reject)


def configuration(**kwargs):
    return {**deepcopy(BASE), "parameter_strategy": CONTRACT_PARAMETER_STRATEGY, **kwargs}


@pytest.fixture(scope="module")
def entries():
    scenario, _, _ = create_fixture(BASE)
    return ToolCatalog(scenario.apps).entries


def contract_for(entries, name="Robot0__inspect_emergence", config=None):
    entry = next(e for e in entries if e["name"] == name)
    return range_contract(entry, (config or BASE)["candidate_catalog"])


def test_end_is_conditioned_on_actual_selected_start_and_field_boundary(entries):
    contract = contract_for(entries)
    assert list(range_values(contract, "start_ridge", {})) == list(range(64))
    ends = prefix_candidates(contract, "end_ridge", {"start_ridge": 20}, ["decision_2"])
    assert {c["arguments"]["end_ridge"] for c in ends["candidates"].values()} == set(range(20, 64))
    assert "value_5" not in ends["candidates"]
    assert list(range_values(contract, "end_ridge", {"start_ridge": 63})) == [63]
    assert contract["max_pass_width"] is None  # Never import Robot's private 8-ridge constant.


def test_declared_tool_width_is_conditional_not_anchored_to_task_chunk(entries):
    contract = contract_for(entries, "TractorApp__harvest")
    assert list(range_values(contract, "end_ridge", {"start_ridge": 20})) == [20, 21, 22, 23]
    assert list(range_values(contract, "end_ridge", {"start_ridge": 62})) == [62, 63]
    assert "declared_catalog:max_pass_width.TractorApp__harvest" in contract["source_refs"]


@pytest.mark.parametrize("start", [-1, 64, True, 2.5])
def test_bad_prefix_is_rejected_not_repaired(entries, start):
    with pytest.raises(ValueError):
        range_values(contract_for(entries), "end_ridge", {"start_ridge": start})


def test_no_silent_truncation_or_guessed_bounds(entries):
    cfg = configuration()
    cfg["candidate_catalog"]["ridge_bounds"]["values"] = [0, 255]
    menu = prefix_candidates(contract_for(entries, config=cfg), "start_ridge", {}, [])
    assert menu["status"] == "candidate_limit_exceeded" and menu["candidates"] == {}
    del cfg["candidate_catalog"]["ridge_bounds"]
    with pytest.raises(ValueError, match="ridge_bounds"):
        contract_for(entries, config=cfg)
    with pytest.raises(ValueError, match="parameter_strategy"):
        validate_parameter_strategy({"parameter_strategy": "unknown"})


def test_complete_call_default_preserved_and_conditional_ranges_can_express_remaining_work(entries):
    old = CandidateResolver(entries, BASE)
    new = CandidateResolver(entries, configuration())
    state = {"task": "Check emergence at ridges 0-63.", "observations": []}
    name = "Robot0__inspect_emergence"
    assert old.generate(name, state) == new.generate(name, state)
    assert {tuple(c["arguments"].values()) for c in old.generate(name, state)["candidates"].values()} == {(0, 63)}
    ranges = completion_ranges(contract_for(entries), {"start_ridge": 8}, ["decision_2"])
    generated = new.generate(name, state, selected_ranges=ranges)
    assert {"start_ridge": 8, "end_ridge": 15} in [c["arguments"] for c in generated["candidates"].values()]
    assert len(generated["candidates"]) == 56


def test_range_selected_before_joint_dose_and_defaults(entries):
    config = configuration()
    name = "FieldOpsApp__irrigate"
    contract = contract_for(entries, name)
    assert contract["prefix_parameters"] == ["start", "end"]
    state = {"task": "只对43-63垄灌溉0.8小时", "observations": []}
    generated = CandidateResolver(entries, config).generate(name, state,
        selected_ranges=completion_ranges(contract, {"start": 43, "end": 63}, ["d2", "d3"]))
    assert generated["status"] == "ready"
    args = [c["arguments"] for c in generated["candidates"].values()]
    assert {"start": 43, "end": 63, "hours": .8} in args
    assert all(a["start"] == 43 and a["end"] == 63 for a in args)


@pytest.mark.parametrize("pair", [(20, 5), (-1, 3), (20, 64), (True, 8), (8, False)])
def test_conditioned_generator_cannot_bypass_range_contract(entries, pair):
    generated = CandidateResolver(entries, configuration()).generate(
        "Robot0__inspect_emergence", {"task": "Check ridges 0-63"},
        selected_ranges=[(pair, "model_selection:test")])
    assert generated["status"] == "unsupported" and generated["candidates"] == {}


def test_all_declared_range_tools_have_legal_conditional_completions(entries):
    resolver = CandidateResolver(entries, configuration())
    contracts = [range_contract(e, BASE["candidate_catalog"]) for e in entries]
    assert sum(c is not None for c in contracts) >= 16
    for contract in filter(None, contracts):
        start, end = contract["start_parameter"], contract["end_parameter"]
        for first in (0, 20, 63):
            final = min(range_values(contract, end, {start: first}))
            selected = {start: first, end: final}
            generated = resolver.generate(contract["tool_name"], {"task": "Inspect ridges 0-63"},
                selected_ranges=completion_ranges(contract, selected, ["test_start", "test_end"]))
            assert generated["status"] == "ready", (contract["tool_name"], generated)
            assert all(all(c["arguments"][k] == v for k,v in selected.items())
                       for c in generated["candidates"].values())


class OfflineUpdater:
    def __init__(self, trace):
        self.trace = trace

    def update(self, data):
        material = {"brief": "Inspect the remaining ridges using returned evidence.",
                    "evidence_refs": data["input_event_ids"]}
        self.trace.emit("scripted_context_update", input=data, material=material)
        return material


def run_robot(tmp_path, *, architecture="jev", state_strategy="visible_state_v1",
              bad_end=False, decisions=80):
    config = configuration(state_strategy=state_strategy)
    config["budgets"]["max_decision_calls"] = decisions
    scenario, engine, _ = create_fixture(config)
    catalog = ToolCatalog(scenario.apps)
    resolver = CandidateResolver(catalog.entries, config)
    trace = TraceWriter(tmp_path / "trace.jsonl", "offline-robot")
    calls, action = [], [0]
    def policy(state, stage, instructions, options):
        calls.append({"stage": stage, "state": deepcopy(state), "options": deepcopy(options)})
        if stage == "tool":
            action[0] += 1
            return "Robot0__inspect_emergence" if action[0] <= 2 else "FINISH"
        if stage == "parameter_start_ridge":
            return "value_0" if action[0] == 1 else "value_8"
        if bad_end:
            return "unsubmitted_invalid_range"
        target = {"start_ridge": 0, "end_ridge": 63} if action[0] == 1 else {"start_ridge": 8, "end_ridge": 15}
        return next(k for k, v in options.items() if v["arguments"] == target)
    backend = ScriptedBackend(policy)
    cls = HybridJevAgent if architecture == "hybrid" else JevAgent
    kwargs = {"updater": OfflineUpdater(trace)} if architecture == "hybrid" else {}
    agent = cls(catalog, backend, resolver, trace, config, **kwargs)
    agent.set_time_manager(engine.time_manager)
    agent.run("Check emergence across ridges 0-63.")
    trace.close()
    return agent, trace, calls, catalog, config


@pytest.mark.parametrize("architecture,state_strategy", [
    ("jev", "visible_state_v1"), ("jev", "task_history_v1"), ("hybrid", "visible_state_v1"),
])
def test_real_robot_partial_coverage_then_remaining_range(tmp_path, architecture, state_strategy):
    agent, trace, calls, catalog, config = run_robot(tmp_path, architecture=architecture, state_strategy=state_strategy)
    executed = [e for e in trace.events if e["event"] == "tool_end"]
    assert agent.stop_reason == "model_finish"
    assert len(executed) == 2 and all(e["status"] == "ok" for e in executed)
    assert executed[0]["result"]["covered_ridges"] == list(range(8))
    assert executed[1]["result"]["covered_ridges"] == list(range(8, 16))
    assert all(len(e["decision_ids"]) == 3 for e in executed)
    assert len(calls) == 7
    assert not audit_parameter_contracts(trace.events, catalog.by_name, config)
    assert all(calls[i]["state"] == calls[j]["state"] for i, j in [(0,1), (1,2), (3,4), (4,5)])


def test_invalid_model_choice_never_executes_or_gets_substituted(tmp_path):
    agent, trace, _, _, _ = run_robot(tmp_path, bad_end=True)
    assert agent.stop_reason == "decision_error"
    assert not any(e["event"] == "tool_start" for e in trace.events)


@pytest.mark.parametrize("decisions", [1, 2])
def test_budget_exhaustion_between_parameter_choices_never_executes(tmp_path, decisions):
    agent, trace, calls, catalog, config = run_robot(tmp_path, decisions=decisions)
    assert agent.stop_reason == "max_decision_calls" and len(calls) == decisions
    assert not any(e["event"] == "tool_start" for e in trace.events)
    assert not audit_parameter_contracts(trace.events, catalog.by_name, config)


def test_auditor_detects_condition_or_dependency_tampering(tmp_path):
    _, trace, _, catalog, config = run_robot(tmp_path)
    rows = deepcopy(trace.events)
    event = next(e for e in rows if e["event"] == "parameter_candidates")
    next(iter(event["generation"]["candidates"].values()))["arguments"]["end_ridge"] = -1
    assert "complete_candidate_preserves_prefix_and_contract" in {
        issue["check"] for issue in audit_parameter_contracts(rows, catalog.by_name, config)}
    rows = deepcopy(trace.events)
    next(e for e in rows if e["event"] == "tool_start")["decision_ids"].pop(1)
    assert "execution_links_conditional_choice_chain" in {
        issue["check"] for issue in audit_parameter_contracts(rows, catalog.by_name, config)}


def test_scripted_hn58_artifacts_and_replay(tmp_path):
    config = configuration()
    path, result = run_mode("scripted", config, tmp_path / "source")
    assert result["scripted_task_success"] is True
    assert result["model_request_count"] == 0
    assert result["decision_call_count"] == 20  # 16 legacy + 4 range prefix choices.
    assert result["task_success"] is result["live_integration_success"] is None
    verified = verify_run(path)
    assert verified["passed"], verified["issues"]
    _, replay = run_mode("replay", config, tmp_path / "replay", workflow_path=path / "workflow.json")
    assert replay["replay"]["replay_success"] is True


@pytest.mark.parametrize("architecture,state_strategy", [("jev", "task_history_v1"), ("hybrid", "visible_state_v1")])
def test_comparison_runner_and_verifier_support_conditional_chain(tmp_path, architecture, state_strategy):
    config = configuration(state_strategy=state_strategy)
    path, result = run_episode(config, tmp_path, architecture=architecture, mode="scripted")
    assert result["stop_reason"] == "model_finish", result
    verified = verify_episode(path)
    assert verified["passed"], verified["issues"]
    assert verified["counts"]["http_attempts"] == 0
