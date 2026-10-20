"""Native CLI routing and original artifact contract; no remote connections."""
from copy import deepcopy
import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

import pytest
from openai.types.chat import ChatCompletion

from fairy.experiments import episode, native_cli
from fairy.experiments.budget import LiveLedger

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("Network forbidden")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def config():
    cfg = json.loads((ROOT / "configs/jev/hn58_hybrid_sync_k4_qwen.json").read_text())
    cfg["llm"] = json.loads((ROOT / "configs/llm/qwen_current.json").read_text())
    return cfg


def test_command_uses_public_native_options_not_experiment_limits(tmp_path):
    cfg = config()
    original = deepcopy(cfg)
    cfg["experiment_limits"] = {"max_tool_attempts": 1, "wall_timeout_seconds": 1}
    argv, env = native_cli.build_command(cfg, tmp_path)
    assert argv[:4] == [sys.executable, "-m", "fairy.cli", "--agent"]
    assert "--max-tool-calls" not in argv and "--timeout-seconds" not in argv
    kwargs = json.loads(argv[argv.index("--scenario-kwargs") + 1])
    assert kwargs["seed"] == 1904 and kwargs["start_time"] == pytest.approx(1786057131.7453332)
    assert env == {"FAIRY_LLM_REQUEST_TIMEOUT_S": "1800", "FAIRY_VLLM_ENABLE_THINKING": "false"}
    assert cfg["llm"] == original["llm"]
    cfg["native_cli"] = {"max_tool_calls": 7, "timeout_seconds": 7200}
    argv, _ = native_cli.build_command(cfg, tmp_path)
    assert argv[argv.index("--max-tool-calls") + 1] == "7"


@pytest.mark.parametrize("change", [
    {"api_key": "do-not-record"}, {"base_url": "http://user:secret@example.invalid/v1"},
    {"api_key_env": "COMPARISON_ALIAS"}, {"unmapped_sampling_option": 42},
])
def test_no_silent_unmapped_options_or_secret_arguments(tmp_path, change):
    cfg = config()
    cfg["llm"].update(change)
    with pytest.raises(ValueError):
        native_cli.build_command(cfg, tmp_path)


def test_live_dispatch_skips_all_comparison_runtime_construction(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Comparison runtime reached")
    monkeypatch.setattr(episode, "create_fixture", forbidden)
    monkeypatch.setattr(episode, "EpisodeRuntime", forbidden)
    monkeypatch.setattr(episode, "TraceWriter", forbidden)
    expected = (tmp_path, {"native": True})
    monkeypatch.setattr(native_cli, "run_native_cli", lambda *args, **kwargs: expected)
    assert episode.run_episode(config(), tmp_path, architecture="llm_native") == expected
    with pytest.raises(ValueError, match="inject"):
        episode.run_episode(config(), tmp_path, architecture="llm_native", offline_native_llm=object())


def test_real_cli_with_fake_model_writes_original_logs_and_executes_tool(tmp_path, monkeypatch):
    from fairy import cli
    from fairy.agents.llm.base_llm import BaseLLM
    from fairy.experiments import native, transports
    cfg = config()
    requests = []
    def chat(messages, tools=None):
        requests.append(deepcopy(messages))
        calls = [{"id": "native-tool", "type": "function", "function": {
            "name": "FarmWorldApp__get_inventory", "arguments": "{}"}}] if len(requests) == 1 else []
        return ChatCompletion.model_validate({"id": "offline", "object": "chat.completion",
            "created": 1, "model": "offline", "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop",
                         "message": {"role": "assistant", "content": "offline", "tool_calls": calls}}]})
    model = SimpleNamespace(provider="offline", model="offline", parallel_tool_calls=True,
                            max_output_tokens=2048, chat_completion=chat)
    monkeypatch.setattr(BaseLLM, "llm_builder", lambda settings: model)
    def forbidden(*args, **kwargs):
        pytest.fail("Comparison observer/runtime/fixture reached native CLI")
    monkeypatch.setattr(native, "attach_native_observers", forbidden)
    monkeypatch.setattr(transports, "build_instrumented_llm", forbidden)
    monkeypatch.setattr(episode, "create_fixture", forbidden)
    monkeypatch.setattr(episode, "EpisodeRuntime", forbidden)
    monkeypatch.setattr(episode, "TraceWriter", forbidden)
    # Invoke the actual CLI entrypoint in-process solely to inject the offline
    # model. Production execute_command is a separate unmodified CLI process.
    def execute(argv, *, cwd, env, console):
        with monkeypatch.context() as m:
            m.chdir(cwd)
            m.setattr(sys, "argv", ["fairy.cli", *argv[3:]])
            cli.main()
        return 0
    monkeypatch.setattr(native_cli, "execute_command", execute)
    ledger = LiveLedger(tmp_path / "ledger.json")
    output, result = episode.run_episode(cfg, tmp_path / "runs", architecture="llm_native", ledger=ledger)
    assert result["execution_status"] == "completed", result
    assert result["model_request_count"] == 2 and result["tool_call_count"] == 1
    assert result["model_http_attempts"] is None
    assert result["task_success"] is result["live_integration_success"] is None
    assert result["trace_verification"]["passed"]
    assert not (output / "trace.jsonl").exists()
    assert not (output / "workflow.json").exists()
    assert not (output / "replay").exists()
    manifest = json.loads((output / "manifest.json").read_text())
    report = json.loads(Path(manifest["native_artifacts"]["run_report"]).read_text())
    assert report["limits"]["max_tool_calls"] == 300
    assert report["limits"]["timeout_seconds"] == 7200
    assert report["seed"] == 1904
    assert report["start_time"] == pytest.approx(1786057131.7453332)
    rows = [json.loads(line) for line in Path(manifest["native_artifacts"]["runtime_log"]).read_text().splitlines()]
    assert any(r["record_type"] == "tool_call_end" for r in rows)
    assert any(r["record_type"] == "llm_call_start" and "messages" in r for r in rows)
    assert any(m["role"] == "tool" for m in requests[1])
    original_bytes = {name: Path(name).read_bytes() for name in manifest["native_artifacts"].values()}
    assert episode.verify_and_replay(output, cfg) == result
    assert all(Path(name).read_bytes() == content for name, content in original_bytes.items())
    snapshot = ledger.snapshot()
    assert snapshot["episodes"][0]["http_attempts"] is None
    assert snapshot["http_attempts_complete"] is False
    from fairy.experiments.access import service_blockers_from_smoke
    access = service_blockers_from_smoke({"runs": [{"architecture": "llm_native", "path": str(output)}]})
    assert access["by_architecture"]["llm_native"] == []


