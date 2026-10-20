"""Launch the unmodified FAIRY CLI; consume its files only after process exit.

No Agent/Engine/SDK observers, tool wrappers, comparison deadlines or replay are
installed in the child. Native artifacts are authoritative and never rewritten.
"""
from __future__ import annotations

import inspect
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import uuid4

from fairy.controllers.run_artifacts import write_json

VERSION = "fairy_native_cli_v1"
ROOT = Path(__file__).resolve().parents[2]


def execute_command(command, *, cwd, env, console):
    """Forward explicit user stop signals; do not add an execution deadline."""
    with subprocess.Popen(command, cwd=cwd, env=env, stdout=console, stderr=subprocess.STDOUT) as process:
        previous = {}
        try:
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous[signum] = signal.getsignal(signum)
                signal.signal(signum, lambda received, frame: process.send_signal(received))
            return process.wait()
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def build_command(config, output):
    """Translate only public CLI options; return argv and native env overrides."""
    from fairy.scenarios.registry import get_scenario_class

    llm = config.get("llm", config.get("llm_native", {}))
    supported = {"provider", "model", "temperature", "base_url", "max_output_tokens",
                 "parallel_tool_calls", "timeout", "enable_thinking", "api_key_env", "authentication_mode"}
    unknown = set(llm) - supported
    if unknown:
        raise ValueError(f"LLM options have no native CLI mapping: {sorted(unknown)}")
    endpoint = llm.get("base_url")
    if endpoint:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Native endpoint must not contain credentials, query or fragment")
    scenario_kwargs = dict(config.get("scenario_kwargs") or {})
    if "seed" in config:
        if "seed" in scenario_kwargs and scenario_kwargs["seed"] != config["seed"]:
            raise ValueError("Conflicting scenario seed")
        scenario_kwargs["seed"] = config["seed"]
    clock = config.get("clock_source", "declared")
    if clock == "checkpoint":
        cls = get_scenario_class(config["scenario_id"])
        label = getattr(cls, "source_checkpoint_label", None)
        if not label:
            raise ValueError("Checkpoint clock requires a declared scenario checkpoint")
        checkpoint = Path(inspect.getfile(cls)).parent / "checkpoints" / f"{label}.json"
        start = json.loads(checkpoint.read_text())["sim_time"]
        for existing in (scenario_kwargs.get("start_time"), config.get("effective_start_time")):
            if existing is not None and abs(float(existing) - float(start)) > 1e-6:
                raise ValueError("Conflicting native scenario start_time")
        # This is FAIRY's public constructor option, not a patched Engine clock.
        scenario_kwargs["start_time"] = start
    elif clock != "declared":
        raise ValueError("Unknown clock_source")
    elif "effective_start_time" in config:
        actual = get_scenario_class(config["scenario_id"])(**scenario_kwargs).start_time
        if actual is None or abs(float(actual) - float(config["effective_start_time"])) > 1e-6:
            raise ValueError("effective_start_time conflicts with native declared clock")
    command = [sys.executable, "-m", "fairy.cli", "--agent", "--scenario", config["scenario_id"],
               "--output-dir", str(Path(output).resolve()),
               "--scenario-kwargs", json.dumps(scenario_kwargs, ensure_ascii=False),
               "--init-kwargs", json.dumps(config.get("init_kwargs") or {}, ensure_ascii=False)]
    for key, flag in (("provider", "--provider"), ("model", "--model"),
                      ("temperature", "--temperature"), ("base_url", "--endpoint"),
                      ("max_output_tokens", "--max-output-tokens"), ("parallel_tool_calls", "--parallel-tool-calls")):
        if key in llm:
            value = llm[key]
            command += [flag, str(value).lower() if type(value) is bool else str(value)]
    for key in ("controller", "system_prompt_mode"):
        if key in config:
            command += ["--" + key.replace("_", "-"), str(config[key])]
    native = config.get("native_cli", {})
    if set(native) - {"max_tool_calls", "timeout_seconds"}:
        raise ValueError("Unsupported native_cli option")
    for key, value in native.items():
        command += ["--" + key.replace("_", "-"), str(value)]
    env = {}
    if "timeout" in llm:
        env["FAIRY_LLM_REQUEST_TIMEOUT_S"] = str(llm["timeout"])
    if "enable_thinking" in llm:
        if llm.get("provider") != "vllm" or type(llm["enable_thinking"]) is not bool:
            raise ValueError("enable_thinking requires native vllm provider and a boolean")
        env["FAIRY_VLLM_ENABLE_THINKING"] = str(llm["enable_thinking"]).lower()
    # Credentials are inherited via the native provider's own variable names.
    # Never copy a key into argv, metadata or an alternate provider variable.
    native_keys = {"vllm": {"VLLM_API_KEY"}, "openai": {"OPENAI_API_KEY"}, "deepseek": {"DEEPSEEK_API_KEY"},
                   "qwen": {"QWEN_API_KEY", "DASHSCOPE_API_KEY"},
                   "openai-compatible": {"LLAMA_API_KEY", "OPENAI_API_KEY"}}.get(llm.get("provider"), set())
    if llm.get("api_key_env") and llm["api_key_env"] not in native_keys:
        raise ValueError(f"Use a native provider credential variable {sorted(native_keys)}, not a comparison-only alias")
    return command, env


