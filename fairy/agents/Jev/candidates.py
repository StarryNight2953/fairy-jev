"""Deterministic finite argument catalogs from exclusively agent-visible data.

No Scenario, Engine, evaluator, checkpoint or private farm object is accepted.
Generating candidates never executes a tool. Each candidate is one full call.
"""
from __future__ import annotations

import itertools
import json
import math
import re
from typing import Any

from .tools import validate_arguments


_RANGE = re.compile(r"(?:(?:ridges?|垄)\s*)?(\d+)\s*[-–—~至到]\s*(\d+)(?:\s*垄)?", re.I)
_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(?:小时|hours?\b|h\b)", re.I)
_IRRIGATION = {"irrigate", "irrigate_range", "irrigate_ridge"}
CANDIDATE_STRATEGY_VERSION = "jev_numeric_visible_v2"
_NUMBER = r"(?<![A-Za-z0-9_.+-])(?P<value>\d+(?:\.\d+)?)"
_PER_RIDGE = r"\s*(?:/\s*(?:垄|ridges?\b)|per\s+ridge\b)"
_LITERS = r"(?:lit(?:er|re)s?(?![A-Za-z])|L(?![A-Za-z])|升)"
_KG = r"(?:kg(?![A-Za-z])|kilograms?(?![A-Za-z])|千克|公斤)"
_TIME_UNIT = r"(?P<unit>小时|分钟|秒|天|hours?\b|minutes?\b|seconds?\b|days?\b|h\b)"
_ACTION_MARKERS = {
    "fungicide": r"杀菌剂|fungicide",
    "herbicide": r"除草剂|herbicide",
    "pesticide": r"杀虫剂|农药|虫害|背负式点喷|pesticide|insecticide",
    "fertilizer": r"肥料|施肥|追肥|fertili[sz]er",
    "fuel": r"加油|燃油|柴油|refuel|fuel|diesel",
    "irrigation": r"灌溉|irrigat\w*",
    "wait": r"等待|推进|wait\w*|advance\s+time",
}
_NUMERIC_KEYS = {"liters_per_ridge", "kg_per_ridge", "liters", "kg", "count", "depth_cm",
                 "seed_spacing_cm", "spacing_cm", "ridge_width_m", "nutrient_amount", "water_mm",
                 "target_moisture_pct", "days", "hours", "duration_hours"}


def _nearest_action(text: str, offset: int) -> str | None:
    """The last named action in the current sentence, never an arbitrary number."""
    prefix = re.split(r"[。；;\n.!?](?!\d)", text[:offset])[-1]
    found = [(match.start(), kind) for kind, pattern in _ACTION_MARKERS.items()
             for match in re.finditer(pattern, prefix, re.I)]
    return max(found)[1] if found else None


def _quantity_action(text: str, match: re.Match) -> str | None:
    # Chinese often places the action after its duration: "0.8小时灌溉".
    suffix = re.split(r"[，,。；;\n.!?]", text[match.end():match.end() + 20])[0]
    found = [(m.start(), kind) for kind, pattern in _ACTION_MARKERS.items()
             for m in re.finditer(pattern, suffix, re.I)]
    if found:
        offset, kind = min(found)
        # An adjacent suffix wins over an earlier wait. Do not leap across
        # another quantity in "等待6小时后执行0.65小时灌溉".
        if re.fullmatch(r"\s*(?:定向|的|的定向|of)?\s*", suffix[:offset], re.I):
            return kind
    return _nearest_action(text, match.start("value"))


def _chemical_action(text: str, match: re.Match) -> str | None:
    action = _quantity_action(text, match)
    if action is not None:
        return action
    # A separate "剂量为..." step may refer to the sole chemical named in
    # the public task. Ambiguous multi-chemical tasks get no guessed binding.
    kinds = [kind for kind in ("fungicide", "herbicide", "pesticide")
             if re.search(_ACTION_MARKERS[kind], text, re.I)]
    return kinds[0] if len(kinds) == 1 else None


