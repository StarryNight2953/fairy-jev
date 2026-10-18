"""Actual-transport and episode cap tests; all HTTP traffic is mocked locally."""
import json
import time

import httpx
import pytest

from fairy.agents.Jev.trace import TraceWriter
from fairy.experiments.budget import BudgetStop, EpisodeRuntime, LiveLedger
from fairy.experiments.transports import ObservedTransport


@pytest.fixture
def runtime_factory(tmp_path):
    traces = []
    def make(limits=None, ledger=None, run_id=None):
        index = len(traces)
        trace = TraceWriter(tmp_path / f"trace-{index}.jsonl", run_id or f"offline-{index}",
                            secrets=["offline-secret-never-log"])
        traces.append(trace)
        return EpisodeRuntime(trace, limits, ledger=ledger)
    yield make
    for trace in traces:
        trace.close()


def test_persistent_global_caps_cannot_reset_with_new_ledger_instance(tmp_path, runtime_factory):
    path = tmp_path / "ledger.json"
    ledger = LiveLedger(path, max_episodes=2, max_http_attempts=3)
    ledger.reserve_episode("run-1", "jev", "smoke", "smoke:jev")
    runtime = runtime_factory(ledger=ledger, run_id="run-1")
    with runtime.logical("jev_tool_choice"):
        for _ in range(3):
            runtime.reserve_http()
    reopened = LiveLedger(path, max_episodes=120, max_http_attempts=14400)
    with pytest.raises(BudgetStop, match="total_model_http_attempts"):
        reopened.reserve_http("run-1")
    reopened.reserve_episode("run-2", "hybrid", "smoke", "smoke:hybrid")
    with pytest.raises(BudgetStop, match="total_live_episodes"):
        reopened.reserve_episode("run-3", "llm_native", "smoke", "smoke:llm_native")
    saved = json.loads(path.read_text())
    assert len(saved["episodes"]) == 2
    assert saved["http_attempts"] == 3
    assert saved["max_episodes"] == 2 and saved["max_http_attempts"] == 3


def test_episode_slots_and_smoke_architecture_cannot_be_reused(tmp_path):
    ledger = LiveLedger(tmp_path / "ledger.json")
    ledger.reserve_episode("one", "jev", "smoke", "jev-smoke")
    with pytest.raises(BudgetStop, match="episode_slot_already_consumed"):
        ledger.reserve_episode("two", "hybrid", "formal", "jev-smoke")
    with pytest.raises(BudgetStop, match="smoke_slot_already_consumed"):
        ledger.reserve_episode("three", "jev", "smoke", "different-name")
    assert len(ledger.snapshot()["episodes"]) == 1


@pytest.mark.parametrize("role,limit_key,reason", [
    ("jev_tool_choice", "max_jev_http_attempts", "max_jev_http_attempts"),
    ("llm_context_update", "max_updater_attempts", "max_updater_attempts"),
    ("llm_native_decision", "max_http_attempts", "max_http_attempts"),
])
def test_actual_attempt_caps_include_failed_attempts(runtime_factory, role, limit_key, reason):
    runtime = runtime_factory({limit_key: 2})
    with runtime.logical(role):
        assert runtime.reserve_http() == "http_0001"
        assert runtime.reserve_http() == "http_0002"
        with pytest.raises(BudgetStop, match=reason):
            runtime.reserve_http()
    assert runtime.counts["http_attempts"] == 2
    assert runtime.role_attempts[role] == 2


def test_jev_tool_and_argument_choices_share_one_jev_role_cap(runtime_factory):
    runtime = runtime_factory({"max_jev_http_attempts": 2})
    for role in ("jev_tool_choice", "jev_argument_choice"):
        with runtime.logical(role):
            runtime.reserve_http()
    with runtime.logical("jev_tool_choice"):
        with pytest.raises(BudgetStop, match="max_jev_http_attempts"):
            runtime.reserve_http()
    assert sum(runtime.role_attempts.values()) == 2


