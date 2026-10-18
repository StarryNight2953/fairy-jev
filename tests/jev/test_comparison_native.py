"""Real native loop + provider SDK behavior, with deterministic network responses."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from openai import OpenAI
from openai.types.chat import ChatCompletion

from fairy.agents.agent.agent import Agent
from fairy.agents.Jev.trace import TraceWriter
from fairy.experiments.budget import BudgetStop, EpisodeRuntime
from fairy.experiments.native import attach_native_observers
from fairy.experiments.transports import ObservedTransport, build_instrumented_llm, instrument_http_client
from fairy.time_manager import TimeManager


def completion(tool_calls=None, *, content="done", usage=None):
    return ChatCompletion.model_validate({"id": "completion", "object": "chat.completion",
        "created": 1, "model": "configured-model", "usage": usage,
        "choices": [{"index": 0, "finish_reason": "tool_calls" if tool_calls else "stop",
            "message": {"role": "assistant", "content": content, "tool_calls": tool_calls}}]})


def call(identifier, name, args):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": args}}


@pytest.fixture
def runtime(tmp_path):
    with TraceWriter(tmp_path / "trace.jsonl", "test", secrets=("private-fixture-secret",)) as trace:
        yield EpisodeRuntime(trace)


def native_agent(runtime, responses, mapping):
    observed_inputs = []
    def chat(messages, tools=None):
        observed_inputs.append(list(messages))
        return responses.pop(0)
    llm = SimpleNamespace(provider="stub", model="configured-model", chat_completion=chat,
                          parallel_tool_calls=True, last_request_id="model_1")
    agent = Agent("test", llm, system_message="native context")
    agent.set_time_manager(TimeManager())
    agent.tools_map = mapping
    agent.tool_schemas = []
    catalog = SimpleNamespace(by_name={name: {"write_operation": False} for name in mapping})
    attach_native_observers(agent, runtime, catalog)
    return agent, observed_inputs


def test_native_multi_tool_response_keeps_serial_order_and_feedback(runtime):
    executed = []
    def first():
        executed.append("first")
        return {"value": 1}
    def second():
        executed.append("second")
        return {"value": 2}
    agent, inputs = native_agent(runtime, [completion([call("a", "first", "{}"),
        call("b", "second", "{}")]), completion()], {"first": first, "second": second})
    assert agent.run("task") == "done"
    assert executed == ["first", "second"]
    assert runtime.counts["tool_attempts"] == 2
    assert agent.llm_turn_count == 2
    assert len([m for m in inputs[1] if isinstance(m, dict) and m.get("role") == "tool"]) == 2
    ends = [e for e in runtime.trace.events if e["event"] == "tool_end"]
    assert [e["tool_call_id"] for e in ends] == ["a", "b"]
    assert all(e["executed"] and e["workflow_step_name"] in agent.workflow.dag for e in ends)


def test_native_preexecution_errors_do_not_spend_tool_budget(runtime):
    executed = []
    def requires_value(value):
        executed.append(value)
        return value
    agent, _ = native_agent(runtime, [completion([call("a", "requires_value", "bad-json"),
        call("b", "missing-tool", "{}"), call("c", "requires_value", "{}"),
        call("d", "requires_value", '{"value":7}')]), completion()], {"requires_value": requires_value})
    agent.run("task")
    assert executed == [7]
    assert runtime.counts["tool_attempts"] == 1
    assert agent.tool_call_count == 4  # Preserve native's legacy diagnostic counter.
    pre = [e for e in runtime.trace.events if e["event"] == "pre_execution_error"]
    assert len(pre) == 3
    assert len([s for s in agent.workflow.dag.values() if s.tool_name]) == 4
    assert all(s.error_stage == "tool_args_parse" for s in agent.workflow.dag.values()
               if s.op_type == "TOOL_ERROR")
    normalized = [e for e in runtime.trace.events if e["event"] == "tool_end" and e.get("error_stage_normalization")]
    assert len(normalized) == 2
    assert all(e["original_native_error_stage"] == "tool_call" for e in normalized)


def test_native_actual_exception_spends_budget_once_and_delivers_error(runtime):
    executed = []
    def raises():
        executed.append(True)
        raise ValueError("actual tool failure")
    agent, inputs = native_agent(runtime, [completion([call("a", "raises", "{}")]), completion()], {"raises": raises})
    agent.run("task")
    assert len(executed) == runtime.counts["tool_attempts"] == 1
    end = next(e for e in runtime.trace.events if e["event"] == "tool_end")
    assert end["executed"] and end["error_stage"] == "tool_call"
    assert "actual tool failure" in inputs[1][-1]["content"]


def test_native_budget_stops_before_second_call_in_same_response(runtime):
    runtime.limits["max_tool_attempts"] = 1
    executed = []
    def tool():
        executed.append(True)
        return "ok"
    agent, _ = native_agent(runtime, [completion([call("a", "tool", "{}"), call("b", "tool", "{}")])], {"tool": tool})
    with pytest.raises(BudgetStop, match="max_tool_attempts"):
        agent.run("task")
    assert len(executed) == 1
    assert len([e for e in runtime.trace.events if e["event"] == "tool_end"]) == 1


def test_sdk_actual_retries_counted_and_raw_body_preserved(runtime):
    requests = []
    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, json={"error": {"message": "temporary"}}, headers={"retry-after": "0"})
        return httpx.Response(200, json=completion().model_dump(mode="json"), headers={"x-request-id": "server-id"})
    client = OpenAI(api_key="private-fixture-secret", http_client=httpx.Client(transport=httpx.MockTransport(respond)))
    instrument_http_client(client._client, runtime)
    client._sleep_for_retry = lambda **kwargs: None
    with runtime.logical("llm_native_decision"):
        response = client.chat.completions.create(model="configured-model", messages=[{"role": "user", "content": "exact task"}])
    assert response.model == "configured-model"
    assert runtime.counts["http_attempts"] == 2
    events = [e for e in runtime.trace.events if e["event"] == "http_attempt_end"]
    assert [e["http_status"] for e in events] == [429, 200]
    assert events[1]["usage"] is None
    assert events[1]["usage_status"] == "unavailable"
    assert events[1]["server_request_id"] == "server-id"
    assert "private-fixture-secret" not in runtime.trace.path.read_text()
    assert requests[0].extensions["timeout"]["read"] <= 900


def test_budget_stop_bypasses_sdk_retry(runtime):
    runtime.limits["max_http_attempts"] = 1
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(429, json={"error": {"message": "temporary"}})
    client = OpenAI(api_key="fixture", http_client=httpx.Client(transport=httpx.MockTransport(respond)))
    instrument_http_client(client._client, runtime)
    client._sleep_for_retry = lambda **kwargs: None
    with runtime.logical("llm_native_decision"), pytest.raises(BudgetStop):
        client.chat.completions.create(model="configured-model", messages=[{"role": "user", "content": "task"}])
    assert len(requests) == runtime.counts["http_attempts"] == 1


def test_httpx2_transport_compatible(runtime):
    import httpx2
    original = httpx2.MockTransport(lambda request: httpx2.Response(200, json={"model": "jev-1.13.0"}))
    with httpx2.Client(transport=ObservedTransport(original, runtime)) as client:
        with runtime.logical("jev_tool_choice"):
            assert client.post("https://fixture.invalid", json={"model": "jev-1.13.0"}).status_code == 200
    assert runtime.role_attempts["jev_tool_choice"] == 1


def test_updater_factory_reuses_existing_client_and_disables_nested_retry(runtime, monkeypatch):
    from fairy.agents.llm.base_llm import BaseLLM
    from fairy.agents.llm.openai_compatible_client import OpenAICompatibleClient
    llm = OpenAICompatibleClient("configured-model", api_key="fixture", base_url="https://fixture.invalid")
    llm.api_client._client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=completion().model_dump(mode="json"))))
    monkeypatch.setattr(BaseLLM, "llm_builder", lambda config: llm)
    instrumented = build_instrumented_llm({"model": "configured-model"}, runtime, "llm_context_update")
    result = instrumented.chat_completion([{"role": "user", "content": "task"}])
    assert instrumented is llm
    assert instrumented.api_client.max_retries == 0
    assert result.choices[0].message.content == "done"
    assert llm.last_request_id == "model_0001"
    assert runtime.role_attempts["llm_context_update"] == 1
    with runtime.logical("llm_context_update") as update_request:
        instrumented.chat_completion([{"role": "user", "content": "initial attempt"}])
        instrumented.chat_completion([{"role": "user", "content": "format repair"}])
        assert llm.last_request_id == update_request
    assert runtime.counts["logical_requests"] == 2
    assert runtime.role_attempts["llm_context_update"] == 3


def test_native_error_replay_restores_preexecution_and_preserves_optional_defaults(runtime, tmp_path):
    from pathlib import Path
    from fairy.agents.Jev.environment import create_fixture
    from fairy.agents.Jev.replay import replay_run
    from fairy.agents.Jev.tools import ToolCatalog
    from fairy.agents.Jev.trace import digest, write_json
    config = json.loads(Path("configs/jev/hn58_irrigation.json").read_text())
    scenario, engine, metadata = create_fixture(config)
    catalog = ToolCatalog(scenario.apps)
    responses = [completion([
        call("unknown", "NotRegistered", "{}"),
        call("missing-args", "FarmWorldApp__get_ridge_state", "{}"),
        call("default-args", "WeatherApp__get_forecast", "{}"),
    ]), completion()]
    agent, _ = native_agent(runtime, responses, catalog.tools_map)
    agent.set_time_manager(engine.time_manager)
    agent.run("Read with the normal native function-call path.")
    assert runtime.counts["tool_attempts"] == 1
    source = tmp_path / "source"
    source.mkdir()
    agent.workflow.save_workflow(str(source / "workflow.json"))
    write_json(source / "manifest.json", {**metadata, "config": config, "backend": "scripted",
        "catalog_hash": catalog.hash, "parameter_catalog_hash": digest(config["candidate_catalog"])})
    result = replay_run(source / "workflow.json", config, tmp_path / "replay")
    assert result["replay"]["replay_success"], result["replay"]["divergences"]
    assert result["replay"]["n_executed"] == 1
    assert result["replay"]["n_restored_error_events"] == 2
    equivalences = result["replay"]["argument_default_equivalences"]
    assert len(equivalences) == 1
    assert equivalences[0]["recorded_arguments"] == {}
    assert equivalences[0]["bound_arguments"] == {"days": 7}
    recorded = [s for s in agent.workflow.dag.values() if s.tool_name == "WeatherApp__get_forecast"]
    assert recorded[0].tool_args == {}
