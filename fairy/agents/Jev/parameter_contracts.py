"""Tool contracts for conditional parameter Choices, using public metadata only.

Range endpoints are selected sequentially. Remaining arguments stay joint: this
does not assume that independently legal doses, seed settings, etc. are mutually
compatible. No scenario, evaluator, app instance or hidden resource state is read.
"""
from __future__ import annotations

from .tools import validate_arguments
from .trace import digest
from .harvest_contract import HARVEST, bind_harvest_contract, validate_harvest_policy

DEFAULT_PARAMETER_STRATEGY = "complete_calls_v1"
CONTRACT_PARAMETER_STRATEGY = "tool_contracts_v1"
ADAPTER_PARAMETER_STRATEGY = "tool_contracts_v2"
CONDITIONAL_STRATEGIES = (CONTRACT_PARAMETER_STRATEGY, ADAPTER_PARAMETER_STRATEGY)
PARAMETER_STRATEGIES = (DEFAULT_PARAMETER_STRATEGY, *CONDITIONAL_STRATEGIES)

# Explicit method contracts; named instances (Robot0, Mavic3M, etc.) retain
# separate identities in the resolved contract and source references.
RANGE_METHODS = {
    "get_ridge_range_state": ("start", "end"),
    "irrigate": ("start", "end"),
    "irrigate_range": ("start", "end"),
    **{method: ("start_ridge", "end_ridge") for method in (
        "apply_fertigation", "fly_survey", "inspect_pests",
        "inspect_crop_health", "inspect_emergence", "apply_fertilizer",
        "plant_seeds", "apply_pesticide", "spray_pesticide", "apply_fungicide",
        "apply_herbicide", "mechanical_weed_control", "replant_seeds",
        "incorporate_residue", "harvest",
    )},
}


def validate_parameter_strategy(config):
    strategy = (config or {}).get("parameter_strategy", DEFAULT_PARAMETER_STRATEGY)
    if strategy not in PARAMETER_STRATEGIES:
        raise ValueError(f"Unknown parameter_strategy: {strategy!r}; expected {PARAMETER_STRATEGIES}")
    return strategy


def range_contract(entry, catalog, strategy=CONTRACT_PARAMETER_STRATEGY):
    """Resolve a declared tool contract; unsupported tools retain the old path.

    Bounds and optional per-pass widths must come from the existing public /
    declared catalog. In particular, no private Robot coverage constant is used.
    """
    names = RANGE_METHODS.get(entry["name"].split("__")[-1])
    if names is None:
        if strategy == ADAPTER_PARAMETER_STRATEGY and entry["name"] in {
                "FieldOpsApp__irrigate_ridge", "FieldOpsApp__apply_pesticide_manual"}:
            bounds = catalog.get("ridge_bounds", {}).get("values", [])
            if len(bounds) != 2 or any(type(v) is not int for v in bounds) or not 0 <= bounds[0] <= bounds[1]:
                raise ValueError("single-ridge contract requires public ridge_bounds")
            parameters = {p["name"]: p for p in entry["parameters"]}
            if parameters.get("ridge_id", {}).get("type") not in {"int", "integer"}:
                raise ValueError("single-ridge contract/signature mismatch")
            return {"version": strategy, "kind": "single_ridge", "tool_name": entry["name"],
                    "ridge_bounds": list(bounds), "prefix_parameters": ["ridge_id"],
                    "joint_remaining_parameters": [k for k in parameters if k != "ridge_id"],
                    "max_ridge_count": 2 if entry["name"].endswith("apply_pesticide_manual") else 1,
                    "source_refs": ["public_catalog:ridge_bounds", "tool_signature:" + entry["name"]],
                    "scope": "Select ridge first, then joint remaining arguments; manual sprayer docstring permits at most two ridges"}
        return None
    parameters = {p["name"]: p for p in entry["parameters"]}
    if not all(k in parameters and parameters[k]["type"] in {"int", "integer"} for k in names):
        raise ValueError(f"range contract/signature mismatch: {entry['name']}")
    bounds = catalog.get("ridge_bounds", {}).get("values", [])
    if (len(bounds) != 2 or any(type(x) is not int for x in bounds)
            or not 0 <= bounds[0] <= bounds[1]):
        raise ValueError("range contract needs public ridge_bounds [minimum, maximum]")
    width = catalog.get("max_pass_width", {}).get(entry["name"])
    if width is not None and (type(width) is not int or width <= 0):
        raise ValueError("declared max_pass_width must be a positive integer")
    rest = [p["name"] for p in entry["parameters"] if p["name"] not in names]
    return {
        "version": strategy, "tool_name": entry["name"],
        "start_parameter": names[0], "end_parameter": names[1],
        "ridge_bounds": list(bounds), "max_pass_width": width,
        "prefix_parameters": [names[0]] + ([names[1]] if rest else []),
        "joint_remaining_parameters": rest,
        "source_refs": ["public_catalog:ridge_bounds", f"tool_signature:{entry['name']}"]
            + ([f"declared_catalog:max_pass_width.{entry['name']}"] if width else []),
        "scope": "Inclusive ridge bounds, ordered endpoints and declared per-pass width only; not a task or resource feasibility proof",
    }


