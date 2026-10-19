"""Actual public briefing regression; no Agent, tool execution, or network."""
from __future__ import annotations

import ast
from pathlib import Path
import socket

import pytest

from fairy.agents.Jev.environment import create_fixture, public_task
from fairy.experiments.suite_inventory import (
    config_for_scenario,
    discover_scenarios,
    preflight_one,
    validate_public_task,
)


ROOT = Path(__file__).resolve().parents[2]
ROWS = discover_scenarios()
AFFECTED = {
    "scenario_l1_hb_disease_drought_r5_irrigation_20_43_action",
    "scenario_l1_hb_laterain_insect_r5_insecticide_36_55_action",
    "scenario_l1_hb_lowcarbon_wetdisease_mid_fungicide_24_47_action",
    "scenario_l1_hb_potassium_dry_r1_fertigation_20_39_action",
    "scenario_l1_hb_skip_rows_emergence_replant_28_31_action",
    "scenario_l1_hb_wetjune_recheck_mid_fungicide_20_43_action",
    "scenario_l1_hb_wetjune_recheck_r5_fungicide_20_43_action",
    "scenario_l2_hb_disease_drought_r5_irrigation_20_43",
    "scenario_l2_hb_hn60_water_budget_r5_irrigation_40_55",
    "scenario_l2_hb_laterain_insect_r5_insecticide_36_55",
    "scenario_l2_hb_lowcarbon_wetdisease_mid_fungicide_24_47",
    "scenario_l2_hb_r5_leaf_feeder_r5_insecticide_16_39",
    "scenario_l2_hb_skip_rows_emergence_replant_28_31",
    "scenario_l2_hb_wetjune_recheck_mid_fungicide_20_43",
    "scenario_l2_hb_wetjune_recheck_r5_fungicide_20_43",
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Public task regressions must not perform network calls")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.mark.parametrize("row", ROWS, ids=lambda row: row["scenario_id"])
def test_all_formal_scenarios_deliver_actual_briefing(row):
    config = config_for_scenario(row, {"seed": 1904})
    scenario, engine, metadata = create_fixture(config)
    task = public_task(engine)
    validate_public_task(task)
    assert metadata["environment_check"] == "passed"
    briefings = [
        event.kwargs.get("content") for event in scenario.events
        if not event.is_oracle and event.function.__name__ == "send_message_to_agent"
    ]
    assert briefings and task in briefings
    if row["scenario_id"] in AFFECTED:
        assert task.startswith("任务：")
        assert "复查" in task or "闭环" in task


@pytest.mark.parametrize("scenario_id", sorted(AFFECTED))
def test_repaired_scenarios_also_deliver_short_briefing(scenario_id):
    row = next(row for row in ROWS if row["scenario_id"] == scenario_id)
    config = config_for_scenario(row, {"seed": 1904})
    scenario, engine, _ = create_fixture(config)
    detailed_task = public_task(engine)
    # Rebuild captured definitions only; no scheduled event is executed.
    scenario.detailed_briefing = False
    scenario.build_events_flow()
    short_task = public_task(engine)
    validate_public_task(short_task)
    assert short_task.startswith("任务：")
    assert short_task != detailed_task
    assert len(short_task) < len(detailed_task)
    assert all("任务：" not in event.event_id for event in scenario.events)


def test_all_management_callers_bind_briefing_and_prefix_correctly():
    """Audit the previously correct callers too, without changing their API."""
    helpers = {"build_management_l1_flow", "build_management_l2_flow"}
    calls = 0
    for path in (ROOT / "fairy/scenarios").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in helpers:
                continue
            calls += 1
            keywords = {kw.arg: kw.value for kw in node.keywords}
            briefing = keywords.get("briefing_text")
            prefix = keywords.get("prefix")
            if briefing is None and len(node.args) > 3:
                briefing = node.args[3]
            if prefix is None and len(node.args) > 4:
                prefix = node.args[4]
            assert isinstance(briefing, ast.Name) and briefing.id == "briefing_text", path
            assert isinstance(prefix, ast.Constant) and prefix.value.startswith("o_"), path
            if path.stem in AFFECTED:
                assert {"briefing_text", "prefix"} <= keywords.keys(), path
    assert calls == 32


@pytest.mark.parametrize("task", [None, "", " \n ", "o_r5_irrigation_20_43", " o_example \n"])
def test_input_guard_rejects_missing_or_identifier_only_briefing(task):
    with pytest.raises(ValueError, match="Invalid public task"):
        validate_public_task(task)


@pytest.mark.parametrize("task", ["任务：灌溉后复查。", "Inspect o_r5_irrigation_20_43 and report.", "Observe."])
def test_input_guard_does_not_grade_task_semantics(task):
    assert validate_public_task(task) is None


def test_preflight_rejects_identifier_even_with_healthy_fixture(monkeypatch):
    from fairy.agents.Jev import environment
    monkeypatch.setattr(environment, "public_task", lambda engine: "o_r5_irrigation_20_43")
    row = next(row for row in ROWS if row["scenario_id"] in AFFECTED)
    with pytest.raises(ValueError, match="only an oracle event identifier"):
        preflight_one(row, {"seed": 1904})
