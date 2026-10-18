"""Versioned LLM advice and exact, agent-visible evidence projections.

This module accepts serialized trace events, never an Engine, Scenario, evaluator,
or checkpoint. The old Jev state remains separate for deterministic candidates.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from ..trace import canonical, digest


VISIBLE_EVENTS = frozenset({"tool_end", "environment_notification", "controller_feedback"})
DEFAULT_REQUEST_LIMITS = {
    "max_request_bytes": 60000,
    "max_request_chars": 60000,
    "max_brief_chars": 4000,
    "max_updater_input_bytes": 120000,
}


def normalized_limits(config=None, *, direct=False):
    config = config or {}
    hybrid = config.get("hybrid", {})
    values = config if direct else {**config.get("request_limits", {}), **hybrid.get("request_limits", {})}
    aliases = {"max_jev_request_bytes": "max_request_bytes", "max_jev_request_chars": "max_request_chars",
               "max_llm_request_bytes": "max_updater_input_bytes"}
    limits = {**DEFAULT_REQUEST_LIMITS, **{aliases.get(key, key): value for key, value in values.items()}}
    if not direct and "max_brief_chars" in hybrid:
        limits["max_brief_chars"] = hybrid["max_brief_chars"]
    if any(type(limits[key]) is not int or limits[key] <= 0 for key in DEFAULT_REQUEST_LIMITS):
        raise ValueError("request size limits must be positive integers")
    return limits


class StateBudgetExceeded(ValueError):
    def __init__(self, measurements):
        self.measurements = measurements
        super().__init__("state_budget_exceeded")


def lengths(value):
    serialized = canonical(value)
    return {"chars": len(serialized), "utf8_bytes": len(serialized.encode("utf-8"))}


def request_size(state, stage, instructions, options, limits=None, model="jev-1.13.0"):
    """Check the full uncompressed request; these are sizes, not token counts."""
    from typesafe_sdk import Choice
    # Use the pinned SDK's exact codec and public Choice type. No client, key,
    # transport, or API operation is needed to prepare these bytes.
    from typesafe_sdk._core.json import serialize
    limits = normalized_limits(limits, direct=True)
    request = {"state": state, "model": model, "questions": {stage: Choice(
        instructions=instructions, criteria=options)}}
    wire = serialize(request)
    measurement = {"components": {
        "task": lengths(state.get("task", "")),
        "system_context": lengths(state.get("system_context", [])),
        "packet": lengths(state.get("packet")),
        "fresh_events": lengths(state.get("fresh_events", [])),
        "instructions": lengths(instructions),
        "tool_menu" if stage == "tool" else "parameter_candidates": lengths(options),
    }, "complete_request": {"chars": len(wire.decode("utf-8")), "utf8_bytes": len(wire)}, "limits": limits,
        "serialization": "typesafe-sdk 0.7.5 _core.json.serialize, public Choice; state/model/questions order matches prepare_system_one; compression disabled",
        "component_serialization": "canonical JSON for independently measured components; complete_request is actual SDK wire encoding",
        "service_context_limit": {"per_question_tokens": 32000, "per_request_tokens": 64000,
            "source": "https://docs.typesafe.ai/models", "checked_on": "2026-10-18"},
        "token_count": None,
        "token_count_status": "unavailable: pinned SDK has no local server-tokenizer interface",
        "budget_semantics": "Configured UTF-8/character proxy based on offline requests; not an exact token bound. No fields or options are truncated.",
        "request_hash": digest(json.loads(wire)), "wire_sha256": hashlib.sha256(wire).hexdigest()}
    measurement["within_budget"] = (
        measurement["complete_request"]["utf8_bytes"] <= limits["max_request_bytes"]
        and measurement["complete_request"]["chars"] <= limits["max_request_chars"])
    if not measurement["within_budget"]:
        raise StateBudgetExceeded(measurement)
    return measurement


def validate_material(payload, allowed_event_ids, max_brief_chars=4000):
    if not isinstance(payload, dict) or set(payload) != {"brief", "evidence_refs"}:
        raise ValueError("material must contain exactly brief and evidence_refs")
    brief, refs = payload["brief"], payload["evidence_refs"]
    if not isinstance(brief, str) or not brief.strip() or len(brief) > max_brief_chars:
        raise ValueError("brief must be nonempty text within max_brief_chars")
    if not isinstance(refs, list) or any(type(ref) is not int for ref in refs):
        raise ValueError("evidence_refs must be a list of integer trace event_id values")
    if len(refs) != len(set(refs)) or not set(refs) <= set(allowed_event_ids):
        raise ValueError("evidence_refs must be unique and refer only to evidence actually supplied")
    return {"brief": brief, "evidence_refs": refs}


class PacketContext:
    def __init__(self, trace, config=None):
        self.trace = trace
        self.config = config or {}
        self.limits = normalized_limits(self.config)
        self.packet = None
        self._covered_boundary = -1

    def visible_events(self, *, through=None):
        return [deepcopy(row) for row in self.trace.events
                if row.get("event") in VISIBLE_EVENTS
                and (through is None or row["event_id"] <= through)]

    def build_update_input(self, task, system_context, tool_options, tool_attempts):
        boundary = self.trace.events[-1]["event_id"] if self.trace.events else -1
        visible = self.visible_events(through=boundary)
        fresh = [row for row in visible if row["event_id"] > self._covered_boundary]
        prior_refs = set((self.packet or {}).get("evidence_refs", []))
        prior = [row for row in visible if row["event_id"] in prior_refs
                 and row["event_id"] <= self._covered_boundary]
        supplied = sorted({row["event_id"] for row in fresh + prior})
        result = {
            "task": task, "system_context": deepcopy(system_context),
            "tool_capabilities": deepcopy(tool_options),
            "previous_packet": deepcopy(self.packet), "fresh_events": fresh,
            "prior_cited_evidence": prior,
            "input_event_ids": supplied, "input_boundary_event_id": boundary,
            "not_resent_prior_event_ids": [row["event_id"] for row in visible
                if row["event_id"] <= self._covered_boundary and row["event_id"] not in supplied],
            "tool_attempts": tool_attempts,
            "evidence_semantics": "Only listed events are supplied as raw evidence. Previous packet is LLM interpretation, not environment truth. Source IDs are integers from this run trace.",
        }
        return result

    def accept_packet(self, payload, update_input, source_request_id=None, tool_attempts=None):
        material = {key: payload[key] for key in ("brief", "evidence_refs") if key in payload}
        material = validate_material(material, update_input["input_event_ids"], self.limits["max_brief_chars"])
        actual_ids = {row["event_id"] for row in
                      update_input["fresh_events"] + update_input["prior_cited_evidence"]}
        if actual_ids != set(update_input["input_event_ids"]):
            raise ValueError("packet coverage does not match supplied raw evidence")
        boundary = update_input["input_boundary_event_id"]
        if type(boundary) is not int or boundary < self._covered_boundary or boundary >= len(self.trace.events):
            raise ValueError("packet boundary is not a current trace prefix")
        visible = {row["event_id"]: row for row in self.visible_events(through=boundary)}
        for row in update_input["fresh_events"] + update_input["prior_cited_evidence"]:
            if row != visible.get(row["event_id"]):
                raise ValueError("packet evidence is not an exact visible trace prefix")
        version = (self.packet or {}).get("packet_version", 0) + 1
        self.packet = {**deepcopy(material), "source_kind": "llm_interpretation_and_advice",
            "packet_id": f"packet_{version:04d}", "packet_version": version,
            "created_after_tool_attempts": update_input["tool_attempts"] if tool_attempts is None else tool_attempts,
            "covers_through_event_id": boundary,
            "input_event_ids": deepcopy(update_input["input_event_ids"]),
            "not_resent_prior_event_ids": deepcopy(update_input["not_resent_prior_event_ids"]),
            "coverage_semantics": "Boundary of this submitted visible prefix; input_event_ids lists exact raw evidence. Earlier omitted events survive only as prior interpretation unless explicitly resent.",
            "source_request_id": source_request_id or payload.get("source_request_id"),
            "update_input_hash": digest(update_input)}
        self._covered_boundary = boundary
        self.trace.emit("packet_accepted", packet=deepcopy(self.packet),
            trigger="initial" if version == 1 else "fixed_tool_interval")
        return deepcopy(self.packet)

    def freeze(self, task, system_context, current_time, budgets):
        if self.packet is None:
            raise ValueError("hybrid decisions require a valid packet")
        visible = self.visible_events()
        fresh = [row for row in visible if row["event_id"] > self._covered_boundary]
        boundary = self.trace.events[-1]["event_id"] if self.trace.events else -1
        return deepcopy({"task": task, "system_context": system_context,
            "current_time": current_time, "packet": self.packet, "fresh_events": fresh,
            "fresh_event_ids": [row["event_id"] for row in fresh],
            "observation_cutoff_event_id": boundary, "budgets": budgets,
            "source_policy": "Task and tool results are public evidence. Packet is LLM interpretation/advice. New evidence may contradict old advice: check its source/time. Advice never constitutes observed facts or tool execution.",
            "finish_semantics": "FINISH means the entire original task is complete."})
