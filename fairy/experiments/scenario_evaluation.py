"""Cross-scenario post-run observations without inventing a task-success gate.

The existing L1/L2 validators mostly detect tool errors; many L3 scenarios have
no validator. Their return values and existing workflow metrics remain evidence,
but neither is promoted to independently established task completion.
"""
from __future__ import annotations

from copy import deepcopy
import inspect
from pathlib import Path
from types import SimpleNamespace

from fairy.agents.Jev.environment import snapshot_environment
from fairy.scenarios.scenario import Scenario
from fairy.scenarios.workflow_validation import evaluate_workflows, workflow_from_oracle_events

VERSION = "scenario_native_v1"
SCOPE = "Original scenario validation and descriptive workflow metrics; no independent semantic task-success contract"


def _native_validation(scenario, workflow, output_dir, *, oracle_available):
    validator = getattr(scenario, "validate", None)
    identity = {
        "module": getattr(validator, "__module__", None),
        "qualname": getattr(validator, "__qualname__", None),
    }
    if not callable(validator) or getattr(validator, "__func__", None) is Scenario.validate:
        return {"success": None, "status": "not_implemented",
                "reason": "Scenario has no overridden task validator", "validator": identity}
    if not oracle_available:
        # A validator may call ensure_oracle_workflow. Never let its fallback run
        # the oracle when static definitions could not be safely cached.
        return {"success": None, "status": "not_executed",
                "reason": "Static oracle workflow unavailable; execution fallback prohibited", "validator": identity}
    completed = [{"event_id": name, "metadata": {"return_value": step.content}}
                 for name, step in workflow.dag.items() if step.tool_name]

    def get_app(name):
        for app in scenario.apps or []:
            if app.name == name or type(app).__name__ == name:
                return app
        raise ValueError(f"No scenario app named {name}")

    env = SimpleNamespace(workflow=workflow, completed_events=completed,
                          dump_dir=str(Path(output_dir)) if output_dir else None,
                          get_app=get_app)
    try:
        signature = inspect.signature(validator)
        try:
            signature.bind(env)
        except TypeError:
            signature.bind()
            value = validator()
        else:
            value = validator(env)
        if hasattr(value, "to_dict"):
            value = value.to_dict()
        if isinstance(value, dict):
            # Preserve original fields verbatim; interpretation is recorded in
            # separate top-level fields rather than overwriting the validator.
            return deepcopy(value)
        if value is None:
            return {"success": None, "status": "no_verdict", "validator": identity}
        if type(value) is bool:
            return {"success": value, "raw_result": value, "validator": identity}
        return {"success": None, "status": "unsupported_result",
                "raw_result": repr(value), "validator": identity}
    except Exception as exc:
        return {"success": None, "status": "error", "error": f"{type(exc).__name__}: {exc}",
                "validator": identity}


