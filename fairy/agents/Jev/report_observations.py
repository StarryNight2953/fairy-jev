"""Optional report evidence from public tool returns, independent of evaluators.

Sensor-zone means are observations covering a zone, not per-ridge measurements
or proof of agronomic recovery. No task-completion verdict is generated here.
"""
from __future__ import annotations

import json

from .tool_adapters import intervals, number, records, successful

VERSION = "irrigation_recheck_evidence_v1"
IRRIGATION = {
    "FieldOpsApp__irrigate", "FieldOpsApp__irrigate_range", "FieldOpsApp__irrigate_ridge",
}
SOIL = {"SensorApp__read_soil_sensors", "SensorApp__read_soil_sensor"}
WAIT = {"SystemApp__advance_time", "SystemApp__wait_for_notification"}


def validate_report_policy(config):
    policy = config.get("report_observation_policy")
    if policy not in (None, VERSION):
        raise ValueError(f"Unknown report_observation_policy: {policy!r}")
    if policy and config.get("parameter_strategy") != "tool_contracts_v2":
        raise ValueError("report_observation_policy requires tool_contracts_v2")
    return policy


def bounds_checked(bounds):
    if (not isinstance(bounds, (tuple, list)) or len(bounds) != 2
            or any(type(x) is not int for x in bounds) or not 0 <= bounds[0] <= bounds[1]):
        raise ValueError("Report evidence needs declared integer ridge bounds")
    return bounds


def _valid_row(row, now):
    before, after = row.get("sim_time_before"), row.get("sim_time_after")
    return (row.get("executed") is True and row.get("status") == "ok" and successful(row)
            and number(now) and number(before) and number(after) and before <= after <= now)


def soil_observations(row, bounds):
    """Return exact measurement provenance for registered public result schemas."""
    lo, hi = bounds_checked(bounds)
    result, name = row.get("result"), row["tool_name"]
    if not isinstance(result, dict):
        return []
    if name not in SOIL:
        return []
    values = result.get("soil_sensors", [result])
    field, mode = "vwc", "sensor_zone_mean"
    if not isinstance(values, list):
        return []
    observed = []
    for index, value in enumerate(values):
        if not isinstance(value, dict) or not successful({"result": value}):
            continue
        reading = value.get(field)
        if not number(reading) or not 0 <= reading <= 1 or value.get("available") is False:
            continue
        start, end = value.get("ridge_start"), value.get("ridge_end")
        if not (type(start) is int and type(end) is int and lo <= start <= end <= hi):
            continue
        container = "soil_sensors" if name == "SensorApp__read_soil_sensors" else None
        observed.append({
            "range": [start, end], "value": reading, "measurement_kind": mode,
            "tool_call_id": row.get("tool_call_id"), "tool_name": name,
            "result_path": f"{container}[{index}].{field}" if container else field,
            "observed_at": row["sim_time_before"],
        })
    return observed


def irrigation_recheck_evidence(state, ridge_bounds):
    lo, hi = bounds_checked(ridge_bounds)
    now = state.get("current_time")
    rows = [r for r in records(state) if _valid_row(r, now)]
    observations = [obs for row in rows for obs in soil_observations(row, ridge_bounds)]
    actions = []
    for row in rows:
        result = row.get("result")
        if row["tool_name"] not in IRRIGATION or not isinstance(result, dict):
            continue
        if result.get("status") != "irrigation_started":
            continue
        ids, ready = result.get("irrigated_ridges"), result.get("effect_ready_at")
        known_ids = (isinstance(ids, list) and bool(ids)
                     and all(type(r) is int and lo <= r <= hi for r in ids))
        target = set(ids) if known_ids else set()
        known_time = number(ready) and ready >= row["sim_time_after"]
        covered, witnesses = set(), []
        if known_ids and known_time:
            for obs in observations:
                if obs["observed_at"] < ready:
                    continue
                intersection = target & set(range(obs["range"][0], obs["range"][1] + 1))
                if intersection:
                    covered.update(intersection)
                    witnesses.append({**obs, "overlap_with_irrigated_ridges": intervals(intersection)})
        waits = [
            r["tool_call_id"] for r in rows
            if r["tool_name"] in WAIT and known_time
            and r["sim_time_before"] >= row["sim_time_after"] and r["sim_time_after"] >= ready
        ]
        status = ("unknown" if not known_ids or not known_time else
                  "observed_after_ready" if target <= covered else
                  "partial" if covered else "not_observed")
        actions.append({
            "irrigation_call_id": row.get("tool_call_id"),
            "irrigated_ranges": intervals(target) if known_ids else None,
            "duration_hours_per_ridge": result.get("duration_hours_per_ridge", result.get("duration_hours")),
            "effect_ready_at": ready if known_time else None,
            "coverage_status": status, "observed_ranges": intervals(covered),
            "unobserved_ranges": intervals(target - covered) if known_ids else None,
            "wait_call_ids": waits, "observations": witnesses,
        })
    return {
        "policy": VERSION, "actions": actions,
        "scope": "Returned irrigation ranges and later returned soil readings only; no simulator state or task-completion judgment",
        "measurement_limit": "Sensor-zone means do not establish each ridge's moisture or agronomic recovery",
    }


def render_irrigation_rechecks(state, ridge_bounds):
    evidence = irrigation_recheck_evidence(state, ridge_bounds)
    actions = evidence["actions"]
    if not actions:
        return "灌溉复查记录：当前可见证据中没有可核实的成功灌溉记录。"
    complete = sum(a["coverage_status"] == "observed_after_ready" for a in actions)
    summary = (
        f"灌溉复查记录：可核实成功灌溉 {len(actions)} 次；其中 {complete} 次已取得工具返回的"
        "响应时间之后、覆盖实际灌溉范围的土壤读数。传感器读数是区域平均值，"
        "不代表每垄实测值或农艺恢复结论；任务的其他要求需分别核对。"
    )
    # Preserve aggregate pending/unknown counts while bounding detailed output.
    details = [{k: v for k, v in action.items() if k != "observations"} for action in actions[-8:]]
    for item, action in zip(details, actions[-8:]):
        # Each individual source keeps its exact observed value and source path.
        item["observations"] = action["observations"][-6:]
        item["older_observation_details_omitted"] = max(0, len(action["observations"]) - 6)
    payload = {
        "coverage_status_counts": {name: sum(a["coverage_status"] == name for a in actions)
                                  for name in ("observed_after_ready", "partial", "not_observed", "unknown")},
        "older_irrigation_details_omitted": max(0, len(actions) - 8),
        "actions": details,
    }
    return summary + "\n" + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
