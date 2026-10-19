import copy
import json
from collections import Counter
from pathlib import Path

import pytest

from fairy.agents.Jev.candidates import (
    CANDIDATE_STRATEGY_VERSION, CandidateResolver, extract_task_parameters,
    factual_report, task_numeric_values, task_wait_seconds, visible_wait_seconds,
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


def test_fungicide_task_dose_is_reachable_and_existing_choices_retained(public_fixture):
    from fairy.scenarios.farm_world_fullseason_v2.l2_l1_splits.hb_disease_then_drought_recovery_tradeoff.scenario_l1_hb_disease_drought_mid_fungicide_20_43_action import ScenarioL1HbDiseaseDroughtMidFungicide2043Action

    scenario = ScenarioL1HbDiseaseDroughtMidFungicide2043Action()
    scenario.setup()
    task = next(e.kwargs["content"] for e in scenario.events if e.event_id == "briefing" and not e.is_oracle)
    catalog, config, state = public_fixture
    result = CandidateResolver(catalog.entries, config).generate("TractorApp__apply_fungicide", {**state, "task": task})
    assert result["status"] == "ready"
    assert result["candidate_strategy_version"] == CANDIDATE_STRATEGY_VERSION
    doses = {c["arguments"]["liters_per_ridge"] for c in result["candidates"].values()}
    assert {2.0, 3.0, 3.8, 4.0} <= doses
    for start, end in [(20, 29), (30, 39), (40, 43)]:
        matching = [c for c in result["candidates"].values() if c["arguments"] == {
            "start_ridge": start, "end_ridge": end, "liters_per_ridge": 3.8}]
        assert matching and any(ref.startswith("visible_task:quantity:") for ref in matching[0]["source_refs"])


@pytest.mark.parametrize("tool,key,expected,excluded", [
    ("TractorApp__apply_fungicide", "liters_per_ridge", 3.8, {6.4, 12.5, 27.5, 30.0, 7.0}),
    ("TractorApp__spray_pesticide", "liters_per_ridge", 6.4, {3.8, 12.5, 27.5, 30.0, 7.0}),
    ("TractorApp__apply_fertilizer", "kg_per_ridge", 12.5, {3.8, 6.4, 27.5, 30.0, 7.0}),
    ("TractorApp__refuel", "liters", 27.5, {3.8, 6.4, 12.5, 7.0}),
    ("TractorApp__load_fungicide", "liters", 30.0, {3.8, 6.4, 12.5, 27.5, 7.0}),
])
def test_task_numbers_are_matched_to_action_and_units(public_fixture, tool, key, expected, excluded):
    catalog, config, state = public_fixture
    task = "步骤7：只处理20-43垄。杀菌剂剂量3.8L/垄；杀虫剂6.4L/垄；施肥12.5kg/垄；加油27.5L；装载杀菌剂30L。"
    result = CandidateResolver(catalog.entries, config).generate(tool, {**state, "task": task})
    assert result["status"] == "ready"
    values = {c["arguments"][key] for c in result["candidates"].values()}
    assert expected in values
    assert values.isdisjoint(excluded)


def test_irrigation_duration_is_separate_from_wait_and_wait_units_convert(public_fixture):
    catalog, config, state = public_fixture
    state = {**state, "task": "对43-63垄灌溉0.8小时，等待2小时后复查；随后等待15分钟，再推进1天。"}
    resolver = CandidateResolver(catalog.entries, config)
    doses = {c["arguments"]["hours"] for c in resolver.generate("FieldOpsApp__irrigate", state)["candidates"].values()}
    assert 0.8 in doses and 2.0 not in doses and 15.0 not in doses
    assert {seconds for seconds, _ in task_wait_seconds(state["task"])} == {7200, 900, 86400}
    for tool, parameter in [("SystemApp__advance_time", "seconds"), ("SystemApp__wait_for_notification", "timeout")]:
        result = resolver.generate(tool, state)
        values = {c["arguments"][parameter] for c in result["candidates"].values()}
        assert {7200, 900, 86400} <= values
        assert 2880 not in values


def test_planting_geometry_and_seed_count_keep_types_and_parameter_meaning(public_fixture):
    catalog, config, state = public_fixture
    state = {**state, "task": "0-63垄，垄距1.2 m，播深3.5 cm、株距7.9 cm；装载20500粒种子。"}
    resolver = CandidateResolver(catalog.entries, config)
    planting = resolver.generate("TractorApp__plant_seeds", state)
    assert planting["status"] == "ready"
    assert any(c["arguments"]["depth_cm"] == 3.5 and c["arguments"]["seed_spacing_cm"] == 7.9 for c in planting["candidates"].values())
    assert {c["arguments"]["ridge_width_m"] for c in resolver.generate("TractorApp__form_ridges", state)["candidates"].values()} >= {1.1, 1.2}
    counts = {c["arguments"]["count"] for c in resolver.generate("TractorApp__load_seeds", state)["candidates"].values()}
    assert 20500 in counts and all(type(count) is int for count in counts)
    for candidate in planting["candidates"].values():
        assert validate_arguments(catalog.by_name["TractorApp__plant_seeds"], candidate["arguments"]) == candidate["arguments"]


def test_midpoints_use_only_spare_capacity_and_never_remove_exact_task_values():
    tool = "TractorApp__load_fungicide"
    entry = {"name": tool, "parameters": [{"name": "liters", "type": "float", "required": True}]}
    config = {"candidate_catalog": {"tool_values": {tool: {"liters": {"values": list(range(1, 255))}}}}}
    resolver = CandidateResolver([entry], config)
    state = {"task": "装载杀菌剂3.8L。"}
    result = resolver.generate(tool, state)
    assert result["status"] == "ready" and result["candidate_count"] == 255
    assert {c["arguments"]["liters"] for c in result["candidates"].values()} == set(range(1, 255)) | {3.8}
    assert result["numeric_enrichment"]["skipped_midpoints"]
    assert result == resolver.generate(tool, state)
    overflow = resolver.generate(tool, {"task": "装载杀菌剂3.8L；装载杀菌剂6.4L。"})
    assert overflow["status"] == "candidate_limit_exceeded" and overflow["candidates"] == {}


@pytest.mark.parametrize("key,kind,task", [
    ("depth_cm", "float", "播深8cm"),
    ("count", "int", "装载3.5粒种子"),
    ("liters_per_ridge", "float", "杀菌剂-3.8L/垄"),
    ("liters_per_ridge", "float", "风速3.8m/s，20-43垄的步骤7。"),
])
def test_invalid_or_unrelated_task_numbers_do_not_become_numeric_choices(key, kind, task):
    method = "load_seeds" if key == "count" else "plant_seeds" if key == "depth_cm" else "apply_fungicide"
    assert task_numeric_values("TractorApp__" + method, {"name": key, "type": kind}, task) == []


@pytest.mark.parametrize("tool,key,task,expected", [
    ("FieldOpsApp__irrigate", "hours", "等待6小时后执行0.65小时定向灌溉", 0.65),
    ("FieldOpsApp__irrigate", "hours", "对43-63垄执行irrigation，hours=0.8，等待响应后复查。", 0.8),
    ("FarmWorldApp__apply_fertigation", "nutrient_amount", "对8-19垄执行水肥恢复：fertigation amount=0.24，water_mm=1.2。", 0.24),
    ("TractorApp__plant_seeds", "seed_spacing_cm", "按4.0 cm播深和7.9 cm株距全田播种。", 7.9),
    ("TractorApp__apply_herbicide", "liters_per_ridge", "装载29.4L除草剂。\n7.只对44-55垄定向施药；按44-53垄和54-55垄两段完成，剂量为2.4L/垄。", 2.4),
    ("FieldOpsApp__apply_pesticide_manual", "liters_per_ridge", "若虫害达到阈值，只对确认区域做人工背负式点喷，每2垄一组，剂量3.2L/垄。", 3.2),
])
def test_public_task_phrasings_bind_correct_numeric_value(tool, key, task, expected):
    assert {v for v, _ in task_numeric_values(tool, {"name": key, "type": "float"}, task)} == {expected}
    if key == "hours" and "6小时" in task:
        assert {v for v, _ in task_wait_seconds(task)} == {21600}


def test_zone_specific_spacing_keeps_declared_values_and_required_zone_combinations(public_fixture):
    from fairy.scenarios.registry import get_scenario_class

    catalog, config, state = public_fixture
    scenario = get_scenario_class("scenario_full_season_hb_three_cultivar_wet_disease_dry_harvest_sequence")()
    scenario.setup()
    task = next(e.kwargs["content"] for e in scenario.events if not e.is_oracle and e.function.__name__ == "send_message_to_agent")
    resolver = CandidateResolver(catalog.entries, config)
    result = resolver.generate("TractorApp__plant_seeds", {**state, "task": task})
    assert result["status"] == "ready" and result["candidate_count"] <= 255
    assert result["numeric_enrichment"]["task_spacing_scope_filtered_candidates"] > 0
    candidates = [c["arguments"] for c in result["candidates"].values()]
    # Every public zone has a reachable task-prescribed spacing at every ridge.
    for start, end, spacing in [(0, 20, 8.2), (21, 42, 7.9), (43, 63, 8.4)]:
        covered = {ridge for c in candidates if c["seed_spacing_cm"] == spacing and c["depth_cm"] == 4.0
                   for ridge in range(c["start_ridge"], c["end_ridge"] + 1)}
        assert covered == set(range(start, end + 1))
    # Existing catalog spacing 10 remains available over all prior ranges.
    for pair, _ in resolver._range_options({**state, "task": task}, "TractorApp__plant_seeds"):
        for depth in (3.0, 4.0, 5.0):
            assert {"start_ridge": pair[0], "end_ridge": pair[1], "depth_cm": depth, "seed_spacing_cm": 10.0} in candidates


def test_ambiguous_spacing_scope_stays_unbound_and_overflow_is_explicit(public_fixture):
    catalog, config, state = public_fixture
    resolver = CandidateResolver(catalog.entries, config)
    task = "ridges 0-20 and ridges 21-42 and ridges 43-63，株距6.1cm、株距7.9cm、株距8.2cm。"
    result = resolver.generate("TractorApp__plant_seeds", {**state, "task": task})
    assert result["status"] == "candidate_limit_exceeded"
    assert result["numeric_enrichment"]["task_spacing_scope_filtered_candidates"] == 0


@pytest.mark.parametrize("task", [
    "灌溉0.65小时后等待6小时", "等待6小时后灌溉0.65小时", "等待6小时后执行0.65小时定向灌溉",
])
def test_duration_binding_never_jumps_over_a_then_transition(task):
    assert {v for v, _ in task_numeric_values("FieldOpsApp__irrigate", {"name": "hours", "type": "float"}, task)} == {0.65}
    assert {v for v, _ in task_wait_seconds(task)} == {21600}
