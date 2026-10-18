"""Offline tests of update scheduling, frozen evidence and hard boundaries."""
from copy import deepcopy

import pytest

from fairy.agents.Jev.agent import JevAgent
from fairy.agents.Jev.client import Decision
from fairy.agents.Jev.hybrid.agent import HybridJevAgent
from fairy.agents.Jev.hybrid.updater import LLMUpdateError
from fairy.agents.Jev.tools import ToolCatalog, ToolExecutor
from fairy.agents.Jev.trace import TraceWriter, canonical
from fairy.apps.app import App
from fairy.experiments.budget import BudgetStop, EpisodeRuntime
from fairy.time_manager import TimeManager
from fairy.tool_utils import app_tool


class SampleApp(App):
    def __init__(self):
        super().__init__()
        self.calls = []

    @app_tool()
    def observe(self) -> dict:
        """Read a changing visible value."""
        self.calls.append("observe")
        return {"value": len(self.calls)}

    @app_tool()
    def optional(self, count: int = 1) -> dict:
        """Use an optional integer argument."""
        self.calls.append(("optional", count))
        return {"count": count}

    @app_tool()
    def failing(self) -> dict:
        """Attempt a native operation that raises."""
        self.calls.append("failing")
        raise RuntimeError("native operation failed")


class SequenceBackend:
    name = "scripted"
    requested_model = "jev-1.13.0"

    def __init__(self, sequence):
        self.sequence, self.calls = iter(sequence), []
        self.before_response = None

    def decide(self, state, question_id, instructions, options):
        self.calls.append({"state": deepcopy(state), "stage": question_id, "options": deepcopy(options)})
        if self.before_response:
            self.before_response(question_id)
        selection = next(self.sequence)
        return Decision(selection, {"backend": "scripted"})


class Resolver:
    def __init__(self, generation=None):
        self.calls = []
        self.generation = generation or {"status": "ready", "candidates": {
            "args_000": {"arguments": {"count": 1}, "source_refs": ["tool_signature:default"]}}}

    def generate(self, tool_name, visible):
        self.calls.append(deepcopy(visible))
        return deepcopy(self.generation)


class Updater:
    def __init__(self):
        self.inputs = []
        self.fail_on = None

    def update(self, update_input):
        self.inputs.append(deepcopy(update_input))
        if len(self.inputs) == self.fail_on:
            raise LLMUpdateError("material unavailable")
        return {"brief": "LLM_ONLY_ADVICE_NOT_AN_OBSERVED_FACT",
                "evidence_refs": update_input["input_event_ids"],
                "source_request_id": f"test_update_{len(self.inputs)}"}


@pytest.fixture
def harness(tmp_path):
    traces = []

    def make(sequence, *, generation=None, limits=None, config=None, pure=False):
        trace = TraceWriter(tmp_path / f"trace-{len(traces)}.jsonl", f"offline-{len(traces)}")
        traces.append(trace)
        app, backend, resolver, updater = SampleApp(), SequenceBackend(sequence), Resolver(generation), Updater()
        catalog = ToolCatalog([app])
        runtime = EpisodeRuntime(trace, {"max_decision_rounds": 60, **(limits or {})})
        configuration = {"budgets": {"max_steps": 60, "max_decision_calls": 100,
                                    "repeated_parameter_failure_limit": 10}, **(config or {})}
        cls = JevAgent if pure else HybridJevAgent
        kwargs = {"runtime": runtime, **({} if pure else {"updater": updater})}
        agent = cls(catalog, backend, resolver, trace, configuration, **kwargs)
        clock = TimeManager()
        clock.reset(1000)
        app.register_time_manager(clock)
        agent.set_time_manager(clock)
        return agent, app, backend, resolver, updater, runtime, trace

    yield make
    for trace in traces:
        trace.close()


def of_type(trace, kind):
    return [row for row in trace.events if row["event"] == kind]


def test_k4_updates_only_at_0_through_36_never_after_40(harness):
    agent, app, backend, resolver, updater, runtime, trace = harness(["SampleApp__observe"] * 40)
    agent.run("Read visible evidence as needed")
    assert [row["tool_attempts"] for row in updater.inputs] == list(range(0, 40, 4))
    assert len(app.calls) == agent.tool_call_count == runtime.counts["tool_attempts"] == 40
    assert agent.stop_reason == "max_tool_attempts"
    assert agent.step_count == runtime.counts["decision_rounds"] == 40
    assert runtime.counts["updates"] == agent.update_count == 10
    assert runtime.counts["http_attempts"] == 0
    assert not resolver.calls
    assert all(row["options"] == agent.catalog.options for row in backend.calls)
    assert backend.calls[4]["state"]["packet"]["packet_version"] == 2
    assert len(updater.inputs[1]["fresh_events"]) == 4
    assert backend.calls[4]["state"]["fresh_events"] == []


