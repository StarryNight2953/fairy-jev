"""Protocol and failure boundaries on actual AppTools, without a live API call."""

from copy import deepcopy
import json

import pytest

from fairy.agents.Jev.agent import JevAgent
from fairy.agents.Jev.client import Decision, DecisionError
from fairy.agents.Jev.tools import ToolCatalog, validate_arguments
from fairy.agents.Jev.trace import TraceWriter
from fairy.apps.app import App
from fairy.time_manager import TimeManager
from fairy.tool_utils import app_tool


class ToyApp(App):
    def __init__(self):
        super().__init__()
        self.executions = []

    @app_tool()
    def observe(self) -> dict:
        """Read a visible condition."""
        self.executions.append(("observe", {}))
        return {"visible": "dry", "revision": len(self.executions)}

    @app_tool()
    def act(self, ridge_id: int, amount: float, enabled: bool = True) -> dict:
        """Act exactly once on one ridge; the native tool owns its clock change."""
        arguments = {"ridge_id": ridge_id, "amount": amount, "enabled": enabled}
        self.executions.append(("act", arguments))
        self.time_manager.add_offset(30)
        return {"status": "started", "arguments": arguments}

    @app_tool()
    def optional(self, days: int = 7) -> dict:
        self.executions.append(("optional", {"days": days}))
        return {"days": days}

    @app_tool()
    def returned_error(self) -> dict:
        self.executions.append(("returned_error", {}))
        return {"status": "rejected", "reason": "quota exceeded"}

    @app_tool()
    def exploding(self) -> dict:
        self.executions.append(("exploding", {}))
        raise RuntimeError("native test failure")


class Backend:
    name = "scripted"

    def __init__(self, sequence, order):
        self.sequence = iter(sequence)
        self.calls = []
        self.order = order

    def decide(self, state, question_id, instructions, options):
        self.order.append(f"decision:{question_id}")
        self.calls.append({"stage": question_id, "state": deepcopy(state), "options": deepcopy(options)})
        selected = next(self.sequence)
        if isinstance(selected, Exception):
            raise selected
        if callable(selected):
            selected = selected()
        return Decision(selected, {"backend": "scripted", "selected_option": selected})


class Resolver:
    def __init__(self, result, order):
        self.result, self.order = result, order
        self.calls = []

    def generate(self, tool_name, visible):
        self.order.append(f"resolve:{tool_name}")
        self.calls.append((tool_name, deepcopy(visible)))
        return deepcopy(self.result)


def ready(arguments):
    return {"status": "ready", "candidates": {"args_000": {"arguments": arguments, "source_refs": ["declared_catalog:test"]}}}


@pytest.fixture
def harness(tmp_path):
    traces = []

    def make(sequence, *, generation=None, budgets=None):
        app, order = ToyApp(), []
        catalog = ToolCatalog([app])
        backend = Backend(sequence, order)
        resolver = Resolver(generation if generation is not None else ready({"ridge_id": 4, "amount": 0.8, "enabled": True}), order)
        trace = TraceWriter(tmp_path / f"trace-{len(traces)}.jsonl", f"test-{len(traces)}")
        traces.append(trace)
        agent = JevAgent(catalog, backend, resolver, trace, {"budgets": {"max_steps": 10, **(budgets or {})}})
        clock = TimeManager()
        clock.reset(1000.0)
        app.register_time_manager(clock)
        agent.set_time_manager(clock)
        return agent, app, backend, resolver, trace, order

    yield make
    for trace in traces:
        trace.close()


def events(trace, kind):
    return [event for event in trace.events if event["event"] == kind]


def test_two_stage_order_single_candidate_and_native_execution(harness):
    agent, app, backend, resolver, trace, order = harness(["ToyApp__act", "args_000", "FINISH"])
    agent.run("Irrigate one ridge")
    assert order == ["decision:tool", "resolve:ToyApp__act", "decision:parameters", "decision:tool"]
    assert len(resolver.calls) == 1
    assert app.executions == [("act", {"ridge_id": 4, "amount": 0.8, "enabled": True})]
    assert agent.tool_call_count == 1
    parameter_request = [call for call in backend.calls if call["stage"] == "parameters"][0]
    assert list(parameter_request["options"]) == ["args_000"]
    assert parameter_request["options"]["args_000"]["source_refs"] == ["declared_catalog:test"]
    # Source references stay in candidate descriptions; only native kwargs execute.
    action = events(trace, "tool_end")[0]
    assert len(action["decision_ids"]) == 2
    assert action["sim_time_after"] - action["sim_time_before"] == pytest.approx(30, abs=0.1)
    assert action["arguments"] == app.executions[0][1]
    assert backend.calls[-1]["state"]["observations"][0]["result"] == action["result"]
    assert agent.stop_reason == "model_finish"
    assert len(agent.workflow) == 1
    assert events(trace, "parameter_candidates")[0]["candidate_count"] == 1