def test_logical_updates_and_actual_update_attempts_have_distinct_caps(runtime_factory):
    runtime = runtime_factory()
    for _ in range(10):
        runtime.reserve_update()
        with runtime.logical("llm_context_update"):
            runtime.reserve_http()
            runtime.reserve_http()  # Initial attempt plus the one allowed retry/repair.
    with pytest.raises(BudgetStop, match="max_updates"):
        runtime.reserve_update()
    with runtime.logical("llm_context_update"):
        with pytest.raises(BudgetStop, match="max_updater_attempts"):
            runtime.reserve_http()
    assert runtime.counts["updates"] == 10
    assert runtime.role_attempts["llm_context_update"] == 20


def test_whole_episode_deadline_interrupts_non_http_tool_and_marks_uncertain_effects(runtime_factory):
    runtime = runtime_factory({"wall_timeout_seconds": 0.03})
    runtime.reserve_tool()
    runtime.active_boundary = "tool"
    started = time.monotonic()
    with pytest.raises(BudgetStop, match="wall_timeout") as exc:
        with runtime.deadline_guard():
            time.sleep(1)
    assert time.monotonic() - started < 0.5
    assert exc.value.side_effects == "unknown"
    assert runtime.counts["tool_attempts"] == 1


def test_transport_clamps_all_timeouts_and_records_body_without_credentials(runtime_factory):
    runtime = runtime_factory()
    runtime.remaining_seconds = lambda: 7.0
    captured = []
    def handler(request):
        captured.append(request.extensions["timeout"])
        return httpx.Response(200, json={"model": "existing", "echo": "offline-secret-never-log"})
    transport = ObservedTransport(httpx.MockTransport(handler), runtime)
    request = httpx.Request("POST", "https://user:offline-secret-never-log@example.invalid/api?key=offline-secret-never-log",
        json={"model": "existing", "task": "public-task"},
        headers={"authorization": "Bearer offline-secret-never-log"},
        extensions={"timeout": {"connect": 20, "read": 2, "write": None}})
    with runtime.logical("llm_native_decision"):
        assert transport.handle_request(request).status_code == 200
    assert captured == [{"connect": 7.0, "read": 2.0, "write": 7.0, "pool": 7.0}]
    serialized = runtime.trace.path.read_text()
    assert "offline-secret-never-log" not in serialized
    start = next(row for row in runtime.trace.events if row["event"] == "http_attempt_start")
    assert start["endpoint"] == "https://example.invalid/api"
    assert start["request_body"] == {"model": "existing", "task": "public-task"}
    assert "authorization" not in start
    assert runtime.counts["http_attempts"] == 1


def test_sdk_automatic_retries_count_each_transport_attempt_and_stop_at_cap(runtime_factory, monkeypatch):
    from openai import OpenAI
    runtime = runtime_factory({"max_http_attempts": 2})
    calls = []
    def unavailable(request):
        calls.append(request)
        return httpx.Response(503, json={"error": {"message": "temporary service failure", "type": "server_error"}})
    monkeypatch.setattr("openai._base_client.time.sleep", lambda _: None)
    with OpenAI(api_key="offline", base_url="https://offline.invalid/v1", max_retries=4,
        http_client=httpx.Client(transport=ObservedTransport(httpx.MockTransport(unavailable), runtime))) as client:
        with runtime.logical("llm_native_decision"):
            with pytest.raises(BudgetStop, match="max_http_attempts"):
                client.chat.completions.create(model="existing", messages=[{"role": "user", "content": "test"}])
    assert len(calls) == 2
    starts = [row for row in runtime.trace.events if row["event"] == "http_attempt_start"]
    assert [row["sdk_retry_index"] for row in starts] == ["0", "1"]
    assert len({row["logical_request_id"] for row in starts}) == 1
    assert [row["attempt_id"] for row in starts] == ["http_0001", "http_0002"]


def test_budget_stop_bypasses_ordinary_exception_retry_handlers():
    retried = False
    with pytest.raises(BudgetStop):
        try:
            raise BudgetStop("max_http_attempts")
        except Exception:
            retried = True
    assert retried is False