def range_values(contract, parameter, bound):
    lo, hi = contract["ridge_bounds"]
    if contract.get("kind") == "single_ridge":
        if parameter != "ridge_id" or bound:
            raise ValueError("single-ridge selection requires an empty prefix")
        return range(lo, hi + 1)
    start, end = contract["start_parameter"], contract["end_parameter"]
    if parameter == start:
        if bound:
            raise ValueError("start selection must have an empty prefix")
    elif parameter == end:
        if set(bound) != {start} or type(bound[start]) is not int or not lo <= bound[start] <= hi:
            raise ValueError("end selection needs one valid, already selected start")
        lo = bound[start]
        if contract["max_pass_width"]:
            hi = min(hi, lo + contract["max_pass_width"] - 1)
    else:
        raise ValueError("parameter is not part of the range contract")
    values = range(lo, hi + 1)
    observed = contract.get("observed_state")
    if observed:
        if contract["tool_name"] != HARVEST:
            raise ValueError("Harvest state constraint bound to a different tool")
        excluded = set(observed["excluded_ridges"])
        if parameter == start:
            return [value for value in values if value not in excluded]
        # An endpoint must not span a hole even when both endpoints are allowed.
        return [value for value in values if not set(range(lo, value + 1)) & excluded]
    return values


def prefix_candidates(contract, parameter, bound, decision_ids):
    values = range_values(contract, parameter, bound)
    base = {"parameter_strategy": contract["version"], "parameter": parameter,
            "bound_arguments": dict(bound), "prefix_decision_ids": list(decision_ids),
            "contract": contract, "missing_fields": []}
    if len(values) > 255:
        return {**base, "status": "candidate_limit_exceeded", "candidates": {},
                "candidate_count_at_least": len(values), "reason": "Range dimension exceeds 255; no silent truncation"}
    if not values:
        return {**base, "status": "needs_context", "candidates": {},
                "reason": "No range remains under current visible unplanted evidence"}
    refs = contract["source_refs"] + [f"model_selection:{d}" for d in decision_ids]
    return {**base, "status": "ready", "reason": None, "candidates": {
        f"value_{value}": {"arguments": {parameter: value}, "source_refs": refs}
        for value in values}}


def completion_ranges(contract, bound, decision_ids):
    start, end = contract["start_parameter"], contract["end_parameter"]
    ends = range_values(contract, end, {start: bound[start]})
    if end in bound:
        if type(bound[end]) is not int or bound[end] not in ends:
            raise ValueError("selected end violates the range contract")
        ends = [bound[end]]
    ref = "model_selection:" + ",".join(decision_ids)
    return [((bound[start], stop), ref) for stop in ends]


def validate_contract_arguments(contract, entry, arguments):
    arguments = validate_arguments(entry, arguments)
    if contract.get("kind") == "single_ridge":
        ridge, count = arguments["ridge_id"], arguments.get("ridge_count", 1)
        lo, hi = contract["ridge_bounds"]
        if (type(ridge) is not int or type(count) is not int or not lo <= ridge <= hi
                or not 1 <= count <= contract["max_ridge_count"] or ridge + count - 1 > hi):
            raise ValueError("single-ridge/count contract violated")
        return arguments
    start, end = contract["start_parameter"], contract["end_parameter"]
    if arguments[end] not in range_values(contract, end, {start: arguments[start]}):
        raise ValueError("complete arguments violate the range contract")
    return arguments


