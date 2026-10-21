"""Thin instrumentation of FAIRY's existing function-calling controller.

The original Agent.run, context, tool schemas, provider and multi-tool order
remain in charge. Only actual execution boundaries reserve comparison budget.
"""
from __future__ import annotations

import inspect
import json
import time
from functools import wraps

from fairy.agents.Jev.tools import ToolCatalog, result_is_error
from fairy.controllers.agent_builder import build_controller_agent
from fairy.controllers.agent_config import get_farm_world_system_prompt


def attach_native_observers(agent, runtime, catalog):
    """Attach to one agent instance; normal FAIRY callers are unaffected."""
    active = {}
    original_call = agent.call_function
    original_check = agent._check_limits
    original_sink = agent.runtime_event_sink

    def sink(event):
        runtime.trace.emit("native_event", native_event=event,
                           sim_time_numeric=agent.time_manager.time() if agent.time_manager else None)
        if original_sink:
            original_sink(event)

    agent.runtime_event_sink = sink

    def check_limits(**kwargs):
        runtime.check()
        return original_check(**kwargs)

    agent._check_limits = check_limits
    for name, function in list(agent.tools_map.items()):
        def make_wrapper(name, function):
            @wraps(function)
            def execute(*args, **kwargs):
                # Python binding errors occur before the native function body.
                # Preserve those errors, but do not count them as executions.
                target = getattr(function, "function", function)
                positional = ((function.class_instance,) + args
                              if hasattr(function, "class_instance") else args)
                try:
                    inspect.signature(target).bind(*positional, **kwargs)
                except TypeError:
                    # Reproduce the native callable's own TypeError text and
                    # error observation. FAIRY AppTool/type_check/event wrappers
                    # only forward to this signature; no native body is entered.
                    return function(*args, **kwargs)
                runtime.check()
                active["tool_attempt"] = runtime.reserve_tool()
                active["executed"] = True
                previous = runtime.active_boundary
                runtime.active_boundary = "tool"
                try:
                    return function(*args, **kwargs)
                finally:
                    runtime.active_boundary = previous
            return execute
        agent.tools_map[name] = make_wrapper(name, function)

    @wraps(original_call)
    def call_function(tool_call):
        runtime.check()
        active.clear()
        active["executed"] = False
        before = agent.time_manager.time()
        started = time.perf_counter()
        workflow_keys = set(agent.workflow.dag)
        tool_name = tool_call.function.name
        raw = tool_call.function.arguments
        try:
            arguments = json.loads(raw or "{}")
        except (ValueError, TypeError):
            arguments = {}
        context = {"tool_call_id": tool_call.id, "tool_name": tool_name,
                   "arguments": arguments, "decision_ids": [getattr(agent.llm, "last_request_id", None)],
                   "step_id": f"native_round_{agent.llm_turn_count:04d}"}
        runtime.trace.emit("tool_start", **context, sim_time_before=before,
                           raw_arguments=raw, source="native_function_call")
        result, error = None, None
        try:
            result = original_call(tool_call)
            return result
        except BaseException as exc:
            error = exc
            raise
        finally:
            from .budget import BudgetStop
            budget_stop = isinstance(error, BudgetStop)
            added = [step for name, step in agent.workflow.dag.items()
                     if name not in workflow_keys and step.tool_name == tool_name]
            workflow_step = added[-1] if added else None
            # Native error observations are the exact bytes delivered back to
            # the model; retain them instead of inventing a second error text.
            if error is not None and workflow_step is not None:
                result = workflow_step.content
            is_error = error is not None or result_is_error(result)
            executed = active["executed"]
            stage = None
            if is_error:
                stage = "tool_call" if executed else "tool_args_parse"
                if error is None:
                    stage = "tool_return"
            original_native_error_stage = workflow_step.error_stage if workflow_step is not None else None
            if workflow_step is not None and not executed and error is not None and not budget_stop:
                # The native Agent labels map lookup/binding errors tool_call.
                # Our actual boundary proves no native body entered; normalize
                # replay metadata only. Preserve original events/messages below.
                workflow_step.error_stage = "tool_args_parse"
            row = runtime.trace.emit("tool_end", **context, result=result,
                status="error" if is_error else "ok", executed=executed,
                tool_attempt=active.get("tool_attempt"),
                error=f"{type(error).__name__}: {error}" if error else (str(result) if is_error else None),
                error_stage=stage, sim_time_before=before, sim_time_after=agent.time_manager.time(),
                wall_elapsed_seconds=time.perf_counter()-started,
                write_operation=bool(catalog.by_name.get(tool_name, {}).get("write_operation")),
                workflow_step_name=workflow_step.name if workflow_step else None,
                original_native_error_stage=original_native_error_stage,
                workflow_error_stage=workflow_step.error_stage if workflow_step else None,
                error_stage_normalization=("observer_proved_no_native_function_entry"
                    if workflow_step is not None and original_native_error_stage != workflow_step.error_stage else None),
                interrupted=budget_stop,
                side_effects=getattr(error, "side_effects", None),
                source="native_function_call")
            if not executed and not budget_stop:
                runtime.trace.emit("pre_execution_error", tool_call_id=tool_call.id,
                    tool_name=tool_name, tool_end_event_id=row["event_id"], error=row["error"],
                    reason=stage, executed=False)
            if workflow_step is not None:
                runtime.trace.emit("observation_added", tool_call_id=tool_call.id,
                    workflow_step_name=workflow_step.name, tool_end_event_id=row["event_id"],
                    source="native_messages_tool_response")

    agent.call_function = call_function
    return agent


