"""Independent, post-run HN58 task assertions; never used to guide decisions."""
from __future__ import annotations

import math
import re
from pathlib import Path
from types import SimpleNamespace

from .environment import public_task, snapshot_environment
from .tools import result_is_error

IRRIGATION_TOOLS = {"FieldOpsApp__irrigate", "FieldOpsApp__irrigate_range", "FieldOpsApp__irrigate_ridge"}
TOLERANCE_HOURS = 1e-9
TOLERANCE_QUOTA_MM = 2e-6  # Native quota accumulation rounds to 6 decimals.


def _target(task):
    match = re.search(r"HEINONG58[^。]*?(?:ridges\s*)?(\d+)\s*[-–]\s*(\d+)", task)
    dose = re.search(r"(\d+(?:\.\d+)?)\s*(?:小时|hours?)\s*灌溉", task)
    if not match or not dose:
        raise ValueError("Visible briefing has no unambiguous HN58 range/duration")
    return list(range(int(match.group(1)), int(match.group(2)) + 1)), float(dose.group(1))


def native_validation(scenario, workflow, output_dir=None):
    """Retain native result; cache static definitions to avoid executing the oracle."""
    from fairy.scenarios.workflow_validation import workflow_from_oracle_events
    scenario._cached_oracle_workflow = workflow_from_oracle_events(scenario)
    events = [{"event_id": name, "metadata": {"return_value": step.content}}
              for name, step in workflow.dag.items() if step.tool_name]
    env = SimpleNamespace(workflow=workflow, completed_events=events,
                          dump_dir=str(Path(output_dir)) if output_dir else None)
    try:
        result = scenario.validate(env)
        return result.to_dict() if hasattr(result, "to_dict") else result
    except Exception as exc:
        return {"success": None, "error": f"{type(exc).__name__}: {exc}"}