def _numeric_value(parameter: dict, value: float) -> int | float | None:
    if not math.isfinite(value) or value <= 0:
        return None
    key, kind = parameter["name"], parameter["type"]
    if key == "depth_cm" and not 3 <= value <= 5:
        return None  # Public plant/replant tool contract.
    if key == "target_moisture_pct" and value > 100:
        return None
    if key == "days" and not 1 <= value <= 7:
        return None  # Public forecast tool contract.
    if kind in {"int", "integer"}:
        return int(value) if value.is_integer() else None
    return value if kind in {"float", "number"} else None


def task_numeric_values(tool_name: str, parameter: dict, task: Any) -> list[tuple[int | float, str]]:
    """Add explicit quantities with matching units and parameter/action meaning.

    This is a finite language adapter, not a task solver. It never reads oracle
    data, copies all task numbers, or estimates a dose from simulator state.
    Multiple stated values remain choices; the model still chooses the action.
    """
    if not isinstance(task, str) or parameter["name"] not in _NUMERIC_KEYS:
        return []
    key, method = parameter["name"], tool_name.split("__")[-1]
    patterns: list[tuple[str, str | None, float]] = []
    chemical = {"apply_fungicide": "fungicide", "load_fungicide": "fungicide",
                "apply_herbicide": "herbicide", "spray_pesticide": "pesticide",
                "apply_pesticide_manual": "pesticide", "load_pesticide": "pesticide",
                "refill_pesticide_tank": "pesticide"}.get(method)
    if key in {"liters_per_ridge", "kg_per_ridge"}:
        unit, action = (_LITERS, chemical) if key == "liters_per_ridge" else (_KG, "fertilizer")
        if action:
            patterns.append((_NUMBER + r"\s*" + unit + _PER_RIDGE, action, 1))
    elif key in {"liters", "kg"}:
        unit = _LITERS if key == "liters" else _KG
        action = chemical or ("fuel" if method == "refuel" else "fertilizer" if key == "kg" else None)
        if action:
            patterns.append((_NUMBER + r"\s*" + unit + r"(?!\s*(?:/|per\b))", action, 1))
    elif key in {"hours", "duration_hours"} and method in _IRRIGATION:
        patterns.append((_NUMBER + r"\s*(?:小时|hours?\b|h\b)", "irrigation", 1))
        patterns.append((r"\b(?:hours|duration_hours)\s*[=:]\s*" + _NUMBER, "irrigation", 1))
    elif key in {"depth_cm", "seed_spacing_cm", "spacing_cm", "ridge_width_m"}:
        label, unit = {
            "depth_cm": (r"播深|播种深度|depth", r"cm(?![A-Za-z])|厘米"),
            "seed_spacing_cm": (r"株距|种子间距|seed\s+spacing|spacing", r"cm(?![A-Za-z])|厘米"),
            "spacing_cm": (r"株距|种子间距|seed\s+spacing|spacing", r"cm(?![A-Za-z])|厘米"),
            "ridge_width_m": (r"垄距|垄宽|ridge\s+width", r"m(?![A-Za-z])|米"),
        }[key]
        patterns.append((rf"(?:{label})\s*(?:为|是|of|=|:|：)?\s*{_NUMBER}\s*(?:{unit})", None, 1))
        patterns.append((rf"{_NUMBER}\s*(?:{unit})\s*(?:{label})", None, 1))
    elif key == "count" and method == "load_seeds":
        patterns.append((_NUMBER + r"\s*(?:粒(?:种子)?|seeds?\b)", None, 1))
    elif key == "target_moisture_pct":
        patterns.append((r"(?:干燥|dry\w*)[^。；;\n]{0,30}?" + _NUMBER + r"\s*%", None, 1))
    elif key == "days" and method == "get_forecast":
        patterns.append((_NUMBER + r"\s*(?:天|days?\b)\s*(?:天气)?(?:预报|forecast)", None, 1))
    elif key == "water_mm":
        patterns.append((r"(?:水量|water(?:\s+amount)?)\s*(?:为|=|:|：)?\s*" + _NUMBER + r"\s*(?:mm\b|毫米)", None, 1))
    elif key == "nutrient_amount":
        patterns.append((r"(?:fertigation|水肥)[^。；;\n]{0,20}?\bamount\s*[=:]\s*" + _NUMBER, None, 1))

    # Unambiguous named numeric arguments (e.g. seed_spacing_cm=7.9).
    # Generic liters/kg and shared chemical rates still need action context.
    if key not in {"liters", "kg", "count", "days", "hours", "duration_hours", "liters_per_ridge", "kg_per_ridge"}:
        aliases = r"(?:seed_spacing_cm|spacing_cm)" if key in {"seed_spacing_cm", "spacing_cm"} else re.escape(key)
        patterns.append((rf"\b{aliases}\s*[=:]\s*{_NUMBER}", None, 1))
    values = []
    for pattern, action, scale in patterns:
        for match in re.finditer(pattern, task, re.I):
            actual_action = _chemical_action(task, match) if action in {"fungicide", "herbicide", "pesticide"} else _quantity_action(task, match)
            if action and actual_action != action:
                continue
            value = _numeric_value(parameter, float(match["value"]) * scale)
            if value is not None:
                values.append((value, f"visible_task:quantity:{key}:{match.start()}-{match.end()}"))
    return list(dict.fromkeys(values))


