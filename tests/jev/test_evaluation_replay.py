from __future__ import annotations

import math

import pytest

from fairy.agents.Jev.environment import create_fixture, snapshot_environment
from fairy.agents.Jev.evaluation import evaluate_task
from fairy.agents.Jev.replay import replay_run
from fairy.agents.Jev.tools import ToolCatalog, ToolExecutor
from fairy.agents.Jev.trace import TraceWriter, write_json
from fairy.apps.farm_world import FarmWorldApp
from fairy.scenarios.workflow import Workflow, WorkflowStep

CONFIG = {"scenario_id": "scenario_l1_three_cultivar_hn58_action_ready_irrigation",
          "seed": 1904, "clock_source": "checkpoint", "scenario_kwargs": {}, "init_kwargs": {}}


def fixture(tmp_path):
    scenario, engine, metadata = create_fixture(CONFIG)
    workflow = Workflow()
    trace = TraceWriter(tmp_path / "trace.jsonl", "test")
    catalog = ToolCatalog(scenario.apps)
    executor = ToolExecutor(catalog, engine.time_manager, workflow, trace)
    return scenario, engine, metadata, workflow, trace, executor


def observations(executor):
    for name, args in [("WeatherApp__get_current_weather", {}), ("SensorApp__read_soil_sensors", {}),
                       ("FarmWorldApp__get_ridge_range_state", {"start": 43, "end": 63}),
                       ("FarmWorldApp__get_inventory", {})]:
        assert executor.execute(name, args)["status"] == "ok"


def finish(executor, engine, result):
    eta = result["result"]["effect_ready_at"]
    executor.execute("SystemApp__advance_time", {"seconds": max(1, math.ceil(eta-engine.time_manager.time()))})
    executor.execute("FarmWorldApp__get_ridge_range_state", {"start": 43, "end": 63})
    executor.execute("AgentUserInterface__send_message_to_user", {"content": "已灌溉并复查目标区。"})


def test_checkpoint_clock_alignment_and_declared_clock_defect():
    scenario, engine, metadata = create_fixture(CONFIG)
    assert metadata["declared_start_time"] < metadata["effective_start_time"]
    assert metadata["environment_check"] == "passed"
    assert all(app.time_manager is engine.time_manager for app in scenario.apps)
    _, _, bad = create_fixture({**CONFIG, "clock_source": "declared"})
    assert bad["environment_check"] == "failed"
    assert not bad["physics_not_ahead"]


@pytest.mark.parametrize("early_finish", [False, True])
def test_empty_and_early_finish_cannot_pass(tmp_path, early_finish):
    scenario, engine, _, workflow, trace, executor = fixture(tmp_path)
    before = snapshot_environment(scenario, engine)
    if early_finish:
        workflow.add_node(WorkflowStep(op_type="SYSTEM", content="model_finish"))
    result = evaluate_task(scenario, engine, workflow, before, output_dir=tmp_path)
    assert result["task_success"] is False
    assert result["native_validation"]["success"] is True  # Native acceptance is insufficient.
    assert not result["assertions"]["new_irrigation_exact_target_and_dose"]["passed"]
    trace.close()


def test_wrong_range_and_single_ridge_never_expand(tmp_path):
    scenario, engine, _, workflow, trace, executor = fixture(tmp_path)
    before = snapshot_environment(scenario, engine)
    observations(executor)
    row = executor.execute("FieldOpsApp__irrigate_ridge", {"ridge_id": 43, "duration_hours": 0.8})
    assert row["result"]["irrigated_ridges"] == [43]
    finish(executor, engine, row)
    result = evaluate_task(scenario, engine, workflow, before, output_dir=tmp_path)
    assert result["task_success"] is False
    actual = result["assertions"]["new_irrigation_exact_target_and_dose"]["evidence"]["hours_by_ridge"]
    assert actual == {43: 0.8}
    trace.close()


def test_repeated_overdose_rejected_by_actual_cumulative_dose(tmp_path):
    scenario, engine, _, workflow, trace, executor = fixture(tmp_path)
    # Isolated adversarial fixture permits excess water so the evaluator itself is tested.
    scenario.get_typed_app(FarmWorldApp)._management_regime.irrigation_quota_mm_total = 10.0
    before = snapshot_environment(scenario, engine)
    observations(executor)
    for _ in range(2):
        row = executor.execute("FieldOpsApp__irrigate", {"start": 43, "end": 63, "hours": 0.8})
        assert row["status"] == "ok"
    finish(executor, engine, row)
    result = evaluate_task(scenario, engine, workflow, before, output_dir=tmp_path)
    assert result["task_success"] is False
    evidence = result["assertions"]["new_irrigation_exact_target_and_dose"]["evidence"]
    assert evidence["hours_by_ridge"][43] == 1.6
    assert len(evidence["new_irrigation_actions"]) == 2
    trace.close()


def test_no_wait_or_recheck_blocks_false_completion(tmp_path):
    scenario, engine, _, workflow, trace, executor = fixture(tmp_path)
    before = snapshot_environment(scenario, engine)
    observations(executor)
    executor.execute("FieldOpsApp__irrigate", {"start": 43, "end": 63, "hours": 0.8})
    executor.execute("AgentUserInterface__send_message_to_user", {"content": "已完成全部灌溉并复查。"})
    result = evaluate_task(scenario, engine, workflow, before, output_dir=tmp_path)
    assert result["task_success"] is False
    assert not result["assertions"]["factual_final_report"]["passed"]
    trace.close()


def test_replay_preexecution_error_restored_without_execution(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    scenario, engine, metadata, workflow, trace, executor = fixture(source)
    before = snapshot_environment(scenario, engine)
    row = executor.execute("FieldOpsApp__irrigate", {"start": True, "end": 63, "hours": 0.8})
    assert row["executed"] is False
    workflow.save_workflow(str(source / "workflow.json"))
    write_json(source / "manifest.json", {**metadata, "config": CONFIG, "backend": "scripted"})
    write_json(source / "evaluation.json", evaluate_task(scenario, engine, workflow, before, output_dir=source))
    trace.close()
    replay = replay_run(source / "workflow.json", CONFIG, tmp_path / "replay")
    assert replay["replay"]["n_executed"] == 0
    assert replay["replay"]["n_restored_error_events"] == 1
    assert replay["replay"]["replay_success"] is True
    assert replay["private_evidence_before"]["actions"] == replay["private_evidence_after"]["actions"]
    assert replay["task_success"] is None
    assert replay["replay_task_success"] is False
    with pytest.raises(ValueError, match="seed"):
        replay_run(source / "workflow.json", {**CONFIG, "seed": 99}, tmp_path / "conflict")


def test_successful_native_loop_and_replay(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    scenario, engine, metadata, workflow, trace, executor = fixture(source)
    before = snapshot_environment(scenario, engine)
    observations(executor)
    row = executor.execute("FieldOpsApp__irrigate", {"start": 43, "end": 63, "hours": 0.8})
    finish(executor, engine, row)
    result = evaluate_task(scenario, engine, workflow, before, output_dir=source)
    assert result["task_success"] is True
    workflow.save_workflow(str(source / "workflow.json"))
    write_json(source / "manifest.json", {**metadata, "config": CONFIG, "backend": "scripted"})
    write_json(source / "evaluation.json", result)
    trace.close()
    replay = replay_run(source / "workflow.json", CONFIG, tmp_path / "replay")
    assert replay["replay"]["replay_success"] is True
    assert all(replay["replay"]["effect_comparisons"].values())
    assert replay["task_success"] is None
    assert replay["replay_task_success"] is True
    assert replay["tool_call_count"] == executor.call_count
