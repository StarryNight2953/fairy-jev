"""Opt-in visible-evidence harvest parameter contract.

Callers supply ordered public trace events and declared ridge bounds/pass width,
never an app instance. Existing configurations leave this policy disabled.
This excludes known unplanted ridges; it does not prove harvest readiness.
"""
from __future__ import annotations

from copy import deepcopy

VERSION = "harvest_observed_state_v1"
HARVEST = "TractorApp__harvest"
RESETS = {"TractorApp__plant_seeds", "TractorApp__replant_seeds"}
READS = {"FarmWorldApp__get_ridge_range_state", "FarmWorldApp__get_overview"}


def validate_harvest_policy(config):
    policy = config.get("harvest_state_policy")
    if policy not in (None, VERSION):
        raise ValueError(f"Unknown harvest_state_policy: {policy!r}")
    if policy and config.get("parameter_strategy") != "tool_contracts_v2":
        raise ValueError("harvest_state_policy requires tool_contracts_v2")
    return policy


def bind_harvest_contract(contract, events, policy):
    """Bind immutable, compact evidence to the selected harvest tool only.

    Used by both the runtime planner and offline verifier. The latter supplies
    the actual preceding trace, not any planner-claimed eligibility facts.
    """
    if not policy or not contract or contract.get("tool_name") != HARVEST:
        return contract
    if policy != VERSION:
        raise ValueError("Unknown harvest state policy")
    facts = observed_unplanted(events, contract["ridge_bounds"])
    grouped = {}
    for ridge, evidence in facts["evidence"].items():
        key = (evidence["event_id"], evidence["result_path"], evidence["basis"])
        grouped.setdefault(key, []).append(int(ridge))
    source_evidence = [
        {"event_id": eid, "result_path": path, "basis": basis, "ridges": ridges}
        for (eid, path, basis), ridges in sorted(grouped.items())
    ]
    bound = deepcopy(contract)
    bound["observed_state"] = {
        "policy": policy, "excluded_ridges": facts["excluded_ridges"],
        "source_evidence": source_evidence,
        "scope": facts["scope"],
    }
    bound["scope"] += "; also excludes ridges known unplanted from preceding public feedback"
    return bound


def _ids(value, lo, hi):
    # Reject an entire malformed field instead of accepting a partial truth.
    if not isinstance(value, list) or any(
        type(r) is not int or not lo <= r <= hi for r in value
    ):
        return None
    return set(value)


def _scope(arguments, lo, hi):
    start, end = arguments.get("start_ridge"), arguments.get("end_ridge")
    if type(start) is int and type(end) is int and lo <= start <= end <= hi:
        return set(range(start, end + 1))
    return set(range(lo, hi + 1))  # Unknown effect scope: forget all restrictions.


