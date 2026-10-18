from __future__ import annotations

import json
from pathlib import Path

import pytest

from fairy.agents.Jev.runner import _coverage, _first_issue, run_mode


CONFIG = json.loads((Path(__file__).resolve().parents[2] / "configs/jev/hn58_irrigation.json").read_text())
REQUIRED = {"manifest.json", "tool_catalog.json", "parameter_support_matrix.json", "trace.jsonl", "workflow.json", "evaluation.json", "summary.md"}


def test_missing_key_preserves_complete_null_status_artifacts(tmp_path, monkeypatch):
    monkeypatch.delenv(CONFIG["api_key_env"], raising=False)
    first, result = run_mode("live", CONFIG, tmp_path)
    second, other = run_mode("live", CONFIG, tmp_path)
    assert first != second
    assert REQUIRED <= {p.name for p in first.iterdir()}
    assert result["stop_reason"] == "missing_api_key"
    assert result["live_integration_success"] is None
    assert result["task_success"] is None
    assert result["model_request_count"] == 0
    assert len(result["missing_stages"]) == 4


def test_scripted_full_loop_and_replay_report_separate_task_status(tmp_path):
    source, result = run_mode("scripted", CONFIG, tmp_path / "source")
    assert REQUIRED <= {p.name for p in source.iterdir()}
    assert result["scripted_loop_check"] == "passed"
    assert result["scripted_task_success"] is True
    assert result["task_success"] is None and result["live_integration_success"] is None
    assert result["decision_call_count"] == 16 and result["tool_call_count"] == 10
    assert result["model_request_count"] == 0
    replay_dir, replay = run_mode("replay", CONFIG, tmp_path / "replay", workflow_path=source / "workflow.json")
    assert REQUIRED <= {p.name for p in replay_dir.iterdir()}
    assert replay["replay"]["replay_success"] is True
    assert replay["replay_task_success"] is True
    assert replay["scripted_task_success"] is True
    assert replay["task_success"] is None


def test_replay_conflict_is_auditable_terminal_failure(tmp_path):
    source, _ = run_mode("scripted", CONFIG, tmp_path / "source")
    replay, result = run_mode("replay", {**CONFIG, "seed": 999}, tmp_path / "replay", workflow_path=source / "workflow.json")
    assert REQUIRED <= {p.name for p in replay.iterdir()}
    assert result["stop_reason"] == "replay_error"
    assert "seed" in result["error"]
    assert result["task_success"] is None


def test_live_coverage_needs_correlated_two_choices_execution_and_later_request():
    ends = [
        {"event": "decision_end", "event_id": 1, "decision_id": "d1", "decision_stage": "tool", "status": "ok", "selected_option": "Tool__act", "response": {"backend": "typesafe_sdk"}},
        {"event": "decision_end", "event_id": 3, "decision_id": "d2", "decision_stage": "parameters", "status": "ok", "selected_option": "args_000", "response": {"backend": "typesafe_sdk"}},
        {"event": "tool_end", "event_id": 5, "tool_call_id": "t1", "decision_ids": ["d1", "d2"], "executed": True, "result": {"status": "ok"}},
    ]
    incomplete = _coverage(ends, "live", True)
    assert incomplete["live_integration_success"] is None
    assert incomplete["missing_stages"] == ["result_in_later_real_request"]
    request = {"event": "decision_start", "event_id": 6, "decision_id": "d3", "state": {"observations": [{"tool_call_id": "t1", "result": {"status": "ok"}}]}}
    complete = _coverage(ends + [request], "live", True)
    assert complete["live_integration_success"] is True
    assert complete["coverage_chains"][0]["decision_ids"] == ["d1", "d2"]
    failed = _coverage(ends + [request, {"event": "decision_end", "status": "error"}], "live", True)
    assert failed["live_integration_success"] is False
    assert _coverage(ends + [request], "scripted", True)["live_integration_success"] is None


TASK = "HEINONG58分区为ridges 43-63。只对HEINONG58区执行0.8小时灌溉并等待复查。"


def _observed_events():
    return [{"event": "tool_end", "event_id": i, "status": "ok", "tool_name": name, "arguments": args, "result": result}
            for i, (name, args, result) in enumerate([
                ("WeatherApp__get_current_weather", {}, {}),
                ("SensorApp__read_soil_sensors", {}, {"soil_sensors": [{"ridge_start": 33, "ridge_end": 63, "vwc": 0.14}]}),
                ("FarmWorldApp__get_ridge_range_state", {"start": 43, "end": 63}, {}),
                ("FarmWorldApp__get_inventory", {}, {}),
            ])]


def _irrigation_event(event_id=4, ridges=None, dose=0.8):
    return {"event": "tool_end", "event_id": event_id, "tool_call_id": f"t{event_id}", "decision_ids": [f"tool_d{event_id}", f"args_d{event_id}"],
            "status": "ok", "executed": True, "tool_name": "FieldOpsApp__irrigate_ridge", "arguments": {"ridge_id": 43, "duration_hours": dose},
            "result": {"status": "irrigation_started", "irrigated_ridges": [43] if ridges is None else ridges, "duration_hours_per_ridge": dose}}


def test_partial_single_ridge_not_wrong_until_premature_finish():
    events = _observed_events() + [_irrigation_event()]
    assert _first_issue(events, TASK) is None
    issue = _first_issue(events + [{"event": "model_finish", "event_id": 5, "decision_id": "finish_d"}], TASK,
                         {"new_irrigation_exact_target_and_dose": {"passed": False}})
    assert issue["event_id"] == 5
    assert issue["decision_id"] == "finish_d"
    assert issue["details"]["remaining_target_ridges"] == list(range(44, 64))
    assert issue["failure_layer"] == "tool_selection"


def test_missing_pre_observations_identifies_first_irrigation_choice():
    issue = _first_issue([_irrigation_event()], TASK)
    assert issue["event_id"] == 4
    assert issue["decision_id"] == "tool_d4"
    assert issue["failure_layer"] == "tool_selection"
    assert len(issue["details"]["missing_pre_irrigation_observations"]) == 4


def test_cumulative_overdose_and_outside_target_identify_argument_choice():
    events = _observed_events() + [_irrigation_event(), _irrigation_event(5)]
    issue = _first_issue(events, TASK)
    assert issue["event_id"] == 5
    assert issue["decision_id"] == "args_d5"
    assert issue["details"]["overdosed_ridges"] == [43]
    outside = _first_issue(_observed_events() + [_irrigation_event(ridges=[42, 43])], TASK)
    assert outside["failure_layer"] == "parameter_selection"
    assert outside["details"]["outside_target_ridges"] == [42]


@pytest.mark.parametrize("stage,expected_layer", [("tool_call", "native_tool"), ("tool_return", "native_tool"), ("tool_args_parse", "execution_adapter")])
def test_native_raised_errors_distinguished_from_preexecution_adapter_errors(stage, expected_layer):
    issue = _first_issue([{"event": "tool_end", "event_id": 0, "status": "error", "error_stage": stage,
                           "executed": stage != "tool_args_parse", "error": "test failure"}], TASK)
    assert issue["failure_layer"] == expected_layer
    assert issue["details"]["error_stage"] == stage
