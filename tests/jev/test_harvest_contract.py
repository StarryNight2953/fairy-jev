"""Offline evidence lifecycle and conditional range coverage checks."""
from copy import deepcopy
import json
from pathlib import Path
import socket
import pytest

from fairy.agents.Jev.harvest_contract import (
    VERSION, HARVEST, bind_harvest_contract, harvest_range_menu, validate_harvest_policy,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("Offline harvest contract tests must not access network")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def result(eid, method, value, **kwargs):
    return {
        "event_id": eid, "event": "tool_end", "tool_name": "TractorApp__" + method,
        "executed": True, "status": "ok", "result": value,
        "arguments": {"start_ridge": 0, "end_ridge": 3}, **kwargs,
    }


def harvest(eid=1, ids=None):
    return result(eid, "harvest", {"status": "ok", "harvested_ridges": [0, 1, 2, 3] if ids is None else ids})


def menu(events):
    return harvest_range_menu(events, [0, 63], 4)


def test_unknown_retains_all_structurally_legal_ranges():
    m = menu([])
    assert m["start_candidates"] == list(range(64))
    assert m["end_candidates_by_start"]["20"] == [20, 21, 22, 23]
    assert m["end_candidates_by_start"]["63"] == [63]
    assert not m["runtime_enabled"]


def test_exclusion_applies_to_start_and_all_endpoints_without_crossing_holes():
    m = menu([harvest(ids=[2, 3, 9])])
    assert 2 not in m["start_candidates"]
    assert m["end_candidates_by_start"]["0"] == [0, 1]
    assert m["end_candidates_by_start"]["7"] == [7, 8]
    assert m["facts"]["evidence"]["3"]["event_id"] == 1
    actual = {(int(s), e) for s, ends in m["end_candidates_by_start"].items() for e in ends}
    expected = {(s, e) for s in range(64) for e in range(s, min(64, s+4))
                if not set(range(s, e+1)) & {2, 3, 9}}
    assert actual == expected  # Neither unsafe combinations nor silent coverage loss.


@pytest.mark.parametrize("method,field", [
    ("plant_seeds", "planted_ridges"), ("replant_seeds", "replanted_ridges"),
])
def test_new_crop_cycle_reopens_only_returned_ridges(method, field):
    m = menu([harvest(), result(2, method, {"status": "ok", field: [1, 2]})])
    assert m["facts"]["excluded_ridges"] == [0, 3]
    assert m["end_candidates_by_start"]["1"] == [1, 2]
    # Reopened means unknown. It does not assert maturity or quota feasibility.
    m2 = menu([harvest(), result(2, method, {"status": "ok", field: [1, 2]}),
               harvest(3, [1])])
    assert m2["facts"]["excluded_ridges"] == [0, 1, 3]


def test_failed_or_ambiguous_reset_does_not_keep_stale_block():
    m = menu([harvest(), result(2, "plant_seeds", {"error": "execution uncertain"},
                               status="error")])
    assert not m["facts"]["excluded_ridges"]
    assert m["facts"]["invalidations"][0]["forgotten_ridges"] == [0, 1, 2, 3]


def test_unexecuted_plant_does_not_invalidate():
    m = menu([harvest(), result(2, "plant_seeds", None, executed=False)])
    assert m["facts"]["excluded_ridges"] == [0, 1, 2, 3]


@pytest.mark.parametrize("kind", ["environment_notification", "crop_state_invalidated", "episode_start"])
def test_environment_or_episode_boundary_forgets_old_prohibitions(kind):
    assert not menu([harvest(), {"event_id": 2, "event": kind}])["facts"]["excluded_ridges"]


def test_form_ridges_invalidates_previous_lifecycle():
    assert not menu([harvest(), result(2, "form_ridges", {"status": "ok"})])["facts"]["excluded_ridges"]


@pytest.mark.parametrize("ids", [[True], [-1], [64], [0, "1"], None])
def test_malformed_harvest_output_not_partially_accepted(ids):
    e = result(1, "harvest", {"status": "ok", "harvested_ridges": ids})
    assert not menu([e])["facts"]["excluded_ridges"]


def test_failed_harvest_is_not_success_and_preserves_prior_confirmed_return():
    failed = result(2, "harvest", {"error": "All ridges must be planted before harvest"},
                    status="error", error_stage="tool_return")
    assert not menu([failed])["facts"]["excluded_ridges"]
    assert menu([harvest(), failed])["facts"]["excluded_ridges"] == [0, 1, 2, 3]
    assert not menu([harvest(), {**failed, "error_stage": "tool_exception"}])["facts"]["excluded_ridges"]


def test_explicit_public_planted_observation_overrides_old_evidence():
    obs = result(2, "ignored", {"ridges": [
        {"ridge_id": 1, "planted": True}, {"ridge_id": 5, "planted": False},
        {"ridge_id": 2, "planted": "true"}, {"ridge_id": 3, "harvested": False},
    ]}, tool_name="FarmWorldApp__get_ridge_range_state")
    m = menu([harvest(), obs])
    assert m["facts"]["excluded_ridges"] == [0, 2, 3, 5]
    assert m["facts"]["evidence"]["5"]["result_path"] == "ridges[1].planted"


def test_all_excluded_yields_context_need_without_finishing_or_synthesizing_action():
    m = menu([harvest(ids=list(range(64)))])
    assert m["status"] == "needs_context" and not m["start_candidates"]
    assert not m["end_candidates_by_start"]
    assert "action" not in m and "task_success" not in m


def test_no_argument_inference_or_model_summary_evidence():
    e = result(1, "harvest", {"status": "ok"})
    llm = {"event_id": 2, "event": "decision_end", "result": {"harvested_ridges": [0, 1, 2, 3]}}
    assert not menu([e, llm])["facts"]["excluded_ridges"]


def test_input_immutable_and_chronology_not_silently_reordered():
    events = [harvest(), result(2, "unload_grain", {"status": "ok"})]
    before = deepcopy(events)
    assert menu(events)["facts"]["excluded_ridges"] == [0, 1, 2, 3]
    assert events == before
    for bad in ([harvest(), harvest()], [harvest(3), harvest(1)]):
        with pytest.raises(ValueError, match="strictly increasing"):
            menu(bad)


@pytest.mark.parametrize("bounds,width", [([2, 1], 4), ([0, True], 4), ([0, 63], 0), ([0, 63], True)])
def test_bad_declared_contract_rejected(bounds, width):
    with pytest.raises(ValueError):
        harvest_range_menu([], bounds, width)


@pytest.mark.parametrize("config", [
    {"harvest_state_policy": "unrecognized"},
    {"harvest_state_policy": VERSION},
    {"harvest_state_policy": VERSION, "parameter_strategy": "tool_contracts_v1"},
])
def test_unrecognized_or_incompatible_policy_fails_before_decisions(config):
    with pytest.raises(ValueError):
        validate_harvest_policy(config)


def test_absent_policy_leaves_contract_exact_and_other_tools_untouched():
    contract = {"tool_name": HARVEST}
    assert validate_harvest_policy({}) is None
    assert bind_harvest_contract(contract, [harvest()], None) is contract
    other = {"tool_name": "Robot0__inspect_emergence"}
    assert bind_harvest_contract(other, [harvest()], VERSION) is other


def run_harvest_agent(tmp_path, *, enabled=True, architecture="jev",
                      state_strategy="task_history_v1", all_excluded=False):
    """Scripted choices, actual native harvests; no provider or hidden-state edit."""
    from fairy.agents.Jev.agent import JevAgent
    from fairy.agents.Jev.candidates import CandidateResolver
    from fairy.agents.Jev.client import ScriptedBackend
    from fairy.agents.Jev.environment import create_fixture, public_task
    from fairy.agents.Jev.hybrid.agent import HybridJevAgent
    from fairy.agents.Jev.tools import ToolCatalog
    from fairy.agents.Jev.trace import TraceWriter

    config = json.loads(Path("configs/jev/hn58_jev_tool_adapters_v2.json").read_text())
    config.update(scenario_id="scenario_l1_hb_coldspring_heihe50_harvest_store_ready",
                  state_strategy=state_strategy)
    if enabled:
        config["harvest_state_policy"] = VERSION
    scenario, engine, _ = create_fixture(config)
    catalog = ToolCatalog(scenario.apps)
    trace = TraceWriter(tmp_path / "trace.jsonl", "offline-native-harvest")
    calls, rounds = [], [0]

    def policy(state, stage, instructions, options):
        calls.append({"stage": stage, "state": deepcopy(state), "options": deepcopy(options)})
        if stage == "tool":
            rounds[0] += 1
            return HARVEST if all_excluded or rounds[0] <= 2 else "FINISH"
        if stage == "parameter_start_ridge":
            return "value_0" if "value_0" in options else "value_4"
        # Select one whole declared pass; no alteration of returned arguments.
        return max(options, key=lambda key: options[key]["arguments"]["end_ridge"])

    class OfflineUpdater:
        def update(self, data):
            material = {"brief": "Use returned crop facts when selecting the next range.",
                        "evidence_refs": data["input_event_ids"]}
            trace.emit("scripted_context_update", input=data, material=material)
            return material

    cls = HybridJevAgent if architecture == "hybrid" else JevAgent
    kwargs = {"updater": OfflineUpdater()} if architecture == "hybrid" else {}
    agent = cls(catalog, ScriptedBackend(policy), CandidateResolver(catalog.entries, config),
                trace, config, **kwargs)
    agent.set_time_manager(engine.time_manager)
    if all_excluded:
        # Synthetic *unit* input to exercise the no-candidate branch, not evidence
        # of an actual all-field harvest or a real runtime success.
        injected = [False]
        def inject_once():
            if not injected[0]:
                trace.emit("tool_end", tool_name=HARVEST, executed=True, status="ok",
                           result={"status": "ok", "harvested_ridges": list(range(64))})
                injected[0] = True
        agent.poll_environment = inject_once
    agent.run(public_task(engine))
    trace.close()
    return agent, trace, calls, catalog, config


@pytest.mark.parametrize("architecture,state_strategy", [
    ("jev", "visible_state_v1"), ("jev", "task_history_v1"), ("hybrid", "visible_state_v1"),
])
def test_actual_native_harvest_removes_used_range_from_next_choice(tmp_path, architecture, state_strategy):
    from fairy.agents.Jev.parameter_contracts import audit_parameter_contracts
    agent, trace, calls, catalog, config = run_harvest_agent(
        tmp_path, architecture=architecture, state_strategy=state_strategy)
    tools = [e for e in trace.events if e["event"] == "tool_end"]
    assert agent.stop_reason == "model_finish"
    assert len(tools) == 2 and all(t["status"] == "ok" for t in tools)
    assert tools[0]["result"]["harvested_ridges"] == [0, 1, 2, 3]
    assert tools[1]["result"]["harvested_ridges"] == [4, 5, 6, 7]
    prefixes = [c for c in calls if c["stage"] == "parameter_start_ridge"]
    assert "value_0" in prefixes[0]["options"] and "value_0" not in prefixes[1]["options"]
    assert len(calls) == 7  # No extra refresh/repair/model calls.
    assert not audit_parameter_contracts(trace.events, catalog.by_name, config)
    plans = [e for e in trace.events if e["event"] == "parameter_plan"]
    fact = plans[1]["contract"]["observed_state"]
    assert fact["excluded_ridges"] == [0, 1, 2, 3]
    assert fact["source_evidence"] == [{
        "event_id": tools[0]["event_id"], "result_path": "harvested_ridges",
        "basis": "successful_harvest", "ridges": [0, 1, 2, 3],
    }]
    # Choices within the round use the exact same frozen model state.
    assert calls[0]["state"] == calls[1]["state"] == calls[2]["state"]
    assert calls[3]["state"] == calls[4]["state"] == calls[5]["state"]


def test_disabled_policy_preserves_repeat_choice_and_native_error(tmp_path):
    from fairy.agents.Jev.parameter_contracts import audit_parameter_contracts
    agent, trace, calls, catalog, config = run_harvest_agent(tmp_path, enabled=False)
    tools = [e for e in trace.events if e["event"] == "tool_end"]
    assert len(tools) == 2 and tools[0]["status"] == "ok" and tools[1]["status"] == "error"
    assert tools[1]["result"]["error"] == "All ridges must be planted before harvest"
    assert not audit_parameter_contracts(trace.events, catalog.by_name, config)
    assert all("observed_state" not in e["contract"] for e in trace.events if e["event"] == "parameter_plan")


def test_all_excluded_feedback_uses_existing_repeat_limit_without_tool_or_finish(tmp_path):
    agent, trace, calls, _, _ = run_harvest_agent(tmp_path, all_excluded=True)
    assert agent.stop_reason == "repeated_parameter_failure"
    assert len(calls) == 3 and all(c["stage"] == "tool" for c in calls)
    assert not any(e["event"] in {"tool_start", "model_finish"} for e in trace.events)
    feedback = [e for e in trace.events if e["event"] == "controller_feedback"]
    assert len(feedback) == 3 and all(e["status"] == "needs_context" for e in feedback)
    assert calls[1]["state"]["controller_feedback"]  # Visible to subsequent choice.


def test_trace_audit_reconstructs_evidence_and_rejects_removed_plan_or_constraints(tmp_path):
    from fairy.agents.Jev.parameter_contracts import audit_parameter_contracts
    _, trace, _, catalog, config = run_harvest_agent(tmp_path)
    changed = deepcopy(trace.events)
    plans = [e for e in changed if e["event"] == "parameter_plan"]
    plans[1]["contract"]["observed_state"]["excluded_ridges"] = []
    assert "parameter_contract_matches_configuration" in {
        i["check"] for i in audit_parameter_contracts(changed, catalog.by_name, config)
    }
    changed = [e for e in trace.events if e["event"] != "parameter_plan"]
    assert "harvest_execution_requires_observed_contract" in {
        i["check"] for i in audit_parameter_contracts(changed, catalog.by_name, config)
    }
    changed = deepcopy(trace.events)
    final = [e for e in changed if e["event"] == "parameter_candidates"][1]
    first = next(iter(final["generation"]["candidates"].values()))
    first["arguments"] = {"start_ridge": 0, "end_ridge": 3}
    assert "complete_candidate_preserves_prefix_and_contract" in {
        i["check"] for i in audit_parameter_contracts(changed, catalog.by_name, config)
    }


@pytest.mark.parametrize("architecture,state_strategy", [
    ("jev", "task_history_v1"), ("hybrid", "visible_state_v1"),
])
def test_full_episode_artifact_verification_and_action_replay(tmp_path, monkeypatch, architecture, state_strategy):
    from fairy.experiments import episode

    class TwoHarvests:
        def __init__(self):
            self.rounds = 0

        def __call__(self, state, stage, instructions, options):
            if stage == "tool":
                self.rounds += 1
                return HARVEST if self.rounds <= 2 else "FINISH"
            if stage == "parameter_start_ridge":
                return "value_0" if "value_0" in options else "value_4"
            return max(options, key=lambda key: options[key]["arguments"]["end_ridge"])

    monkeypatch.setattr(episode, "ScriptedPolicy", TwoHarvests)
    config = json.loads(Path("configs/jev/hn58_jev_tool_adapters_v2.json").read_text())
    config.update(
        scenario_id="scenario_l1_hb_coldspring_heihe50_harvest_store_ready",
        harvest_state_policy=VERSION, state_strategy=state_strategy,
        evaluation_profile="scenario_native_v1",
    )
    path, result = episode.run_episode(config, tmp_path, architecture=architecture, mode="scripted")
    assert result["stop_reason"] == "model_finish", result
    assert result["task_success"] is result["live_integration_success"] is None
    checked = episode.verify_and_replay(path, config)
    assert checked["trace_verification"]["passed"], checked["trace_verification"]
    assert checked["replay_verification"]["passed"], checked["replay_verification"]
    assert checked["trace_verification"]["counts"]["http_attempts"] == 0
    manifest = json.loads((path / "manifest.json").read_text())
    assert manifest["harvest_state_policy"] == VERSION
    assert len(manifest["harvest_state_contract_hash"]) == 64
    events = [json.loads(line) for line in (path / "trace.jsonl").read_text().splitlines()]
    native = [e for e in events if e["event"] == "tool_end"]
    assert len(native) == 2 and all(e["status"] == "ok" for e in native)
    assert [e["result"]["harvested_ridges"] for e in native] == [[0, 1, 2, 3], [4, 5, 6, 7]]
