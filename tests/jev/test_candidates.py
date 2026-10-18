import copy
import json
from collections import Counter
from pathlib import Path

import pytest

from fairy.agents.Jev.candidates import (
    CandidateResolver, extract_task_parameters, factual_report, visible_wait_seconds,
)
from fairy.agents.Jev.tools import ToolCatalog, validate_arguments
from fairy.scenarios.farm_world_fullseason_v2.l2_l1_splits.hb_three_cultivar_wet_disease_dry_harvest_sequence.scenario_l1_three_cultivar_hn58_action_ready_irrigation import ScenarioL1ThreeCultivarHN58ActionReadyIrrigation


@pytest.fixture(scope="module")
def public_fixture():
    scenario = ScenarioL1ThreeCultivarHN58ActionReadyIrrigation()
    scenario.setup()
    catalog = ToolCatalog(scenario.apps)
    briefing = next(e.kwargs["content"] for e in scenario.events if e.event_id == "briefing" and not e.is_oracle)
    config = json.loads(Path("configs/jev/hn58_irrigation.json").read_text())
    state = {"task": briefing, "current_time": 1000, "observations": [], "recent_actions": []}
    return catalog, config, state


def test_actual_all_tool_support_and_complete_validated_calls(public_fixture):
    catalog, config, state = public_fixture
    resolver = CandidateResolver(catalog.entries, config)
    assert len(resolver.support_matrix()) == 58
    assert Counter(row["parameter_category"] for row in resolver.support_matrix()) == {
        "no_parameters": 20, "required": 34, "optional_only": 4,
    }
    for entry in catalog.entries:
        result = resolver.generate(entry["name"], state)
        assert result["status"] == "ready", result
        assert 1 <= result["candidate_count"] <= 255
        for candidate in result["candidates"].values():
            arguments = candidate["arguments"]
            assert set(arguments) == {p["name"] for p in entry["parameters"]}
            assert validate_arguments(entry, arguments) == arguments
            assert candidate["source_refs"]


def test_briefing_region_duration_changes_are_not_scenario_answers(public_fixture):
    catalog, config, state = public_fixture
    resolver = CandidateResolver(catalog.entries, config)
    assert extract_task_parameters(state["task"])["target_range"] == [43, 63]
    changed = {**state, "task": state["task"].replace("43-63", "45-60").replace("0.8小时", "0.4小时")}
    assert extract_task_parameters(changed["task"])["target_range"] == [45, 60]
    result = resolver.generate("FieldOpsApp__irrigate", changed)
    arguments = [x["arguments"] for x in result["candidates"].values()]
    assert {"start": 45, "end": 60, "hours": 0.4} in arguments
    assert {"start": 43, "end": 63, "hours": 0.8} not in arguments
    assert extract_task_parameters("only irrigate ridges 8-12 for 0.6 hours")["target_range"] == [8, 12]
    assert extract_task_parameters("No zone or duration given")["target_range"] is None


def test_only_selected_tool_full_calls_and_distinct_irrigation_semantics(public_fixture):
    catalog, config, state = public_fixture
    resolver = CandidateResolver(catalog.entries, config)
    expected = {
        "irrigate": {"start", "end", "hours"},
        "irrigate_range": {"start", "end", "duration_hours"},
        "irrigate_ridge": {"ridge_id", "duration_hours"},
    }
    for method, keys in expected.items():
        result = resolver.generate("FieldOpsApp__" + method, state)
        assert all(set(c["arguments"]) == keys for c in result["candidates"].values())
        assert all("source_refs" not in c["arguments"] for c in result["candidates"].values())


def test_optional_defaults_and_positive_waits(public_fixture):
    catalog, config, state = public_fixture
    resolver = CandidateResolver(catalog.entries, config)
    dry = resolver.generate("FarmWorldApp__dry_grain", state)
    forecast = resolver.generate("WeatherApp__get_forecast", state)
    assert {"target_moisture_pct": 13.0} in [c["arguments"] for c in dry["candidates"].values()]
    assert {"days": 7} in [c["arguments"] for c in forecast["candidates"].values()]
    for name in ["SystemApp__advance_time", "SystemApp__wait_for_notification"]:
        result = resolver.generate(name, state)
        assert all(sum(c["arguments"].values()) > 0 for c in result["candidates"].values())
    advanced = {**state, "observations": [{"tool_name": "FieldOpsApp__irrigate", "tool_call_id": "call1", "result": {"effect_ready_at": 8200}, "status": "ok", "sim_time_after": 1000}]}
    assert visible_wait_seconds(advanced) == [(7200, "tool_result:call1:effect_ready_at")]
    result = resolver.generate("SystemApp__advance_time", advanced)
    assert {"seconds": 7200, "minutes": 0, "hours": 0, "days": 0} in [c["arguments"] for c in result["candidates"].values()]
    assert visible_wait_seconds({**advanced, "current_time": 8201}) == []


