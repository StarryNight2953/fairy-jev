"""Frozen Jev diagnostic sends use the real SDK with offline HTTP only."""
import json

import httpx2
import pytest

from fairy.agents.Jev.trace import write_json
from fairy.experiments.budget import BudgetStop
from fairy.experiments.jev_state_probe import GROUPS, _ProbeRuntime, prepare_plan, run_probe


SECRET = "unit-test-frozen-jev-secret"


@pytest.fixture
def plan(tmp_path, monkeypatch):
    monkeypatch.setenv("PROBE_TEST_KEY", SECRET)
    body = {"state": {"task": "检查目标后灌溉", "observations": [{"result": {"quota": 1.4}}]},
            "model": "jev-1.13.0", "questions": {"tool": {"type": "choice", "instructions": "Choose",
                "criteria": {"inspect": {"purpose": "Inspect target"}, "FINISH": "Finish"}}}}
    start = {"event": "http_attempt_start", "run_id": "source-run", "event_id": 4,
             "attempt_id": "http_0001", "endpoint": "https://api.typesafe.ai/v1/systemone", "request_body": body}
    end = {"event": "http_attempt_end", "run_id": "source-run", "event_id": 5,
           "attempt_id": "http_0001", "http_status": 400,
           "raw_response_body": '{"detail":{"error_type":"max_tokens_exceeded"}}'}
    (tmp_path / "source.jsonl").write_text(json.dumps(start) + "\n" + json.dumps(end) + "\n")
    write_json(tmp_path / "manifest.json", {"run_id": "source-run", "config": {
        "model": "jev-1.13.0", "api_key_env": "PROBE_TEST_KEY", "request_timeout_seconds": 30,
        "base_url": "https://api.typesafe.ai"}})
    write_json(tmp_path / "state.json", {"task": body["state"]["task"], "actions_so_far": []})
    write_json(tmp_path / "ledger.json", {"ledger_id": "preserved-ledger", "max_episodes": 89,
        "max_http_attempts": 8000, "http_attempts": 1441,
        "episodes": [{"run_id": "old", "architecture": "jev", "stage": "debug", "slot": "old", "status": "completed", "http_attempts": 1441}]})
    values = {"source_trace": "source.jsonl", "source_manifest": "manifest.json", "event_id": 4,
              "attempt_id": "http_0001", "new_state_path": "state.json", "output_dir": "output",
              "ledger_path": "ledger.json", "run_id": "offline-probe"}
    write_json(tmp_path / "plan.json", values)
    return tmp_path / "plan.json"


def accepted():
    return {"model": "jev-served", "usage": {"input_tokens": 10, "output_tokens": 2},
            "answers": {"tool": {"type": "choice", "choice": "inspect", "confidence": 0.7,
                                  "probabilities": {"inspect": 0.7, "FINISH": 0.3}}}}


def test_frozen_inputs_two_calls_exactly_and_existing_caps_preserved(plan):
    before = json.loads((plan.parent / "ledger.json").read_text())
    requests = []
    def handler(request):
        assert (plan.parent / "output/preregistration.json").is_file()
        requests.append(json.loads(request.content))
        assert set(request.extensions["timeout"].values()) == {30.0}
        if len(requests) == 1:
            return httpx2.Response(400, json={"detail": {"error_type": "max_tokens_exceeded"}})
        return httpx2.Response(200, json=accepted(), headers={"x-typesafe-request-id": "probe-B"})
    result = run_probe(plan, transport=httpx2.MockTransport(handler))
    assert len(requests) == result["http_attempts"] == 2
    assert requests[0]["model"] == requests[1]["model"]
    assert requests[0]["questions"] == requests[1]["questions"]
    assert list(requests[0]["questions"]["tool"]["criteria"]) == ["inspect", "FINISH"]
    assert requests[1]["state"] == json.loads((plan.parent / "state.json").read_text())
    assert result["status"] == "completed"
    assert [row["valid_choice"] for row in result["groups"]] == [False, True]
    assert result["groups"][1]["response"]["request_id"] == "probe-B"
    assert result["task_success"] is result["live_integration_success"] is None
    assert result["source_preserved"] and result["tools_executed"] == result["agent_runs"] == 0
    after = json.loads((plan.parent / "ledger.json").read_text())
    assert after["ledger_id"] == before["ledger_id"]
    assert after["max_episodes"] == 89 and after["max_http_attempts"] == 8000
    assert after["episodes"][:-1] == before["episodes"]
    assert after["http_attempts"] == before["http_attempts"] + 2
    assert after["episodes"][-1]["http_attempts"] == 2
    assert result["budget"]["limits"]["wall_timeout_seconds"] == 120
    assert result["budget"]["limits"]["max_http_attempts"] == 2


