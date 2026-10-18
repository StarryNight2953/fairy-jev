import hashlib
import json
from pathlib import Path

import pytest

from fairy.agents.Jev.trace import write_json
from fairy.experiments.access import service_blockers_from_smoke


def source(tmp_path, architecture, events, *, integration=None, task=None, llm_model="configured-model",
           run_name=None, jev_model="jev-1.13.0"):
    root = tmp_path / (run_name or architecture)
    root.mkdir()
    write_json(root / "manifest.json", {"architecture": architecture, "config": {
        "model": jev_model, "base_url": "https://api.typesafe.ai", "api_key_env": "TYPESAFE_API_KEY",
        "llm": {"provider": "deepseek", "model": llm_model, "base_url": "https://api.deepseek.com", "api_key_env": "DEEPSEEK_API_KEY"}}})
    write_json(root / "evaluation.json", {"execution_status": "integration_error", "live_integration_success": integration,
                                          "task_success": task})
    (root / "trace.jsonl").write_text("".join(json.dumps({"event_id": index, **event}) + "\n" for index, event in enumerate(events)))
    return {"architecture": architecture, "path": str(root)}


def http(status, *, role="jev_tool_choice"):
    shared = {"attempt_id": "http_1", "logical_request_id": "model_1", "role": role,
              "endpoint": "https://api.typesafe.ai/v1/systemone"}
    return [{"event": "http_attempt_start", **shared},
            {"event": "http_attempt_end", **shared, "http_status": status,
             "raw_response_body": json.dumps({"status": status, "title": "Provider response"})}]


def test_region_refusal_blocks_same_service_jev_and_hybrid_without_changing_source(tmp_path):
    records = [source(tmp_path, "jev", http(451)), source(tmp_path, "hybrid", []), source(tmp_path, "llm_native", [])]
    before = (Path(records[0]["path"]) / "evaluation.json").read_bytes()
    result = service_blockers_from_smoke({"runs": records})
    assert result["blocked"]
    assert result["by_architecture"]["jev"][0]["http_status"] == 451
    assert result["by_architecture"]["hybrid"][0]["failure_layer"] == "external_service"
    assert not result["by_architecture"]["llm_native"]
    assert (Path(records[0]["path"]) / "evaluation.json").read_bytes() == before
    assert result["by_architecture"]["jev"][0]["event_id"] == 1


@pytest.mark.parametrize("status", [401, 403])
def test_explicit_auth_refusal_is_access_block(tmp_path, status):
    record = source(tmp_path, "llm_native", http(status, role="llm_native_decision"))
    blocker = service_blockers_from_smoke({"runs": [record]})["by_architecture"]["llm_native"][0]
    assert blocker["classification"] == "confirmed_access_block"
    assert blocker["persistent_block"]


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_transient_without_chain_is_unknown_not_permanent(tmp_path, status):
    record = source(tmp_path, "jev", http(status))
    blocker = service_blockers_from_smoke({"runs": [record]})["by_architecture"]["jev"][0]
    assert blocker["classification"] == "unknown_access_status"
    assert not blocker["persistent_block"]


def test_recovered_transient_does_not_block_formal(tmp_path):
    record = source(tmp_path, "jev", http(429), integration=True, task=False)
    assert not service_blockers_from_smoke({"runs": [record]})["blocked"]


def test_missing_credentials_are_explicit_and_do_not_become_model_failure(tmp_path):
    skipped = [{"event": "run_skipped", "reason": "missing_credentials", "missing_credentials": ["DEEPSEEK_API_KEY"]}]
    records = [source(tmp_path, "llm_native", skipped), source(tmp_path, "hybrid", []), source(tmp_path, "jev", [])]
    result = service_blockers_from_smoke({"runs": records})
    assert result["by_architecture"]["llm_native"][0]["missing_credentials"] == ["DEEPSEEK_API_KEY"]
    assert result["by_architecture"]["hybrid"]
    assert not result["by_architecture"]["jev"]


def test_task_failure_or_error_string_cannot_fabricate_http_denial(tmp_path):
    record = source(tmp_path, "jev", [{"event": "decision_end", "status": "error", "error": "451"}], task=False)
    assert not service_blockers_from_smoke({"runs": [record]})["blocked"]