def evaluate_task(scenario, engine, workflow, before, *, events=(), environment_valid=True, output_dir=None):
    """Preserve observed outcomes; unknown task completion is always ``None``.

    No model requests, oracle actions, or new acceptance thresholds are used.
    Static oracle definitions support the repository's existing descriptive
    workflow metrics and prevent native validators from executing an oracle.
    """
    after = snapshot_environment(scenario, engine)
    oracle_error = metrics_error = None
    oracle = None
    metrics = None
    try:
        oracle = workflow_from_oracle_events(scenario)
        scenario._cached_oracle_workflow = oracle
    except Exception as exc:
        oracle_error = f"{type(exc).__name__}: {exc}"
    if oracle is not None:
        try:
            metrics = evaluate_workflows(oracle, workflow.to_dict())
        except Exception as exc:
            metrics_error = f"{type(exc).__name__}: {exc}"
    native = _native_validation(scenario, workflow, output_dir, oracle_available=oracle is not None)
    native_success = native.get("success")
    native_success = native_success if type(native_success) is bool else None
    return {
        "evaluator_version": VERSION,
        "evaluation_scope": SCOPE,
        "evaluation_status": "observational_only",
        "weak_contract": True,
        "task_success": None,
        "task_success_status": "not_independently_assessed",
        "task_success_limitations": [
            "A native true may only mean no detected tool errors; it does not establish task completion.",
            "A native false may reflect a recoverable tool error; it is not a general semantic failure verdict.",
            "Workflow similarity is descriptive, with no newly imposed success threshold.",
        ],
        "environment_check": "passed" if environment_valid else "failed",
        "assertions": {},
        "first_failed_assertion": None,
        "failure_layer": None if environment_valid else "environment",
        "native_validation": native,
        "native_contract_success": native_success,
        "workflow_metrics": metrics,
        "workflow_metrics_scope": "Existing exact tool-and-argument path metrics against static oracle definitions; not semantic equivalence",
        "workflow_metrics_error": metrics_error,
        "oracle_definition_error": oracle_error,
        "oracle_definition_source": "workflow_from_oracle_events; no oracle execution",
        "oracle_step_count": len(oracle) if oracle is not None else None,
        "private_evidence_before": deepcopy(before),
        "private_evidence_after": after,
    }


def generic_first_issue(events, task="", assertions=None):
    """Earliest recorded technical/candidate/limit issue, without task guesses.

    ``task`` and ``assertions`` retain the experiment dispatch interface. A
    FINISH or native terminal response is not itself a demonstrated failure.
    """
    for event in events:
        kind = event.get("event")
        layer = reason = None
        details = {}
        if kind == "http_attempt_end" and (
            event.get("status") in {"error", "http_error"}
            or isinstance(event.get("http_status"), int) and event["http_status"] >= 400
        ):
            layer, reason = "model_service", "Recorded model HTTP attempt failed"
            details = {key: event.get(key) for key in
                       ("http_status", "role", "logical_request_id", "attempt_id", "error", "raw_response_body")}
        elif kind == "decision_end" and event.get("status") == "error":
            layer, reason = "decision_backend", event.get("error") or "Decision failed"
        elif kind in {"hybrid_update_error", "llm_update_error"}:
            layer, reason = "llm_context_update", event.get("error") or "Context update failed"
        elif kind == "controller_feedback":
            layer, reason = "parameter_candidates", event.get("reason") or event.get("status") or "Candidate preparation failed"
            details = {key: event.get(key) for key in ("tool_name", "status", "missing_fields", "executed")}
        elif kind == "tool_end" and event.get("status") == "error":
            layer = "native_tool" if event.get("executed") else "execution_adapter"
            reason = event.get("error") or "Recorded tool execution error"
            details = {key: event.get(key) for key in ("tool_name", "executed", "error_stage", "result")}
        elif kind == "state_budget_exceeded":
            layer, reason = "context_budget", "Model input exceeded declared size limit"
            details = {"measurements": event.get("measurements")}
        elif kind in {"budget_stop", "episode_budget_stop", "run_end", "native_run_end"}:
            stop = str(event.get("reason") or event.get("stop_reason") or "")
            if kind in {"budget_stop", "episode_budget_stop"} or stop.startswith(("max_", "total_")) or stop in {"wall_timeout", "timeout"}:
                layer, reason = "run_budget", stop or "Recorded run limit reached"
                details = {"stop_reason": stop, "side_effects": event.get("side_effects")}
        if reason:
            return {
                "event_id": event.get("event_id"), "step_id": event.get("step_id"),
                "decision_id": event.get("decision_id"), "decision_ids": event.get("decision_ids"),
                "tool_call_id": event.get("tool_call_id"), "failure_layer": layer,
                "reason": reason, "details": details, "evaluator_version": VERSION,
                "evidence": "trace.jsonl",
                "scope": "Earliest recorded technical or execution-limit issue; not an independent task-success verdict",
            }
    return None
