from __future__ import annotations

import argparse
import json
from pathlib import Path

from .runner import run_mode


def main():
    parser = argparse.ArgumentParser(description="Independent two-stage Jev agent for FAIRY")
    parser.add_argument("--architecture", choices=["jev", "hybrid"], default="jev")
    parser.add_argument("--mode", choices=["inventory", "sdk-smoke", "scripted", "live", "replay"], required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--workflow")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.architecture == "hybrid" and args.mode in {"scripted", "live"}:
        from fairy.experiments.episode import run_episode, verify_and_replay
        output, result = run_episode(config, args.output_dir, architecture="hybrid", mode=args.mode)
        result = verify_and_replay(output, config)
    else:
        output, result = run_mode(args.mode, config, args.output_dir, workflow_path=args.workflow)
    print(json.dumps({"output_dir": str(output), **{key: result.get(key) for key in (
        "environment_check", "scripted_loop_check", "live_integration_success", "live_coverage_status",
        "scripted_task_success", "task_success", "stop_reason", "failure_layer", "model_request_count",
        "decision_call_count", "tool_call_count", "wall_elapsed_seconds")}}, ensure_ascii=False, indent=2))
    return 1 if result.get("stop_reason") in {"runner_error", "invalid_environment", "sdk_smoke_error", "replay_error"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