def test_no_execution_errors_are_fresh_but_do_not_trigger_update(harness):
    bad = {"status": "needs_context", "missing_fields": ["count"], "candidates": {}, "reason": "no evidence"}
    agent, app, backend, resolver, updater, runtime, trace = harness(
        ["SampleApp__optional"] * 5 + ["FINISH"], generation=bad)
    agent.run("Read missing evidence")
    assert [row["tool_attempts"] for row in updater.inputs] == [0]
    assert not app.calls
    assert runtime.counts["tool_attempts"] == 0
    assert runtime.counts["decision_rounds"] == 6
    assert len(of_type(trace, "controller_feedback")) == 5
    assert backend.calls[1]["state"]["fresh_events"][0]["event"] == "controller_feedback"
    assert all(row["options"] == agent.catalog.options for row in backend.calls)
    assert agent.stop_reason == "model_finish"


def test_native_exception_counts_attempt_and_is_submitted_at_k_boundary(harness):
    agent, app, backend, resolver, updater, runtime, trace = harness(["SampleApp__failing"] * 4 + ["FINISH"])
    agent.run("Handle native errors")
    assert runtime.counts["tool_attempts"] == 4
    assert [row["tool_attempts"] for row in updater.inputs] == [0, 4]
    assert all(row["status"] == "error" and row["executed"] for row in updater.inputs[1]["fresh_events"])
    assert app.calls == ["failing"] * 4  # No automatic retry of a failed action.


def test_repeated_preexecution_errors_use_round_cap_not_legacy_repeat_policy(harness):
    bad = {"status": "needs_context", "missing_fields": ["count"], "candidates": {}}
    agent, app, backend, resolver, updater, runtime, trace = harness(
        ["SampleApp__optional"] * 4, generation=bad, limits={"max_decision_rounds": 4},
        config={"budgets": {"max_steps": 20, "repeated_parameter_failure_limit": 3}})
    agent.run("Keep errors visible until the independent round budget")
    assert agent.stop_reason == "max_decision_rounds"
    assert len(backend.calls) == 4 and not app.calls
    assert len(updater.inputs) == 1


def test_two_choices_share_packet_and_snapshot_and_candidates_exclude_advice(harness):
    agent, app, backend, resolver, updater, runtime, trace = harness(
        ["SampleApp__optional", "args_000", "FINISH"])
    def notification(stage):
        if stage == "tool" and len(backend.calls) == 1:
            agent.on_environment_event({"new_fact": "arrived during Choice"})
    backend.before_response = notification
    agent.run("Use the original task")
    first, parameters, following = backend.calls
    assert first["state"] == parameters["state"]
    assert first["state"]["packet"]["packet_id"] == "packet_0001"
    assert not first["state"]["fresh_events"]
    assert any(row["event"] == "environment_notification" for row in following["state"]["fresh_events"])
    assert "arrived during Choice" not in canonical(resolver.calls[0])
    assert "LLM_ONLY_ADVICE_NOT_AN_OBSERVED_FACT" not in canonical(resolver.calls)
    assert "packet" not in resolver.calls[0]
    assert list(parameters["options"]) == ["args_000"]  # Single optional candidate still gets Choice.
    assert app.calls == [("optional", 1)]
    assert len(updater.inputs) == 1
    starts = of_type(trace, "decision_start")
    assert starts[0]["packet_id"] == starts[1]["packet_id"]
    assert starts[0]["observation_cutoff_event_id"] == starts[1]["observation_cutoff_event_id"]
    assert starts[0]["role"] == "jev_tool_choice"
    assert starts[1]["role"] == "jev_argument_choice"


@pytest.mark.parametrize("limits,config,expected", [
    ({"max_tool_attempts": 4}, None, "max_tool_attempts"),
    ({"max_decision_rounds": 4}, None, "max_decision_rounds"),
    ({}, {"budgets": {"max_steps": 60, "max_decision_calls": 4}}, "max_decision_calls"),
])
def test_stop_checked_before_update_at_boundary(harness, limits, config, expected):
    agent, app, backend, resolver, updater, runtime, trace = harness(
        ["SampleApp__observe"] * 4, limits=limits, config=config)
    agent.run("Keep budgets")
    assert agent.stop_reason == expected
    assert len(updater.inputs) == 1
    assert len(app.calls) == 4


def test_finish_before_k_does_not_add_update_or_tool(harness):
    agent, app, backend, resolver, updater, runtime, trace = harness(["FINISH"])
    agent.run("Entire original task")
    assert len(updater.inputs) == 1
    assert not app.calls
    assert runtime.counts["decision_rounds"] == 1
    assert agent.stop_reason == "model_finish"
    assert backend.calls[0]["state"]["finish_semantics"] == "FINISH means the entire original task is complete."