def test_other_llm_configuration_does_not_inherit_refusal(tmp_path):
    records = [source(tmp_path, "llm_native", http(403, role="llm_native_decision")),
               source(tmp_path, "hybrid", [], llm_model="other-existing-model")]
    result = service_blockers_from_smoke({"runs": records})
    assert result["by_architecture"]["llm_native"]
    assert not result["by_architecture"]["hybrid"]


def test_current_local_configuration_drops_old_key_block_but_preserves_jev_451(tmp_path):
    skipped = [{"event":"run_skipped", "reason":"missing_credentials", "missing_credentials":["DEEPSEEK_API_KEY"]}]
    records = [source(tmp_path, "llm_native", skipped), source(tmp_path, "jev", http(451))]
    old = json.loads((Path(records[0]['path'])/'manifest.json').read_text())['config']
    current = {**old, 'llm': {'provider':'vllm', 'model':'Qwen3.6-35B-A3B-FP8',
        'base_url':'http://local-fixture:8000/v1', 'authentication_mode':'none'}}
    result = service_blockers_from_smoke({'runs':records},target_configs={a:current for a in ('llm_native','jev','hybrid')})
    assert not result['by_architecture']['llm_native']
    for architecture in ('jev','hybrid'):
        assert [x['http_status'] for x in result['by_architecture'][architecture]] == [451]
    assert any(x['reason']=='missing_credentials' for x in result['observations'])


def test_available_credential_only_resolves_missing_variable_not_real_denial(tmp_path):
    skipped = [{"event":"run_skipped", "reason":"missing_credentials", "missing_credentials":["DEEPSEEK_API_KEY"]}]
    record = source(tmp_path,'llm_native',skipped+http(401,role='llm_native_decision'))
    result = service_blockers_from_smoke({'runs':[record]},available_credentials={'DEEPSEEK_API_KEY'})
    assert [x['reason'] for x in result['by_architecture']['llm_native']] == ['service_authentication_denied']


def timed_http(status, when, *, attempt_id="http_1", endpoint="https://api.typesafe.ai/v1/systemone"):
    events = http(status)
    for event in events:
        event.update(attempt_id=attempt_id, endpoint=endpoint)
    events[0]["wall_timestamp"] = when - 0.5
    events[1].update(wall_timestamp=when, status="ok" if status == 200 else "error")
    return events


def test_later_paired_success_supersedes_451_for_same_service_and_preserves_sources(tmp_path):
    denied = source(tmp_path, "jev", timed_http(451, 100), run_name="old_denial")
    succeeded = source(tmp_path, "hybrid", timed_http(200, 200), run_name="later_success",
                       integration=True, task=False)
    protected = {path: path.read_bytes() for record in (denied, succeeded)
                 for path in Path(record["path"]).iterdir()}
    # Ordering in the index is deliberately opposite to wall-clock chronology.
    result = service_blockers_from_smoke({"runs": [succeeded, denied]})
    assert not result["blocked"]
    assert not result["by_architecture"]["jev"]
    assert not result["by_architecture"]["hybrid"]
    assert len(result["resolved_blockers"]) == 1
    resolved = result["resolved_blockers"][0]
    assert resolved["reason"] == "service_access_restricted"
    assert resolved["http_status"] == 451
    assert resolved["resolution_status"] == "superseded_by_later_success"
    assert resolved in result["observations"]
    proof = resolved["resolved_by"]
    assert proof["source_run"] == succeeded["path"]
    assert proof["event_id"] == 1
    assert proof["http_status"] == 200
    assert proof["trace_sha256"] == hashlib.sha256(
        (Path(succeeded["path"]) / "trace.jsonl").read_bytes()).hexdigest()
    assert all(path.read_bytes() == content for path, content in protected.items())


def test_later_success_in_same_run_supersedes_451(tmp_path):
    events = timed_http(451, 100, attempt_id="denied") + timed_http(200, 200, attempt_id="success")
    record = source(tmp_path, "jev", events)
    result = service_blockers_from_smoke({"runs": [record]})
    assert not result["blocked"]
    assert result["resolved_blockers"][0]["resolved_by"]["event_id"] == 3


