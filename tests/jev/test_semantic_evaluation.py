"""Semantic acceptance over real simulator effects and adversarial evidence."""
from __future__ import annotations

import math

import pytest

from fairy.agents.Jev.environment import snapshot_environment
from fairy.experiments.evaluation import evaluate_task, semantic_first_issue
from test_evaluation_replay import fixture, observations


def irrigate(executor):
    return executor.execute("FieldOpsApp__irrigate_range", {"start": 43, "end": 63, "duration_hours": 0.8})


def wait(executor, engine, result):
    executor.execute("SystemApp__advance_time", {"seconds": max(1, math.ceil(result["result"]["effect_ready_at"] - engine.time_manager.time()))})


def report(executor, content="已完成目标区灌溉并复查土壤水分。"):
    executor.execute("AgentUserInterface__send_message_to_user", {"content": content})


@pytest.mark.parametrize("sensors,passed", [(None, True), (["S4", "S5", "S6"], True), (["S5", "S6"], False)])
def test_sensor_recheck_actual_coverage_not_tool_name(tmp_path, sensors, passed):
    scenario, engine, _, workflow, trace, executor = fixture(tmp_path)
    before = snapshot_environment(scenario, engine)
    observations(executor)
    action = irrigate(executor)
    wait(executor, engine, action)
    if sensors is None:
        executor.execute("SensorApp__read_soil_sensors", {})
    else:
        for sensor in sensors:
            executor.execute("SensorApp__read_soil_sensor", {"sensor_id": sensor})
    report(executor)
    result = evaluate_task(scenario, engine, workflow, before)
    assert result["task_success"] is passed
    assert result["legacy_evaluation_v1"]["task_success"] is False
    assert result["assertions"]["processed_waited_rechecked_response"]["passed"] is passed
    coverage = result["assertions"]["processed_waited_rechecked_response"]["evidence"]["returned_coverage"]
    assert (43 in coverage) is passed
    trace.close()


def test_soil_read_before_response_cannot_count_as_post_response_recheck(tmp_path):
    scenario, engine, _, workflow, trace, executor = fixture(tmp_path)
    before = snapshot_environment(scenario, engine)
    observations(executor)
    action = irrigate(executor)
    read = executor.execute("SensorApp__read_soil_sensors", {})
    assert read["sim_time_before"] < action["result"]["effect_ready_at"]
    wait(executor, engine, action)
    report(executor)
    result = evaluate_task(scenario, engine, workflow, before)
    assert result["task_success"] is False
    assert result["assertions"]["processed_waited_rechecked_response"]["evidence"]["returned_coverage"] == []
    trace.close()


@pytest.mark.parametrize("alter", ["pre", "post"])
def test_range_arguments_do_not_prove_returned_coverage(tmp_path, alter):
    scenario, engine, _, workflow, trace, executor = fixture(tmp_path)
    before = snapshot_environment(scenario, engine)
    observations(executor)
    pre = next(step for step in workflow.dag.values() if step.tool_name == "FarmWorldApp__get_ridge_range_state")
    action = irrigate(executor)
    wait(executor, engine, action)
    row = executor.execute("FarmWorldApp__get_ridge_range_state", {"start": 43, "end": 63})
    post = workflow.dag[row["tool_call_id"]]
    # Adversarial evidence fixture: requested all target ridges, returned only one.
    chosen = pre if alter == "pre" else post
    chosen.content = {"ridges": chosen.content["ridges"][:1]}
    report(executor)
    result = evaluate_task(scenario, engine, workflow, before)
    assert result["legacy_evaluation_v1"]["task_success"] is True
    assert result["task_success"] is False
    assertion = "pre_irrigation_observations" if alter == "pre" else "processed_waited_rechecked_response"
    assert result["assertions"][assertion]["passed"] is False
    trace.close()


@pytest.mark.parametrize("early", [False, True])
def test_native_terminal_report_is_accepted_only_after_complete_recheck(tmp_path, early):
    scenario, engine, _, workflow, trace, executor = fixture(tmp_path)
    before = snapshot_environment(scenario, engine)
    observations(executor)
    action = irrigate(executor)
    before_recheck = engine.time_manager.time()
    wait(executor, engine, action)
    executor.execute("SensorApp__read_soil_sensors", {})
    events = [{"event": "native_event", "event_id": 900, "sim_time_numeric": before_recheck if early else engine.time_manager.time(),
        "native_event": {"event_type": "llm_call_end", "status": "ok", "tool_calls": [],
                         "assistant_content": "Target irrigation completed; post-response moisture recheck completed."}}]
    result = evaluate_task(scenario, engine, workflow, before, events=events)
    assert result["task_success"] is (not early)
    assert result["legacy_evaluation_v1"]["task_success"] is False
    trace.close()


