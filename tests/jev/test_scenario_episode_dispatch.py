"""Cross-scenario runner assessment dispatch, with all network sends forbidden."""
from __future__ import annotations

import json
from pathlib import Path
import socket

from openai.types.chat import ChatCompletion
import pytest

from fairy.agents.Jev import replay as replay_module
from fairy.agents.Jev.scripted import ScriptedPolicy
from fairy.experiments import episode
from fairy.experiments.scripted_native import NativeFixtureLLM


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("dispatch tests must not use network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def harvest_config():
    config = json.loads(Path("configs/jev/hn58_hybrid_sync_k4_qwen.json").read_text())
    config.update(
        scenario_id="scenario_l1_hb_r5_leaf_feeder_harvest_ready",
        evaluation_profile="scenario_native_v1",
    )
    return config


class WeatherOnlyPolicy(ScriptedPolicy):
    sequence = ["WeatherApp__get_current_weather", "FINISH"]


class WeatherOnlyNative(NativeFixtureLLM):
    def chat_completion(self, messages, tools=None):
        self.index += 1
        self.last_request_id = f"scripted_native_{self.index:04d}"
        message = {"role": "assistant", "content": "Weather observed; task completion unverified."}
        if self.index == 1:
            message["tool_calls"] = [{
                "id": "weather_fixture_1", "type": "function",
                "function": {"name": "WeatherApp__get_current_weather", "arguments": "{}"},
            }]
        return ChatCompletion.model_validate({
            "id": self.last_request_id, "created": 0, "object": "chat.completion",
            "model": self.model, "choices": [{"index": 0,
                "finish_reason": "tool_calls" if self.index == 1 else "stop", "message": message}],
        })


@pytest.mark.parametrize("architecture", ["llm_native", "jev", "hybrid"])
def test_non_irrigation_episode_and_replay_do_not_apply_hn58_contract(tmp_path, monkeypatch, architecture):
    def hn58_forbidden(*args, **kwargs):
        raise AssertionError("HN58 assessment must not run for the generic profile")
    monkeypatch.setattr(episode, "evaluate_task", hn58_forbidden)
    monkeypatch.setattr(episode, "semantic_first_issue", hn58_forbidden)
    monkeypatch.setattr(episode, "apply_semantic_contract", hn58_forbidden)
    monkeypatch.setattr(replay_module, "evaluate_task", hn58_forbidden)
    monkeypatch.setattr(episode, "ScriptedPolicy", WeatherOnlyPolicy)
    config = harvest_config()
    output, result = episode.run_episode(
        config, tmp_path, architecture=architecture, mode="scripted",
        offline_native_llm=lambda runtime: WeatherOnlyNative(runtime),
    )
    assert result["execution_status"] == "completed"
    assert result["tool_call_count"] == 1
    assert result["task_success"] is result["scripted_task_success"] is None
    assert result["assertions"] == result["task_assertions"] == {}
    assert result["first_issue"] is None
    assert result["scripted_loop_check"] == "not_assessed"
    assert result["model_http_attempts"] == 0
    assert "evaluation_error" not in result
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["evaluation_profile"] == manifest["evaluator_version"] == "scenario_native_v1"
    assert manifest["evaluator_hash"] == episode.file_hash(episode.ROOT / "fairy/experiments/scenario_evaluation.py")
    verified = episode.verify_and_replay(output, config)
    assert verified["trace_verification"]["passed"] is True
    assert verified["replay_verification"]["passed"] is True
    assert verified["replay_verification"]["task_success"] is None
    replayed = json.loads((output / "replay/evaluation.json").read_text())
    assert replayed["model_request_count"] == 0
    assert replayed["task_success"] is replayed["replay_task_success"] is None
    assert all(replayed["replay"]["effect_comparisons"].values())
    assert "native_validation" in replayed
    assert not (output / "replay/evaluation_semantic_v2.json").exists()


def test_replay_rejects_evaluation_profile_change_before_environment_creation(tmp_path, monkeypatch):
    (tmp_path / "manifest.json").write_text(json.dumps({
        "config": {"evaluation_profile": "scenario_native_v1"},
    }))
    monkeypatch.setattr(replay_module, "create_fixture", lambda *args: pytest.fail("conflict loaded environment"))
    with pytest.raises(ValueError, match="evaluation_profile"):
        replay_module.replay_run(
            tmp_path / "workflow.json", {"evaluation_profile": "hn58_semantic_v2"}, tmp_path / "replay",
        )
    assert not (tmp_path / "replay").exists()


def test_old_manifest_does_not_allow_silent_profile_change(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"config": {}}))
    with pytest.raises(ValueError, match="evaluation_profile"):
        replay_module.replay_run(
            tmp_path / "workflow.json", {"evaluation_profile": "scenario_native_v1"}, tmp_path / "replay",
        )


def test_invalid_profile_is_rejected_before_creating_run_evidence(tmp_path):
    config = {**harvest_config(), "evaluation_profile": "typo"}
    with pytest.raises(ValueError, match="Unknown evaluation_profile"):
        episode.run_episode(config, tmp_path, mode="scripted")
    assert list(tmp_path.iterdir()) == []


def test_default_assessment_remains_hn58():
    evaluator, diagnosis, version, source = episode.evaluation_adapter({})
    assert evaluator is episode.evaluate_task
    assert diagnosis is episode.semantic_first_issue
    assert version == "hn58_semantic_v2"
    assert source == episode.ROOT / "fairy/experiments/evaluation.py"