def native_artifacts(output, scenario_id):
    return {key: str(output / f"{scenario_id}.{suffix}") for key, suffix in (
        ("progress_log", "progress.jsonl"), ("runtime_log", "runtime_log.jsonl"),
        ("agent_workflow", "agent_workflow.json"), ("run_report", "run_report.json"))}


def verify_native_files(output):
    """Read-only integrity check, not a replay or task acceptance check."""
    output = Path(output)
    manifest = json.loads((output / "manifest.json").read_text())
    checks = []
    for key, name in manifest["native_artifacts"].items():
        path = Path(name)
        try:
            if path.suffix == ".jsonl":
                for line in path.read_text().splitlines():
                    if line.strip():
                        json.loads(line)
            else:
                json.loads(path.read_text())
            checks.append({"artifact": key, "passed": True})
        except (OSError, ValueError) as exc:
            checks.append({"artifact": key, "passed": False, "error": str(exc)})
    return {"passed": all(c["passed"] for c in checks), "checks": checks,
            "scope": "native artifact presence and JSON syntax only; no custom trace or replay"}


def native_service_blockers(output):
    """Classify explicit native client errors, without inventing HTTP codes."""
    manifest = json.loads((Path(output) / "manifest.json").read_text())
    path = Path(manifest["native_artifacts"]["runtime_log"])
    if not path.exists():
        return []
    failed = None
    network_errors = {"APIConnectionError", "APITimeoutError", "AuthenticationError", "PermissionDeniedError",
                      "RateLimitError", "InternalServerError", "ConnectError", "ProxyError", "ConnectTimeout"}
    for line in path.read_text().splitlines():
        row = json.loads(line)
        if row.get("record_type") == "llm_call_end":
            failed = None
        elif row.get("record_type") == "llm_call_error" and row.get("error_type") in network_errors:
            failed = {"service": "qwen", "http_status": None, "error_type": row["error_type"],
                      "reason": "current_native_cli_service_error", "source_path": str(path),
                      "record_index": row.get("record_index")}
    return [failed] if failed else []