def test_no_parameters_skips_resolver_and_second_choice(harness):
    agent, app, backend, resolver, trace, order = harness(["ToyApp__observe", "FINISH"])
    agent.run("Observe")
    assert not resolver.calls
    assert order == ["decision:tool", "decision:tool"]
    assert app.executions == [("observe", {})]
    assert len(events(trace, "tool_end")[0]["decision_ids"]) == 1
    assert events(trace, "parameter_candidates")[0]["generation"]["candidates"]["args_000"]["arguments"] == {}


def test_optional_only_tool_still_gets_parameter_choice_and_real_defaults(harness):
    agent, app, backend, resolver, trace, order = harness(["ToyApp__optional", "args_000", "FINISH"], generation=ready({}))
    agent.run("Get a forecast")
    assert [call["stage"] for call in backend.calls] == ["tool", "parameters", "tool"]
    assert app.executions == [("optional", {"days": 7})]
    assert events(trace, "tool_end")[0]["arguments"] == {"days": 7}


def test_full_tool_menu_is_unchanged_after_parameter_failure(harness):
    failure = {"status": "needs_context", "missing_fields": ["ridge_id"], "reason": "No public target", "candidates": {}}
    agent, app, backend, resolver, trace, order = harness(["ToyApp__act", "ToyApp__observe", "FINISH"], generation=failure)
    agent.run("Read more context")
    menus = [call["options"] for call in backend.calls if call["stage"] == "tool"]
    assert menus[0] == menus[1] == menus[2] == agent.catalog.options
    assert "ToyApp__act" in menus[1]
    assert backend.calls[1]["state"]["controller_feedback"][0]["executed"] is False
    assert backend.calls[1]["state"]["controller_feedback"][0]["missing_fields"] == ["ridge_id"]
    assert app.executions == [("observe", {})]
    assert len({event["candidate_hash"] for event in events(trace, "decision_start")}) == 1


@pytest.mark.parametrize("generation", [
    {"status": "ready", "candidates": {}},
    ready({"amount": 0.8}),
    ready({"ridge_id": 4, "amount": 0.8, "unknown": 1}),
    ready({"ridge_id": True, "amount": 0.8}),
    {"status": "unsupported", "reason": "No public resolver", "candidates": {}},
])
def test_invalid_or_empty_candidates_do_not_execute_or_request_parameters(harness, generation):
    agent, app, backend, resolver, trace, order = harness(["ToyApp__act", "FINISH"], generation=generation)
    agent.run("Choose a legal action")
    assert not app.executions
    assert agent.tool_call_count == 0
    assert all(call["stage"] == "tool" for call in backend.calls)
    assert len(events(trace, "controller_feedback")) == 1
    assert trace.events[-1]["event"] == "run_end"


def test_candidate_overflow_stops_for_configuration_without_truncation(harness):
    generation = {"status": "ready", "candidates": {
        f"args_{i:03d}": {"arguments": {"ridge_id": i, "amount": 0.8}, "source_refs": ["declared_catalog:test"]}
        for i in range(256)}}
    agent, app, backend, resolver, trace, order = harness(["ToyApp__act"], generation=generation)
    agent.run("Select one candidate")
    assert agent.stop_reason == "candidate_limit_exceeded"
    assert agent.failure_layer == "candidate_configuration"
    assert not app.executions
    assert agent.decision_count == 1
    generated = events(trace, "parameter_candidates")[0]
    assert generated["candidate_count"] == len(generated["generation"]["candidates"]) == 256


