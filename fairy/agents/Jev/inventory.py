"""Read-only source and runtime tool audit, without executing scenario oracles.

Scenario ``setup`` captures its event graph, but none of those events is executed.
Failed initialization remains a failure even when a declared app list is inspectable.
"""

from __future__ import annotations

import ast
from collections import Counter
from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path
import time
import traceback
from typing import Any

from fairy.agents.agent.toolset_builder import build_toolset
from fairy.controllers.agent_builder import FARM_AGENT_HIDDEN_TOOL_NAMES
from fairy.scenarios.registry import get_scenario_class, list_scenarios

TARGET_SCENARIO = "scenario_l1_three_cultivar_hn58_action_ready_irrigation"


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def source_implementation_inventory(repository: str | Path | None = None) -> dict[str, Any]:
    """Count decorated function definitions via AST, including qualified source sites."""
    root = Path(repository) if repository is not None else Path(__file__).resolve().parents[3]
    rows: list[dict[str, Any]] = []
    for path in sorted((root / "fairy").rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "fairy.tool_utils":
                for alias in node.names:
                    aliases[alias.asname or alias.name] = alias.name

        def walk(node: ast.AST, prefix: str = "") -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    qualified = f"{prefix}.{child.name}" if prefix else child.name
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        decorators = []
                        for decorator in child.decorator_list:
                            target = decorator.func if isinstance(decorator, ast.Call) else decorator
                            if isinstance(target, ast.Name):
                                name = aliases.get(target.id, target.id)
                            elif isinstance(target, ast.Attribute):
                                name = target.attr
                            else:
                                continue
                            if name in {"app_tool", "agent_tool"}:
                                decorators.append(name)
                        if decorators:
                            rel = str(path.relative_to(root))
                            bucket = "apps" if rel.startswith("fairy/apps/") else (
                                "scenarios" if rel.startswith("fairy/scenarios/") else "controllers_a2a"
                            )
                            rows.append({"path": rel, "line": child.lineno,
                                         "qualified_name": qualified, "decorators": decorators,
                                         "category": bucket})
                    walk(child, qualified)
                else:
                    walk(child, prefix)

        walk(tree)
    return {"method": "AST decorated function definitions (app_tool/agent_tool), not decorator text occurrences",
            "count": len(rows), "by_category": dict(Counter(row["category"] for row in rows)),
            "definitions": rows}


def _catalog_counts(apps: list[Any], hidden: bool) -> dict[str, Any]:
    tools, schemas, tools_map = build_toolset(
        apps, excluded_tool_names=FARM_AGENT_HIDDEN_TOOL_NAMES if hidden else set()
    )
    names = [schema["function"]["name"] for schema in schemas]
    counts = Counter(names)
    return {"tool_count": len(tools), "schema_count": len(schemas),
            "unique_name_count": len(counts), "tools_map_count": len(tools_map),
            "duplicate_names": sorted(name for name, count in counts.items() if count > 1),
            "counts_consistent": len(tools) == len(schemas) == len(counts) == len(tools_map),
            "names": names}


def target_catalog(scenario_id: str = TARGET_SCENARIO) -> dict[str, Any]:
    """Return the full registered catalog, including explicitly hidden tool entries."""
    scenario = get_scenario_class(scenario_id)()
    scenario.setup()
    apps = list(scenario.apps or [])
    tools, schemas, _ = build_toolset(apps)
    entries = []
    categories: Counter[str] = Counter()
    for tool, schema in zip(tools, schemas):
        name = schema["function"]["name"]
        parameters = [{"name": arg.name, "type": arg.arg_type, "required": not arg.has_default,
                       "has_default": arg.has_default, "default": arg.default}
                      for arg in tool.args]
        visible = name not in FARM_AGENT_HIDDEN_TOOL_NAMES
        category = "no_parameters" if not parameters else (
            "required_parameters" if any(p["required"] for p in parameters) else "optional_only"
        )
        if visible:
            categories[category] += 1
        entries.append({"name": name, "description": schema["function"]["description"],
                        "parameters": parameters, "schema": schema,
                        "signature": str(inspect.signature(tool.function)),
                        "visible": visible, "parameter_category": category,
                        "write_operation": tool.write_operation,
                        "app_class": tool.class_name, "app_instance": tool.app_name})
    return {"scenario_id": scenario_id, "hidden_tool_names": sorted(FARM_AGENT_HIDDEN_TOOL_NAMES),
            "registered": _catalog_counts(apps, False), "agent_visible": _catalog_counts(apps, True),
            "visible_parameter_categories": dict(categories),
            "choice_count_with_finish": sum(categories.values()) + 1, "tools": entries}


def _missing_workbook_declaration(scenario: Any) -> dict[str, Any] | None:
    """Audit a known static registration list, without pretending the fixture initialized."""
    from fairy.scenarios.farm_world_fullseason.tangyan5_table_fitted.scenario_tangyan5_table_fitted import (
        ScenarioTangyan5TableFittedBase,
    )
    if not isinstance(scenario, ScenarioTangyan5TableFittedBase):
        return None
    from fairy.apps.agent_user_interface import AgentUserInterface
    from fairy.apps.farm_world import FarmWorldApp, SensorApp, TractorApp, WeatherApp
    from fairy.apps.system import SystemApp

    source = inspect.getsource(ScenarioTangyan5TableFittedBase.init_and_populate_apps)
    expected = "self.apps = [aui, farm_world, weather, sensor, tractor, system]"
    if expected not in source:
        return {"status": "unresolved", "reason": "The audited static declaration changed"}
    farm, weather = FarmWorldApp(), WeatherApp()
    apps = [AgentUserInterface(), farm, weather, SensorApp(farm_world_app=farm),
            TractorApp(farm_world_app=farm, weather_app=weather), SystemApp()]
    return {"status": "inferred_from_static_app_declaration_only",
            "initialization_success": False,
            "source_path": inspect.getsourcefile(ScenarioTangyan5TableFittedBase),
            "source_line": inspect.getsourcelines(ScenarioTangyan5TableFittedBase.init_and_populate_apps)[1],
            "source_declaration": expected, "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "app_classes": [app.__class__.__name__ for app in apps],
            "agent_visible": _catalog_counts(apps, True)}


def audit_all_scenarios(output_path: str | Path | None = None) -> dict[str, Any]:
    """Initialize every registered scenario and audit actual ordinary-mode menus.

    ``output_path`` is an output directory. Per-scenario results are flushed as
    JSONL, so an interrupted audit still retains completed evidence.
    """
    started = time.monotonic()
    output = Path(output_path) if output_path is not None else None
    progress = None
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
        progress = (output / "scenario_audit.jsonl").open("w")
    records = []
    all_names: set[str] = set()
    visible_names: set[str] = set()
    a2a_wrapper_names: set[str] = set()
    scenario_ids = list_scenarios()
    try:
        for scenario_id in scenario_ids:
            item_started = time.monotonic()
            scenario = None
            try:
                scenario_class = get_scenario_class(scenario_id)
                scenario = scenario_class()
                scenario.setup()
                apps = list(scenario.apps or [])
                raw, visible = _catalog_counts(apps, False), _catalog_counts(apps, True)
                all_names.update(raw["names"])
                visible_names.update(visible["names"])
                from fairy.apps.agent_user_interface import AgentUserInterface
                from fairy.apps.system import SystemApp
                for app in apps:
                    if (getattr(app, "name", "") not in {"InternalContacts", "AgentUserInterface", "SystemApp"}
                            and not isinstance(app, (AgentUserInterface, SystemApp))):
                        a2a_wrapper_names.add(f"{app.name}__expert_agent")
                record = {"scenario_id": scenario_id, "status": "initialized",
                          "scenario_class": f"{scenario_class.__module__}.{scenario_class.__name__}",
                          "registered": raw, "agent_visible": visible,
                          "menu_with_finish_fits_255": visible["unique_name_count"] + 1 <= 255,
                          "app_instances": [{"name": app.name, "class": app.__class__.__name__} for app in apps]}
            except Exception as exc:
                record = {"scenario_id": scenario_id, "status": "initialization_failed",
                          "exception_type": type(exc).__name__, "error": str(exc),
                          "traceback": traceback.format_exc()}
                if isinstance(exc, FileNotFoundError):
                    record["missing_external_data"] = str(getattr(exc, "filename", None) or exc)
                if scenario is not None:
                    declaration = _missing_workbook_declaration(scenario)
                    if declaration is not None:
                        record["declared_catalog_without_initialization"] = declaration
            record["elapsed_seconds"] = time.monotonic() - item_started
            records.append(record)
            if progress is not None:
                progress.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                progress.flush()
    finally:
        if progress is not None:
            progress.close()
    successes = [row for row in records if row["status"] == "initialized"]
    failures = [row for row in records if row["status"] == "initialization_failed"]
    source = source_implementation_inventory()
    target = target_catalog()
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_implementations": source,
        "registered_scenarios": len(scenario_ids), "initialized_scenarios": len(successes),
        "failed_initializations": len(failures),
        "failed_scenario_ids": [row["scenario_id"] for row in failures],
        "ordinary_mode_names": sorted(all_names), "ordinary_mode_name_count": len(all_names),
        "agent_visible_names": sorted(visible_names), "agent_visible_name_count": len(visible_names),
        "max_visible_tools_per_initialized_scenario": max((row["agent_visible"]["unique_name_count"] for row in successes), default=0),
        "all_initialized_counts_consistent": all(row["registered"]["counts_consistent"] and row["agent_visible"]["counts_consistent"] for row in successes),
        "all_initialized_menus_fit_255_with_finish": all(row["menu_with_finish_fits_255"] for row in successes),
        "a2a_derived_upper_bound": {
            "status": "derived_not_all_configurations_executed",
            "method": "Observed ordinary visible names plus every eligible app-instance expert_agent entry and FinalAnswerTool__final_answer",
            "eligible_wrapper_names": sorted(a2a_wrapper_names),
            "visible_name_count_upper_bound": len(visible_names | a2a_wrapper_names | {"FinalAnswerTool__final_answer"}),
        },
        "target": {key: value for key, value in target.items() if key != "tools"},
        "no_oracle_execution": True, "elapsed_seconds": time.monotonic() - started,
        "scenario_results": records,
    }
    if output is not None:
        _write(output / "tool_catalog.json", target)
        _write(output / "source_implementations.json", source)
        _write(output / "tool_audit.json", summary)
    return summary