def task_wait_seconds(task: Any) -> list[tuple[int, str]]:
    """Convert explicitly requested waits to seconds, separate from irrigation."""
    if not isinstance(task, str):
        return []
    values = []
    for match in re.finditer(_NUMBER + r"\s*" + _TIME_UNIT, task, re.I):
        if _quantity_action(task, match) != "wait":
            continue
        unit = match["unit"].lower()
        factor = 86400 if unit in {"天", "day", "days"} else 3600 if unit in {"小时", "h", "hour", "hours"} else 60 if unit in {"分钟", "minute", "minutes"} else 1
        seconds = float(match["value"]) * factor
        if math.isfinite(seconds) and seconds > 0:
            values.append((math.ceil(seconds), f"visible_task:wait:{match.start()}-{match.end()}"))
    return list(dict.fromkeys(values))


def _task_spacing_scope_matches(arguments: dict, refs: list[str], task: Any) -> bool:
    """Bind new per-zone spacing to its stated range; retain catalog choices.

    Expanding three explicit cultivar spacings over every unrelated zone can
    exceed Choice capacity and creates combinations absent from the task. A
    nearby public range restricts only that new task-derived spacing value.
    """
    if not isinstance(task, str) or "start_ridge" not in arguments:
        return True
    for key in ("seed_spacing_cm", "spacing_cm"):
        if key not in arguments:
            continue
        if any(ref.endswith(f".{key}.default") or (ref.startswith("declared_catalog:") and f".{key}" in ref) for ref in refs):
            continue
        scopes = []
        for ref in refs:
            match = re.fullmatch(rf"visible_task:quantity:{key}:(\d+)-(\d+)", ref)
            if not match:
                continue
            prefix = re.split(r"[。；;\n]", task[:int(match[1])])[-1]
            ranges = [m for m in _RANGE.finditer(prefix)
                      if 0 <= int(m[1]) <= int(m[2]) < 1000]
            # Require one explicitly labelled adjacent range in this clause.
            # "0-20 and 21-42" is ambiguous; retain unbound choices in that case.
            if (len(ranges) == 1 and re.search(r"ridges?|垄", ranges[0][0], re.I)
                    and len(prefix) - ranges[0].end() <= 160):
                scopes.append((int(ranges[0][1]), int(ranges[0][2])))
            else:
                scopes.append(None)
        if scopes and None not in scopes and not any(
                start <= arguments["start_ridge"] <= arguments["end_ridge"] <= end for start, end in scopes):
            return False
    return True