def test_cli_failure_preserves_console_and_does_not_retry(tmp_path, monkeypatch):
    count = []
    def fail(command, *, cwd, env, console):
        count.append(command)
        console.write("original native error\n")
        return 1
    monkeypatch.setattr(native_cli, "execute_command", fail)
    output, result = native_cli.run_native_cli(config(), tmp_path / "runs", ledger=LiveLedger(tmp_path / "ledger.json"))
    assert len(count) == 1
    assert result["execution_status"] == "integration_error"
    assert result["model_http_attempts"] is None
    assert (output / "console.log").read_text() == "original native error\n"


def test_signal_forwarding_without_timeout_or_transport_wrapping(tmp_path, monkeypatch):
    import signal
    observed = {}
    handlers = {}
    class Process:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def send_signal(self, signum): observed["signal"] = signum
        def wait(self):
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return -signal.SIGTERM
    def popen(argv, **kwargs):
        observed.update(argv=argv, kwargs=kwargs)
        return Process()
    monkeypatch.setattr(native_cli.subprocess, "Popen", popen)
    monkeypatch.setattr(signal, "getsignal", lambda sig: "original")
    monkeypatch.setattr(signal, "signal", lambda sig, handler: handlers.update({sig: handler}))
    assert native_cli.execute_command(["test"], cwd=tmp_path, env={}, console=None) == -signal.SIGTERM
    assert observed["signal"] == signal.SIGTERM
    assert "timeout" not in observed["kwargs"]
    assert all(handler == "original" for handler in handlers.values())


def test_explicit_termination_keeps_partial_native_files(tmp_path, monkeypatch):
    import signal
    def interrupted(command, *, cwd, env, console):
        console.write("partial native output\n")
        return -signal.SIGTERM
    monkeypatch.setattr(native_cli, "execute_command", interrupted)
    output, result = native_cli.run_native_cli(config(), tmp_path / "runs", ledger=LiveLedger(tmp_path / "ledger.json"))
    assert result["execution_status"] == "interrupted_by_user"
    assert result["partial_evidence_preserved"] is True
    assert (output / "console.log").read_text() == "partial native output\n"


def test_native_service_error_is_not_synthesized_as_http_response(tmp_path):
    log = tmp_path / "native.runtime_log.jsonl"
    (tmp_path / "manifest.json").write_text(json.dumps({"native_artifacts": {"runtime_log": str(log)}}))
    log.write_text(json.dumps({"record_type": "llm_call_error", "record_index": 3,
                               "error_type": "APIConnectionError"}) + "\n")
    blockers = native_cli.native_service_blockers(tmp_path)
    assert blockers[0]["http_status"] is None
    assert blockers[0]["record_index"] == 3
    with log.open("a") as f:
        f.write(json.dumps({"record_type": "llm_call_end", "record_index": 5}) + "\n")
    assert native_cli.native_service_blockers(tmp_path) == []