def test_failed_update_never_reuses_previous_packet(harness):
    agent, app, backend, resolver, updater, runtime, trace = harness(["SampleApp__observe"] * 4 + ["FINISH"])
    updater.fail_on = 2
    agent.run("Stop if material cannot be updated")
    assert agent.stop_reason == "llm_update_error"
    assert agent.step_count == 4
    assert len(backend.calls) == len(app.calls) == 4
    assert runtime.counts["updates"] == 2
    assert agent.update_count == 1
    assert of_type(trace, "hybrid_update_error")[-1]["retained_packet_used"] is False


def test_initial_updater_failure_leaves_zero_decision_rounds(harness):
    agent, app, backend, resolver, updater, runtime, trace = harness(["FINISH"])
    updater.fail_on = 1
    agent.run("Handle provider failure")
    assert agent.stop_reason == "llm_update_error"
    assert agent.step_count == runtime.counts["decision_rounds"] == 0
    assert not backend.calls


def test_oversized_complete_request_stops_without_truncating_menu(harness):
    agent, app, backend, resolver, updater, runtime, trace = harness(["FINISH"], config={
        "hybrid": {"request_limits": {"max_jev_request_bytes": 500}}})
    original = deepcopy(agent.catalog.options)
    agent.run("Keep original task and all options")
    assert agent.stop_reason == "state_budget_exceeded"
    assert not backend.calls and not app.calls
    assert agent.catalog.options == original
    measurement = of_type(trace, "state_budget_exceeded")[-1]["measurements"]
    assert measurement["components"]["tool_menu"]["utf8_bytes"] > 500


def test_exhausted_http_budget_stops_half_action_with_no_tool_or_updater(harness):
    agent, app, backend, resolver, updater, runtime, trace = harness(
        ["SampleApp__optional"], limits={"max_http_attempts": 1})
    backend.before_response = lambda stage: runtime.reserve_http()
    agent.run("Respect model attempt cap")
    assert agent.stop_reason == "max_http_attempts"
    assert len(backend.calls) == 1 and not app.calls
    assert len(updater.inputs) == 1
    assert of_type(trace, "decision_end")[0]["status"] == "ok"
    assert of_type(trace, "budget_stop")[-1]["reason"] == "max_http_attempts"


def test_updater_deadline_never_schedules_a_following_jev_call(harness):
    agent, app, backend, resolver, updater, runtime, trace = harness(["FINISH"])
    original = updater.update
    def expired(update_input):
        result = original(update_input)
        runtime.deadline = runtime.started - 1
        return result
    updater.update = expired
    agent.run("No request after deadline")
    assert agent.stop_reason == "wall_timeout"
    assert not backend.calls and not app.calls
    assert runtime.counts["decision_rounds"] == 0


def test_interrupted_native_call_preserves_unknown_side_effects(harness):
    agent, app, backend, resolver, updater, runtime, trace = harness(["SampleApp__observe"])
    def interrupted():
        runtime.stop("wall_timeout")
    agent.catalog.tools_map["SampleApp__observe"] = interrupted
    agent.run("Bound real native attempt")
    assert agent.stop_reason == "wall_timeout"
    assert runtime.counts["tool_attempts"] == agent.tool_call_count == 1
    assert of_type(trace, "tool_interrupted")[0]["executed"] is True
    assert of_type(trace, "budget_stop")[-1]["side_effects"] == "unknown"
    assert runtime.active_boundary is None
    terminal = of_type(trace, "tool_end")[0]
    assert terminal["executed"] is terminal["interrupted"] is True
    assert terminal["result"] is None and terminal["workflow_step_name"] is None
    assert not agent.workflow.dag


def test_preexecution_argument_rejection_does_not_reserve_native_attempt(harness):
    agent, app, backend, resolver, updater, runtime, trace = harness(["FINISH"])
    row = agent.executor.execute("SampleApp__optional", {"count": True})
    assert row["executed"] is False
    assert runtime.counts["tool_attempts"] == 0
    assert not app.calls


def test_runtime_hook_does_not_add_hybrid_material_to_pure_jev(harness):
    agent, app, backend, resolver, updater, runtime, trace = harness(["SampleApp__observe", "FINISH"], pure=True)
    agent.run("Original pure task")
    assert not updater.inputs
    assert all("packet" not in row["state"] for row in backend.calls)
    assert "observations" in backend.calls[-1]["state"]
    assert app.calls == ["observe"]
    assert not of_type(trace, "packet_accepted")