def test_late_quota_read_cannot_repair_pre_irrigation_requirement(tmp_path):
    scenario, engine, _, workflow, trace, executor = fixture(tmp_path)
    before = snapshot_environment(scenario, engine)
    for name, args in [("WeatherApp__get_current_weather", {}), ("SensorApp__read_soil_sensors", {}),
                       ("FarmWorldApp__get_ridge_range_state", {"start": 43, "end": 63})]:
        executor.execute(name, args)
    action = irrigate(executor)
    executor.execute("FarmWorldApp__get_inventory", {})
    wait(executor, engine, action)
    executor.execute("SensorApp__read_soil_sensors", {})
    report(executor)
    result = evaluate_task(scenario, engine, workflow, before)
    assert result["task_success"] is False
    assert result["assertions"]["pre_irrigation_observations"]["evidence"]["quota"] is False
    assert result["assertions"]["processed_waited_rechecked_response"]["passed"] is True
    assert result["first_failed_assertion"] == "pre_irrigation_observations"
    trace.close()


def test_complete_effects_do_not_turn_explicit_incomplete_report_into_success(tmp_path):
    scenario, engine, _, workflow, trace, executor = fixture(tmp_path)
    before = snapshot_environment(scenario, engine)
    observations(executor)
    action = irrigate(executor)
    wait(executor, engine, action)
    executor.execute("SensorApp__read_soil_sensors", {})
    report(executor, "已灌溉，但尚未同时确认全部目标区复查，不足以宣称任务完成。")
    result = evaluate_task(scenario, engine, workflow, before)
    assert result["assertions"]["processed_waited_rechecked_response"]["passed"] is True
    assert result["assertions"]["factual_final_report"]["passed"] is False
    trace.close()


def test_returned_overview_can_supply_pre_irrigation_target_operational_state(tmp_path):
    scenario, engine, _, workflow, trace, executor = fixture(tmp_path)
    before = snapshot_environment(scenario, engine)
    for name in ("WeatherApp__get_current_weather", "SensorApp__read_soil_sensors",
                 "FarmWorldApp__get_farm_overview", "FarmWorldApp__get_inventory"):
        executor.execute(name, {})
    action = irrigate(executor)
    wait(executor, engine, action)
    executor.execute("SensorApp__read_soil_sensors", {})
    report(executor)
    result = evaluate_task(scenario, engine, workflow, before)
    assert result["assertions"]["pre_irrigation_observations"]["passed"] is True
    assert result["legacy_evaluation_v1"]["assertions"]["pre_irrigation_observations"]["passed"] is False
    assert result["task_success"] is True
    assert semantic_first_issue(trace.events, engine._resolve_agent_task(), result["assertions"]) is None
    trace.close()


def test_native_premature_terminal_response_has_evidenced_termination_issue():
    task = "HEINONG58 ridges 43-63 执行0.8小时灌溉。"
    events = [{"event_id": 4, "event": "native_event", "native_event": {
        "event_type": "llm_call_end", "status": "ok", "tool_calls": [], "assistant_content": "Done"}}]
    issue = semantic_first_issue(events, task, {"pre_irrigation_observations": {"passed": False}})
    assert issue["event_id"] == 4
    assert issue["failure_layer"] == "termination"
    assert issue["details"]["unmet_task_assertions"] == ["pre_irrigation_observations"]


def test_service_failure_located_before_decision_error_without_task_assertions():
    events = [{"event_id": 2, "event": "http_attempt_end", "status": "http_error", "http_status": 451,
               "role": "jev_tool_choice", "logical_request_id": "model_0001", "attempt_id": "http_0001",
               "raw_response_body": "Service unavailable in request region"},
              {"event_id": 3, "event": "decision_end", "status": "error", "error": "Backend error"}]
    issue = semantic_first_issue(events, "HEINONG58 ridges 43-63 执行0.8小时灌溉。")
    assert issue["event_id"] == 2 and issue["failure_layer"] == "model_service"
    assert issue["details"]["http_status"] == 451
