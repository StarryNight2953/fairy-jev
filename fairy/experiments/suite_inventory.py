"""Discover the registered three-level suite without setting up any scenario.

The current suite comprises every registered L1/L2 split and the formal L3
manifest.  Historical physics and full-season scenarios are opt-in and remain
identified as legacy.  Discovery only imports definitions and reads source and
checkpoint files; it never calls setup, a model, an oracle, or a tool.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import inspect
import json
from pathlib import Path
import re

from fairy.scenarios.registry import get_scenario_class, list_scenarios
from fairy.scenarios.scenario import Scenario


ROOT = Path(__file__).resolve().parents[2]
FORMAL_L3_MANIFEST = ROOT / "scripts/fullseason/formal_90_scenario_ids.txt"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validator_identity(scenario_class) -> dict:
    validator = scenario_class.validate
    path = Path(inspect.getfile(validator)).resolve()
    return {
        "module": validator.__module__,
        "qualname": validator.__qualname__,
        "source_path": str(path),
        "source_sha256": _sha256(path),
        "is_default_no_result_validator": validator is Scenario.validate,
    }


def discover_scenarios(include_legacy: bool = False) -> list[dict]:
    """Return JSON-serializable, uniquely identified scenario rows.

    Default scope: 78 L1 + 78 L2 + 90 formal L3 in the current repository.
    ``include_legacy`` additionally includes the eight physics action-tick L1,
    eight physics L2, and twelve non-formal full-season scenarios.  Unlevelled
    baseline and Tangyan experiments are never silently assigned a level.
    Counts are observations tested separately, not discovery filters.
    """
    registered = set(list_scenarios())
    formal_ids = [
        line.strip() for line in FORMAL_L3_MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(formal_ids) != len(set(formal_ids)):
        raise ValueError("Formal L3 manifest contains duplicate scenario IDs")
    missing = sorted(set(formal_ids) - registered)
    if missing:
        raise ValueError(f"Formal L3 scenarios are not registered: {missing}")
    selection = {}
    for scenario_id in registered:
        if scenario_id.startswith("scenario_l1_"):
            selection[scenario_id] = ("L1", "registered_split", False)
        elif scenario_id.startswith("scenario_l2_"):
            selection[scenario_id] = ("L2", "registered_split", False)
        elif scenario_id in formal_ids:
            selection[scenario_id] = ("L3", "formal_l3_manifest", False)
        elif include_legacy:
            if "physics_action_tick" in scenario_id:
                selection[scenario_id] = ("L1", "legacy_physics_action_tick", True)
            elif scenario_id.startswith("scenario_physics_"):
                selection[scenario_id] = ("L2", "legacy_physics", True)
            elif scenario_id.startswith("scenario_full_season_"):
                selection[scenario_id] = ("L3", "legacy_full_season", True)

    rows = []
    for scenario_id, (level, membership, legacy) in sorted(selection.items(), key=lambda pair: (pair[1][0], pair[0])):
        scenario_class = get_scenario_class(scenario_id)
        source = Path(inspect.getfile(scenario_class)).resolve()
        label = getattr(scenario_class, "source_checkpoint_label", None)
        checkpoint_path = source.parent / "checkpoints" / f"{label}.json" if label else None
        checkpoint_exists = checkpoint_path.is_file() if checkpoint_path else None
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8")) if checkpoint_exists else None
        checkpoint_time = checkpoint.get("sim_time") if checkpoint is not None else None
        declared_time = getattr(scenario_class, "start_time", None)
        source_attributes = {
            name: getattr(scenario_class, name, None)
            for name in (
                "source_l3_scenario_id", "source_checkpoint_label", "source_checkpoint_date",
                "source_dap", "source_growth_stage", "seed", "duration", "queue_based_loop",
                "time_increment_in_seconds", "detailed_briefing", "expects_agent_harvest",
            )
        }
        row = {
            "scenario_id": scenario_id,
            "level": level,
            "membership": membership,
            "legacy": legacy,
            "class_module": scenario_class.__module__,
            "class_qualname": scenario_class.__qualname__,
            "class_source_path": str(source),
            "class_source_sha256": _sha256(source),
            "checkpoint_label": label,
            "checkpoint_path": str(checkpoint_path) if checkpoint_path else None,
            "checkpoint_exists": checkpoint_exists,
            "checkpoint_sha256": _sha256(checkpoint_path) if checkpoint_exists else None,
            "clock_source": "checkpoint" if label else "declared",
            "declared_start_time": declared_time,
            "checkpoint_sim_time": checkpoint_time,
            "effective_start_time": checkpoint_time if label else declared_time,
            "declared_minus_checkpoint_seconds": (
                float(declared_time) - float(checkpoint_time)
                if declared_time is not None and checkpoint_time is not None else None
            ),
            "source_attributes": source_attributes,
            "native_validator": _validator_identity(scenario_class),
            "selection_manifest": str(FORMAL_L3_MANIFEST) if membership == "formal_l3_manifest" else None,
            "selection_manifest_sha256": _sha256(FORMAL_L3_MANIFEST) if membership == "formal_l3_manifest" else None,
        }
        # Fail on any newly introduced non-serializable metadata at discovery,
        # before a costly live batch has begun.
        json.dumps(row, ensure_ascii=False, allow_nan=False)
        rows.append(row)
    return rows


def config_for_scenario(row: dict, baseconfig: dict) -> dict:
    """Copy shared configuration and apply only scenario identity and clock."""
    config = deepcopy(baseconfig)
    config.update(scenario_id=row["scenario_id"], clock_source=row["clock_source"])
    # A time assertion copied from an HN58-specific config is not a suite policy.
    config.pop("effective_start_time", None)
    if row["effective_start_time"] is not None:
        config["effective_start_time"] = row["effective_start_time"]
    return config


def validate_public_task(task: str) -> None:
    """Reject missing briefings and bare oracle IDs, not judge task semantics.

    A swapped ``briefing_text``/``prefix`` argument can otherwise produce a
    healthy fixture whose entire user task is an internal ``o_*`` event ID.
    This deliberately narrow input check does not prescribe task wording,
    actions, or success criteria.
    """
    if not isinstance(task, str) or not task.strip():
        raise ValueError("Invalid public task: briefing is empty")
    if re.fullmatch(r"o_[A-Za-z0-9_]+", task.strip()):
        raise ValueError("Invalid public task: briefing contains only an oracle event identifier")


def preflight_one(row: dict, baseconfig: dict) -> dict:
    """Initialize one isolated fixture and inspect its public interface.

    Call in a fresh subprocess for each scenario: setup captures oracle event
    definitions but does not execute them.  This function does not construct an
    Agent, model backend, ToolExecutor, or run/replay an Engine.  Exceptions are
    left to the batch caller to archive with the affected scenario identity.
    """
    from fairy.agents.Jev.environment import create_fixture, public_task
    from fairy.agents.Jev.tools import ToolCatalog

    for path_key, hash_key in (
        ("class_source_path", "class_source_sha256"),
        ("checkpoint_path", "checkpoint_sha256"),
    ):
        if row.get(path_key):
            path = Path(row[path_key])
            if not path.is_file() or _sha256(path) != row.get(hash_key):
                raise ValueError(f"Scenario input changed since discovery: {path}")
    config = config_for_scenario(row, baseconfig)
    scenario, engine, metadata = create_fixture(config)
    catalog = ToolCatalog(scenario.apps)
    task = public_task(engine)
    validate_public_task(task)
    result = {
        "scenario_id": row["scenario_id"],
        "level": row["level"],
        "status": "passed" if metadata["environment_check"] == "passed" else "failed",
        "fixture_metadata": metadata,
        "catalog_sha256": catalog.hash,
        "visible_tool_count": len(catalog.entries),
        "registered_tool_count": len(catalog.all_entries),
        "choice_count_including_finish": len(catalog.options),
        "public_task": task,
        "public_task_sha256": hashlib.sha256(task.encode("utf-8")).hexdigest(),
        "native_validator": _validator_identity(type(scenario)),
        "executed_tools": 0,
        "executed_oracle_events": 0,
        "model_requests": 0,
        "scope": "fixture initialization and public-interface inspection only",
    }
    json.dumps(result, ensure_ascii=False, allow_nan=False)
    return result
