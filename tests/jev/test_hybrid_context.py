from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from fairy.agents.Jev.hybrid.context import PacketContext, StateBudgetExceeded, request_size
from fairy.agents.Jev.hybrid.updater import LLMUpdater, LLMUpdateError
from fairy.agents.Jev.trace import TraceWriter
from fairy.experiments.budget import BudgetStop


def accept(context, *, brief="Observed public evidence; verify unfinished work.", refs=None, tools=0):
    update = context.build_update_input("Original task", ["Public constraint"], {"read": {}, "FINISH": "Complete whole task"}, tools)
    payload = {"brief": brief, "evidence_refs": update["input_event_ids"] if refs is None else refs}
    context.accept_packet(payload, update, "model_0001", tools)
    return update


def test_packet_replacement_new_feedback_and_exact_evidence_boundary(tmp_path):
    with TraceWriter(tmp_path / "trace.jsonl", "offline") as trace:
        trace.emit("run_start", task="Original task")
        trace.emit("fixture_private", secret_gold="must not reach models")
        context = PacketContext(trace)
        first = accept(context)
        assert first["input_event_ids"] == []
        result = trace.emit("tool_end", tool_name="read", result={"water": 2}, status="ok")
        frozen = context.freeze("Original task", [], 7, {})
        assert frozen["fresh_event_ids"] == [result["event_id"]]
        assert frozen["fresh_events"][0]["result"] == {"water": 2}
        assert "must not reach models" not in json.dumps(frozen)
        second = accept(context, brief="The latest result is water 2.", tools=4)
        assert second["fresh_events"] == [result]
        projected = context.freeze("Original task", [], 8, {})
        assert projected["packet"]["packet_version"] == 2
        assert projected["fresh_events"] == []
        assert "Observed public evidence" not in json.dumps(projected)
        assert frozen["packet"]["packet_version"] == 1  # Frozen action snapshot is independent.
        # A later update includes original cited evidence, not just its identifier.
        third = context.build_update_input("Original task", [], {}, 8)
        assert third["prior_cited_evidence"] == [result]
        assert third["input_event_ids"] == [result["event_id"]]


@pytest.mark.parametrize("hidden_kind", ["decision_end", "parameter_candidates", "private_evaluation", "fixture"])
def test_cannot_cite_nonvisible_or_future_events(tmp_path, hidden_kind):
    with TraceWriter(tmp_path / "trace.jsonl", "offline") as trace:
        private = trace.emit(hidden_kind, content="hidden")
        context = PacketContext(trace)
        update = context.build_update_input("task", [], {}, 0)
        for ref in (private["event_id"], 10000, True):
            with pytest.raises(ValueError):
                context.accept_packet({"brief": "Claim", "evidence_refs": [ref]}, update, "request", 0)
        assert context.packet is None


def test_environment_and_preexecution_errors_visible_with_exact_ids(tmp_path):
    with TraceWriter(tmp_path / "trace.jsonl", "offline") as trace:
        context = PacketContext(trace)
        accept(context)
        rows = [trace.emit("tool_end", executed=False, error_stage="tool_args_parse", result={"error": "bad"}),
                trace.emit("controller_feedback", status="needs_context", executed=False),
                trace.emit("environment_notification", content="new notification")]
        frozen = context.freeze("task", [], 2, {})
        assert frozen["fresh_events"] == rows
        update = context.build_update_input("task", [], {}, 0)
        tampered = deepcopy(update)
        tampered["fresh_events"][0]["executed"] = True
        with pytest.raises(ValueError, match="exact visible"):
            context.accept_packet({"brief": "bad", "evidence_refs": []}, tampered, "request", 0)


def test_request_budget_includes_criteria_and_does_not_truncate():
    state = {"task": "must retain this", "packet": {"brief": "意见"}, "fresh_events": []}
    options = {"a": "x" * 1000, "FINISH": "whole task"}
    initial = deepcopy(options)
    with pytest.raises(StateBudgetExceeded) as error:
        request_size(state, "tool", "choose", options, {"max_jev_request_bytes": 400})
    assert error.value.measurements["components"]["tool_menu"]["utf8_bytes"] > 1000
    assert options == initial
    result = request_size(state, "tool", "choose", options)
    assert result["token_count"] is None
    assert result["complete_request"]["utf8_bytes"] > result["complete_request"]["chars"]


def test_size_matches_pinned_sdk_actual_offline_wire_bytes():
    import hashlib
    import httpx2
    from typesafe_sdk import Choice, TypeSafeClient, RetryPolicy
    captured = []
    def transport(request):
        captured.append(request.content)
        return httpx2.Response(200, json={"model": "jev-1.13.0", "usage": {"input_tokens": 7, "output_tokens": 3},
            "answers": {"tool": {"type": "choice", "choice": "read", "confidence": 1,
                                 "probabilities": {"read": 1}}}})
    state = {"task": "检查土壤", "fresh_events": [{"result": {"vwc": 0.18}}]}
    options = {"read": {"purpose": "读取", "arguments": {}}}
    measured = request_size(state, "tool", "Select", options)
    with TypeSafeClient(api_key="offline-fixture-key", model="jev-1.13.0", compression=False,
                        retry=RetryPolicy(max_retries=0), transport=httpx2.MockTransport(transport)) as client:
        client.system_one(state=state, questions={"tool": Choice(instructions="Select", criteria=options)})
    assert len(captured) == 1
    assert measured["complete_request"] == {"chars": len(captured[0].decode()), "utf8_bytes": len(captured[0])}
    assert measured["wire_sha256"] == hashlib.sha256(captured[0]).hexdigest()


