"""Verify actual SDK request timeouts without connecting to a model service."""
from __future__ import annotations

import json
from pathlib import Path
import socket

import httpx
from openai import OpenAI
import pytest

from fairy.agents.Jev.trace import TraceWriter
from fairy.agents.llm.base_llm import BaseLLM
from fairy.experiments.budget import EpisodeRuntime
from fairy.experiments.transports import build_instrumented_llm


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("LLM timeout tests must not perform network calls")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.delenv("FAIRY_LLM_REQUEST_TIMEOUT_S", raising=False)


def test_default_is_30_minutes_and_explicit_config_overrides_environment(monkeypatch):
    assert BaseLLM("fixture", 0.1).request_timeout == 1800
    monkeypatch.setenv("FAIRY_LLM_REQUEST_TIMEOUT_S", "120.5")
    assert BaseLLM("fixture", 0.1).request_timeout == 120.5
    assert BaseLLM("fixture", 0.1, timeout=45).request_timeout == 45
    assert BaseLLM("fixture", 0.1, timeout=None).request_timeout == 120.5
    monkeypatch.setenv("FAIRY_LLM_REQUEST_TIMEOUT_S", "")
    assert BaseLLM("fixture", 0.1).request_timeout == 1800


@pytest.mark.parametrize("value", [0, -1, "nan", "inf", "-inf"])
def test_invalid_timeout_rejected_before_client_construction(value):
    with pytest.raises(ValueError, match="finite positive"):
        BaseLLM("fixture", 0.1, timeout=value)


@pytest.mark.parametrize("path", [
    "configs/llm/qwen_current.json",
    "configs/jev/hn58_hybrid_sync_k4.json",
    "configs/jev/hn58_hybrid_sync_k4_qwen.json",
])
def test_current_llm_configs_use_1800_without_changing_typesafe_timeout(path):
    config = json.loads(Path(path).read_text())
    assert config.get("llm", config)["timeout"] == 1800
    if "request_timeout_seconds" in config:
        assert config["request_timeout_seconds"] == 30


@pytest.mark.parametrize("role,sdk_retries", [
    ("llm_native_decision", 2),
    ("llm_context_update", 0),
])
@pytest.mark.parametrize("remaining,expected", [(7200, 1800), (250, 250)])
def test_native_and_updater_wire_timeout_and_deadline_clamp(
    tmp_path, monkeypatch, role, sdk_retries, remaining, expected,
):
    import fairy.agents.llm.vllm_client as vllm

    requests = []
    config = json.loads(Path("configs/llm/qwen_current.json").read_text())

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={
            "id": "fixture", "object": "chat.completion", "created": 1,
            "model": config["model"], "choices": [{"index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "done"}}],
        })

    def client(**kwargs):
        return OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handle)))

    monkeypatch.setattr(vllm, "OpenAI", client)
    with TraceWriter(tmp_path / "trace.jsonl", "timeout-fixture") as trace:
        runtime = EpisodeRuntime(trace, {"wall_timeout_seconds": 7200})
        monkeypatch.setattr(runtime, "remaining_seconds", lambda: remaining)
        llm = build_instrumented_llm(config, runtime, role)
        try:
            llm.chat_completion([{"role": "user", "content": "fixture"}])
            assert llm.request_timeout == 1800
            assert llm.api_client.max_retries == sdk_retries
            assert len(requests) == 1
            phases = {name: expected for name in ("connect", "read", "write", "pool")}
            assert requests[0].extensions["timeout"] == phases
            event = next(e for e in trace.events if e["event"] == "http_attempt_start")
            assert event["timeout_by_phase"] == phases
            assert event["remaining_deadline_seconds"] == remaining
        finally:
            llm.api_client.close()
