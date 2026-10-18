"""Fixed-interval LLM material, with the original Jev two-Choice execution loop."""
from __future__ import annotations

from ..agent import JevAgent
from ..trace import digest
from .context import PacketContext, StateBudgetExceeded, request_size
from .updater import LLMUpdateError


class HybridJevAgent(JevAgent):
    """The LLM writes advice; only Jev choices can execute FAIRY tools.

    Parent hooks keep candidate construction on a separate, frozen original
    VisibleState. A packet is never inserted into that state or its fact ledger.
    """

    def __init__(self, catalog, backend, resolver, trace, config=None, *, runtime=None, updater=None):
        super().__init__(catalog, backend, resolver, trace, config, runtime=runtime)
        if updater is None:
            raise ValueError("hybrid requires an explicit LLM or offline scripted updater")
        self.updater = updater
        self.packet_context = PacketContext(trace, self.config)
        hybrid = self.config.get("hybrid", {})
        self.update_interval = hybrid.get("update_interval", 4)
        if type(self.update_interval) is not int or self.update_interval < 1:
            raise ValueError("hybrid.update_interval must be a positive integer")
        self._tools_at_last_update = 0
        self.update_count = 0

    def _before_round(self):
        # Do not spend an update after any known episode or decision budget ends.
        if self._limit(tool=True, decision=True):
            return
        if self.runtime:
            jev_attempts = sum(count for role, count in self.runtime.role_attempts.items()
                               if role.startswith("jev_"))
            if jev_attempts >= self.runtime.limits["max_jev_http_attempts"]:
                self.runtime.stop("max_jev_http_attempts")
        if (self.packet_context.packet is not None
                and self.tool_call_count - self._tools_at_last_update < self.update_interval):
            return
        if self.runtime:
            self.runtime.reserve_update()
        update_input = self.packet_context.build_update_input(
            self.state.task, self.state.system_context, self.catalog.options, self.tool_call_count)
        self.trace.emit("hybrid_update_due", step_id=self.current_step_id,
            trigger="initial" if self.packet_context.packet is None else "fixed_tool_interval",
            tool_attempts=self.tool_call_count, update_interval=self.update_interval,
            input_event_ids=update_input["input_event_ids"],
            input_boundary_event_id=update_input["input_boundary_event_id"])
        try:
            payload = self.updater.update(update_input)
            self.packet_context.accept_packet(payload, update_input,
                source_request_id=payload.get("source_request_id"), tool_attempts=self.tool_call_count)
        except StateBudgetExceeded as exc:
            self.stop_reason, self.failure_layer = "state_budget_exceeded", "context_budget"
            self.trace.emit("state_budget_exceeded", step_id=self.current_step_id,
                role="llm_context_update", measurements=exc.measurements)
            return
        except (LLMUpdateError, ValueError, TypeError, KeyError) as exc:
            self.stop_reason, self.failure_layer = "llm_update_error", "llm_context_update"
            self.trace.emit("hybrid_update_error", step_id=self.current_step_id,
                error=f"{type(exc).__name__}: {exc}", tool_attempts=self.tool_call_count,
                retained_packet_used=False)
            return
        self.update_count += 1
        self._tools_at_last_update = self.tool_call_count
        self._limit(tool=True, decision=True)

    def _decision_state(self, visible):
        budgets = self.runtime.snapshot() if self.runtime else {
            "limits": self.budgets, "tool_attempts": self.tool_call_count,
            "decision_rounds": self.step_count, "decision_calls": self.decision_count}
        projected = self.packet_context.freeze(visible["task"], visible["system_context"],
                                               visible["current_time"], budgets)
        # This internal snapshot is audit evidence for deterministic candidates;
        # it is not concatenated into the material projection sent to Jev.
        self.trace.emit("hybrid_snapshot", step_id=self.current_step_id,
            **self._decision_metadata(projected), rendered_state_hash=digest(projected),
            candidate_evidence=visible, candidate_evidence_hash=digest(visible),
            candidate_source_policy="Original public VisibleState only; LLM packet excluded")
        return projected

    def _decision_metadata(self, state):
        return {"packet_id": state["packet"]["packet_id"],
            "packet_version": state["packet"]["packet_version"],
            "observation_cutoff_event_id": state["observation_cutoff_event_id"],
            "fresh_event_ids": state["fresh_event_ids"]}

    def _parameter_failure(self, tool_name, visible, generated):
        # Failed candidate construction is visible immediately but does not
        # execute a tool, refresh material, or create another task controller.
        feedback = {"tool_name": tool_name, "status": generated["status"],
            "missing_fields": generated.get("missing_fields", []),
            "reason": generated.get("reason"), "executed": False}
        self.state.add_feedback(feedback)
        self.trace.emit("controller_feedback", step_id=self.current_step_id, **feedback)
        if generated["status"] == "candidate_limit_exceeded":
            self.stop_reason, self.failure_layer = "candidate_limit_exceeded", "candidate_configuration"
        # Repeated needs_context is bounded by the episode's decision-round,
        # actual HTTP and wall-time limits. Pure Jev's legacy policy is intact.

    def _decide(self, stage, state, instructions, options):
        try:
            measurements = request_size(state, stage, instructions, options,
                limits=self.packet_context.limits,
                model=getattr(self.backend, "requested_model", None) or "jev-1.13.0")
        except StateBudgetExceeded as exc:
            self.stop_reason, self.failure_layer = "state_budget_exceeded", "context_budget"
            self.trace.emit("state_budget_exceeded", step_id=self.current_step_id,
                decision_stage=stage, **self._decision_metadata(state), measurements=exc.measurements)
            return None, None
        self.trace.emit("jev_request_size", step_id=self.current_step_id,
            decision_id=f"decision_{self.decision_count + 1:04d}", decision_stage=stage,
            **self._decision_metadata(state), measurements=measurements)
        return super()._decide(stage, state, instructions, options)