def test_repeated_same_missing_context_is_bounded(harness):
    failure = {"status": "needs_context", "missing_fields": ["ridge_id"], "reason": "No public target", "candidates": {}}
    agent, app, backend, resolver, trace, order = harness(["ToyApp__act"] * 3, generation=failure,
                                                       budgets={"repeated_parameter_failure_limit": 3})
    agent.run("Observe before acting")
    assert agent.stop_reason == "repeated_parameter_failure"
    assert agent.step_count == agent.decision_count == 3
    assert not app.executions
    assert len(events(trace, "controller_feedback")) == 3


@pytest.mark.parametrize("selection", ["ToyApp__unknown", DecisionError("injected network failure", {"request_id": None})])
def test_decision_errors_leave_linked_flushed_start_end_and_run_end(harness, selection):
    agent, app, backend, resolver, trace, order = harness([selection])
    agent.run("Inspect")
    assert agent.stop_reason == "decision_error"
    assert agent.failure_layer == "decision_backend"
    assert not app.executions
    start, end = events(trace, "decision_start")[0], events(trace, "decision_end")[0]
    assert start["decision_id"] == end["decision_id"]
    assert start["attempt_id"] == end["attempt_id"]
    assert end["status"] == "error"
    assert [json.loads(line) for line in trace.path.read_text().splitlines()][-1]["event"] == "run_end"


def test_invalid_parameter_response_never_executes(harness):
    agent, app, backend, resolver, trace, order = harness(["ToyApp__act", "args_unsubmitted"])
    agent.run("Inspect")
    assert agent.stop_reason == "decision_error"
    assert not app.executions
    assert agent.decision_count == 2
    assert events(trace, "decision_end")[-1]["decision_stage"] == "parameters"


@pytest.mark.parametrize("tool,error_stage", [("ToyApp__returned_error", "tool_return"), ("ToyApp__exploding", "tool_call")])
def test_native_errors_are_recorded_and_visible_in_next_decision(harness, tool, error_stage):
    agent, app, backend, resolver, trace, order = harness([tool, "FINISH"])
    agent.run("Respond to actual errors")
    assert len(app.executions) == agent.tool_call_count == 1
    row = events(trace, "tool_end")[0]
    assert row["status"] == "error"
    assert row["executed"] is True
    assert row["error_stage"] == error_stage
    assert backend.calls[-1]["state"]["observations"][0]["status"] == "error"
    assert next(iter(agent.workflow.dag.values())).error_stage == error_stage


@pytest.mark.parametrize("expiration_stage", ["tool", "parameters"])
def test_deadline_boundary_prevents_new_action_after_inflight_request(harness, monkeypatch, expiration_stage):
    now = [100.0]
    monkeypatch.setattr("fairy.agents.Jev.agent.time.perf_counter", lambda: now[0])

    def expire():
        now[0] += 11
        return "ToyApp__act" if expiration_stage == "tool" else "args_000"

    sequence = [expire] if expiration_stage == "tool" else ["ToyApp__act", expire]
    agent, app, backend, resolver, trace, order = harness(sequence, budgets={"wall_timeout_seconds": 10})
    agent.run("Bounded run")
    assert agent.stop_reason == "wall_timeout"
    assert not app.executions
    assert agent.decision_count == (1 if expiration_stage == "tool" else 2)
    assert trace.events[-1]["timeout_overrun_seconds"] == 1
    assert events(trace, "budget_stop")[0]["actual_elapsed_seconds"] == 11


@pytest.mark.parametrize("budgets,sequence,reason,executions", [
    ({"max_decision_calls": 1}, ["ToyApp__act"], "max_decision_calls", 0),
    ({"max_steps": 1}, ["ToyApp__observe"], "max_steps", 1),
    ({"max_tool_calls": 1}, ["ToyApp__observe"], "max_tool_calls", 1),
])
def test_budgets_stop_without_extra_requests_or_actions(harness, budgets, sequence, reason, executions):
    agent, app, backend, resolver, trace, order = harness(sequence, budgets=budgets)
    agent.run("Stay bounded")
    assert agent.stop_reason == reason
    assert len(app.executions) == agent.tool_call_count == executions
    assert len(backend.calls) == 1