def run_native(scenario, engine, config, runtime, *, llm=None):
    """Legacy offline fixture harness; live baselines use fairy.cli directly."""
    from .budget import BudgetStop

    if llm is None:
        raise ValueError("Live native execution must use fairy.cli; this harness requires an offline client")
    parallel = bool(getattr(llm, "parallel_tool_calls", True))
    agent = build_controller_agent(config.get("controller", "farm_baseline_react"), llm=llm,
        toolsets=scenario.apps,
        system_message=get_farm_world_system_prompt(config.get("system_prompt_mode", "fairy"),
                                                    parallel_tool_calls=parallel))
    catalog = ToolCatalog(scenario.apps)
    attach_native_observers(agent, runtime, catalog)
    engine.agent = agent
    agent.set_time_manager(engine.time_manager)
    runtime.trace.emit("native_adapter", controller=agent.name,
        native_retry_max_retries=agent.max_llm_call_retries,
        native_retry_sleep_seconds=agent.llm_retry_sleep_seconds,
        native_rate_limit_sleep_seconds=agent.rate_limit_retry_sleep_seconds,
        auxiliary_llm_calls="none in this native controller implementation",
        automatic_observation="tool responses and Engine notifications only",
        multi_tool_semantics="existing serial order within each response",
        tool_catalog_hash=catalog.hash, full_tool_names=list(catalog.by_name))
    try:
        # Agent's historical count includes JSON errors. Actual native-body
        # attempts are bounded by reserve_tool, so do not use that old counter.
        engine.run_scenario_agent(max_tool_calls=None, timeout_seconds=None, setup_scenario=False)
    except BudgetStop as exc:
        agent.stop_reason = getattr(exc, "reason", str(exc))
    except Exception as exc:
        agent.stop_reason = "native_error"
        runtime.trace.emit("run_error", error=f"{type(exc).__name__}: {exc}", failure_layer="native_agent")
    finally:
        runtime.trace.emit("native_run_end", stop_reason=agent.stop_reason,
            native_tool_call_count=agent.tool_call_count, native_llm_turn_count=agent.llm_turn_count)
    return agent, agent.workflow, agent.stop_reason
