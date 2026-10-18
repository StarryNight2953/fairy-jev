"""Independent serial Jev loop; one selected native call means one execution."""
from __future__ import annotations

import time
from collections import Counter

from fairy.scenarios.workflow import Workflow, WorkflowStep

from .client import DecisionError
from .state import VisibleState
from .tools import ToolExecutor, validate_arguments
from .trace import digest

DEFAULT_BUDGETS = {"max_steps": 40, "max_tool_calls": 40, "max_decision_calls": 80,
                   "wall_timeout_seconds": 600, "repeated_parameter_failure_limit": 3}


class JevAgent:
    def __init__(self, catalog, backend, resolver, trace, config=None):
        self.catalog, self.backend, self.resolver, self.trace = catalog, backend, resolver, trace
        self.config = config or {}
        self.budgets = {**DEFAULT_BUDGETS, **self.config.get("budgets", {})}
        if any(float(value) <= 0 for value in self.budgets.values()):
            raise ValueError("run budgets must be positive")
        self.state = VisibleState(**self.config.get("history", {}))
        self.workflow = Workflow()
        self.time_manager = None
        self.executor = None
        self.poll_environment = lambda: None
        self.decision_count = 0
        self.step_count = 0
        self.stop_reason = None
        self.failure_layer = None
        self.run_elapsed_seconds = 0.0
        self._failures = Counter()
        self.current_step_id = None

    @property
    def tool_call_count(self):
        return self.executor.call_count if self.executor else 0

    def set_time_manager(self, shared_clock):
        self.time_manager = shared_clock
        self.executor = ToolExecutor(self.catalog, shared_clock, self.workflow, self.trace)

    def add_system_context(self, content):
        if content not in self.state.system_context:
            self.state.system_context.append(content)
            self.trace.emit("system_context", content=content)

    def on_environment_event(self, content, *, time=None):
        self.state.add_observation({"kind": "environment_notification", "result": content,
            "sim_time_after": self.time_manager.time(), "time": time})
        self.workflow.add_node(WorkflowStep(op_type="system", content=content, time=time))
        self.trace.emit("environment_notification", step_id=self.current_step_id, content=content,
                        sim_time=self.time_manager.time())

    def record_event(self, event, **fields):
        return self.trace.emit(event, **fields)

    def _limit(self, *, decision=False, tool=False):
        elapsed = time.perf_counter() - self._started
        reason = None
        if elapsed >= self.budgets["wall_timeout_seconds"]:
            reason = "wall_timeout"
        elif decision and self.decision_count >= self.budgets["max_decision_calls"]:
            reason = "max_decision_calls"
        elif tool and self.tool_call_count >= self.budgets["max_tool_calls"]:
            reason = "max_tool_calls"
        if reason:
            self.stop_reason = reason
            self.trace.emit("budget_stop", step_id=self.current_step_id, reason=reason,
                actual_elapsed_seconds=elapsed, budgets=self.budgets,
                timeout_semantics="Checked at decision/tool boundaries; an in-flight request may overrun")
        return reason is not None

    def _decide(self, stage, state, instructions, options):
        if self._limit(decision=True):
            return None, None
        self.decision_count += 1
        decision_id = f"decision_{self.decision_count:04d}"
        context = {"step_id": self.current_step_id, "decision_id": decision_id,
                   "decision_stage": stage, "attempt_id": f"{decision_id}_attempt_1"}
        started = time.perf_counter()
        self.trace.emit("decision_start", **context, state=state, instructions=instructions,
            criteria=options, candidate_order=list(options), candidate_hash=digest(options),
            candidate_count=len(options), catalog_hash=self.catalog.hash,
            requested_model=getattr(self.backend, "requested_model", None),
            sdk_version=getattr(self.backend, "sdk_version", None), sim_time=self.time_manager.time())
        try:
            decision = self.backend.decide(state, stage, instructions, options)
            # Enforce the same boundary even for injected backends.
            if decision.selected_option not in options:
                raise DecisionError("invalid_return_option", decision.record)
        except Exception as exc:
            self.trace.emit("decision_end", **context, status="error", error=f"{type(exc).__name__}: {exc}",
                response=getattr(exc, "record", {}), wall_elapsed_seconds=time.perf_counter()-started,
                sim_time=self.time_manager.time())
            self.stop_reason, self.failure_layer = "decision_error", "decision_backend"
            return None, decision_id
        self.trace.emit("decision_end", **context, status="ok", selected_option=decision.selected_option,
            response=decision.record, wall_elapsed_seconds=time.perf_counter()-started,
            sim_time=self.time_manager.time())
        return decision.selected_option, decision_id

    def _parameter_failure(self, tool_name, visible, generated):
        feedback = {"tool_name": tool_name, "status": generated["status"],
                    "missing_fields": generated.get("missing_fields", []), "reason": generated.get("reason"),
                    "executed": False}
        self.state.add_feedback(feedback)
        self.trace.emit("controller_feedback", step_id=self.current_step_id, **feedback)
        key = digest({"tool": tool_name, "task": visible["task"], "observations": visible["observations"],
                      "status": generated["status"], "missing": generated.get("missing_fields", []),
                      "reason": generated.get("reason")})
        self._failures[key] += 1
        if generated["status"] == "candidate_limit_exceeded":
            self.stop_reason, self.failure_layer = "candidate_limit_exceeded", "candidate_configuration"
        elif self._failures[key] >= self.budgets["repeated_parameter_failure_limit"]:
            self.stop_reason, self.failure_layer = "repeated_parameter_failure", "parameter_candidates"

    def run(self, input, max_tool_calls=None, timeout_seconds=None):
        if self.executor is None:
            raise ValueError("Engine must supply a shared TimeManager before run")
        if self.step_count or self.decision_count:
            raise ValueError("A JevAgent instance is single-run; create a fresh run and trace")
        if max_tool_calls is not None:
            self.budgets["max_tool_calls"] = max_tool_calls
        if timeout_seconds is not None:
            self.budgets["wall_timeout_seconds"] = timeout_seconds
        self.state.task = input
        self._started = time.perf_counter()
        self.trace.emit("run_start", backend=self.backend.name, budgets=self.budgets,
                        task=input, catalog_hash=self.catalog.hash)
        try:
            for step in range(1, int(self.budgets["max_steps"])+1):
                if self._limit(tool=True):
                    break
                self.step_count = step
                self.current_step_id = f"step_{step:04d}"
                self.poll_environment()
                visible = self.state.snapshot(self.time_manager.time())
                selected, tool_decision = self._decide("tool", visible,
                    "Based on the task, visible observations, and results of previous actions, which single tool should be called next? "
                    "Every available tool is listed. Observe necessary facts before acting; use FINISH to stop. "
                    "Tool execution errors and missing argument context are feedback, not evidence of task completion.", self.catalog.options)
                if selected is None:
                    break
                if self._limit():
                    break
                if selected == "FINISH":
                    self.stop_reason = "model_finish"
                    self.trace.emit("model_finish", step_id=self.current_step_id, decision_id=tool_decision)
                    break
                entry = self.catalog.by_name[selected]
                decision_ids = [tool_decision]
                if not entry["parameters"]:
                    generated = {"status": "ready", "candidates": {"args_000": {"arguments": {}, "source_refs": ["tool_signature:no_parameters"]}}}
                else:
                    generated = self.resolver.generate(selected, visible)
                candidates = generated.get("candidates", {})
                if generated.get("status") == "ready":
                    if not 1 <= len(candidates) <= 255:
                        generated = {**generated, "status": "candidate_limit_exceeded" if len(candidates)>255 else "needs_context",
                                     "reason": "Parameter Choice requires 1–255 complete candidates"}
                    else:
                        try:
                            for candidate in candidates.values():
                                validate_arguments(entry, candidate["arguments"])
                        except (ValueError, KeyError, TypeError) as exc:
                            generated = {**generated, "status": "needs_context", "reason": f"invalid_candidate: {exc}"}
                self.trace.emit("parameter_candidates", step_id=self.current_step_id, tool_decision_id=tool_decision,
                    tool_name=selected, generation=generated, candidate_count=len(candidates),
                    candidate_order=list(candidates), candidate_hash=digest(candidates))
                if generated.get("status") != "ready":
                    self._parameter_failure(selected, visible, generated)
                    if self.stop_reason:
                        break
                    continue
                if entry["parameters"]:
                    parameter_choice, argument_decision = self._decide("parameters", visible,
                        f"For the already selected native tool {selected}, choose one complete argument dictionary "
                        "that best serves the visible task and observations. Each candidate is one invocation; "
                        "source_refs describe deterministic candidate construction, not model reasoning.", candidates)
                    if parameter_choice is None:
                        break
                    decision_ids.append(argument_decision)
                else:
                    parameter_choice = "args_000"
                if self._limit(tool=True):
                    break
                arguments = candidates[parameter_choice]["arguments"]
                row = self.executor.execute(selected, arguments, step_id=self.current_step_id, decision_ids=decision_ids)
                self.state.add_observation(row)
                self.trace.emit("observation_added", step_id=self.current_step_id,
                    tool_call_id=row["tool_call_id"], observation_id=len(self.state.observations)-1)
                self.poll_environment()
            if self.stop_reason is None:
                self.stop_reason = "max_steps"
        except Exception as exc:
            self.stop_reason, self.failure_layer = "run_error", "controller"
            self.trace.emit("run_error", step_id=self.current_step_id, error=f"{type(exc).__name__}: {exc}")
        finally:
            self.run_elapsed_seconds = time.perf_counter()-self._started
            self.trace.emit("run_end", stop_reason=self.stop_reason, failure_layer=self.failure_layer,
                step_count=self.step_count, decision_count=self.decision_count, tool_call_count=self.tool_call_count,
                wall_elapsed_seconds=self.run_elapsed_seconds,
                timeout_overrun_seconds=max(0.0, self.run_elapsed_seconds-self.budgets["wall_timeout_seconds"]))
        return self.workflow
