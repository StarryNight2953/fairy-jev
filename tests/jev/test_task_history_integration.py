"""Exercise both runner entry points without model/network access."""
import json
import socket
from pathlib import Path

import pytest

from fairy.agents.Jev.runner import run_mode
from fairy.experiments.episode import run_episode, verify_and_replay


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("State strategy regression must stay offline")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def config():
    return json.loads(Path("configs/jev/hn58_task_history.json").read_text())


def test_comparison_runner_records_second_strategy_and_replays_tools(tmp_path):
    cfg = config()
    output, result = run_episode(cfg, tmp_path, architecture="jev", mode="scripted")
    checked = verify_and_replay(output, cfg)
    manifest = json.loads((output / "manifest.json").read_text())
    events = [json.loads(line) for line in (output / "trace.jsonl").read_text().splitlines()]
    decisions = [e for e in events if e["event"] == "decision_start"]
    assert manifest["state_strategy"] == "task_history_v1"
    assert manifest["implementation_version"] == "jev_task_history_v1_observed"
    assert all(e["state_strategy"] == "task_history_v1" for e in decisions)
    assert len(decisions[-1]["state"]["actions_so_far"]) == result["tool_call_count"] == 10
    assert result["scripted_task_success"] is True
    assert result["live_integration_success"] is None
    assert result["model_http_attempts"] == 0
    assert checked["trace_verification"]["passed"]
    assert checked["replay_verification"]["passed"]


def test_standalone_runner_accepts_second_strategy(tmp_path):
    output, result = run_mode("scripted", config(), tmp_path)
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["state_strategy"] == "task_history_v1"
    assert result["scripted_loop_check"] == "passed"
    assert result["model_request_count"] == 0


@pytest.mark.parametrize("architecture", ["hybrid", "llm_native"])
def test_other_architectures_reject_new_pure_strategy_before_setup(tmp_path, architecture):
    with pytest.raises(ValueError, match="only by pure Jev"):
        run_episode(config(), tmp_path, architecture=architecture, mode="scripted")
    assert not list(tmp_path.iterdir())


def test_alternative_template_only_changes_projection_and_identity():
    original = json.loads(Path("configs/jev/hn58_irrigation.json").read_text())
    alternative = config()
    for key in ("state_strategy", "implementation_version", "configuration_note"):
        alternative.pop(key)
    assert alternative == original
