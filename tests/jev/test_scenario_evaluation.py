"""Offline tests for truthful cross-scenario scoring and oracle isolation."""
from __future__ import annotations

from types import SimpleNamespace
import socket

import pytest

from fairy.experiments import scenario_evaluation as evaluation
from fairy.scenarios.scenario import Scenario
from fairy.scenarios.validation_result import ScenarioValidationResult
from fairy.scenarios.workflow import Workflow, WorkflowStep
from fairy.scenarios.workflow_validation import append_workflow_evaluation


@pytest.fixture(autouse=True)
def offline_snapshots(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("This unit test must not access the network")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(evaluation, "snapshot_environment", lambda scenario, engine: {"sim_time": 2, "actions": ["actual"]})


def score(scenario, tmp_path, workflow=None, **kwargs):
    return evaluation.evaluate_task(scenario, object(), workflow or Workflow(),
                                    {"sim_time": 1, "actions": []}, output_dir=tmp_path, **kwargs)


def test_base_validator_is_unimplemented_not_called(tmp_path, monkeypatch):
    def forbidden(self):
        raise AssertionError("Base validator must not be called with any signature")
    monkeypatch.setattr(Scenario, "validate", forbidden)
    result = score(Scenario(), tmp_path)
    assert result["native_validation"]["status"] == "not_implemented"
    assert result["native_contract_success"] is None
    assert result["task_success"] is None
    assert result["assertions"] == {}


@pytest.mark.parametrize("native_success", [True, False])
def test_weak_native_verdict_is_preserved_without_semantic_promotion(tmp_path, native_success):
    class Weak(Scenario):
        def validate(self, env):
            assert env.workflow is not None
            return ScenarioValidationResult(success=native_success, rationale="Only checks tool errors",
                                            metadata={"raw": "preserved"})
    result = score(Weak(), tmp_path)
    assert result["native_contract_success"] is native_success
    assert result["native_validation"]["success"] is native_success
    assert result["native_validation"]["metadata"] == {"raw": "preserved"}
    assert result["task_success"] is None
    assert result["weak_contract"] is True
    assert result["first_failed_assertion"] is None
    assert result["failure_layer"] is None


def test_zero_argument_override_returning_none_is_no_verdict(tmp_path):
    class NoVerdict(Scenario):
        def validate(self):
            return None
    result = score(NoVerdict(), tmp_path)
    assert result["native_validation"]["status"] == "no_verdict"
    assert result["native_contract_success"] is None


def test_validator_exception_preserves_evidence_without_claiming_task_failure(tmp_path):
    class Broken(Scenario):
        def validate(self, env):
            raise ValueError("broken legacy validator")
    result = score(Broken(), tmp_path)
    assert result["native_validation"]["status"] == "error"
    assert result["native_validation"]["error"] == "ValueError: broken legacy validator"
    assert result["task_success"] is None
    assert result["failure_layer"] is None
    assert result["private_evidence_before"] == {"sim_time": 1, "actions": []}
    assert result["private_evidence_after"] == {"sim_time": 2, "actions": ["actual"]}


def test_static_oracle_metrics_and_native_validation_never_execute_oracle(tmp_path, monkeypatch):
    from fairy.controllers.engine import Engine
    calls = []

    class FakeApp:
        name = "FakeApp"
        def inspect(self, ridge_id: int):
            calls.append(ridge_id)
            raise AssertionError("Oracle action must never execute")

    class Weak(Scenario):
        def validate(self, env):
            assert env.completed_events[0]["metadata"]["return_value"] == {"ridge_id": 4}
            return append_workflow_evaluation(self, env, ScenarioValidationResult(success=True))

    def forbidden(*args, **kwargs):
        raise AssertionError("Oracle Engine fallback must never run")
    monkeypatch.setattr(Engine, "run_scenario_oracle", forbidden)
    app = FakeApp()
    scenario = Weak(scenario_id="offline_fixture")
    scenario.events = [SimpleNamespace(is_oracle=True, app=app, function=app.inspect,
                                       args=(), kwargs={"ridge_id": 4}, event_id="oracle_read", dependencies=[])]
    workflow = Workflow()
    workflow.add_node(WorkflowStep(name="read", tool_name="FakeApp__inspect", tool_args={"ridge_id": 4},
                                   content={"ridge_id": 4}, op_type="READ"))
    result = score(scenario, tmp_path, workflow)
    assert calls == []
    assert result["native_contract_success"] is True
    assert result["workflow_metrics"]["path_correctness"] == 1.0
    # Preserve the existing metric's single-match behavior: _ktc returns an
    # empty match list below two actions, so its coverage is zero even here.
    assert result["workflow_metrics"]["coverage"] == 0.0
    assert result["task_success"] is None
    assert result["oracle_step_count"] == 1
    assert (tmp_path / "workflows" / "workflow_oracle_offline_fixture.json").exists()


def test_oracle_definition_failure_does_not_trigger_native_oracle_fallback(tmp_path, monkeypatch):
    class Weak(Scenario):
        def validate(self, env):
            raise AssertionError("Native validator must be skipped without safe oracle cache")
    def broken(scenario):
        raise ValueError("malformed static definitions")
    monkeypatch.setattr(evaluation, "workflow_from_oracle_events", broken)
    result = score(Weak(), tmp_path)
    assert result["native_validation"]["status"] == "not_executed"
    assert result["oracle_definition_error"] == "ValueError: malformed static definitions"
    assert result["task_success"] is None


def test_invalid_environment_still_preserves_native_verdict_as_raw_evidence(tmp_path):
    class Weak(Scenario):
        def validate(self, env):
            return {"success": True, "rationale": "legacy"}
    result = score(Weak(), tmp_path, environment_valid=False)
    assert result["environment_check"] == "failed"
    assert result["failure_layer"] == "environment"
    assert result["native_contract_success"] is True
    assert result["task_success"] is None


def test_validator_internal_type_error_is_not_retried_as_another_signature(tmp_path):
    calls = []
    class Broken(Scenario):
        def validate(self, env):
            calls.append(env)
            raise TypeError("inside validator")
    result = score(Broken(), tmp_path)
    assert len(calls) == 1
    assert result["native_validation"]["error"] == "TypeError: inside validator"
    assert result["task_success"] is None


@pytest.mark.parametrize("event,layer", [
    ({"event": "http_attempt_end", "status": "http_error", "http_status": 451}, "model_service"),
    ({"event": "decision_end", "status": "error", "error": "invalid option"}, "decision_backend"),
    ({"event": "controller_feedback", "status": "candidate_limit_exceeded"}, "parameter_candidates"),
    ({"event": "tool_end", "status": "error", "executed": True}, "native_tool"),
    ({"event": "tool_end", "status": "error", "executed": False}, "execution_adapter"),
    ({"event": "state_budget_exceeded", "measurements": {"bytes": 200000}}, "context_budget"),
    ({"event": "episode_budget_stop", "reason": "max_tool_calls"}, "run_budget"),
])
def test_first_issue_captures_technical_evidence(event, layer):
    event = {"event_id": 5, **event}
    issue = evaluation.generic_first_issue([event, {"event_id": 6, "event": "model_finish"}])
    assert issue["event_id"] == 5
    assert issue["failure_layer"] == layer
    assert issue["evaluator_version"] == evaluation.VERSION


def test_generic_diagnosis_does_not_apply_hn58_preconditions_or_guess_finish_failure():
    events = [
        {"event_id": 0, "event": "tool_end", "status": "ok", "executed": True,
         "result": {"status": "irrigation_started", "irrigated_ridges": [4], "duration_hours": 1.25}},
        {"event_id": 1, "event": "model_finish"},
        {"event_id": 2, "event": "run_end", "stop_reason": "model_finish"},
    ]
    assert evaluation.generic_first_issue(events, "Perform a different task", {"legacy": {"passed": False}}) is None