@pytest.mark.parametrize("arguments", [
    {"ridge_id": True, "amount": 0.8}, {"ridge_id": 4.0, "amount": 0.8},
    {"ridge_id": 4, "amount": True}, {"ridge_id": 4, "amount": float("inf")},
    {"ridge_id": 4, "amount": float("nan")}, {"ridge_id": 4, "amount": "0.8"},
    {"ridge_id": 4, "amount": 0.8, "enabled": 1},
])
def test_signature_rejects_bool_number_confusion_and_nonfinite(arguments):
    entry = ToolCatalog([ToyApp()]).by_name["ToyApp__act"]
    with pytest.raises(ValueError):
        validate_arguments(entry, arguments)


def test_signature_preserves_int_float_bool_and_real_default():
    entry = ToolCatalog([ToyApp()]).by_name["ToyApp__act"]
    assert validate_arguments(entry, {"ridge_id": 4, "amount": 1}) == {"ridge_id": 4, "amount": 1, "enabled": True}
    assert validate_arguments(entry, {"ridge_id": 4, "amount": 0.8, "enabled": False}) == {
        "ridge_id": 4, "amount": 0.8, "enabled": False}


@pytest.mark.parametrize("nonfinite,label", [(float("nan"), "nan"), (float("inf"), "inf"), (-float("inf"), "-inf")])
def test_direct_executor_rejects_nonfinite_with_json_trace_and_workflow(harness, tmp_path, nonfinite, label):
    agent, app, backend, resolver, trace, order = harness([])
    row = agent.executor.execute("ToyApp__act", {"ridge_id": 4, "amount": nonfinite})
    assert row["executed"] is False
    assert row["status"] == "error"
    assert row["error_stage"] == "tool_args_parse"
    assert not app.executions
    assert agent.tool_call_count == 0
    start, end = events(trace, "tool_start")[0], events(trace, "tool_end")[0]
    assert start["tool_call_id"] == end["tool_call_id"]
    assert start["arguments"]["amount"] == {"__nonfinite_float__": label}
    assert end["arguments"]["amount"] == {"__nonfinite_float__": label}
    workflow_path = tmp_path / f"invalid-{label}.json"
    agent.workflow.save_workflow(str(workflow_path))

    def reject_nonstandard_constant(value):
        pytest.fail(f"Invalid JSON numeric token in artifact: {value}")

    json.loads(workflow_path.read_text(), parse_constant=reject_nonstandard_constant)
    for line in trace.path.read_text().splitlines():
        json.loads(line, parse_constant=reject_nonstandard_constant)


@pytest.mark.parametrize("nonfinite", [float("nan"), float("inf"), -float("inf")])
def test_injected_nonfinite_candidate_preserves_failure_lifecycle(harness, nonfinite):
    agent, app, backend, resolver, trace, order = harness(
        ["ToyApp__act", "FINISH"], generation=ready({"ridge_id": 4, "amount": nonfinite}))
    agent.run("Reject nonfinite candidates without losing trace")
    assert not app.executions
    assert agent.stop_reason == "model_finish"
    assert [call["stage"] for call in backend.calls] == ["tool", "tool"]
    candidate_event = events(trace, "parameter_candidates")[0]
    assert candidate_event["generation"]["status"] == "needs_context"
    assert "must be finite" in candidate_event["generation"]["reason"]
    assert candidate_event["generation"]["candidates"]["args_000"]["arguments"]["amount"] == {"__nonfinite_float__": repr(nonfinite)}
    assert len(events(trace, "controller_feedback")) == 1
    assert len(events(trace, "decision_start")) == len(events(trace, "decision_end")) == 2
    assert trace.events[-1]["event"] == "run_end"


def test_pre_execution_failures_keep_unique_workflow_and_trace_ids(harness):
    agent, app, backend, resolver, trace, order = harness([])
    executor = agent.executor
    missing = executor.execute("ToyApp__act", {"amount": 0.8})
    invalid = executor.execute("ToyApp__act", {"ridge_id": True, "amount": 0.8})
    success = executor.execute("ToyApp__act", {"ridge_id": 4, "amount": 0.8})
    assert missing["executed"] is invalid["executed"] is False
    assert missing["error_stage"] == invalid["error_stage"] == "tool_args_parse"
    assert success["executed"] is True
    assert len(app.executions) == agent.tool_call_count == 1
    assert len({row["tool_call_id"] for row in [missing, invalid, success]}) == 3
    assert len(agent.workflow) == 3
    assert len(events(trace, "tool_start")) == len(events(trace, "tool_end")) == 3