def observed_unplanted(events, ridge_bounds):
    """Fold one episode's strictly ordered public events, retaining provenance.

    Harvest success proves unplanted for returned harvested_ridges under the
    reviewed FAIRY tool contract. Replanting merely removes this prohibition;
    it never proves readiness. Missing/malformed reset results and ambiguous
    exceptions also forget restrictions rather than reuse potentially old facts.
    Any environment notification is conservatively treated as an invalidation.
    """
    lo, hi = ridge_bounds
    if type(lo) is not int or type(hi) is not int or not 0 <= lo <= hi:
        raise ValueError("Expected declared integer ridge bounds")
    known, invalidations = {}, []
    previous = -1

    def forget(ridges, event, reason):
        affected = sorted(set(known) & ridges)
        for ridge in affected:
            del known[ridge]
        invalidations.append(
            {"event_id": event["event_id"], "reason": reason, "forgotten_ridges": affected}
        )

    for event in events:
        eid = event.get("event_id")
        if type(eid) is not int or eid <= previous:
            raise ValueError("Need strictly increasing event_id within one episode")
        previous = eid
        kind = event.get("event")
        if kind in {"run_start", "episode_start", "environment_notification", "crop_state_invalidated"}:
            forget(set(known), event, kind)
            continue
        if kind != "tool_end" or event.get("executed") is not True:
            continue
        name, result = event.get("tool_name"), event.get("result")
        args = event.get("arguments") or {}
        success = (
            event.get("status") == "ok" and not event.get("error")
            and isinstance(result, dict) and not result.get("error")
            and result.get("success") is not False
            and result.get("status") not in {"error", "failed", "failure", "rejected"}
        )
        if name in RESETS:
            # Even a failed/ambiguous planting attempt may have partial effects.
            # This is deliberately conservative; it can permit another bad choice.
            field = "planted_ridges" if name.endswith("__plant_seeds") else "replanted_ridges"
            affected = _ids(result.get(field), lo, hi) if success else None
            forget(
                affected if affected is not None else _scope(args, lo, hi),
                event, "plant_or_replant_observed",
            )
        elif name == "TractorApp__form_ridges":
            # Field preparation starts another lifecycle; no hidden field state read.
            forget(set(known), event, "field_preparation_observed")
        elif name == HARVEST:
            if success and result.get("status") == "ok":
                ridges = _ids(result.get("harvested_ridges"), lo, hi)
                if ridges is not None:
                    for ridge in ridges:
                        known[ridge] = {
                            "event_id": eid, "tool_call_id": event.get("tool_call_id"),
                            "result_path": "harvested_ridges", "basis": "successful_harvest",
                        }
            elif event.get("error_stage") != "tool_return":
                forget(_scope(args, lo, hi), event, "ambiguous_harvest_execution")
        elif name in READS and success:
            # Only explicit bool planted observations count, not growth stage,
            # NDVI, harvested flags, task text or LLM summaries.
            ridges = result.get("ridges")
            if isinstance(ridges, list):
                for index, row in enumerate(ridges):
                    if not isinstance(row, dict):
                        continue
                    ridge, planted = row.get("ridge_id"), row.get("planted")
                    if type(ridge) is not int or not lo <= ridge <= hi or type(planted) is not bool:
                        continue
                    if planted:
                        forget({ridge}, event, "explicit_planted_observation")
                    else:
                        known[ridge] = {
                            "event_id": eid, "tool_call_id": event.get("tool_call_id"),
                            "result_path": f"ridges[{index}].planted",
                            "basis": "explicit_unplanted_observation",
                        }
    return {
        "version": VERSION,
        "excluded_ridges": sorted(known),
        "evidence": {str(ridge): deepcopy(known[ridge]) for ridge in sorted(known)},
        "invalidations": invalidations,
        "scope": "Known unplanted only; all other ridges remain unknown, not harvest-ready",
    }


def harvest_range_menu(events, ridge_bounds, max_pass_width):
    """Enumerate legal conditional prefixes/completions without selecting one.

    A blocked ridge anywhere in a range excludes that entire range. Endpoints
    stay >= start and within the caller's declared pass width. No candidates
    means needs_context, not FINISH, a replacement action, or a forced refresh.
    """
    if type(max_pass_width) is not int or max_pass_width <= 0:
        raise ValueError("Expected positive declared max_pass_width")
    facts = observed_unplanted(events, ridge_bounds)
    lo, hi = ridge_bounds
    excluded = set(facts["excluded_ridges"])
    ends = {}
    for start in range(lo, hi + 1):
        allowed = []
        for end in range(start, min(hi, start + max_pass_width - 1) + 1):
            if end in excluded:
                break
            allowed.append(end)
        if allowed:
            ends[str(start)] = allowed
    return {
        "version": VERSION, "tool_name": HARVEST,
        "ridge_bounds": list(ridge_bounds), "max_pass_width": max_pass_width,
        "status": "ready" if ends else "needs_context",
        "start_candidates": [int(start) for start in ends],
        "end_candidates_by_start": ends, "facts": facts,
        "reason": None if ends else "No range remains under current visible unplanted evidence",
        "runtime_enabled": False,  # This helper only enumerates; runtime uses bind_harvest_contract.
    }