@pytest.mark.parametrize("status", [401, 403, 429, 451])
def test_access_block_stops_B_without_retry_and_redacts_secrets(plan, status):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx2.Response(status, json={"error": {"message": "echo " + SECRET}})
    result = run_probe(plan, transport=httpx2.MockTransport(handler))
    assert len(requests) == 1
    assert result["status"] == "access_blocked"
    assert result["skipped_groups"] == [GROUPS[1]]
    for path in (plan.parent / "output").iterdir():
        assert SECRET not in path.read_text()


def test_cap_blocks_probe_without_resetting_ledger(plan):
    path = plan.parent / "ledger.json"
    ledger = json.loads(path.read_text())
    ledger["max_http_attempts"] = ledger["http_attempts"]
    write_json(path, ledger)
    result = run_probe(plan, transport=httpx2.MockTransport(lambda _: pytest.fail("network forbidden")))
    assert result["status"] == "budget_exhausted"
    assert result["stop_reason"] == "total_model_http_attempts"
    assert result["http_attempts"] == 0
    assert json.loads(path.read_text())["http_attempts"] == ledger["http_attempts"]


def test_missing_key_does_not_consume_episode(plan, monkeypatch):
    monkeypatch.delenv("PROBE_TEST_KEY")
    before = json.loads((plan.parent / "ledger.json").read_text())
    result = run_probe(plan, transport=httpx2.MockTransport(lambda _: pytest.fail("network forbidden")))
    assert result["status"] == "probe_error" and result["http_attempts"] == 0
    assert json.loads((plan.parent / "ledger.json").read_text()) == before


@pytest.mark.parametrize("change", ["third_group", "task", "extra_choice_field", "extra_request_field", "wrong_event"])
def test_invalid_plan_rejected_before_outputs_or_network(plan, change):
    if change == "third_group":
        data = json.loads(plan.read_text()); data["groups"] = [*GROUPS, "C"]
        write_json(plan, data)
    elif change == "task":
        write_json(plan.parent / "state.json", {"task": "different task", "actions_so_far": []})
    else:
        path = plan.parent / "source.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        if change == "extra_choice_field":
            rows[0]["request_body"]["questions"]["tool"]["temperature"] = 0
        elif change == "extra_request_field":
            rows[0]["request_body"]["temperature"] = 0
        else:
            rows[0]["event_id"] = 99
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    with pytest.raises(ValueError):
        run_probe(plan, transport=httpx2.MockTransport(lambda _: pytest.fail("network forbidden")))
    assert not (plan.parent / "output").exists()


def test_output_cannot_be_reused(plan):
    run_probe(plan, transport=httpx2.MockTransport(lambda _: httpx2.Response(200, json=accepted())))
    with pytest.raises(FileExistsError):
        run_probe(plan, transport=httpx2.MockTransport(lambda _: pytest.fail("network forbidden")))


def test_runtime_never_reserves_third_request(tmp_path):
    from fairy.agents.Jev.trace import TraceWriter
    with TraceWriter(tmp_path / "trace.jsonl", "test") as trace:
        runtime = _ProbeRuntime(trace, {"max_http_attempts": 2, "max_jev_http_attempts": 2})
        runtime.reserve_http("jev_tool_choice")
        runtime.reserve_http("jev_tool_choice")
        runtime.check()  # Parsing the last allowed response is permitted.
        with pytest.raises(BudgetStop) as exc:
            runtime.reserve_http("jev_tool_choice")
        assert exc.value.reason == "max_http_attempts" and runtime.counts["http_attempts"] == 2
