"""Alternative pure-Jev input preserves raw visible history without summarizing."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from fairy.agents.Jev.agent import JevAgent
from fairy.agents.Jev.candidates import CandidateResolver
from fairy.agents.Jev.client import Decision
from fairy.agents.Jev.state import (
    DEFAULT_STATE_STRATEGY, TASK_HISTORY_STRATEGY, VisibleState, validate_state_strategy,
)
from fairy.agents.Jev.tools import ToolCatalog
from fairy.agents.Jev.trace import TraceWriter, canonical
from fairy.apps.app import App
from fairy.tool_utils import app_tool


def observation(index, *, status="ok", result=None):
    return {"tool_name": "FarmWorldApp__read", "tool_call_id": f"tool_{index}",
            "decision_ids": [f"decision_{index}"], "write_operation": False,
            "arguments": {"range": [index, index + 1]},
            "result": result if result is not None else {"reading": index},
            "status": status, "sim_time_before": 1000 + index,
            "sim_time_after": 1000.5 + index, "executed": True}


def test_full_history_ignores_legacy_retention_bounds_and_orders_notifications():
    state = VisibleState(max_observations=1, max_recent_actions=1, max_feedback=1)
    state.task = "Read every update, then report"
    state.system_context = ["Public system context"]
    for index in range(8):
        state.add_observation(observation(index))
        state.add_observation({"kind": "environment_notification", "result": {"ready": index},
                               "time": f"notification-{index}", "sim_time_after": 1001 + index})
        state.add_feedback({"reason": "missing context", "attempt": index})
    snapshot = state.task_history_snapshot(1010)
    assert snapshot["task"] == state.task
    assert snapshot["current_time"] == 1010
    assert snapshot["system_context"] == ["Public system context"]
    assert len(snapshot["actions_so_far"]) == len(snapshot["environment_notifications"]) == 8
    assert len(snapshot["controller_feedback"]) == 8
    assert [row["observation_id"] for row in snapshot["actions_so_far"]] == list(range(0, 16, 2))
    assert [row["observation_id"] for row in snapshot["environment_notifications"]] == list(range(1, 16, 2))
    assert [row["result"] for row in snapshot["actions_so_far"]] == [{"reading": i} for i in range(8)]
    assert snapshot["environment_notifications"][0]["time"] == "notification-0"
    assert all(row["action"] == "FarmWorldApp__read" for row in snapshot["actions_so_far"])
    assert all("tool_call_id" not in row and "write_operation" not in row and "decision_ids" not in row
               for row in snapshot["actions_so_far"])
    assert set(snapshot) == {"task", "current_time", "system_context", "actions_so_far",
                             "environment_notifications", "controller_feedback"}


def test_results_errors_and_nulls_are_preserved_once_per_action():
    state = VisibleState()
    for index in range(3):
        row = observation(index, status="error", result={"reason": f"raw native error {index}"})
        row.update(error=f"native detail {index}", error_stage="tool_call", executed=index != 0)
        state.add_observation(row)
    null = observation(3)
    null["result"] = None
    state.add_observation(null)
    projected = state.task_history_snapshot(1005)
    actions = projected["actions_so_far"]
    for index, action in enumerate(actions[:3]):
        assert action["result"] == {"reason": f"raw native error {index}"}
        assert action["status"] == "error"
        assert action["error"] == f"native detail {index}"
        assert action["error_stage"] == "tool_call"
        assert action["executed"] is (index != 0)
        assert canonical(projected).count(f"raw native error {index}") == 1
    assert actions[-1]["result"] is None
    assert "error" not in actions[-1] and "error_stage" not in actions[-1]


def test_projection_is_deep_copy_and_does_not_mutate_legacy_snapshot():
    state = VisibleState()
    source = observation(0, result={"nested": [1, {"quota": 1.4}]})
    state.add_observation(source)
    state.add_observation({"kind": "environment_notification", "result": {"text": ["ready"]}})
    state.add_feedback({"missing_fields": ["ridge"]})
    state.system_context.append({"public": ["context"]})
    legacy = state.snapshot(1100)
    projected = state.task_history_snapshot(1100)
    source["result"]["nested"][1]["quota"] = 99
    projected["actions_so_far"][0]["result"]["nested"][1]["quota"] = 88
    projected["actions_so_far"][0]["arguments"]["range"].append(99)
    projected["environment_notifications"][0]["result"]["text"].append("changed")
    projected["controller_feedback"][0]["missing_fields"].append("changed")
    projected["system_context"][0]["public"].append("changed")
    assert state.snapshot(1100) == legacy
    assert state.task_history_snapshot(1100)["actions_so_far"][0]["result"]["nested"][1]["quota"] == 1.4


def test_large_result_is_not_secretly_cropped_or_summarized():
    state = VisibleState(max_observations=1)
    large = "完整返回:" + "农" * 1_000_001
    state.add_observation(observation(0, result={"large": large}))
    state.add_observation(observation(1, result={"small": "later"}))
    projected = state.task_history_snapshot(1005)
    assert projected["actions_so_far"][0]["result"]["large"] == large
    assert len(projected["actions_so_far"]) == 2
    assert len(canonical(projected)) > 1_000_001


@pytest.mark.parametrize("strategy", [None, "", "unknown", 123, ["task_history_v1"]])
def test_unknown_strategy_is_rejected_before_any_run(strategy):
    with pytest.raises(ValueError, match="state_strategy"):
        JevAgent(None, None, None, None, {"state_strategy": strategy})


def test_default_projection_is_exact_legacy_object_and_explicit_old_strategy_matches():
    assert validate_state_strategy(None) == validate_state_strategy({}) == DEFAULT_STATE_STRATEGY
    state = VisibleState()
    state.add_observation(observation(0))
    visible = state.snapshot(1234)
    for config in ({}, {"state_strategy": DEFAULT_STATE_STRATEGY}):
        agent = JevAgent(None, None, None, None, config)
        agent.state = state
        assert agent._decision_state(visible) is visible
        assert canonical(agent._decision_state(visible)) == canonical(state.snapshot(1234))


class HistoryApp(App):
    def __init__(self):
        super().__init__()
        self.calls = []

    @app_tool()
    def observe(self) -> dict:
        """Read a visible condition."""
        self.calls.append("observe")
        return {"condition": "dry", "quota": 1.4}

    @app_tool()
    def apply(self, amount: float) -> dict:
        """Apply a selected dose."""
        self.calls.append(("apply", amount))
        return {"applied": amount}

    @app_tool()
    def fail(self) -> dict:
        """Return a native error for feedback."""
        self.calls.append("fail")
        raise RuntimeError("unchanged native error")


class FrozenClock:
    def time(self):
        return 1000.0


class RecordingBackend:
    name = "scripted"

    def __init__(self, sequence):
        self.sequence, self.calls, self.state_objects = iter(sequence), [], []

    def decide(self, state, question_id, instructions, options):
        self.state_objects.append(state)
        self.calls.append({"state": deepcopy(state), "stage": question_id, "options": deepcopy(options)})
        selection = next(self.sequence)
        return Decision(selection, {"backend": "scripted", "selected_option": selection})


class RecordingResolver:
    def __init__(self):
        self.calls = []

    def generate(self, tool_name, visible):
        self.calls.append(deepcopy(visible))
        return {"status": "ready", "candidates": {
            "args_000": {"arguments": {"amount": 0.8}, "source_refs": ["declared_catalog:test"]}}}


def test_scripted_independent_loop_uses_same_projection_for_both_choices_and_real_feedback(tmp_path):
    app = HistoryApp()
    catalog = ToolCatalog([app])
    backend = RecordingBackend(["HistoryApp__observe", "HistoryApp__apply", "args_000",
                                "HistoryApp__fail", "FINISH"])
    resolver = RecordingResolver()
    with TraceWriter(tmp_path / "trace.jsonl", "task-history-offline") as trace:
        agent = JevAgent(catalog, backend, resolver, trace, {"state_strategy": TASK_HISTORY_STRATEGY})
        clock = FrozenClock()
        app.register_time_manager(clock)
        agent.set_time_manager(clock)
        agent.add_system_context("Observe public facts only")
        notifications = set()

        def poll():
            count = agent.tool_call_count
            if count in (0, 1) and count not in notifications:
                notifications.add(count)
                agent.on_environment_event({"after_tool": count}, time=f"time-{count}")

        agent.poll_environment = poll
        agent.run("Read, apply 0.8, and keep the failure")
    assert agent.stop_reason == "model_finish"
    assert app.calls == ["observe", ("apply", 0.8), "fail"]
    assert [call["stage"] for call in backend.calls] == ["tool", "tool", "parameters", "tool", "tool"]
    assert backend.state_objects[1] is backend.state_objects[2]
    assert backend.calls[1]["state"] == backend.calls[2]["state"]
    assert "observations" in resolver.calls[0] and "actions_so_far" not in resolver.calls[0]
    assert resolver.calls[0]["observations"][0]["result"] == {"condition": "dry", "quota": 1.4}
    final = backend.calls[-1]["state"]
    assert final["task"] == "Read, apply 0.8, and keep the failure"
    assert final["system_context"] == ["Observe public facts only"]
    assert [row["action"] for row in final["actions_so_far"]] == ["HistoryApp__observe", "HistoryApp__apply", "HistoryApp__fail"]
    assert [row["observation_id"] for row in final["actions_so_far"]] == [1, 3, 4]
    assert [row["observation_id"] for row in final["environment_notifications"]] == [0, 2]
    assert final["actions_so_far"][-1]["result"] == {"error": "RuntimeError: unchanged native error"}
    assert final["actions_so_far"][-1]["executed"] is True
    assert final["actions_so_far"][-1]["error_stage"] == "tool_call"
    assert trace.events[1]["event"] == "run_start"
    assert trace.events[1]["state_strategy"] == TASK_HISTORY_STRATEGY
    decisions = [row for row in trace.events if row["event"] == "decision_start"]
    for recorded, request in zip(decisions, backend.calls):
        assert recorded["state"] == request["state"]
        assert recorded["state_strategy"] == TASK_HISTORY_STRATEGY
        assert recorded["state_measurements"]["canonical_chars"] == len(canonical(request["state"]))
        assert "state_measurements" not in request["state"]
    assert all(call["options"] == catalog.options for call in backend.calls if call["stage"] == "tool")


def test_state_strategy_does_not_change_actual_hn58_parameter_candidates():
    from fairy.scenarios.farm_world_fullseason_v2.l2_l1_splits.hb_three_cultivar_wet_disease_dry_harvest_sequence.scenario_l1_three_cultivar_hn58_action_ready_irrigation import ScenarioL1ThreeCultivarHN58ActionReadyIrrigation

    scenario = ScenarioL1ThreeCultivarHN58ActionReadyIrrigation()
    scenario.setup()
    catalog = ToolCatalog(scenario.apps)
    config = json.loads(Path("configs/jev/hn58_irrigation.json").read_text())
    state = VisibleState()
    state.task = next(event.kwargs["content"] for event in scenario.events if event.event_id == "briefing" and not event.is_oracle)
    state.add_observation({"tool_name": "FieldOpsApp__irrigate", "tool_call_id": "tool_1",
                           "arguments": {"start": 43, "end": 63, "hours": 0.8},
                           "result": {"effect_ready_at": 8200}, "status": "ok", "sim_time_after": 1000})
    visible = state.snapshot(1000)
    outputs = []
    for strategy in (DEFAULT_STATE_STRATEGY, TASK_HISTORY_STRATEGY):
        selected_config = {**config, "state_strategy": strategy}
        agent = JevAgent(catalog, None, None, None, selected_config)
        agent.state = state
        projected = agent._decision_state(visible)
        assert ("actions_so_far" in projected) == (strategy == TASK_HISTORY_STRATEGY)
        resolver = CandidateResolver(catalog.entries, selected_config)
        outputs.append({entry["name"]: resolver.generate(entry["name"], visible) for entry in catalog.entries})
    assert outputs[0] == outputs[1]
    assert all(generated["status"] == "ready" for generated in outputs[0].values())
    assert state.snapshot(1000) == visible
