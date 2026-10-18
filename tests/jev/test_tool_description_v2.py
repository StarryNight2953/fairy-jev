"""Offline regression for the single get_inventory description change.

Build the real HN58 tool interfaces and capture provider-bound objects without
constructing SDK clients or issuing requests. No model action is scripted here.
"""
from __future__ import annotations

from copy import deepcopy
import inspect
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

from fairy.agents.agent.toolset_builder import build_toolset
from fairy.agents.Jev.client import OfficialBackend
from fairy.agents.Jev.environment import create_fixture, public_task, snapshot_environment
from fairy.agents.Jev.hybrid.context import PacketContext, request_size
from fairy.agents.Jev.hybrid.updater import LLMUpdater
from fairy.agents.Jev.tools import ToolCatalog
from fairy.agents.Jev.trace import TraceWriter
from fairy.apps.farm_world import FarmWorldApp
from fairy.controllers.agent_builder import FARM_AGENT_HIDDEN_TOOL_NAMES, build_controller_agent


INVENTORY = "FarmWorldApp__get_inventory"
IRRIGATION_LOG = "FieldOpsApp__get_irrigation_log"
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def no_network():
    def reject(*args, **kwargs):
        raise AssertionError("This description regression must not use the network")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket, "create_connection", reject)
        patch.setattr(socket.socket, "connect", reject)
        patch.setattr(socket.socket, "connect_ex", reject)
        yield


@pytest.fixture(scope="module")
def hn58(no_network):
    config = json.loads((ROOT / "configs/jev/hn58_irrigation.json").read_text())
    scenario, engine, metadata = create_fixture(config)
    assert metadata["environment_check"] == "passed"
    assert metadata["seed"] == 1904
    return scenario, engine, ToolCatalog(scenario.apps)


def test_user_description_reaches_real_catalog_and_builder(hn58):
    scenario, _, catalog = hn58
    description = inspect.getdoc(FarmWorldApp.get_inventory)
    assert "management resource limits and usage" in description
    assert "including irrigation quota" in description
    assert "post-harvest market information" in description
    assert catalog.by_name[INVENTORY]["description"] == description
    assert catalog.options[INVENTORY]["purpose"] == description
    assert catalog.by_name[IRRIGATION_LOG]["description"] == (
        "Return the irrigation history for all ridges."
    )
    _, schemas, mapping = build_toolset(
        list(scenario.apps), excluded_tool_names=FARM_AGENT_HIDDEN_TOOL_NAMES
    )
    assert schemas == catalog.schemas
    assert list(mapping) == list(catalog.tools_map)
    assert len(schemas) == 58
    assert len(catalog.options) == 59
    assert list(catalog.options) == list(mapping) + ["FINISH"]


def test_native_controller_exposes_the_same_description(hn58):
    scenario, _, catalog = hn58
    # The actual native controller builder needs only provider metadata here.
    # It is never run and no LLM/provider client is constructed.
    agent = build_controller_agent(
        "farm_baseline_react", llm=SimpleNamespace(provider="offline"),
        toolsets=list(scenario.apps),
    )
    assert agent.tool_schemas == catalog.schemas
    functions = {row["function"]["name"]: row["function"] for row in agent.tool_schemas}
    assert functions[INVENTORY]["description"] == catalog.options[INVENTORY]["purpose"]
    assert functions[INVENTORY]["parameters"] == catalog.options[INVENTORY]["parameters"]


class RequestCaptured(BaseException):
    """Stop at the offline provider boundary, before any response or action."""


def test_jev_backend_serializes_actual_choice_with_new_description(hn58):
    from typesafe_sdk._core.json import serialize

    _, engine, catalog = hn58
    captured = []

    def capture(*, state, questions):
        captured.append(serialize({"state": state, "model": "jev-1.13.0", "questions": questions}))
        raise RequestCaptured

    # Skip __init__: it reads credentials and constructs a live SDK client.
    # decide() still constructs the real public SDK Choice on its normal path.
    backend = object.__new__(OfficialBackend)
    backend.requested_model = "jev-1.13.0"
    backend.sdk_version = "offline-serialization"
    backend.client = SimpleNamespace(system_one=capture)
    state = {"task": public_task(engine), "fresh_events": []}
    with pytest.raises(RequestCaptured):
        backend.decide(state, "tool", "Offline description propagation check", catalog.options)
    assert len(captured) == 1
    body = json.loads(captured[0])
    assert body["questions"]["tool"]["criteria"] == catalog.options
    assert body["questions"]["tool"]["criteria"][INVENTORY]["purpose"] == (
        catalog.by_name[INVENTORY]["description"]
    )
    measured = request_size(
        state, "tool", "Offline description propagation check", catalog.options
    )
    assert measured["within_budget"]
    assert measured["complete_request"]["utf8_bytes"] == len(captured[0])


def test_hybrid_updater_receives_the_same_full_tool_capabilities(hn58, tmp_path):
    _, engine, catalog = hn58
    captured = []

    def capture(messages, tools=None):
        captured.append((deepcopy(messages), tools))
        raise RequestCaptured

    with TraceWriter(tmp_path / "trace.jsonl", "offline-description-v2") as trace:
        context = PacketContext(trace)
        update_input = context.build_update_input(public_task(engine), [], catalog.options, 0)
        assert update_input["tool_capabilities"] == catalog.options
        updater = LLMUpdater(SimpleNamespace(chat_completion=capture, max_output_tokens=2048))
        with pytest.raises(RequestCaptured):
            updater.update(update_input)
    assert len(captured) == 1
    messages, tools = captured[0]
    assert tools is None  # Updater carries capabilities in its user JSON.
    provider_input = json.loads(messages[1]["content"])
    assert provider_input["tool_capabilities"] == catalog.options
    assert provider_input["tool_capabilities"][INVENTORY]["purpose"] == (
        catalog.by_name[INVENTORY]["description"]
    )


def test_inventory_read_preserves_return_contract_and_environment(hn58):
    scenario, engine, catalog = hn58
    farm = scenario.get_typed_app(FarmWorldApp)
    inventory = deepcopy(farm._inventory.to_dict())
    expected = {
        **inventory,
        "management_regime": deepcopy(farm._management_regime.to_dict()),
        "postharvest_market": deepcopy(farm._postharvest_market.to_dict()),
    }
    before = snapshot_environment(scenario, engine)
    result = catalog.tools_map[INVENTORY]()
    after = snapshot_environment(scenario, engine)
    # Wall-clock time can advance during a read; all captured farm state must not.
    before.pop("sim_time")
    after.pop("sim_time")
    assert before == after
    assert result == expected
    assert set(inventory) == {
        "seed_stock", "pesticide_liters", "fertilizer_kg", "fuel_liters",
        "harvest_grain_kg", "warehouse_grain_kg", "grain_dried",
    }
    assert set(result) == set(inventory) | {"management_regime", "postharvest_market"}
    assert result["management_regime"]["irrigation_quota_mm_total"] == 1.4
    assert result["management_regime"]["irrigation_used_mm_total"] == 0.0
    assert catalog.by_name[INVENTORY]["write_operation"] is False
    assert catalog.by_name[INVENTORY]["schema"] == {
        "type": "object", "properties": {}, "required": []
    }
