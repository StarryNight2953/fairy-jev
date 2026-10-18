"""Exercise the real TypeSafe SDK serialization using an offline HTTP transport."""

import json

import httpx2
import pytest

from fairy.agents.Jev.client import DecisionError, OfficialBackend
from fairy.agents.Jev.trace import TraceWriter


TEST_SECRET = "unit-test-typesafe-secret-do-not-log"
CONFIG = {"api_key_env": "JEV_UNIT_TEST_API_KEY", "base_url": "https://api.typesafe.ai",
          "model": "jev-1.13.0", "request_timeout_seconds": 7.25}


@pytest.fixture
def backend_factory(monkeypatch):
    monkeypatch.setenv(CONFIG["api_key_env"], TEST_SECRET)
    backends = []

    def make(handler):
        backend = OfficialBackend(CONFIG, transport=httpx2.MockTransport(handler))
        backends.append(backend)
        return backend

    yield make
    for backend in backends:
        backend.close()


def response_body(choice="inspect", *, usage=None, confidence=0.735, question_id="next_tool"):
    return {"model": "jev-1.13.0-served", "usage": {} if usage is None else usage,
            "answers": {question_id: {"type": "choice", "choice": choice,
                        "confidence": confidence, "probabilities": {"inspect": 0.735, "FINISH": 0.265}}}}


def test_official_wire_contract_and_response_metadata(backend_factory):
    requests = []
    payload = response_body(usage={"input_tokens": 123, "output_tokens": 4})

    def handler(request):
        requests.append(request)
        return httpx2.Response(200, json=payload, headers={"x-typesafe-request-id": "req-offline-1"})

    backend = backend_factory(handler)
    state = {"task": "检查灌溉区域", "observations": [{"rain": False}], "current_time": 1786057131.7453332}
    instructions = "基于任务、观察和已执行结果，下一步应调用哪个工具？"
    options = {"inspect": {"description": "Read the visible target", "parameters": {"type": "object"}},
               "FINISH": "Stop without claiming task success"}
    decision = backend.decide(state, "next_tool", instructions, options)

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
    assert request.headers["authorization"] == f"Bearer {TEST_SECRET}"
    assert "content-encoding" not in request.headers  # compression=False
    assert set(request.extensions["timeout"].values()) == {7.25}
    wire = json.loads(request.content)
    assert wire["state"] == state
    assert wire["model"] == CONFIG["model"]
    assert wire["questions"] == {"next_tool": {"type": "choice", "instructions": instructions, "criteria": options}}
    assert not {"temperature", "max_tokens", "parallel_tool_calls", "retry_policy"} & wire.keys()
    assert list(wire["questions"]["next_tool"]["criteria"]) == list(options)
    assert decision.selected_option == "inspect"
    assert decision.record["requested_model"] == "jev-1.13.0"
    assert decision.record["response_model"] == payload["model"]
    assert decision.record["request_id"] == "req-offline-1"
    assert decision.record["http_status"] == 200
    assert decision.record["sdk_version"] == "0.7.5"
    assert decision.record["retry_max_retries"] == 0
    assert decision.record["parsed_response"] == payload
    assert json.loads(decision.record["raw_response_body"]) == payload
    assert decision.record["confidence"] == 0.735
    assert decision.record["probabilities"] == {"inspect": 0.735, "FINISH": 0.265}
    assert decision.record["usage"] == {"input_tokens": 123, "output_tokens": 4}
    assert decision.record["missing_metadata_reason"] is None


def test_unreported_usage_and_request_id_remain_null(backend_factory):
    backend = backend_factory(lambda request: httpx2.Response(200, json=response_body()))
    decision = backend.decide({}, "next_tool", "Select next tool", {"inspect": "Observe", "FINISH": "Stop"})
    assert decision.record["usage"] == {"input_tokens": None, "output_tokens": None}
    assert decision.record["request_id"] is None
    assert decision.record["confidence"] == 0.735
    assert "did not supply" in decision.record["missing_metadata_reason"]


def test_invalid_return_option_retains_full_response(backend_factory):
    backend = backend_factory(lambda request: httpx2.Response(200, json=response_body("unsubmitted")))
    with pytest.raises(DecisionError, match="invalid_return_option") as caught:
        backend.decide({}, "next_tool", "Choose only submitted options", {"inspect": "Observe"})
    assert caught.value.record["selected_option"] == "unsubmitted"
    assert caught.value.record["http_status"] == 200
    assert json.loads(caught.value.record["raw_response_body"])["answers"]["next_tool"]["choice"] == "unsubmitted"