def audit_parameter_contracts(events, entries, config):
    """Check prefix provenance, conditional menus and final native arguments.

    Shared by both artifact verifiers. Interrupted prefixes are valid evidence;
    execution is valid only after every linked choice has succeeded in order.
    """
    issues, plans, starts, choices, menus, generations = [], {}, {}, {}, {}, {}
    policy = validate_harvest_policy(config)

    def check(ok, code, event):
        if not ok:
            issues.append({"check": code, "event_id": event.get("event_id"), "detail": None})

    for position, event in enumerate(events):
        kind, step = event.get("event"), event.get("step_id")
        if kind == "run_start" and policy:
            check(event.get("harvest_state_policy") == policy, "harvest_policy_matches_configuration", event)
        elif kind == "parameter_plan":
            contract = event.get("contract", {})
            try:
                expected = range_contract(entries[event["tool_name"]], config.get("candidate_catalog", {}), validate_parameter_strategy(config))
                expected = bind_harvest_contract(expected, events[:position], policy)
            except (KeyError, ValueError, TypeError):
                expected = None
            source = choices.get(event.get("tool_decision_id"), {})
            check(validate_parameter_strategy(config) in CONDITIONAL_STRATEGIES
                  and step not in plans and expected is not None and expected == contract,
                  "parameter_contract_matches_configuration", event)
            check(source.get("status") == "ok" and source.get("decision_stage") == "tool"
                  and source.get("selected_option") == event.get("tool_name"),
                  "parameter_plan_follows_selected_tool", event)
            plans[step] = {"contract": contract, "tool_decision_id": event.get("tool_decision_id"),
                           "ids": [], "bound": {}}
        elif kind == "parameter_stage_candidates":
            plan = plans.get(step)
            generation = event.get("generation", {})
            check(plan is not None, "prefix_has_tool_contract", event)
            if plan is not None:
                try:
                    parameter = plan["contract"]["prefix_parameters"][len(plan["ids"])]
                    expected = prefix_candidates(plan["contract"], parameter, plan["bound"], plan["ids"])
                except (KeyError, ValueError, IndexError, TypeError):
                    expected, parameter = None, None
                check(expected == generation and event.get("decision_stage") == f"parameter_{parameter}",
                      "conditional_prefix_menu_exact", event)
            check(event.get("candidate_hash") == digest(generation.get("candidates", {}))
                  and event.get("candidate_order") == list(generation.get("candidates", {})),
                  "prefix_menu_hash_and_order", event)
            menus[step] = event
        elif kind == "decision_start":
            starts[event.get("decision_id")] = event
            if event.get("decision_stage", "").startswith("parameter_"):
                menu = menus.get(step, {})
                check(menu.get("generation", {}).get("status") == "ready"
                      and event.get("decision_stage") == menu.get("decision_stage")
                      and event.get("criteria") == menu.get("generation", {}).get("candidates"),
                      "prefix_request_matches_menu", event)
            if step in plans and event.get("decision_stage") != "tool":
                source = starts.get(plans[step]["tool_decision_id"], {})
                check(event.get("state") == source.get("state"), "parameter_plan_frozen_state", event)
        elif kind == "decision_end":
            choices[event.get("decision_id")] = event
            if event.get("decision_stage", "").startswith("parameter_") and event.get("status") == "ok":
                source = starts.get(event.get("decision_id"), {})
                selected = source.get("criteria", {}).get(event.get("selected_option"))
                check(step in plans and selected is not None, "prefix_choice_submitted", event)
                if step in plans and selected is not None:
                    plans[step]["bound"].update(selected["arguments"])
                    plans[step]["ids"].append(event["decision_id"])
        elif kind == "parameter_candidates":
            claimed = event.get("generation", {})
            if claimed.get("parameter_strategy") in CONDITIONAL_STRATEGIES or claimed.get("prefix_decision_ids"):
                check(step in plans, "conditional_candidates_have_recorded_plan", event)
            if step not in plans:
                continue
            plan, generation = plans[step], event.get("generation", {})
            generations[step] = event
            check(generation.get("parameter_strategy") == validate_parameter_strategy(config)
                  and generation.get("contract") == plan["contract"]
                  and generation.get("bound_arguments") == plan["bound"]
                  and generation.get("prefix_decision_ids") == plan["ids"]
                  and len(plan["ids"]) == len(plan["contract"]["prefix_parameters"]),
                  "complete_candidates_follow_all_prefix_choices", event)
            for candidate in generation.get("candidates", {}).values():
                args = candidate.get("arguments", {})
                try:
                    valid = validate_contract_arguments(plan["contract"], entries[event["tool_name"]], args) == args
                except (ValueError, KeyError, TypeError):
                    valid = False
                check(valid and all(args.get(k) == v for k, v in plan["bound"].items()),
                      "complete_candidate_preserves_prefix_and_contract", event)
        elif kind == "tool_start":
            if policy and event.get("tool_name") == HARVEST:
                check(step in plans, "harvest_execution_requires_observed_contract", event)
            if step not in plans:
                continue
            plan = plans[step]
            ids = event.get("decision_ids", [])
            answer = choices.get(ids[-1] if ids else None, {})
            check(ids == [plan["tool_decision_id"], *plan["ids"], answer.get("decision_id")]
                  and answer.get("status") == "ok" and answer.get("decision_stage") == "parameters"
                  and step in generations,
                  "execution_links_conditional_choice_chain", event)
    return issues
