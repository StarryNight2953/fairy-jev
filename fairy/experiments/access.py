"""Read-only preflight classification from saved smoke access evidence.

This does not probe services, retry requests, change credentials, rewrite old
results, or infer a model failure from an unavailable service. It is a derived
classification; original status and raw response remain in their source run.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from .paths import resolve_saved_path

ARCHITECTURES = ("llm_native", "jev", "hybrid")
RULES_VERSION = "smoke_access_evidence_v2"


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _service(config, family):
    selected = config if family == "jev" else config.get("llm", {})
    return {"family": family, "provider": "typesafe" if family == "jev" else selected.get("provider"),
            "model": selected.get("model"), "base_url": str(selected.get("base_url") or "").rstrip("/")}


def _requirements(config, architecture):
    from .credentials import required_credential_names
    return required_credential_names(config, architecture)


def _uses(architecture, family):
    return architecture == "hybrid" or architecture == ("jev" if family == "jev" else "llm_native")


def _timestamp(event):
    value = event.get("wall_timestamp")
    return value if type(value) in (int, float) and math.isfinite(value) else None


def _successful_access(loaded):
    """Paired actual successes, independent of task completion or index order."""
    successes = []
    for source in loaded:
        starts = {e.get("attempt_id"): e for e in source["events"]
                  if e.get("event") == "http_attempt_start"}
        config = source["manifest"].get("config", {})
        for event in source["events"]:
            start = starts.get(event.get("attempt_id"))
            if (event.get("event") != "http_attempt_end" or not start
                    or event.get("http_status") != 200 or event.get("status") != "ok"
                    or "raw_response_body" not in event or _timestamp(event) is None):
                continue
            role = event.get("role", "")
            if (not event.get("logical_request_id")
                    or start.get("role") != role
                    or start.get("logical_request_id") != event["logical_request_id"]
                    or not event.get("endpoint")
                    or start.get("endpoint") != event["endpoint"]):
                continue
            family = "jev" if role.startswith("jev_") else "llm" if role in {
                "llm_native_decision", "llm_native_summary", "llm_context_update"} else None
            if family is None:
                continue
            successes.append({"source_architecture": source["architecture"],
                "source_run": str(source["root"]), "trace_path": str(source["root"] / "trace.jsonl"),
                "trace_sha256": source["trace_sha256"], "event_id": event.get("event_id"),
                "attempt_id": event["attempt_id"], "wall_timestamp": _timestamp(event),
                "http_status": 200, "role": role, "endpoint": event["endpoint"],
                "service": _service(config, family)})
    return successes


def service_blockers_from_smoke(index, *, target_configs=None, available_credentials=()):
    """Return deterministic blockers/evidence by architecture, without mutation.

    ``index`` is the saved smoke index dict or its path. Only actual paired
    http_attempt events establish HTTP access denial. Missing credentials require
    the saved run_skipped event. HTTP 429/5xx remain unresolved transient failures
    when that smoke did not establish integration; they are not called permanent.
    A source block transfers only to architectures configured for the same service
    identity (or the same missing credential variable), never to an unrelated LLM.
    When configurations change, callers provide their actual current target
    configs. A newly available credential resolves only a prior missing-variable
    observation, never an actual HTTP access refusal. A historical 451 is no
    longer active when a later paired HTTP 200 proves access to that same service
    and endpoint. Both observations remain auditable; missing chronology does
    not establish recovery. A subsequent refusal still blocks. Source files
    remain intact; 401/403 and budget rules are unchanged.
    """
    if isinstance(index, (str, Path)):
        index = _json(resolve_saved_path(index))
    by_architecture = {architecture: [] for architecture in ARCHITECTURES}
    observations, loaded = [], []
    for record in index.get("runs", []):
        architecture = record.get("architecture")
        if architecture not in by_architecture:
            continue
        root = resolve_saved_path(record["path"])
        try:
            manifest = _json(root / "manifest.json")
            evaluation = _json(root / "evaluation.json")
            trace_path = root / "trace.jsonl"
            events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, ValueError) as error:
            blocker = {"reason": "smoke_evidence_unavailable", "failure_layer": "evidence",
                "classification": "unknown_access_status", "persistent_block": False,
                "source_architecture": architecture, "source_run": str(root),
                "detail": f"{type(error).__name__}: {error}"}
            by_architecture[architecture].append(blocker)
            observations.append(blocker)
            continue
        loaded.append({"architecture": architecture, "root": root, "manifest": manifest,
                       "evaluation": evaluation, "events": events, "trace_sha256": _hash(trace_path)})

    targets = (target_configs if target_configs is not None else
               {item['architecture']: item['manifest'].get('config', {}) for item in loaded})
    successes = _successful_access(loaded)
    resolved_blockers = []
    available_credentials = set(available_credentials)
    for source in loaded:
        architecture, root = source["architecture"], source["root"]
        config = source["manifest"].get("config", {})
        events, evaluation = source["events"], source["evaluation"]
        starts = {event.get("attempt_id"): event for event in events if event.get("event") == "http_attempt_start"}
        source_blocks = []
        for event in events:
            evidence = {"source_architecture": architecture, "source_run": str(root),
                "trace_path": str(root / "trace.jsonl"), "trace_sha256": source["trace_sha256"],
                "event_id": event.get("event_id"), "original_execution_status": evaluation.get("execution_status"),
                "original_task_success": evaluation.get("task_success")}
            if event.get("event") == "run_skipped" and event.get("reason") == "missing_credentials":
                names = [name for name in event.get("missing_credentials", [])
                         if isinstance(name, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", name)]
                if names:
                    source_blocks.append({**evidence, "reason": "missing_credentials",
                        "failure_layer": "external_credentials", "classification": "confirmed_missing_credentials",
                        "persistent_block": True, "missing_credentials": names,
                        "condition": "Requires the configured credential to be available; no model request was made by this skipped run."})
                continue
            if event.get("event") != "http_attempt_end" or event.get("attempt_id") not in starts:
                continue
            status, role = event.get("http_status"), event.get("role", "")
            if status not in {401, 403, 451, 429} and not (isinstance(status, int) and 500 <= status <= 599):
                continue
            if "raw_response_body" not in event:
                continue
            family = "jev" if role.startswith("jev_") else "llm" if role in {"llm_native_decision", "llm_native_summary", "llm_context_update"} else None
            if family is None:
                continue
            paired = starts[event["attempt_id"]]
            if paired.get("role") != role or paired.get("logical_request_id") != event.get("logical_request_id"):
                continue
            raw = event["raw_response_body"]
            serialized = raw if isinstance(raw, str) else json.dumps(raw, sort_keys=True)
            item = {**evidence, "attempt_id": event.get("attempt_id"), "http_status": status,
                    "wall_timestamp": _timestamp(event),
                    "endpoint": event.get("endpoint", paired.get("endpoint")), "role": role,
                    "service": _service(config, family),
                    "response_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
                    "raw_response_reference": f"trace.jsonl event_id={event.get('event_id')} raw_response_body"}
            if status in {401, 403, 451}:
                item.update(reason={401: "service_authentication_denied", 403: "service_authorization_denied", 451: "service_access_restricted"}[status],
                    failure_layer="external_credentials" if status == 401 else "external_service",
                    classification="confirmed_access_block", persistent_block=True,
                    condition="Observed access refusal; requires an external access/configuration change, not replacement model actions.")
                source_blocks.append(item)
            elif evaluation.get("live_integration_success") is not True:
                item.update(reason="unresolved_transient_service_failure", failure_layer="external_service",
                    classification="unknown_access_status", persistent_block=False,
                    condition="HTTP 429/5xx does not establish permanent denial, but this smoke did not demonstrate a usable integration chain.")
                source_blocks.append(item)
            else:
                observations.append({**item, "reason": "recovered_transient_http_error",
                    "classification": "informational", "persistent_block": False})
        for blocker in source_blocks:
            if blocker.get("http_status") == 451 and blocker.get("wall_timestamp") is not None:
                later = [s for s in successes if s["service"] == blocker["service"]
                         and s["endpoint"] == blocker["endpoint"]
                         and s["wall_timestamp"] > blocker["wall_timestamp"]]
                if later:
                    resolved = {**blocker, "resolution_status": "superseded_by_later_success",
                        "active_block": False, "resolved_by": max(later, key=lambda s: s["wall_timestamp"]),
                        "resolution_scope": "Later same-service/endpoint HTTP 200 supersedes this historical 451 for preflight; it does not guarantee future access or task success."}
                    observations.append(resolved)
                    resolved_blockers.append(resolved)
                    continue
            observations.append(blocker)
            for target_architecture, target_config in targets.items():
                if blocker["reason"] == "missing_credentials":
                    applies = bool((set(blocker["missing_credentials"]) - available_credentials)
                                   & set(_requirements(target_config, target_architecture)))
                else:
                    service = blocker["service"]
                    applies = _uses(target_architecture, service["family"]) and _service(target_config, service["family"]) == service
                if applies:
                    by_architecture[target_architecture].append(dict(blocker))
    # Multiple missing-key artifacts can prove the same condition; retain one
    # per evidence event, never collapse distinct actual HTTP refusals.
    for architecture, blockers in by_architecture.items():
        unique = {}
        for blocker in blockers:
            key = (blocker.get("reason"), blocker.get("trace_path"), blocker.get("event_id"), blocker.get("source_run"))
            unique[key] = blocker
        by_architecture[architecture] = list(unique.values())
    return {"rules_version": RULES_VERSION, "blocked": any(by_architecture.values()),
        "by_architecture": by_architecture, "observations": observations,
        "resolved_blockers": resolved_blockers,
        "scope": "Derived access preflight from immutable smoke evidence; no API calls or edits. A block is not a model task failure and does not erase original status.",
        "recommended_formal_status": "blocked" if any(by_architecture.values()) else "eligible_for_other_formal_checks"}
