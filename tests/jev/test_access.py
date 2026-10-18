import json
from pathlib import Path

import pytest

from fairy.agents.Jev.trace import write_json
from fairy.experiments.access import service_blockers_from_smoke


def source(tmp_path, architecture, events, *, integration=None, task=None, llm_model="configured-model"):
    root = tmp_path / architecture
    root.mkdir()
    write_json(root / "manifest.json", {"architecture": architecture, "config": {
        "model": "jev-1.13.0", "base_url": "https://api.typesafe.ai", "api_key_env": "TYPESAFE_API_KEY",
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