def extract_task_parameters(task: Any) -> dict:
    """Parse stated task values; no scenario ID lookup or answer fallback.

    Cultivar labels are matched to the range immediately following the label.
    An explicit 'only/只对' instruction identifies the requested zone. Ambiguous
    tasks retain all stated ranges but have no guessed target range.
    """
    text = task if isinstance(task, str) else json.dumps(task, ensure_ascii=False)
    ranges = []
    for match in _RANGE.finditer(text):
        start, end = map(int, match.groups())
        # Excludes date-like 2026-08 while leaving bounds to the public catalog.
        if 0 <= start <= end < 10000 and start < 1000:
            if [start, end] not in ranges:
                ranges.append([start, end])
    target = None
    explicit = re.search(r"(?:只(?:对|灌溉)?|only(?:\s+irrigate)?)\s*(?:ridges?\s*)?(\d+)\s*[-–—~至到]\s*(\d+)", text, re.I)
    if explicit:
        target = [int(explicit[1]), int(explicit[2])]
    else:
        label = re.search(r"(?:只对|only\s+(?:the\s+)?)([A-Za-z][A-Za-z0-9_-]*)", text, re.I)
        if label:
            zone = re.search(re.escape(label[1]) + r"(?:分区|区|\s+zone)?(?:为|:|\s)*ridges?\s*(\d+)\s*[-–—~至到]\s*(\d+)", text, re.I)
            if zone:
                target = [int(zone[1]), int(zone[2])]
        if target is None and len(ranges) == 1:
            target = ranges[0]
    durations = [float(m[1]) for m in _DURATION.finditer(text)]
    duration = durations[0] if len(set(durations)) == 1 else None
    return {"target_range": target, "irrigation_hours": duration, "ranges": ranges,
            "source_refs": {"target_range": "visible_task:target_range", "irrigation_hours": "visible_task:duration"}}


def visible_records(state: dict) -> list[dict]:
    """Deduplicate recent actions also retained in observations."""
    rows, seen = [], set()
    for item in list(state.get("observations", [])) + list(state.get("recent_actions", [])) + list(state.get("action_facts", [])):
        if not isinstance(item, dict) or not item.get("tool_name"):
            continue
        key = item.get("tool_call_id") or json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
        if key not in seen:
            rows.append(item)
            seen.add(key)
    return sorted(rows, key=lambda item: item.get("sim_time_before", 0) or 0)


def _successful(row: dict) -> bool:
    result = row.get("result")
    return row.get("status") != "error" and not row.get("error") and not (
        isinstance(result, dict) and (result.get("error") or result.get("success") is False
                                     or result.get("status") in {"error", "failed", "failure", "rejected"}))


def visible_wait_seconds(state: dict) -> list[tuple[int, str]]:
    """Remaining time to returned effect_ready_at or ETA, in integer seconds."""
    now = float(state.get("current_time") or 0)
    values = []
    for row in visible_records(state):
        result = row.get("result")
        if not isinstance(result, dict) or not _successful(row):
            continue
        ref = f"tool_result:{row.get('tool_call_id', row['tool_name'])}"
        if isinstance(result.get("effect_ready_at"), (int, float)):
            remaining = float(result["effect_ready_at"]) - now
            if math.isfinite(remaining) and remaining > 0:
                values.append((max(1, math.ceil(remaining)), ref + ":effect_ready_at"))
        elif isinstance(result.get("eta_minutes_to_confirmation"), (int, float)):
            then = float(row.get("sim_time_after", now))
            remaining = then + float(result["eta_minutes_to_confirmation"]) * 60 - now
            if math.isfinite(remaining) and remaining > 0:
                values.append((max(1, math.ceil(remaining)), ref + ":eta_minutes_to_confirmation"))
    return list(dict.fromkeys(values))


