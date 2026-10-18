"""Bounded native-tool control experiment, explicitly not a model decision run."""
from __future__ import annotations

import math
from pathlib import Path

from fairy.scenarios.workflow import Workflow

from .environment import create_fixture, public_task, snapshot_environment
from .evaluation import _target, evaluate_task
from .tools import ToolCatalog, ToolExecutor
from .trace import TraceWriter, write_json


def run_environment_check(config, output_dir):
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    scenario, engine, metadata = create_fixture(config)
    catalog, workflow = ToolCatalog(scenario.apps), Workflow()
    before = snapshot_environment(scenario, engine)
    ridges, hours = _target(public_task(engine))
    trace = TraceWriter(output / "trace.jsonl", output.name)
    executor = ToolExecutor(catalog, engine.time_manager, workflow, trace)
    trace.emit("environment_control_start", backend="direct_native_control", metadata=metadata)
    rows = []
    try:
        for name, args in [
            ("WeatherApp__get_current_weather", {}),
            ("SensorApp__read_soil_sensors", {}),
            ("FarmWorldApp__get_ridge_range_state", {"start": min(ridges), "end": max(ridges)}),
            ("FarmWorldApp__get_inventory", {}),
            ("FieldOpsApp__irrigate", {"start": min(ridges), "end": max(ridges), "hours": hours}),
        ]:
            rows.append(executor.execute(name, args))
            if rows[-1]["status"] != "ok":
                raise RuntimeError(f"Environment control tool failed: {name}: {rows[-1]['error']}")
        immediate = snapshot_environment(scenario, engine)
        effect_ready_at = rows[-1]["result"]["effect_ready_at"]
        seconds = max(1, math.ceil(effect_ready_at - engine.time_manager.time()))
        rows.append(executor.execute("SystemApp__advance_time", {"seconds": seconds}))
        rows.append(executor.execute("FarmWorldApp__get_ridge_range_state", {"start": min(ridges), "end": max(ridges)}))
        rows.append(executor.execute("SensorApp__read_soil_sensors", {}))
        rows.append(executor.execute("FarmWorldApp__get_inventory", {}))
        rows.append(executor.execute("AgentUserInterface__send_message_to_user", {"content": (
            f"环境控制实验：已对 {min(ridges)}–{max(ridges)} 垄每垄灌溉 {hours} 小时；"
            "已按工具返回 effect_ready_at 等待，并复查目标区状态、土壤传感器和配额。"
            "这是一段直接原生工具脚本，不是 Jev 模型决策。"
        )}))
        result = evaluate_task(scenario, engine, workflow, before,
                               environment_valid=metadata["environment_check"] == "passed", output_dir=output)
        result["backend"] = "direct_native_control"
        result["environment_check"] = "passed" if result["task_success"] else "failed"
        result["immediate_post_irrigation_private_evidence"] = immediate
        result["model_request_count"] = 0
        result["tool_call_count"] = executor.call_count
        write_json(output / "evaluation.json", result)
        write_json(output / "manifest.json", {**metadata, "backend": "direct_native_control", "config": config})
        write_json(output / "tool_catalog.json", catalog.to_dict())
        workflow.save_workflow(str(output / "workflow.json"))
        trace.emit("environment_control_end", status=result["environment_check"], tool_call_count=executor.call_count)
        return result
    finally:
        trace.close()


if __name__ == "__main__":
    import argparse
    import json
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    result = run_environment_check({
        "scenario_id": "scenario_l1_three_cultivar_hn58_action_ready_irrigation",
        "seed": 1904, "clock_source": "checkpoint", "scenario_kwargs": {}, "init_kwargs": {},
    }, args.output_dir)
    print(json.dumps({"environment_check": result["environment_check"],
                      "tool_call_count": result["tool_call_count"],
                      "assertions": {k: v["passed"] for k, v in result["assertions"].items()}}, indent=2))