class FakeLLM:
    max_output_tokens = 2048
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []
        self.last_request_id = None

    def chat_completion(self, messages, tools=None):
        assert tools is None
        self.calls.append(deepcopy(messages))
        self.last_request_id = f"model_{len(self.calls):04d}"
        output = next(self.outputs)
        if isinstance(output, BaseException):
            raise output
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=output))])


INPUT = {"task": "task", "input_event_ids": [4], "fresh_events": [{"event_id": 4, "result": "seen"}]}
GOOD = json.dumps({"brief": "The supplied observation says seen.", "evidence_refs": [4]})


def test_updater_one_format_repair_uses_same_evidence():
    llm = FakeLLM(["not json", GOOD])
    updater = LLMUpdater(llm)
    result = updater.update(INPUT)
    assert result["source_request_id"] == "model_0002"
    assert len(llm.calls) == 2
    assert llm.calls[0] == llm.calls[1][:2]
    assert "not json" == llm.calls[1][2]["content"]


def test_transient_then_bad_format_has_no_third_attempt():
    llm = FakeLLM([TimeoutError("temporary"), "bad", GOOD])
    with pytest.raises(LLMUpdateError):
        LLMUpdater(llm).update(INPUT)
    assert len(llm.calls) == 2


def test_invalid_citation_fails_after_one_repair_and_budget_stop_propagates():
    bad = json.dumps({"brief": "Unsupported", "evidence_refs": [99]})
    llm = FakeLLM([bad, bad])
    with pytest.raises(LLMUpdateError):
        LLMUpdater(llm).update(INPUT)
    assert len(llm.calls) == 2
    limited = FakeLLM([BudgetStop("max_http_attempts")])
    with pytest.raises(BudgetStop):
        LLMUpdater(limited).update(INPUT)
    assert len(limited.calls) == 1


def test_updater_oversize_fails_before_provider_call():
    llm = FakeLLM([GOOD])
    updater = LLMUpdater(llm, {"hybrid": {"request_limits": {"max_llm_request_bytes": 20}}})
    with pytest.raises(StateBudgetExceeded):
        updater.update(INPUT)
    assert llm.calls == []


def test_empty_choices_is_a_bounded_format_error():
    class EmptyChoices(FakeLLM):
        def chat_completion(self, messages, tools=None):
            self.calls.append(deepcopy(messages))
            return SimpleNamespace(choices=[])
    llm = EmptyChoices([])
    with pytest.raises(LLMUpdateError, match="bounded repair"):
        LLMUpdater(llm).update(INPUT)
    assert len(llm.calls) == 2


@pytest.mark.parametrize("first_attempt", ["format_error", "transient_429"])
def test_update_repairs_share_one_logical_request_and_two_actual_http_attempts(tmp_path, monkeypatch, first_attempt):
    import httpx
    from fairy.agents.llm.base_llm import BaseLLM
    from fairy.agents.llm.openai_compatible_client import OpenAICompatibleClient
    from fairy.experiments.budget import EpisodeRuntime
    from fairy.experiments.transports import build_instrumented_llm
    requests = []
    def respond(request):
        requests.append(request)
        if len(requests) == 1 and first_attempt == "transient_429":
            return httpx.Response(429, json={"error": {"message": "temporary"}})
        content = "not JSON" if len(requests) == 1 else GOOD
        return httpx.Response(200, json={"id": "fixture", "object": "chat.completion", "created": 1,
            "model": "configured-model", "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": content}}]})
    llm = OpenAICompatibleClient("configured-model", api_key="offline-fixture-key", base_url="https://fixture.invalid")
    llm.api_client._client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(BaseLLM, "llm_builder", lambda config: llm)
    with TraceWriter(tmp_path / "trace.jsonl", "offline") as trace:
        runtime = EpisodeRuntime(trace)
        instrumented = build_instrumented_llm({"model": "configured-model"}, runtime, "llm_context_update")
        result = LLMUpdater(instrumented, trace=trace).update(INPUT)
        assert result["source_request_id"] == "model_0001"
        assert runtime.counts["logical_requests"] == 1
        assert runtime.counts["http_attempts"] == len(requests) == 2
        starts = [row for row in trace.events if row["event"] == "http_attempt_start"]
        assert {row["logical_request_id"] for row in starts} == {"model_0001"}
        assert [row["attempt_id"] for row in starts] == ["http_0001", "http_0002"]
        assert llm.api_client.max_retries == 0
    llm.api_client.close()