def factual_report(state: dict) -> str:
    """Conservative factual template, never a substitute for task evaluation."""
    task = extract_task_parameters(state.get("task", ""))
    target, requested = task["target_range"], task["irrigation_hours"]
    rows = visible_records(state)
    errors = [row for row in rows if not _successful(row)]
    irrigation = [row for row in rows if row["tool_name"].split("__")[-1] in _IRRIGATION and _successful(row)
                  and isinstance(row.get("result"), dict) and row["result"].get("status") == "irrigation_started"]
    doses, ready_at, last_irrigation = {}, None, None
    for row in irrigation:
        result = row["result"]
        dose = result.get("duration_hours_per_ridge", result.get("duration_hours"))
        if isinstance(dose, (int, float)):
            for ridge in result.get("irrigated_ridges", []):
                doses[int(ridge)] = doses.get(int(ridge), 0) + float(dose)
        if isinstance(result.get("effect_ready_at"), (int, float)):
            ready_at = max(ready_at or 0, float(result["effect_ready_at"]))
        last_irrigation = row.get("sim_time_after")
    target_ids = set(range(target[0], target[1] + 1)) if target else set()
    correct = {r for r, hours in doses.items() if requested is not None and math.isclose(hours, requested, abs_tol=1e-8)} & target_ids
    waited = ready_at is not None and any(
        row["tool_name"] in {"SystemApp__advance_time", "SystemApp__wait_for_notification"}
        and _successful(row) and float(row.get("sim_time_after") or 0) >= ready_at
        and float(row.get("sim_time_before") or 0) >= float(last_irrigation or 0) for row in rows)
    rechecks = [row for row in rows if row["tool_name"] == "FarmWorldApp__get_ridge_range_state"
                and _successful(row) and target and row.get("arguments", {}).get("start") == target[0]
                and row.get("arguments", {}).get("end") == target[1]
                and isinstance(row.get("result"), dict)
                and {r.get("ridge_id") for r in row["result"].get("ridges", []) if isinstance(r, dict)} >= target_ids
                and ready_at is not None and float(row.get("sim_time_before") or 0) >= ready_at]
    facts = [f"本次可见记录中成功启动灌溉 {len(irrigation)} 次，工具返回覆盖 {len(doses)} 垄。"]
    if target and requested is not None:
        facts.append(f"任务目标 {target[0]}–{target[1]} 垄、每垄 {requested:g} 小时；累计剂量匹配的目标垄为 {len(correct)}/{len(target_ids)}。")
    if doses.keys() - target_ids and target:
        facts.append(f"存在目标区外灌溉记录：{sorted(doses.keys() - target_ids)}。")
    if errors:
        facts.append(f"已有 {len(errors)} 次工具错误，最近错误：{str(errors[-1].get('result'))[:500]}。")
    if waited and rechecks:
        facts.append("已通过等待工具到达返回的响应时间，并完成目标区状态复查；复查结果已保存在可见工具记录中。")
    else:
        facts.append("尚未同时确认响应时间后的等待与完整目标区复查。")
    incomplete_ledger = "action_facts" not in state and bool(state.get("history_retention", {}).get("omitted_observation_ids"))
    if incomplete_ledger:
        facts.append("部分历史已省略且无完整动作事实账本，不能核实累计剂量。")
    if not (target_ids and correct == target_ids and set(doses) == target_ids and waited and rechecks and not errors and not incomplete_ledger):
        facts.append("当前记录不足以宣称整个目标任务完成。")
    else:
        facts.append("目标灌溉剂量与等待后复查已有工具记录支持；最终任务验收由独立断言执行。")
    return "".join(facts)


