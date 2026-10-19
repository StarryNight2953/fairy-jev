"""Suite scope and clock identity checks without setup, tools, or networking."""
from collections import Counter
import json
import socket

import pytest

from fairy.experiments.suite_inventory import config_for_scenario, discover_scenarios
from fairy.scenarios.scenario import Scenario


@pytest.fixture(autouse=True)
def forbid_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Inventory discovery must not execute setup or network calls")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(Scenario, "setup", forbidden)


def test_current_suite_is_all_registered_splits_and_formal_l3():
    rows = discover_scenarios()
    assert len(rows) == len({row["scenario_id"] for row in rows}) == 246
    assert Counter(row["level"] for row in rows) == {"L1": 78, "L2": 78, "L3": 90}
    assert all(not row["legacy"] for row in rows)
    assert all(row["class_source_sha256"] for row in rows)
    assert json.loads(json.dumps(rows)) == rows
    l3 = [row for row in rows if row["level"] == "L3"]
    assert all(row["membership"] == "formal_l3_manifest" for row in l3)
    assert all(row["native_validator"]["is_default_no_result_validator"] for row in l3)


def test_legacy_scope_is_explicit_and_does_not_duplicate_current_suite():
    rows = discover_scenarios(include_legacy=True)
    assert len(rows) == len({row["scenario_id"] for row in rows}) == 274
    assert Counter(row["level"] for row in rows) == {"L1": 86, "L2": 86, "L3": 102}
    assert sum(row["legacy"] for row in rows) == 28
    assert {row["scenario_id"] for row in discover_scenarios()} < {row["scenario_id"] for row in rows}


def test_split_clocks_are_checkpoint_based_and_full_season_clocks_declared():
    rows = discover_scenarios()
    splits = [row for row in rows if row["level"] in {"L1", "L2"}]
    assert all(row["clock_source"] == "checkpoint" and row["checkpoint_exists"] for row in splits)
    assert all(row["checkpoint_sha256"] and row["effective_start_time"] == row["checkpoint_sim_time"] for row in splits)
    assert sum(abs(row["declared_minus_checkpoint_seconds"]) > 1e-6 for row in splits) == 13
    assert all(row["clock_source"] == "declared" and row["checkpoint_path"] is None for row in rows if row["level"] == "L3")
    hn58 = next(row for row in rows if row["scenario_id"] == "scenario_l1_three_cultivar_hn58_action_ready_irrigation")
    assert hn58["effective_start_time"] == pytest.approx(1786057131.7453332)
    assert hn58["declared_start_time"] != hn58["effective_start_time"]


def test_scenario_config_preserves_shared_controls_without_mutating_base():
    rows = discover_scenarios()
    row = next(row for row in rows if row["level"] == "L3")
    base = {"scenario_id": "old", "clock_source": "checkpoint", "effective_start_time": 1,
            "seed": 1904, "budgets": {"max_steps": 200}}
    config = config_for_scenario(row, base)
    assert config["scenario_id"] == row["scenario_id"]
    assert config["clock_source"] == "declared"
    assert config["effective_start_time"] == row["declared_start_time"]
    assert config["seed"] == 1904 and config["budgets"] == base["budgets"]
    config["budgets"]["max_steps"] = 3
    assert base["budgets"]["max_steps"] == 200 and base["effective_start_time"] == 1