def run_native_cli(config, output_dir, *, stage="debug", experiment_id=None, slot=None,
                   ledger=None, require_clean=False, external_blockers=None):
    from .episode import artifact_hashes, file_hash, git
    from .budget import BudgetStop, LiveLedger

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_llm_native_cli_" + uuid4().hex[:8]
    output = Path(output_dir).resolve() / run_id
    command, env_overrides = build_command(config, output)
    # CLI performs repo-side export cleanup relative to cwd. Use an empty cwd
    # so original exports and concurrent experiments cannot be removed.
    output.mkdir(parents=True, exist_ok=False)
    cwd = output / "cli_workdir"
    cwd.mkdir()
    status = git("status", "--porcelain")
    manifest = {"run_id": run_id, "architecture": "llm_native", "mode": "live",
                "implementation_version": VERSION, "launch_mode": VERSION,
                "experiment_id": experiment_id, "slot": slot, "stage": stage,
                "code_commit": git("rev-parse", "HEAD"), "source_clean": not bool(status),
                "command": command, "cwd": str(cwd), "environment_overrides": env_overrides,
                "native_artifacts": native_artifacts(output, config["scenario_id"]),
                "scenario_id": config["scenario_id"],
                "source_hashes": {p: file_hash(ROOT / p) for p in (
                    "fairy/cli.py", "fairy/controllers/engine.py", "fairy/agents/agent/agent.py",
                    "fairy/agents/llm/base_llm.py", "fairy/agents/llm/vllm_client.py")},
                "comparison_runtime_installed": False,
                "comparison_limits_applied": False,
                "evaluation_profile": "fairy_cli_native_report",
                "http_accounting": "unobserved; native SDK retries are not instrumented"}
    write_json(output / "manifest.json", manifest)
    write_json(output / "metrics.json", {"model_http_attempts": None, "input_tokens": None,
               "output_tokens": None, "cost": {"status": "unavailable", "amount": None}})
    result = {"architecture": "llm_native", "implementation_version": VERSION,
              "execution_status": "blocked", "task_success": None, "live_integration_success": None,
              "scripted_task_success": None, "task_assertions": {}, "native_validation": None,
              "model_http_attempts": None, "model_request_count": None, "tool_call_count": None,
              "stop_reason": None, "failure_layer": None, "wall_elapsed_seconds": None,
              "acceptance_scope": "native report retained; historical custom acceptance not run"}
    started = time.monotonic()
    reserved = False
    try:
        if require_clean and status:
            raise ValueError("formal_source_not_clean")
        if external_blockers:
            result.update(stop_reason="preflight_blocked", blockers=external_blockers)
        else:
            if ledger is None:
                ledger = LiveLedger(ROOT / config.get("global_live_ledger", "runs/agent_comparison/live_budget_20261018.json"))
            ledger.reserve_episode(run_id, "llm_native", stage, slot or run_id)
            reserved = True
            ledger.mark_native_cli(run_id)
            env = {**os.environ, **env_overrides, "PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
            with (output / "console.log").open("x") as console:
                # No subprocess timeout: the original Agent owns its own limits.
                exit_code = execute_command(command, cwd=cwd, env=env, console=console)
            manifest["exit_code"] = exit_code
            report_path = Path(manifest["native_artifacts"]["run_report"])
            report = json.loads(report_path.read_text()) if report_path.exists() else {}
            metrics = report.get("metrics", {})
            result.update(execution_status=report.get("status", "integration_error"),
                          stop_reason=report.get("stopped_reason", "native_cli_exit_without_report"),
                          native_validation=(report.get("evaluation") or {}).get("validation"),
                          native_report=str(report_path), native_exit_code=exit_code,
                          model_request_count=metrics.get("llm_call_count"),
                          tool_call_count=metrics.get("tool_call_count"),
                          wall_elapsed_seconds=report.get("wall_time_seconds"),
                          native_error=report.get("error"))
            if exit_code != 0:
                result["execution_status"] = "integration_error"
                result["failure_layer"] = "native_cli"
            result["trace_verification"] = verify_native_files(output)
            if not result["trace_verification"]["passed"]:
                result["execution_status"] = "integration_error"
                result["failure_layer"] = "native_artifacts"
            if exit_code in {-signal.SIGINT, -signal.SIGTERM}:
                result.update(execution_status="interrupted_by_user", stop_reason="termination_signal",
                              failure_layer=None, partial_evidence_preserved=True)
            result["replay_verification"] = {"status": "not_run", "reason": "native CLI artifacts only; no custom replay"}
            usage = metrics.get("token_usage", {})
            write_json(output / "metrics.json", {**metrics, "model_http_attempts": None,
                       "input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"),
                       "source": str(report_path), "cost": {"status": "unavailable", "amount": None}})
    except (Exception, BudgetStop) as exc:
        # Keep original CLI console/files; outer failures are metadata only.
        result.update(stop_reason=getattr(exc, "reason", type(exc).__name__), error=str(exc))
        if reserved:
            result.update(execution_status="integration_error", failure_layer="native_cli_launcher")
    finally:
        result["launcher_wall_seconds"] = time.monotonic() - started
        if reserved:
            ledger.finish(run_id, result["execution_status"])
        write_json(output / "manifest.json", manifest)
        write_json(output / "evaluation.json", result)
        artifact_hashes(output)
    return output, result