class CandidateResolver:
    """Only chosen-tool candidates are generated; 255 is a hard limit."""

    def __init__(self, catalog_entries: list[dict], config: dict):
        self.entries = {entry["name"]: entry for entry in catalog_entries if entry.get("visible", True)}
        self.catalog = config.get("candidate_catalog", {})

    def _strategy(self, entry):
        parameters = entry["parameters"]
        if not parameters:
            return "no_parameters"
        names = {p["name"] for p in parameters}
        method = entry["name"].split("__")[-1]
        if method == "send_message_to_user": return "factual_report"
        if method == "advance_time": return "positive_visible_wait"
        if method == "wait_for_notification": return "positive_notification_timeout"
        if "sensor_id" in names: return "sensor_public_ids"
        if {"start", "end"} <= names or {"start_ridge", "end_ridge"} <= names: return "paired_range_and_tool_values"
        if "ridge_id" in names: return "ridge_and_tool_values"
        if "implement" in names: return "public_implement_enum"
        if "seed_type" in names: return "public_seed_enum_and_count"
        return "tool_specific_values_and_defaults"

    def support_matrix(self) -> list[dict]:
        rows = []
        for name, entry in self.entries.items():
            required = [p["name"] for p in entry["parameters"] if p["required"]]
            optional = [p["name"] for p in entry["parameters"] if not p["required"]]
            values = self.catalog.get("tool_values", {}).get(name, {})
            units, sources = {}, {}
            for p in entry["parameters"]:
                key = p["name"]
                if key in {"start", "end", "start_ridge", "end_ridge", "ridge_id"}:
                    units[key], sources[key] = "inclusive ridge ID", ["visible_task:ranges", "visible_tool_result:ranges", "public_catalog:ridge_bounds", "declared_catalog:ranges"]
                elif key in {"hours", "duration_hours"} and name.split("__")[-1] in _IRRIGATION:
                    units[key], sources[key] = "hours per ridge", ["visible_task:quantity", "declared_catalog:irrigation_hours"]
                elif key in {"seconds", "minutes", "hours", "days", "timeout"} and name.startswith("SystemApp__"):
                    units[key], sources[key] = key if key != "timeout" else "seconds", ["visible_task:wait", "visible_tool_result:effect_ready_at/ETA", "declared_catalog:positive_waits"]
                elif key == "sensor_id":
                    units[key], sources[key] = "sensor identifier", ["public_catalog:sensor_ids"]
                elif key in {"implement", "seed_type"}:
                    units[key], sources[key] = self.catalog.get(key, {}).get("unit"), [self.catalog.get(key, {}).get("source", "public_catalog")]
                elif key == "content":
                    units[key], sources[key] = "text", ["visible_task", "visible_executed_actions", "visible_rechecks", "deterministic_report_template"]
                else:
                    units[key] = values.get(key, {}).get("unit", {"advance_time": "boolean", "liters_per_ridge": "L per ridge", "target_moisture_pct": "percent", "days": "days"}.get(key, "signature value"))
                    sources[key] = ([f"declared_catalog:tool_values.{name}.{key}"] if key in values else []) + ([] if p["required"] else [f"tool_signature:{key}.default"])
                    if key in _NUMERIC_KEYS:
                        sources[key] += ["visible_task:quantity", "declared_catalog:numeric_midpoint_if_capacity"]
            rows.append({"tool_name": name, "parameter_category": "no_parameters" if not entry["parameters"] else "required" if required else "optional_only",
                         "required_parameters": required, "optional_parameters": optional,
                         "resolver_id": self._strategy(entry), "implementation_status": "implemented",
                         "value_sources": sources, "units": units,
                         "limits": {"max_candidates": 255, "max_pass_width": self.catalog.get("max_pass_width", {}).get(name)},
                         "finite_catalog_not_exhaustive": True, "candidate_strategy_version": CANDIDATE_STRATEGY_VERSION})
        return rows

    def _range_options(self, state, tool_name):
        options = []
        for pair in extract_task_parameters(state.get("task", ""))["ranges"]:
            options.append((pair, "visible_task:ranges"))
        for row in visible_records(state):
            args, result = row.get("arguments", {}), row.get("result")
            pair = [args.get("start", args.get("start_ridge")), args.get("end", args.get("end_ridge"))]
            if all(type(x) is int for x in pair): options.append((pair, f"tool_arguments:{row.get('tool_call_id', row['tool_name'])}"))
            if isinstance(result, dict) and isinstance(result.get("ridges"), list):
                ids = sorted(r["ridge_id"] for r in result["ridges"] if isinstance(r, dict) and type(r.get("ridge_id")) is int)
                if ids and ids == list(range(ids[0], ids[-1] + 1)):
                    options.append(([ids[0], ids[-1]], f"tool_result:{row.get('tool_call_id', row['tool_name'])}:ridges"))
        for pair in self.catalog.get("ranges", {}).get("values", []): options.append((pair, "declared_catalog:ranges"))
        bounds = self.catalog.get("ridge_bounds", {}).get("values", [])
        width = self.catalog.get("max_pass_width", {}).get(tool_name)
        output = []
        for pair, ref in options:
            if len(pair) != 2 or any(type(x) is not int for x in pair): continue
            start, end = pair
            if start > end or (bounds and not bounds[0] <= start <= end <= bounds[1]): continue
            for first in range(start, end + 1, width or end - start + 1):
                output.append(((first, min(end, first + width - 1) if width else end), ref + (f":single_pass_max_{width}" if width else "")))
        return output

    def _values(self, name, parameter, state):
        key, method = parameter["name"], name.split("__")[-1]
        values = []
        if not parameter["required"]:
            values.append((parameter["default"], f"tool_signature:{name}.{key}.default"))
        if key == "sensor_id":
            kind = "soil" if "soil" in method else "canopy"
            values.extend((v, f"public_catalog:sensor_ids.{kind}") for v in self.catalog.get("sensor_ids", {}).get(kind, []))
        elif key == "ridge_id":
            bounds = self.catalog.get("ridge_bounds", {}).get("values", [])
            if bounds: values.extend((v, "public_catalog:ridge_bounds") for v in range(bounds[0], bounds[1] + 1))
        elif key in {"implement", "seed_type"}:
            values.extend((v, f"public_catalog:{key}") for v in self.catalog.get(key, {}).get("values", []))
        elif key in {"hours", "duration_hours"} and method in _IRRIGATION:
            values.extend((v, "declared_catalog:irrigation_hours") for v in self.catalog.get("irrigation_hours", {}).get("values", []))
        elif key == "content":
            values.append((factual_report(state), "deterministic_template:visible_executed_facts"))
        values.extend((v, f"declared_catalog:tool_values.{name}.{key}") for v in self.catalog.get("tool_values", {}).get(name, {}).get(key, {}).get("values", []))
        values.extend(task_numeric_values(name, parameter, state.get("task", "")))
        unique = {}
        for value, ref in values:
            marker = json.dumps(value, sort_keys=True)
            if marker in unique: unique[marker][1].append(ref)
            else: unique[marker] = [value, [ref]]
        return list(unique.values())

    def _add_numeric_midpoints(self, tool_name, parameters, groups, group_indices):
        """Densify catalog intervals only in remaining complete-call capacity.

        Original/default/task choices are never removed. When their product
        already exceeds 255, generation retains its explicit overflow result.
        Midpoints are optional proposals, not newly inferred task requirements.
        """
        additions, skipped = [], []
        for parameter in parameters:
            key = parameter["name"]
            if key not in _NUMERIC_KEYS or key not in group_indices:
                continue
            declared = self.catalog.get("tool_values", {}).get(tool_name, {}).get(key, {}).get("values", [])
            points = sorted(set(float(v) for v in declared if type(v) in {int, float} and math.isfinite(v)))
            group = groups[group_indices[key]]
            for left, right in zip(points, points[1:]):
                midpoint = _numeric_value(parameter, round((left + right) / 2, 8))
                if midpoint is None or any(args[key] == midpoint for args, _ in group):
                    continue
                if math.prod(len(g) + (g is group) for g in groups) > 255:
                    skipped.append({"parameter": key, "value": midpoint, "reason": "complete_call_capacity"})
                    continue
                group.append(({key: midpoint}, [f"declared_catalog:numeric_midpoint:{tool_name}.{key}:{left:g}:{right:g}"]))
                additions.append({"parameter": key, "value": midpoint})
        return {"added_midpoints": additions, "skipped_midpoints": skipped,
                "original_and_explicit_task_choices_preserved": True,
                "task_spacing_scope_policy": "Only task-derived spacing is bound to a single adjacent public ridge range; ambiguous scope stays unbound",
                "task_spacing_scope_filtered_candidates": 0}

    def generate(self, tool_name: str, visible_state: dict) -> dict:
        if tool_name not in self.entries:
            return {"status": "unsupported", "candidates": {}, "missing_fields": [], "reason": "tool absent from visible catalog"}
        entry = self.entries[tool_name]
        parameters = entry["parameters"]
        base = {"tool_name": tool_name, "resolver_id": self._strategy(entry), "missing_fields": [], "reason": None,
                "candidate_strategy_version": CANDIDATE_STRATEGY_VERSION}
        if not parameters:
            return {**base, "status": "ready", "candidates": {"args_000": {"arguments": {}, "source_refs": ["tool_signature:no_parameters"]}}, "candidate_count": 1}
        method = tool_name.split("__")[-1]
        if method in {"advance_time", "wait_for_notification"}:
            waits = visible_wait_seconds(visible_state)
            waits += task_wait_seconds(visible_state.get("task", ""))
            key = "wait_seconds" if method == "advance_time" else "notification_timeout"
            waits += [(v, f"declared_catalog:{key}") for v in self.catalog.get(key, {}).get("values", [])]
            raw = []
            for seconds, ref in waits:
                if type(seconds) is not int or seconds <= 0: continue
                args = {"seconds": seconds, "minutes": 0, "hours": 0, "days": 0} if method == "advance_time" else {"timeout": seconds}
                raw.append((args, [ref]))
        else:
            names = {p["name"] for p in parameters}
            pair_names = ("start", "end") if {"start", "end"} <= names else ("start_ridge", "end_ridge") if {"start_ridge", "end_ridge"} <= names else None
            groups, group_indices = [], {}
            if pair_names:
                ranges = self._range_options(visible_state, tool_name)
                if not ranges:
                    return {**base, "status": "needs_context", "candidates": {}, "missing_fields": list(pair_names), "reason": "no public legal range"}
                groups.append([({pair_names[0]: pair[0], pair_names[1]: pair[1]}, [ref]) for pair, ref in ranges])
            for parameter in parameters:
                key = parameter["name"]
                if pair_names and key in pair_names: continue
                values = self._values(tool_name, parameter, visible_state)
                if not values:
                    base["missing_fields"].append(key)
                group_indices[key] = len(groups)
                groups.append([({key: value}, refs) for value, refs in values])
            if base["missing_fields"]:
                return {**base, "status": "needs_context", "candidates": {}, "reason": "required values absent from visible facts, signature defaults and declared catalog"}
            base["numeric_enrichment"] = self._add_numeric_midpoints(tool_name, parameters, groups, group_indices)
            raw = ((dict(pair for args, _ in combo for pair in args.items()), [ref for _, refs in combo for ref in refs]) for combo in itertools.product(*groups))
        candidates, deduped = {}, {}
        for arguments, refs in raw:
            if not _task_spacing_scope_matches(arguments, refs, visible_state.get("task", "")):
                base["numeric_enrichment"]["task_spacing_scope_filtered_candidates"] += 1
                continue
            bounds = self.catalog.get("ridge_bounds", {}).get("values", [])
            if "ridge_count" in arguments and bounds and arguments["ridge_id"] + arguments["ridge_count"] - 1 > bounds[1]: continue
            try:
                arguments = validate_arguments(entry, arguments)
            except ValueError as exc:
                return {**base, "status": "unsupported", "candidates": {}, "reason": f"invalid declared candidate: {exc}"}
            marker = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
            if marker in deduped:
                existing = candidates[deduped[marker]]["source_refs"]
                existing.extend(ref for ref in refs if ref not in existing)
                continue
            if len(candidates) == 255:
                return {**base, "status": "candidate_limit_exceeded", "candidates": {}, "candidate_count_at_least": 256,
                        "reason": "selected-tool candidate set exceeds 255; adjust declared catalog, never silently truncate"}
            candidate_id = f"args_{len(candidates):03d}"
            deduped[marker] = candidate_id
            candidates[candidate_id] = {"arguments": arguments, "source_refs": list(dict.fromkeys(refs))}
        if not candidates:
            return {**base, "status": "needs_context", "candidates": {}, "reason": "no legal candidates from visible data and declared catalog"}
        return {**base, "status": "ready", "candidates": candidates, "candidate_count": len(candidates)}