@pytest.mark.parametrize("options", [{}, {f"candidate_{i}": str(i) for i in range(256)}])
def test_invalid_choice_size_never_sends_http(backend_factory, options):
    requests = []
    backend = backend_factory(lambda request: requests.append(request))
    with pytest.raises(DecisionError, match="1–255"):
        backend.decide({}, "next_tool", "Select next tool", options)
    assert not requests


@pytest.mark.parametrize("status", [401, 429, 503])
def test_http_error_single_attempt_and_trace_redaction(backend_factory, tmp_path, status):
    requests = []
    payload = {"error": {"message": f"Provider echoed {TEST_SECRET}",
                         "Authorization": f"Bearer {TEST_SECRET}"}}

    def handler(request):
        requests.append(request)
        return httpx2.Response(status, json=payload, headers={"x-typesafe-request-id": "req-failed", "retry-after": "1"})

    backend = backend_factory(handler)
    with pytest.raises(DecisionError) as caught:
        backend.decide({}, "next_tool", "Choose", {"inspect": "Observe"})
    assert len(requests) == 1  # Includes retryable 429/503; automatic SDK retry is disabled.
    record = caught.value.record
    assert record["http_status"] == status
    assert record["request_id"] == "req-failed"
    assert record["raw_response_body"] == payload
    assert record["usage"] is None
    assert record["retry_max_retries"] == 0
    path = tmp_path / "trace.jsonl"
    with TraceWriter(path, "offline-error", secrets=[TEST_SECRET]) as trace:
        trace.emit("decision_failed", decision_id="d1", attempt_id="a1", record=record,
                   exception=str(caught.value), request_headers={"Authorization": f"Bearer {TEST_SECRET}"})
        # Events are immediately flushed; no need to wait for context-manager close.
        stored = json.loads(path.read_text())
    assert TEST_SECRET not in path.read_text()
    assert stored["request_headers"]["Authorization"] == "[REDACTED]"
    assert stored["record"]["raw_response_body"]["error"]["Authorization"] == "[REDACTED]"
    assert stored["record"]["http_status"] == status


@pytest.mark.parametrize("exception_type", [httpx2.ConnectError, httpx2.ReadTimeout])
def test_transport_error_records_failure_without_retry(backend_factory, tmp_path, exception_type):
    requests = []

    def handler(request):
        requests.append(request)
        raise exception_type(f"offline connection failure {TEST_SECRET}", request=request)

    backend = backend_factory(handler)
    with pytest.raises(DecisionError) as caught:
        backend.decide({}, "next_tool", "Choose", {"inspect": "Observe"})
    assert len(requests) == 1
    record = caught.value.record
    assert record["http_status"] is None
    assert record["request_id"] is None
    assert record["raw_response_body"] is None
    assert record["error_type"] in {"TypeSafeAPIConnectionError", "TypeSafeAPITimeoutError"}
    with TraceWriter(tmp_path / "trace.jsonl", "offline-transport", secrets=[TEST_SECRET]) as trace:
        trace.emit("decision_failed", decision_id="d1", attempt_id="a1", record=record, exception=str(caught.value))
    assert TEST_SECRET not in (tmp_path / "trace.jsonl").read_text()


def test_malformed_response_preserves_http_evidence(backend_factory):
    payload = {"model": "jev-served", "usage": {}, "answers": {"next_tool": {"type": "choice", "choice": "inspect"}}}
    backend = backend_factory(lambda request: httpx2.Response(200, json=payload, headers={"x-typesafe-request-id": "req-malformed"}))
    with pytest.raises(DecisionError) as caught:
        backend.decide({}, "next_tool", "Choose", {"inspect": "Observe"})
    assert caught.value.record["error_type"] == "TypeSafeAPIResponseValidationError"
    assert caught.value.record["http_status"] == 200
    assert caught.value.record["request_id"] == "req-malformed"
    assert caught.value.record["raw_response_body"] == payload


def test_missing_key_is_explicit_and_never_sends_http(monkeypatch):
    monkeypatch.delenv(CONFIG["api_key_env"], raising=False)
    with pytest.raises(ValueError, match="missing_api_key"):
        OfficialBackend(CONFIG, transport=httpx2.MockTransport(lambda request: pytest.fail("Unexpected request")))
