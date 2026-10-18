"""No network: exercise the configured vLLM SDK, native loop and updater."""
from copy import deepcopy
import json

import httpx
import pytest
from openai import OpenAI

from fairy.agents.Jev.hybrid.updater import LLMUpdater
from fairy.agents.Jev.trace import TraceWriter
from fairy.experiments.budget import EpisodeRuntime
from fairy.experiments.credentials import prepare_llm_config, required_credential_names
from fairy.experiments.episode import credential_requirements
from fairy.experiments.transports import build_instrumented_llm
from fairy.agents.agent.agent import Agent
from fairy.time_manager import TimeManager


LOCAL = {"provider": "vllm", "model": "Qwen3.6-35B-A3B-FP8",
    "base_url": "http://100.115.106.71:8000/v1", "authentication_mode": "none",
    "temperature": 0.1, "max_output_tokens": 2048, "parallel_tool_calls": True,
    "enable_thinking": False, "timeout": 30}


@pytest.fixture
def runtime(tmp_path):
    with TraceWriter(tmp_path / "trace.jsonl", "local-qwen", secrets=("real-key-sentinel",)) as trace:
        yield EpisodeRuntime(trace)


def response(content="done", tool_calls=None):
    return {"id": "test", "object": "chat.completion", "created": 1,
        "model": LOCAL["model"], "choices": [{"index": 0,
            "finish_reason": "tool_calls" if tool_calls else "stop",
            "message": {"role": "assistant", "content": content, "tool_calls": tool_calls}}]}


def fake_openai(monkeypatch, handler):
    import fairy.agents.llm.vllm_client as module
    def create(**kwargs):
        return OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(module, "OpenAI", create)


def test_local_no_key_preflight_and_configuration_are_explicit(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    config = {"llm": deepcopy(LOCAL)}
    before = deepcopy(config)
    assert required_credential_names(config, "llm_native") == []
    assert credential_requirements(config, "llm_native") == []
    assert credential_requirements(config, "hybrid") == ["TYPESAFE_API_KEY"]
    assert prepare_llm_config(LOCAL)["api_key"] == "EMPTY"
    assert config == before and "api_key" not in config["llm"]


@pytest.mark.parametrize("config", [
    {"provider": "vllm", "authentication_mode": "other"},
    {"provider": "deepseek", "authentication_mode": "none", "base_url": "https://configured"},
    {"provider": "vllm", "authentication_mode": "none"},
])
def test_invalid_auth_configuration_fails_before_request(config):
    with pytest.raises(ValueError):
        prepare_llm_config(config)
    with pytest.raises(ValueError):
        required_credential_names({"llm": config}, "llm_native")


def test_provider_environment_mapping_and_alias_fallback(monkeypatch):
    for name in ("OPENAI_API_KEY", "LLAMA_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY", "VLLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert required_credential_names({"llm": {"provider": "deepseek"}}, "llm_native") == ["DEEPSEEK_API_KEY"]
    assert required_credential_names({"llm": {"provider": "openai"}}, "llm_native") == ["OPENAI_API_KEY"]
    assert required_credential_names({"llm": {"provider": "vllm"}}, "llm_native") == ["VLLM_API_KEY"]
    monkeypatch.setenv("DASHSCOPE_API_KEY", "real-key-sentinel")
    assert required_credential_names({"llm": {"provider": "qwen"}}, "llm_native") == ["DASHSCOPE_API_KEY"]
    monkeypatch.setenv("LOCAL_NAMED_CREDENTIAL", "real-key-sentinel")
    configured = {"provider": "vllm", "api_key_env": "LOCAL_NAMED_CREDENTIAL"}
    assert prepare_llm_config(configured)["api_key"] == "real-key-sentinel"
    assert "api_key" not in configured


def test_native_function_call_path_preserves_full_schema_and_uses_public_placeholder(runtime, monkeypatch):
    monkeypatch.setenv("VLLM_API_KEY", "real-key-sentinel")
    requests, actions = [], []
    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json=response(tool_calls=[{"id": "call_1", "type": "function",
                "function": {"name": "read", "arguments": "{}"}}]))
        return httpx.Response(200, json=response())
    fake_openai(monkeypatch, handle)
    original = deepcopy(LOCAL)
    llm = build_instrumented_llm(LOCAL, runtime)
    agent = Agent("native", llm, system_message="Public task context")
    agent.set_time_manager(TimeManager())
    agent.tools_map = {"read": lambda: actions.append("read") or {"observed": True}}
    schema = {"type": "function", "function": {"name": "read", "description": "Read", "parameters": {"type": "object", "properties": {}}}}
    agent.tool_schemas = [schema]
    agent.run("Read then report.")
    assert actions == ["read"] and agent.stop_reason == "agent_finished"
    assert len(requests) == 2 and llm.api_client.max_retries == 2
    assert all(request.headers["authorization"] == "Bearer EMPTY" for request in requests)
    body = json.loads(requests[0].content)
    assert requests[0].url.path == "/v1/chat/completions"
    assert body["model"] == LOCAL["model"] and body["tools"] == [schema]
    assert body["max_tokens"] == 2048 and body["parallel_tool_calls"] is True
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert any(message.get("role") == "tool" for message in json.loads(requests[1].content)["messages"])
    assert "real-key-sentinel" not in runtime.trace.path.read_text()
    assert LOCAL == original


def test_updater_local_provider_repairs_once_without_tools_in_one_logical_update(runtime, monkeypatch):
    monkeypatch.setenv("VLLM_API_KEY", "real-key-sentinel")
    requests = []
    def handle(request):
        requests.append(request)
        content = "invalid JSON" if len(requests) == 1 else '{"brief":"Observed public evidence only.","evidence_refs":[]}'
        return httpx.Response(200, json=response(content))
    fake_openai(monkeypatch, handle)
    llm = build_instrumented_llm(LOCAL, runtime, "llm_context_update")
    updater = LLMUpdater(llm, trace=runtime.trace)
    result = updater.update({"input_event_ids": [], "task": "public task"})
    assert result["source_request_id"] == "model_0001"
    assert len(requests) == 2 and llm.api_client.max_retries == 0
    assert runtime.counts["http_attempts"] == 2 and runtime.counts["logical_requests"] == 1
    assert all(json.loads(request.content).get("tools") is None for request in requests)
    assert all(request.headers["authorization"] == "Bearer EMPTY" for request in requests)
    assert all(json.loads(request.content)["max_tokens"] == 2048 for request in requests)
    assert "real-key-sentinel" not in runtime.trace.path.read_text()