@pytest.mark.parametrize("success_time", [50, 100])
def test_success_not_strictly_later_than_451_does_not_resolve_it(tmp_path, success_time):
    records = [source(tmp_path, "jev", timed_http(451, 100), run_name="denial"),
               source(tmp_path, "jev", timed_http(200, success_time), run_name="success")]
    result = service_blockers_from_smoke({"runs": records})
    assert [item["http_status"] for item in result["by_architecture"]["jev"]] == [451]
    assert not result["resolved_blockers"]


@pytest.mark.parametrize("difference", ["model", "endpoint"])
def test_later_success_for_other_model_or_endpoint_cannot_resolve_451(tmp_path, difference):
    denied = source(tmp_path, "jev", timed_http(451, 100), run_name="denial")
    success_events = timed_http(200, 200, endpoint=("https://api.typesafe.ai/v1/other"
                                                 if difference == "endpoint"
                                                 else "https://api.typesafe.ai/v1/systemone"))
    succeeded = source(tmp_path, "jev", success_events, run_name="success",
                       jev_model="other-model" if difference == "model" else "jev-1.13.0")
    target_config = json.loads((Path(denied["path"]) / "manifest.json").read_text())["config"]
    result = service_blockers_from_smoke({"runs": [denied, succeeded]},
                                        target_configs={"jev": target_config, "hybrid": target_config})
    assert result["by_architecture"]["jev"]
    assert result["by_architecture"]["hybrid"]
    assert not result["resolved_blockers"]


@pytest.mark.parametrize("invalid", ["unpaired", "no_success_timestamp", "no_refusal_timestamp",
                                     "role_mismatch", "logical_id_mismatch", "missing_response", "not_ok"])
def test_incomplete_success_evidence_cannot_resolve_451(tmp_path, invalid):
    denial_events, success_events = timed_http(451, 100), timed_http(200, 200)
    if invalid == "unpaired":
        success_events.pop(0)
    elif invalid == "no_success_timestamp":
        for event in success_events:
            event.pop("wall_timestamp")
    elif invalid == "no_refusal_timestamp":
        for event in denial_events:
            event.pop("wall_timestamp")
    elif invalid == "role_mismatch":
        success_events[0]["role"] = "jev_argument_choice"
    elif invalid == "logical_id_mismatch":
        success_events[0]["logical_request_id"] = "different_logical_request"
    elif invalid == "missing_response":
        success_events[1].pop("raw_response_body")
    elif invalid == "not_ok":
        success_events[1]["status"] = "error"
    records = [source(tmp_path, "jev", denial_events, run_name="denial"),
               source(tmp_path, "jev", success_events, run_name="success")]
    result = service_blockers_from_smoke({"runs": records})
    assert result["by_architecture"]["jev"]
    assert not result["resolved_blockers"]


def test_new_refusal_after_success_remains_blocking(tmp_path):
    records = [source(tmp_path, "jev", timed_http(451, 100), run_name="old_denial"),
               source(tmp_path, "jev", timed_http(200, 200), run_name="success"),
               source(tmp_path, "jev", timed_http(451, 300), run_name="new_denial")]
    result = service_blockers_from_smoke({"runs": records})
    blockers = result["by_architecture"]["jev"]
    assert len(blockers) == 1
    assert blockers[0]["source_run"] == records[2]["path"]
    assert blockers[0]["http_status"] == 451
    assert len(result["resolved_blockers"]) == 1
    assert result["resolved_blockers"][0]["source_run"] == records[0]["path"]


@pytest.mark.parametrize("status", [401, 403])
def test_later_success_does_not_change_authentication_or_authorization_policy(tmp_path, status):
    records = [source(tmp_path, "jev", timed_http(status, 100), run_name="denial"),
               source(tmp_path, "jev", timed_http(200, 200), run_name="success")]
    result = service_blockers_from_smoke({"runs": records})
    assert [item["http_status"] for item in result["by_architecture"]["jev"]] == [status]
    assert not result["resolved_blockers"]