def test_real_enum_catalog_and_sensor_tool_specific_ids(public_fixture):
    from fairy.apps.farm_world.models import SeedType
    from fairy.apps.farm_world.tractor_app import ATTACHMENTS
    catalog, config, state = public_fixture
    resolver = CandidateResolver(catalog.entries, config)
    def values(name, parameter):
        return {c["arguments"][parameter] for c in resolver.generate(name, state)["candidates"].values()}
    assert values("TractorApp__attach_implement", "implement") == set(ATTACHMENTS)
    assert values("TractorApp__load_seeds", "seed_type") == {v.value for v in SeedType}
    assert values("SensorApp__read_soil_sensor", "sensor_id") == {f"S{i}" for i in range(1, 7)}
    assert values("SensorApp__read_canopy_sensor", "sensor_id") == {f"C{i}" for i in range(1, 7)}
    assert values("TractorApp__replant_seeds", "full_replant") == {False, True}


def test_finite_catalog_missing_and_overflow_are_explicit(public_fixture):
    catalog, config, state = public_fixture
    config = copy.deepcopy(config)
    config["candidate_catalog"]["irrigation_hours"]["values"] = [0.1, 0.2, 0.3, 0.4]
    resolver = CandidateResolver(catalog.entries, config)
    before = list(resolver.entries)
    result = resolver.generate("FieldOpsApp__irrigate_ridge", state)
    assert result["status"] == "candidate_limit_exceeded"
    assert result["candidates"] == {}
    assert list(resolver.entries) == before
    config["candidate_catalog"]["tool_values"]["TractorApp__load_fertilizer"]["kg"]["values"] = []
    result = CandidateResolver(catalog.entries, config).generate("TractorApp__load_fertilizer", state)
    assert result["status"] == "needs_context"
    assert result["missing_fields"] == ["kg"]
    assert resolver.generate("Hidden__not_in_catalog", state)["status"] == "unsupported"


def test_per_pass_candidates_never_become_implicit_multiple_executions(public_fixture):
    catalog, config, state = public_fixture
    resolver = CandidateResolver(catalog.entries, config)
    for name, width in config["candidate_catalog"]["max_pass_width"].items():
        for candidate in resolver.generate(name, state)["candidates"].values():
            args = candidate["arguments"]
            assert 1 <= args["end_ridge"] - args["start_ridge"] + 1 <= width


def test_report_never_claims_completion_from_single_ridge_or_no_recheck(public_fixture):
    _, _, state = public_fixture
    action = {"tool_name": "FieldOpsApp__irrigate_ridge", "tool_call_id": "t1", "arguments": {"ridge_id": 43, "duration_hours": 0.8},
              "result": {"status": "irrigation_started", "irrigated_ridges": [43], "duration_hours_per_ridge": 0.8, "effect_ready_at": 8200},
              "status": "ok", "sim_time_before": 0, "sim_time_after": 1000}
    state = {**state, "observations": [action], "recent_actions": [action]}
    report = factual_report(state)
    assert "1/21" in report and "不足以宣称" in report
    assert "成功启动灌溉 1 次" in report  # deduplicates recent_actions
    action = {**action, "tool_name": "FieldOpsApp__irrigate", "result": {**action["result"], "irrigated_ridges": list(range(43, 64))}}
    state = {**state, "observations": [action], "recent_actions": []}
    assert "21/21" in factual_report(state) and "不足以宣称" in factual_report(state)
    waiting = {"tool_name": "SystemApp__advance_time", "tool_call_id": "t2", "arguments": {"seconds": 7200}, "result": {"status": "ok"},
               "status": "ok", "sim_time_before": 1000, "sim_time_after": 8200}
    recheck = {"tool_name": "FarmWorldApp__get_ridge_range_state", "tool_call_id": "t3", "arguments": {"start": 43, "end": 63},
               "result": {"ridges": [{"ridge_id": i, "soil_vwc": 0.3} for i in range(43,64)]}, "status": "ok", "sim_time_before": 8201, "sim_time_after": 8201}
    complete = {**state, "observations": [action, waiting, recheck]}
    assert "已有工具记录支持" in factual_report(complete)
    duplicate = {**action, "tool_call_id": "t4", "sim_time_before": 9000, "sim_time_after": 10000}
    assert "不足以宣称" in factual_report({**complete, "observations": [action, waiting, recheck, duplicate]})


def test_action_fact_ledger_preserves_overdose_and_errors_across_history_truncation(public_fixture):
    from fairy.agents.Jev.state import VisibleState
    _, _, source = public_fixture
    visible = VisibleState(max_observations=5, max_recent_actions=3)
    visible.task = source["task"]
    for number in range(2):
        visible.add_observation({"tool_name": "FieldOpsApp__irrigate", "tool_call_id": f"irrigate{number}", "write_operation": True,
                                 "status": "ok", "sim_time_before": number, "sim_time_after": number + 1,
                                 "result": {"status": "irrigation_started", "irrigated_ridges": list(range(43,64)),
                                            "duration_hours_per_ridge": 0.8, "effect_ready_at": 100}})
    visible.add_observation({"tool_name": "TractorApp__refuel", "tool_call_id": "old_error", "status": "error", "write_operation": True,
                             "sim_time_before": 3, "sim_time_after": 3, "result": {"error": "injected prior failure"}})
    for number in range(30):
        visible.add_observation({"tool_name": f"ObservedApp{number}__read", "tool_call_id": f"read{number}", "status": "ok",
                                 "sim_time_before": number + 10, "sim_time_after": number + 10, "result": {"ok": True}})
    report = factual_report(visible.snapshot(200))
    assert "成功启动灌溉 2 次" in report
    assert "0/21" in report
    assert "injected prior failure" in report
    assert "不足以宣称" in report
