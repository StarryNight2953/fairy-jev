"""Independent serial Jev loop; one selected native call means one execution."""
from __future__ import annotations

import time
from collections import Counter
from contextlib import nullcontext

from fairy.scenarios.workflow import Workflow, WorkflowStep
from fairy.experiments.budget import BudgetStop

from .client import DecisionError
from .state import TASK_HISTORY_STRATEGY, VisibleState, validate_state_strategy
from .tools import ToolExecutor, validate_arguments
from .trace import canonical, digest
from .harvest_contract import bind_harvest_contract, validate_harvest_policy
from .parameter_contracts import (
    CONTRACT_PARAMETER_STRATEGY, CONDITIONAL_STRATEGIES, completion_ranges, prefix_candidates, range_contract,
    validate_contract_arguments, validate_parameter_strategy,
)

DEFAULT_BUDGETS = {"max_steps": 40, "max_tool_calls": 40, "max_decision_calls": 80,
                   "wall_timeout_seconds": 7200, "repeated_parameter_failure_limit": 3}


class JevAgent:
    def __init__(self, catalog, backend, resolver, trace, config=None, *, runtime=None):
        self.catalog, self.backend, self.resolver, self.trace = catalog, backend, resolver, trace
        self.config = config or {}
        self.state_strategy = validate_state_strategy(self.config)
        self.parameter_strategy = validate_parameter_strategy(self.config)
        self.harvest_state_policy = validate_harvest_policy(self.config)
        self.runtime = runtime
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
        self.executor = ToolExecutor(self.catalog, shared_clock, self.workflow, self.trace,
            before_execute=self._reserve_native_tool if self.runtime else None,
            after_execute=self._finish_native_tool if self.runtime else None)

    def _reserve_native_tool(self):
        self.runtime.reserve_tool()
        self.runtime.active_boundary = "tool"

    def _finish_native_tool(self):
        self.runtime.active_boundary = None

    def _before_round(self):
        """Optional architecture hook, after polling and before the frozen state."""

    def _decision_state(self, visible):
        """Select only model input; candidates always use the original visible."""
        if self.state_strategy == TASK_HISTORY_STRATEGY:
            return self.state.task_history_snapshot(visible["current_time"])
        return visible

    def _decision_metadata(self, state):
        serialized = canonical(state)
        return {"state_strategy": self.state_strategy, "state_measurements": {
            "canonical_chars": len(serialized), "utf8_bytes": len(serialized.encode("utf-8")),
            "raw_tool_observations": sum(bool(row.get("tool_name")) for row in self.state.observations),
            "raw_environment_observations": sum(not row.get("tool_name") for row in self.state.observations),
            "raw_controller_feedback": len(self.state.feedback),
        }}

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
        if self.runtime:
            self.runtime.check()
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
        role = "jev_tool_choice" if stage == "tool" else "jev_argument_choice"
        logical = self.runtime.logical(role) if self.runtime else nullcontext(None)
        with logical as request_id:
            return self._invoke_decision(stage, state, instructions, options, decision_id, request_id, role)

    def _invoke_decision(self, stage, state, instructions, options, decision_id, request_id, role):
        context = {"step_id": self.current_step_id, "decision_id": decision_id,
                   "decision_stage": stage, "attempt_id": None if self.runtime else f"{decision_id}_attempt_1"}
        context.update(self._decision_metadata(state))
        if self.runtime:
            context.update(role=role, request_id=request_id)
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
        except BudgetStop as exc:
            self.trace.emit("decision_end", **context, status="stopped", reason=exc.reason,
                error=f"{type(exc).__name__}: {exc}", side_effects=exc.side_effects,
                wall_elapsed_seconds=time.perf_counter()-started, sim_time=self.time_manager.time())
            raise
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

    def _generate_parameters(self, tool_name, visible, decision_state, decision_ids):
        """Opt-in conditional range choices; preserve the original final Choice.

        Every subdecision uses the same frozen model state and is counted by the
        existing budget/transport. Dependencies are explicit in the question and
        criteria, never materialized as invented tool observations.
        """
        if self.parameter_strategy not in CONDITIONAL_STRATEGIES:
            return self.resolver.generate(tool_name, visible)
        blocked = self.resolver.precondition_failure(tool_name, visible)
        if blocked:
            return blocked
        try:
            contract = range_contract(self.catalog.by_name[tool_name], self.resolver.catalog, self.parameter_strategy)
            contract = bind_harvest_contract(contract, self.trace.events, self.harvest_state_policy)
        except ValueError as exc:
            return {"status": "needs_context", "candidates": {}, "reason": str(exc)}
        if contract is None:
            return self.resolver.generate(tool_name, visible)
        self.trace.emit("parameter_plan", step_id=self.current_step_id, tool_name=tool_name,
                        tool_decision_id=decision_ids[0], contract=contract)
        bound, prefix_ids = {}, []
        for parameter in contract["prefix_parameters"]:
            generation = prefix_candidates(contract, parameter, bound, prefix_ids)
            candidates = generation["candidates"]
            stage = f"parameter_{parameter}"
            self.trace.emit("parameter_stage_candidates", step_id=self.current_step_id,
                tool_name=tool_name, tool_decision_id=decision_ids[0], decision_stage=stage,
                generation=generation, candidate_count=len(candidates),
                candidate_order=list(candidates), candidate_hash=digest(candidates))
            if generation["status"] != "ready":
                self._parameter_failure(tool_name, visible, generation)
                return None
            selected, decision_id = self._decide(stage, decision_state,
                f"For the selected native tool {tool_name}, choose {parameter}. "
                f"Already selected arguments: {canonical(bound)}. "
                f"Tool contract: {canonical(contract)}. "
                "Choose from the submitted values; this selection alone does not execute the tool.", candidates)
            if selected is None:
                return None
            prefix_ids.append(decision_id)
            decision_ids.append(decision_id)
            bound.update(candidates[selected]["arguments"])
            if self._limit():
                return None
        if contract.get("kind") == "single_ridge":
            generated = self.resolver.generate(tool_name, visible,
                selected_arguments=bound, prefix_decision_ids=prefix_ids)
        else:
            generated = self.resolver.generate(tool_name, visible,
                selected_ranges=completion_ranges(contract, bound, prefix_ids))
        generated.update(parameter_strategy=self.parameter_strategy, contract=contract,
                         bound_arguments=bound, prefix_decision_ids=prefix_ids)
        return generated

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
                        task=input, catalog_hash=self.catalog.hash, state_strategy=self.state_strategy,
                        parameter_strategy=self.parameter_strategy,
                        **({"harvest_state_policy": self.harvest_state_policy} if self.harvest_state_policy else {}))
        try:
            for step in range(1, int(self.budgets["max_steps"])+1):
                if self._limit(tool=True):
                    break
                if self.runtime and self.runtime.counts["decision_rounds"] >= self.runtime.limits["max_decision_rounds"]:
                    self.runtime.stop("max_decision_rounds")
                self.current_step_id = f"step_{step:04d}"
                self.poll_environment()
                self._before_round()
                if self.stop_reason:
                    break
                if self.runtime:
                    self.runtime.reserve_round()
                self.step_count = step
                visible = self.state.snapshot(self.time_manager.time())
                decision_state = self._decision_state(visible)
                selected, tool_decision = self._decide("tool", decision_state,
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
                    generated = self._generate_parameters(selected, visible, decision_state, decision_ids)
                    if generated is None:
                        if self.stop_reason:
                            break
                        continue
                candidates = generated.get("candidates", {})
                if generated.get("status") == "ready":
                    if not 1 <= len(candidates) <= 255:
                        generated = {**generated, "status": "candidate_limit_exceeded" if len(candidates)>255 else "needs_context",
                                     "reason": "Parameter Choice requires 1–255 complete candidates"}
                    else:
                        try:
                            for candidate in candidates.values():
                                validate_arguments(entry, candidate["arguments"])
                                if generated.get("contract"):
                                    validate_contract_arguments(generated["contract"], entry, candidate["arguments"])
                                    if any(candidate["arguments"].get(k) != v for k, v in generated["bound_arguments"].items()):
                                        raise ValueError("candidate changed an already selected argument")
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
                    parameter_choice, argument_decision = self._decide("parameters", decision_state,
                        f"For the already selected native tool {selected}, choose one complete argument dictionary "
                        "that best serves the visible task and observations. Each candidate is one invocation; "
                        "source_refs describe deterministic candidate construction, not model reasoning."
                        + (f" Already selected arguments: {canonical(generated['bound_arguments'])}. "
                           f"Tool contract: {canonical(generated['contract'])}." if generated.get("contract") else ""), candidates)
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
        except BudgetStop as exc:
            self.stop_reason = exc.reason
            self.trace.emit("budget_stop", step_id=self.current_step_id, reason=exc.reason,
                side_effects=exc.side_effects,
                budget_consumption=self.runtime.snapshot() if self.runtime else None)
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