def evaluate_task(scenario, engine, workflow, before, *, environment_valid=True, output_dir=None):
    """Check only newly added actions; native success alone is never task success."""
    after = snapshot_environment(scenario, engine)
    target, required_hours = _target(public_task(engine))
    target_set = set(target)
    steps = [s.to_dict() for s in workflow.dag.values() if s.tool_name]
    successful = [s for s in steps if s.get("op_type") != "TOOL_ERROR" and not result_is_error(s.get("content"))]
    irrigation_steps = [s for s in successful if s["tool_name"] in IRRIGATION_TOOLS
                        and isinstance(s.get("content"), dict) and s["content"].get("status") == "irrigation_started"]
    first_irrigation = min((s.get("time_before") or math.inf for s in irrigation_steps), default=math.inf)
    pre = [s for s in successful if s.get("time_after") is not None and s["time_after"] <= first_irrigation]
    pre_names = {s["tool_name"] for s in pre}
    observed_range = set()
    for step in pre:
        args = step.get("tool_args") or {}
        if step["tool_name"] == "FarmWorldApp__get_ridge_range_state":
            observed_range.update(range(args["start"], args["end"] + 1))
        elif step["tool_name"] == "FarmWorldApp__get_ridge_state":
            observed_range.add(args["ridge_id"])
    observed_soil = set()
    for step in pre:
        if step["tool_name"] not in {"SensorApp__read_soil_sensors", "SensorApp__read_soil_sensor"}:
            continue
        content = step.get("content") or {}
        readings = content.get("soil_sensors", [content]) if isinstance(content, dict) else []
        for reading in readings:
            if isinstance(reading, dict) and "vwc" in reading and "ridge_start" in reading and "ridge_end" in reading:
                observed_soil.update(range(reading["ridge_start"], reading["ridge_end"] + 1))
    observation_checks = {
        "weather": "WeatherApp__get_current_weather" in pre_names,
        "soil": target_set <= observed_soil,
        "target_state": target_set <= observed_range,
        "quota": "FarmWorldApp__get_inventory" in pre_names,
        "before_actual_irrigation": bool(irrigation_steps),
    }
    prefix_unchanged = after["actions"][:len(before["actions"])] == before["actions"]
    new_actions = after["actions"][len(before["actions"]):]
    new_irrigation = [a for a in new_actions if a["action_type"] == "irrigation"]
    dose_by_ridge = {}
    expected_usage = 0.0
    for action in new_irrigation:
        params = action["parameters"]
        dose = float(params.get("duration_hours", 0))
        water_mm = float(params.get("estimated_water_mm", dose * 5.0))
        expected_usage += water_mm * len(action["ridge_ids"]) / after["num_ridges"]
        for rid in action["ridge_ids"]:
            dose_by_ridge[rid] = dose_by_ridge.get(rid, 0.0) + dose
    coverage_ok = prefix_unchanged and set(dose_by_ridge) == target_set and all(
        abs(dose_by_ridge.get(rid, 0) - required_hours) <= TOLERANCE_HOURS for rid in target)
    regime_before, regime_after = before["management_regime"], after["management_regime"]
    used_before = float(regime_before["irrigation_used_mm_total"])
    used_after = float(regime_after["irrigation_used_mm_total"])
    quota = regime_before.get("irrigation_quota_mm_total")
    quota_ok = bool(new_irrigation) and expected_usage > 0 and abs(used_after-used_before-expected_usage) <= TOLERANCE_QUOTA_MM
    quota_ok = quota_ok and (quota is None or used_after <= float(quota) + TOLERANCE_QUOTA_MM)
    ready_at = max((float(s["content"]["effect_ready_at"]) for s in irrigation_steps), default=math.inf)
    waits = [s for s in successful if s["tool_name"] in {"SystemApp__advance_time", "SystemApp__wait_for_notification"}
             and (s.get("time_before") or 0) >= first_irrigation and (s.get("time_after") or 0) >= ready_at]
    rechecks = [s for s in successful if s["tool_name"] in {"FarmWorldApp__get_ridge_range_state", "FarmWorldApp__get_ridge_state"}
                and (s.get("time_before") or 0) >= ready_at]
    rechecked_ridges = set()
    first_recheck = math.inf
    for step in rechecks:
        args = step["tool_args"]
        rechecked_ridges.update(range(args["start"], args["end"] + 1) if "start" in args else [args["ridge_id"]])
        if target_set <= rechecked_ridges and math.isinf(first_recheck):
            first_recheck = step.get("time_after") or math.inf
    complete_recheck = target_set <= rechecked_ridges
    deltas = {str(rid): {key: after["soil"][str(rid)][key] - before["soil"][str(rid)][key]
                        for key in ("top_vwc", "root_vwc")} for rid in target}
    response_ok = all(deltas[str(rid)]["top_vwc"] > 1e-8 or deltas[str(rid)]["root_vwc"] > 1e-8 for rid in target)
    processed_ok = bool(new_irrigation) and not target_set.intersection(after["pending_irrigation_ridges"])
    # A report must follow completion of the whole target recheck, including per-ridge calls.
    reports = [s for s in successful if s["tool_name"] == "AgentUserInterface__send_message_to_user"
               and (s.get("time_before") or 0) >= first_recheck and str(s["tool_args"].get("content", "")).strip()]
    report_text = "\n".join(str(s["tool_args"].get("content", "")) for s in reports)
    report_has_facts = bool(reports) and bool(re.search(r"灌溉|irrigat", report_text, re.I)) and bool(re.search(r"复查|recheck|检查|observ", report_text, re.I))
    assertions = {
        "pre_irrigation_observations": {"passed": all(observation_checks.values()), "evidence": observation_checks},
        "new_irrigation_exact_target_and_dose": {"passed": coverage_ok, "evidence": {
            "checkpoint_action_prefix_unchanged": prefix_unchanged, "new_irrigation_actions": new_irrigation,
            "target_ridges": target, "required_hours": required_hours, "hours_by_ridge": dose_by_ridge,
            "tolerance_hours": TOLERANCE_HOURS}},
        "quota_consumption": {"passed": quota_ok, "evidence": {
            "formula": "sum(action.estimated_water_mm * len(action.ridge_ids) / num_ridges)",
            "irrigation_mapping_mm_per_hour": 5.0, "num_ridges": after["num_ridges"],
            "used_before": used_before, "used_after": used_after, "expected_delta": expected_usage,
            "quota_total": quota, "tolerance_field_mm": TOLERANCE_QUOTA_MM}},
        "processed_waited_rechecked_response": {"passed": processed_ok and bool(waits) and complete_recheck and response_ok,
            "evidence": {"processed": processed_ok, "effect_ready_at": ready_at if math.isfinite(ready_at) else None,
                "wait_steps": [s["name"] for s in waits], "recheck_steps": [s["name"] for s in rechecks],
                "soil_deltas": deltas, "response_present": response_ok,
                "physical_semantics": "subdaily irrigation may apply immediately; recheck must occur after visible ETA"}},
        "factual_final_report": {"passed": report_has_facts and coverage_ok and complete_recheck and bool(waits) and response_ok,
            "evidence": {"report_steps": [s["name"] for s in reports], "content": report_text}},
    }
    passed = all(a["passed"] for a in assertions.values())
    failed = next((name for name, value in assertions.items() if not value["passed"]), None)
    return {"task_success": passed if environment_valid else None,
            "environment_check": "passed" if environment_valid else "failed",
            "assertions": assertions, "first_failed_assertion": failed,
            "failure_layer": "environment" if not environment_valid else ("task_assertion" if failed else None),
            "native_validation": native_validation(scenario, workflow, output_dir),
            "private_evidence_before": before, "private_evidence_after": after}
