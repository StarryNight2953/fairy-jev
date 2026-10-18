"""Shared actual-attempt budgets and a POSIX whole-episode deadline.

BudgetStop deliberately bypasses ordinary SDK/Agent retry handlers. It is caught
only at the episode boundary; no tool is silently replaced or retried.
"""
from __future__ import annotations

import json
import signal
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from fairy.agents.Jev.trace import write_json


class BudgetStop(BaseException):
    def __init__(self, reason, *, side_effects="not_started"):
        self.reason, self.side_effects = reason, side_effects
        super().__init__(reason)


DEFAULT_LIMITS = {
    "max_tool_attempts": 40, "max_decision_rounds": 40,
    "max_http_attempts": 120, "max_jev_http_attempts": 100,
    "max_updater_attempts": 20, "max_updates": 10,
    "wall_timeout_seconds": 900,
}


class LiveLedger:
    """Persistent task-wide cap. Stages/run names never reset consumed budget."""
    def __init__(self, path, *, max_episodes=12, max_http_attempts=1440):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_episodes, self.max_http_attempts = max_episodes, max_http_attempts
        if not self.path.exists():
            with self.path.open("x") as f:
                json.dump({"ledger_id": uuid4().hex, "episodes": [], "http_attempts": 0,
                           "max_episodes": max_episodes, "max_http_attempts": max_http_attempts}, f)

    @contextmanager
    def _locked(self):
        import fcntl
        with self.path.open("r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            data = json.load(f)
            try:
                yield data
            finally:
                f.seek(0)
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.truncate()
                f.flush()
                fcntl.flock(f, fcntl.LOCK_UN)

    def reserve_episode(self, run_id, architecture, stage, slot):
        with self._locked() as data:
            if len(data["episodes"]) >= min(self.max_episodes, data["max_episodes"]):
                raise BudgetStop("total_live_episodes")
            if any(r["slot"] == slot for r in data["episodes"]):
                raise BudgetStop("episode_slot_already_consumed")
            if stage == "smoke" and any(r["stage"] == "smoke" and r["architecture"] == architecture for r in data["episodes"]):
                raise BudgetStop("smoke_slot_already_consumed")
            data["episodes"].append({"run_id": run_id, "architecture": architecture,
                                     "stage": stage, "slot": slot, "http_attempts": 0, "status": "reserved"})

    def reserve_http(self, run_id):
        with self._locked() as data:
            if data["http_attempts"] >= min(self.max_http_attempts, data["max_http_attempts"]):
                raise BudgetStop("total_model_http_attempts")
            episode = next(r for r in data["episodes"] if r["run_id"] == run_id)
            data["http_attempts"] += 1
            episode["http_attempts"] += 1
            episode["status"] = "running"

    def finish(self, run_id, status):
        with self._locked() as data:
            next(r for r in data["episodes"] if r["run_id"] == run_id)["status"] = status

    def snapshot(self):
        with self._locked() as data:
            return json.loads(json.dumps(data))


class EpisodeRuntime:
    def __init__(self, trace, limits=None, *, ledger=None):
        self.trace = trace
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        if any(float(v) <= 0 for v in self.limits.values()):
            raise ValueError("all episode limits must be positive")
        self.budget = self
        self.ledger = ledger
        self.started = time.monotonic()
        self.deadline = self.started + self.limits["wall_timeout_seconds"]
        self.counts = Counter()
        self.role_attempts = Counter()
        self.role_logical_requests = Counter()
        self.current_role = None
        self.current_request_id = None
        self.active_boundary = None
        self.stop_reason = None

    def remaining_seconds(self):
        return max(0.0, self.deadline-time.monotonic())

    def stop(self, reason):
        self.stop_reason = reason
        raise BudgetStop(reason, side_effects="unknown" if self.active_boundary == "tool" else "not_started")

    def check(self):
        if self.remaining_seconds() <= 0:
            self.stop("wall_timeout")
        if self.counts["tool_attempts"] >= self.limits["max_tool_attempts"]:
            self.stop("max_tool_attempts")
        if self.counts["http_attempts"] >= self.limits["max_http_attempts"]:
            self.stop("max_http_attempts")

    def reserve_round(self):
        self.check()
        if self.counts["decision_rounds"] >= self.limits["max_decision_rounds"]:
            self.stop("max_decision_rounds")
        self.counts["decision_rounds"] += 1

    def reserve_update(self):
        self.check()
        if self.counts["updates"] >= self.limits["max_updates"]:
            self.stop("max_updates")
        self.counts["updates"] += 1

    def reserve_tool(self):
        self.check()
        self.counts["tool_attempts"] += 1
        return self.counts["tool_attempts"]

    def reserve_http(self, role=None, logical_id=None):
        self.check()
        role = role or self.current_role
        if not role:
            raise RuntimeError("HTTP attempt has no model role")
        if role.startswith("jev_") and sum(v for k,v in self.role_attempts.items() if k.startswith("jev_")) >= self.limits["max_jev_http_attempts"]:
            self.stop("max_jev_http_attempts")
        if role == "llm_context_update" and self.role_attempts[role] >= self.limits["max_updater_attempts"]:
            self.stop("max_updater_attempts")
        if self.ledger:
            self.ledger.reserve_http(self.trace.run_id)
        self.counts["http_attempts"] += 1
        self.role_attempts[role] += 1
        return f"http_{self.counts['http_attempts']:04d}"

    @contextmanager
    def logical(self, role):
        self.check()
        previous = self.current_role, self.current_request_id
        self.counts["logical_requests"] += 1
        self.role_logical_requests[role] += 1
        self.current_role = role
        self.current_request_id = f"model_{self.counts['logical_requests']:04d}"
        started = time.monotonic()
        request_id = self.current_request_id
        self.trace.emit("model_request_start", role=role, request_id=request_id)
        try:
            yield request_id
        finally:
            self.trace.emit("model_request_end", role=role, request_id=request_id,
                            elapsed_seconds=time.monotonic()-started)
            self.current_role, self.current_request_id = previous

    @contextmanager
    def deadline_guard(self):
        if not hasattr(signal, "setitimer"):
            raise RuntimeError("Strict whole-episode deadline requires POSIX setitimer")
        old_handler = signal.getsignal(signal.SIGALRM)
        def expired(*_):
            self.stop("wall_timeout")
        signal.signal(signal.SIGALRM, expired)
        previous = signal.setitimer(signal.ITIMER_REAL, self.remaining_seconds())
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old_handler)
            if previous[0] > 0:
                signal.setitimer(signal.ITIMER_REAL, *previous)

    def snapshot(self):
        return {"limits": self.limits, "counts": dict(self.counts),
                "role_http_attempts": dict(self.role_attempts),
                "role_logical_requests": dict(self.role_logical_requests),
                "elapsed_seconds": time.monotonic()-self.started,
                "remaining_seconds": self.remaining_seconds(), "stop_reason": self.stop_reason,
                "deadline_policy": "POSIX whole-episode timer; per-HTTP timeout clamped to remaining time"}
