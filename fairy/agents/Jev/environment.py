"""Runner-only environment assembly. Private physics evidence never enters state."""
from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import asdict
from pathlib import Path

from fairy.apps.farm_world import FarmWorldApp
from fairy.controllers.engine import Engine
from fairy.scenarios.registry import get_scenario_class


def create_fixture(config: dict, agent=None):
    """Return (scenario, engine, metadata), aligning the instance before Engine.

    This loads captured event definitions but never executes their oracle nodes.
    ``checkpoint`` is an explicit runner policy, not a global clock repair.
    """
    scenario_cls = get_scenario_class(config["scenario_id"])
    scenario_kwargs = dict(config.get("scenario_kwargs") or {})
    init_kwargs = dict(config.get("init_kwargs") or {})
    scenario = scenario_cls(**scenario_kwargs)
    scenario.seed = int(config.get("seed", scenario.seed))
    declared_start_time = scenario.start_time
    label = getattr(scenario, "source_checkpoint_label", None)
    checkpoint_path = None
    checkpoint = None
    if label:
        checkpoint_path = Path(inspect.getfile(scenario_cls)).parent / "checkpoints" / f"{label}.json"
        if not checkpoint_path.is_file():
            raise ValueError(f"Declared checkpoint unavailable: {checkpoint_path}")
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    clock_source = config.get("clock_source", "declared")
    if clock_source not in {"checkpoint", "declared"}:
        raise ValueError("clock_source must be checkpoint or declared")
    if clock_source == "checkpoint":
        if checkpoint is None or "sim_time" not in checkpoint:
            raise ValueError("clock_source=checkpoint requires a checkpoint with sim_time")
        scenario.start_time = float(checkpoint["sim_time"])
    if "effective_start_time" in config:
        if abs(float(config["effective_start_time"]) - float(scenario.start_time)) > 1e-6:
            raise ValueError("effective_start_time conflicts with source clock policy")
    scenario.setup(**init_kwargs)
    engine = Engine(agent=agent, scenario=scenario)
    farm = scenario.get_typed_app(FarmWorldApp)
    before = snapshot_environment(scenario, engine)
    shared = all(app.time_manager is engine.time_manager for app in scenario.apps)
    if agent is not None:
        shared = shared and getattr(agent, "time_manager", None) is engine.time_manager
    last_physics = before["last_physics_sim_time"]
    physics_not_ahead = last_physics is None or last_physics <= engine.time_manager.time()
    metadata = {
        "scenario_id": config["scenario_id"], "seed": scenario.seed,
        "scenario_kwargs": scenario_kwargs, "init_kwargs": init_kwargs,
        "clock_source": clock_source, "declared_start_time": declared_start_time,
        "effective_start_time": scenario.start_time,
        "checkpoint_path": str(checkpoint_path) if checkpoint_path else None,
        "checkpoint_hash": hashlib.sha256(checkpoint_path.read_bytes()).hexdigest() if checkpoint_path else None,
        "checkpoint_sim_time": checkpoint.get("sim_time") if checkpoint else None,
        "physics_last_update_before_first_tool": last_physics,
        "shared_time_manager": shared, "physics_not_ahead": physics_not_ahead,
        "environment_check": "passed" if shared and physics_not_ahead and farm.physics_active else "failed",
        "environment_check_scope": "fixture clock/shared manager; physical control experiment recorded separately",
    }
    return scenario, engine, metadata


def snapshot_environment(scenario, engine) -> dict:
    """Non-mutating private post-run evidence; never give this to a decision backend."""
    farm = scenario.get_typed_app(FarmWorldApp)
    physics = farm.physics
    return {
        "sim_time": engine.time_manager.time(), "num_ridges": farm.num_ridges,
        "physics_active": farm.physics_active,
        "last_physics_sim_time": physics.last_physics_sim_time,
        "actions": [asdict(action) for action in physics.action_log],
        "management_regime": farm._management_regime.to_dict(),
        "soil": {str(rid): {"top_vwc": state.top_vwc, "root_vwc": state.root_vwc}
                 for rid, state in physics.soil.states.items()},
        "pending_irrigation_ridges": sorted(
            rid for rid, actions in physics.pending_management_actions_by_ridge.items()
            if any(str(getattr(a.action_type, "value", a.action_type)).lower() == "irrigation" for a in actions)
        ),
    }


def public_task(engine) -> str:
    """Use FAIRY's briefing resolver; it does not include oracle action nodes."""
    return engine._resolve_agent_task()
