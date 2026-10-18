"""Public tool catalog, strict argument binding, and single native executions."""
from __future__ import annotations

import inspect
import math
import time
from datetime import datetime, timezone

from fairy.agents.agent.toolset_builder import build_toolset
from fairy.controllers.agent_builder import FARM_AGENT_HIDDEN_TOOL_NAMES
from fairy.scenarios.workflow import WorkflowStep

from .trace import digest, json_safe


class ToolCatalog:
    def __init__(self, apps):
        tools, schemas, mapping = build_toolset(list(apps))
        names = [s["function"]["name"] for s in schemas]
        if len(names) != len(set(names)) or len(names) != len(mapping) or len(tools) != len(names):
            raise ValueError("tool_schema/name/tools_map cardinality mismatch")
        self.all_entries = []
        for tool, schema in zip(tools, schemas):
            name = schema["function"]["name"]
            parameters = [{"name": arg.name, "type": arg.arg_type,
                           "required": not arg.has_default, **({"default": arg.default} if arg.has_default else {})}
                          for arg in tool.args]
            self.all_entries.append({"name": name, "description": tool.function_description or "",
                "parameters": parameters, "schema": schema["function"]["parameters"],
                "signature": str(inspect.signature(tool.function)),
                "visible": name not in FARM_AGENT_HIDDEN_TOOL_NAMES,
                "visibility_rule": "FARM_AGENT_HIDDEN_TOOL_NAMES", "write_operation": bool(tool.write_operation)})
        self.entries = [e for e in self.all_entries if e["visible"]]
        self.by_name = {e["name"]: e for e in self.entries}
        self.tools, self.schemas, self.tools_map = build_toolset(
            list(apps), excluded_tool_names=FARM_AGENT_HIDDEN_TOOL_NAMES)
        if len(self.entries) + 1 > 255:
            raise ValueError("candidate_limit_exceeded: full tool menu plus FINISH exceeds 255")
        if len(self.entries) != len(self.schemas) or len(self.entries) != len(self.tools_map):
            raise ValueError("visible tool catalog cardinality mismatch")
        self.hash = digest(self.entries)
        self.options = {e["name"]: {"purpose": e["description"], "parameters": e["schema"],
                                    "signature_parameters": e["parameters"]} for e in self.entries}
        self.options["FINISH"] = "Stop the run. Choose only when the task is complete or no further useful action is possible. Task success is evaluated independently."

    def to_dict(self):
        return {"catalog_hash": self.hash, "registered_count": len(self.all_entries),
                "visible_count": len(self.entries), "choice_count": len(self.options),
                "tools": self.all_entries, "candidate_order": list(self.options)}


def validate_arguments(entry: dict, arguments: dict) -> dict:
    """Validate actual signature types, rejecting bool-as-number and nonfinite values."""
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    parameters = {p["name"]: p for p in entry["parameters"]}
    unknown = set(arguments) - set(parameters)
    if unknown:
        raise ValueError(f"unknown arguments: {sorted(unknown)}")
    bound = {}
    for name, parameter in parameters.items():
        if name not in arguments:
            if parameter["required"]:
                raise ValueError(f"missing required argument: {name}")
            value = parameter["default"]
        else:
            value = arguments[name]
        kind = parameter["type"]
        valid = {"int": lambda: type(value) is int, "integer": lambda: type(value) is int,
                 "float": lambda: type(value) in (int, float), "number": lambda: type(value) in (int, float),
                 "str": lambda: type(value) is str, "string": lambda: type(value) is str,
                 "bool": lambda: type(value) is bool, "boolean": lambda: type(value) is bool}
        if kind not in valid or not valid[kind]():
            raise ValueError(f"{name} requires {kind}, got {type(value).__name__}")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
        bound[name] = value
    return bound


def result_is_error(result) -> bool:
    return isinstance(result, dict) and (
        bool(result.get("error")) or result.get("success") is False
        or str(result.get("status", "")).lower() in {"error", "failed", "failure", "rejected"})


class ToolExecutor:
    def __init__(self, catalog, time_manager, workflow, trace):
        self.catalog, self.time_manager, self.workflow, self.trace = catalog, time_manager, workflow, trace
        self.call_count = 0
        self.attempt_count = 0

    def execute(self, tool_name, arguments, *, tool_call_id=None, step_id=None, decision_ids=None):
        self.attempt_count += 1
        tool_call_id = tool_call_id or f"tool_{self.attempt_count:04d}"
        before, wall_start = self.time_manager.time(), time.perf_counter()
        context = {"step_id": step_id, "tool_call_id": tool_call_id,
                   "decision_ids": decision_ids or [], "tool_name": tool_name, "arguments": arguments}
        self.trace.emit("tool_start", **context, sim_time_before=before)
        bound, executed, error_stage, error = arguments, False, None, None
        try:
            if tool_name not in self.catalog.tools_map:
                raise ValueError(f"unknown/hidden tool: {tool_name}")
            bound = validate_arguments(self.catalog.by_name[tool_name], arguments)
        except Exception as exc:
            error_stage, error = "tool_args_parse", f"{type(exc).__name__}: {exc}"
            result = {"error": error}
        else:
            try:
                executed = True
                self.call_count += 1
                result = self.catalog.tools_map[tool_name](**bound)
                if result_is_error(result):
                    error_stage, error = "tool_return", str(result)
            except Exception as exc:
                error_stage, error = "tool_call", f"{type(exc).__name__}: {exc}"
                result = {"error": error}
        after = self.time_manager.time()
        row = {**context, "arguments": bound, "result": result, "status": "error" if error else "ok",
               "write_operation": bool(self.catalog.by_name.get(tool_name, {}).get("write_operation")),
               "executed": executed, "error": error, "error_stage": error_stage,
               "sim_time_before": before, "sim_time_after": after,
               "wall_elapsed_seconds": time.perf_counter() - wall_start}
        self.workflow.add_node(WorkflowStep(name=tool_call_id, content=result,
            op_type="TOOL_ERROR" if error else ("WRITE" if self.catalog.by_name[tool_name]["write_operation"] else "READ"),
            tool_name=tool_name, tool_args=json_safe(bound), time_before=before, time_after=after,
            time=datetime.fromtimestamp(after, timezone.utc).isoformat(), error_stage=error_stage))
        self.trace.emit("tool_end", **row)
        return row
