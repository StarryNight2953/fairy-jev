"""Read-only observation of a registered batch; never starts or retries a sample.

Only derived reports under --output are written. Traces are streamed after a
result record appears, and cached by size/mtime. Unknown semantic task outcomes
stay unknown, regardless of the native validator or workflow similarity.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shlex
import statistics
import subprocess
import time
from zoneinfo import ZoneInfo


def now():
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, path)


def variant_of(row):
    return row.get("variant") or row.get("architecture")


def error_ref(event, path, line):
    return {"trace": str(path), "line": line, "event_id": event.get("event_id"),
            "event": event.get("event"), "role": event.get("role"),
            "tool_name": event.get("tool_name"), "status": event.get("status"),
            "http_status": event.get("http_status"), "error_type": event.get("error_type"),
            "step_id": event.get("step_id"), "attempt_id": event.get("attempt_id")}


def scan_trace(path):
    """Stream one immutable trace, retaining references instead of raw payloads."""
    path = Path(path)
    result = {"http_attempts": 0, "http_errors": 0, "http_400_max_tokens_exceeded": 0,
              "read_timeout_attempts": 0, "tool_errors": 0, "tool_attempts": 0,
              "http_elapsed_seconds": 0.0, "http_by_role": {},
              "first_http_error": None, "first_tool_error": None,
              "first_error": None, "incomplete_final_line": False, "invalid_line": None}
    roles = Counter()
    with path.open() as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.endswith("\n"):
                result["incomplete_final_line"] = True
                break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                result["invalid_line"] = line_number
                break
            if not isinstance(event, dict):
                result["invalid_line"] = line_number
                break
            kind = event.get("event")
            ref = error_ref(event, path, line_number)
            if kind == "http_attempt_end":
                result["http_attempts"] += 1
                roles[str(event.get("role", "unknown"))] += 1
                result["http_elapsed_seconds"] += event.get("wall_elapsed_seconds") or 0.0
                bad = event.get("status") != "ok"
                if bad:
                    result["http_errors"] += 1
                    result["first_http_error"] = result["first_http_error"] or ref
                    result["first_error"] = result["first_error"] or ref
                # Service error codes are carried in raw/parsed response bodies.
                # Search only the failure response, never the request/task text.
                response = json.dumps(event.get("parsed_response"), ensure_ascii=False) + str(event.get("raw_response_body", ""))
                if event.get("http_status") == 400 and "max_tokens_exceeded" in response:
                    result["http_400_max_tokens_exceeded"] += 1
                if bad and event.get("error_type") == "ReadTimeout":
                    result["read_timeout_attempts"] += 1
            elif kind == "tool_end":
                result["tool_attempts"] += 1
                feedback = event.get("result")
                bad = (event.get("status") in {"error", "failed", "tool_error"}
                       or bool(event.get("error"))
                       or isinstance(feedback, dict) and (feedback.get("status") in {"error", "failed"} or bool(feedback.get("error"))))
                if bad:
                    result["tool_errors"] += 1
                    result["first_tool_error"] = result["first_tool_error"] or ref
                    result["first_error"] = result["first_error"] or ref
    result["http_by_role"] = dict(roles)
    return result


def cached_trace(path, cache):
    path = Path(path)
    if not path.is_file():
        return None
    stat = path.stat()
    identity = [stat.st_size, stat.st_mtime_ns]
    key = str(path.resolve())
    saved = cache.get(key)
    if saved is None or saved.get("identity") != identity:
        saved = {"identity": identity, "summary": scan_trace(path)}
        cache[key] = saved
    return saved["summary"]


def result_rows(out, slots, cache):
    rows = []
    for slot in slots:
        record_path = out / "results" / f"{slot['slot']}.json"
        record = read_json(record_path)
        if not isinstance(record, dict):
            continue
        evaluation = record.get("evaluation") or record
        run_path = Path(record["path"]) if record.get("path") else None
        metrics = read_json(run_path / "metrics.json", {}) if run_path else {}
        # The worker publishes results only after its live loop ends. A live
        # trace without a result record is deliberately never inspected here.
        trace = cached_trace(run_path / "trace.jsonl", cache) if run_path else None
        weak = (evaluation.get("weak_contract") is True
                or evaluation.get("evaluation_status") == "observational_only"
                or evaluation.get("task_success_status") == "not_independently_assessed")
        task = evaluation.get("task_success")
        if weak or task not in (True, False):
            task = None
        rows.append({"slot": slot["slot"], "scenario_id": slot["scenario_id"],
                     "level": slot.get("level"), "variant": variant_of(slot),
                     "architecture": slot["architecture"], "state_strategy": slot.get("state_strategy"),
                     "execution_status": evaluation.get("execution_status"),
                     "live_integration_success": evaluation.get("live_integration_success"),
                     "task_success": task, "raw_task_success": evaluation.get("task_success"),
                     "evaluation_profile": evaluation.get("evaluator_version"),
                     "native_contract_success": evaluation.get("native_contract_success", (evaluation.get("native_validation") or {}).get("success")),
                     "stop_reason": evaluation.get("stop_reason"), "failure_layer": evaluation.get("failure_layer"),
                     "model_http_attempts": evaluation.get("model_http_attempts", evaluation.get("new_model_http_attempts", 0)),
                     "tool_call_count": evaluation.get("tool_call_count", 0),
                     "wall_elapsed_seconds": evaluation.get("wall_elapsed_seconds"),
                     "input_tokens": metrics.get("input_tokens"), "output_tokens": metrics.get("output_tokens"),
                     "first_issue": evaluation.get("first_issue"), "task_assertions": evaluation.get("task_assertions", evaluation.get("assertions")),
                     "workflow_metrics": evaluation.get("workflow_metrics"),
                     "trace_verification": (evaluation.get("trace_verification") or {}).get("passed"),
                     "replay_verification": (evaluation.get("replay_verification") or {}).get("passed"),
                     "trace_observation": trace, "partial_evidence_preserved": record.get("partial_evidence_preserved", False),
                     "run_path": str(run_path) if run_path else None, "record_path": str(record_path)})
    return rows


def grouped(rows, slots):
    groups = {}
    for variant in dict.fromkeys(variant_of(s) for s in slots):
        selected = [r for r in rows if r["variant"] == variant]
        def tri(field):
            return {"true": sum(r.get(field) is True for r in selected),
                    "false": sum(r.get(field) is False for r in selected),
                    "unknown": sum(r.get(field) is not True and r.get(field) is not False for r in selected)}
        wall = [r["wall_elapsed_seconds"] for r in selected if isinstance(r.get("wall_elapsed_seconds"), (int, float))]
        errors = {}
        for field in ("http_400_max_tokens_exceeded", "read_timeout_attempts", "http_errors", "tool_errors"):
            errors[field] = {"attempts": sum((r.get("trace_observation") or {}).get(field, 0) for r in selected),
                             "runs": sum((r.get("trace_observation") or {}).get(field, 0) > 0 for r in selected)}
        groups[variant] = {"planned": sum(variant_of(s) == variant for s in slots), "recorded": len(selected),
                           "execution_status_counts": dict(Counter(str(r["execution_status"]) for r in selected)),
                           "stop_reason_counts": dict(Counter(str(r["stop_reason"]) for r in selected)),
                           "task_success": tri("task_success"), "live_integration_success": tri("live_integration_success"),
                           "model_http_attempts": sum(r.get("model_http_attempts") or 0 for r in selected),
                           "tool_calls": sum(r.get("tool_call_count") or 0 for r in selected),
                           "reported_input_tokens": sum(r.get("input_tokens") or 0 for r in selected),
                           "reported_output_tokens": sum(r.get("output_tokens") or 0 for r in selected),
                           "token_metrics_missing_runs": sum(r.get("input_tokens") is None or r.get("output_tokens") is None for r in selected),
                           "wall_seconds": {"known_runs": len(wall), "total": sum(wall), "median": statistics.median(wall) if wall else None},
                           "errors": errors}
    return groups


def baseline_comparison(rows, baseline, cache):
    if baseline is None:
        return {"batch": None, "matched": [], "interpretation": "No historical baseline supplied."}
    baseline = Path(baseline).resolve()
    old_pre = read_json(baseline / "preregistration.json", {})
    needed = {(row["scenario_id"], "jev" if row["variant"] == "jev_task_history" else row["variant"])
              for row in rows}
    old_slots = [slot for slot in old_pre.get("slots", [])
                 if (slot["scenario_id"], variant_of(slot)) in needed]
    old_rows = result_rows(baseline, old_slots, cache)
    by_key = {(r["scenario_id"], r["variant"]): r for r in old_rows}
    comparisons = []
    fields = ("task_success", "live_integration_success", "execution_status", "stop_reason",
              "model_http_attempts", "tool_call_count", "wall_elapsed_seconds", "input_tokens", "output_tokens")
    for row in rows:
        old_variant = "jev" if row["variant"] == "jev_task_history" else row["variant"]
        old = by_key.get((row["scenario_id"], old_variant))
        if old is None:
            continue
        comparisons.append({"scenario_id": row["scenario_id"], "new_variant": row["variant"],
                            "old_variant": old_variant, "architecture": row["architecture"],
                            "baseline_mapping": "task_history_v1 compared descriptively with historical default Jev" if row["variant"] == "jev_task_history" else "same scenario and architecture",
                            "new": {k: row[k] for k in fields}, "old": {k: old[k] for k in fields},
                            "new_trace_errors": row.get("trace_observation"), "old_trace_errors": old.get("trace_observation"),
                            "new_record": row["record_path"], "old_record": old["record_path"]})
    return {"batch": str(baseline), "matched": comparisons,
            "interpretation": "Historical outcomes are comparison-only, never reused as new samples. Models, timeout, task inputs and numeric candidates may differ; one sample is not a causal test or success-rate estimate."}


def scheduler_alive(pid, out):
    """Protect against PID reuse: require the exact module, action and output."""
    completed = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False)
    try:
        args = shlex.split(completed.stdout.strip())
        index = args.index("fairy.experiments.full_suite")
        target = args[args.index("--output") + 1]
        return (completed.returncode == 0 and index > 0 and args[index - 1] == "-m"
                and args[index + 1] == "run" and Path(target).resolve() == Path(out).resolve())
    except (ValueError, IndexError):
        return False


def render(summary):
    progress = summary["progress"]
    lines = ["# L1 真实运行观察", "", f"更新：{summary['updated_at']}；批次：`{summary['experiment_id']}`。",
             f"状态：**{summary['status']}**；已记录 {summary['recorded']}/{summary['planned']}；等待 {progress.get('pending', 'unknown')}；活动 {len(progress.get('active') or [])}。", "",
             "每个场景、每个实现只运行一次。这里只汇总已写出结果的运行；活动中的请求不作为最终结果。`completed` 表示批次槽位已记录，不代表任务成功。",
             "除 HN58 的现有独立验收外，其他场景的 `task_success` 保持 unknown；原生验证与轨迹相似度不能代替语义任务验收。", "",
             "| 实现 | 已记录/计划 | 集成 true/false/unknown | 任务 true/false/unknown | HTTP | 工具 | 上下文400 次数/运行 | ReadTimeout 次数/运行 | 中位耗时秒 |",
             "| --- | ---: | --- | --- | ---: | ---: | --- | --- | ---: |"]
    for variant, group in summary["groups"].items():
        tri = lambda field: "/".join(str(group[field][v]) for v in ("true", "false", "unknown"))
        err = lambda field: f"{group['errors'][field]['attempts']}/{group['errors'][field]['runs']}"
        median = group["wall_seconds"]["median"]
        lines.append(f"| {variant} | {group['recorded']}/{group['planned']} | {tri('live_integration_success')} | {tri('task_success')} | {group['model_http_attempts']} | {group['tool_calls']} | {err('http_400_max_tokens_exceeded')} | {err('read_timeout_attempts')} | {median:.3f} |" if median is not None else f"| {variant} | {group['recorded']}/{group['planned']} | {tri('live_integration_success')} | {tri('task_success')} | {group['model_http_attempts']} | {group['tool_calls']} | {err('http_400_max_tokens_exceeded')} | {err('read_timeout_attempts')} | unknown |")
    lines += ["", "各实现的停止原因：", ""]
    for variant, group in summary["groups"].items():
        lines.append(f"- `{variant}`：`{json.dumps(group['stop_reason_counts'], ensure_ascii=False)}`；已报告输入/输出 tokens {group['reported_input_tokens']}/{group['reported_output_tokens']}，缺失用量运行 {group['token_metrics_missing_runs']}。")
    lines += ["", f"历史对照已匹配 {len(summary['baseline']['matched'])} 组；仅比较相同场景与架构。新增 `jev_task_history` 明确对照历史默认 Jev。历史结果未计入本轮样本，未开始/中断不当作任务失败。",
              "历史到当前的模型、超时、任务输入或候选空间可能同时变化；单次结果差异不证明因果或架构总体优劣，耗时也不直接证明效率提升。费用未知。", "",
              "逐组断言、首次 HTTP/工具错误的位置、用量、耗时及逐组历史对照见 [observation_summary.json](observation_summary.json)。原始请求、响应和工具反馈保存在各运行 `trace.jsonl`，位置见 [evidence_index.json](evidence_index.json)。", "",
              "监测器不调用模型、不执行工具、不修改 Agent、配置、历史批次或预算，不重启任何槽位。"]
    if progress.get("active"):
        lines += ["", "活动槽位：", ""] + [f"- `{slot}`" for slot in progress["active"]]
    return "\n".join(lines) + "\n"


def refresh(out, *, baseline=None, scheduler_running=None):
    out = Path(out).resolve()
    pre = read_json(out / "preregistration.json")
    if not isinstance(pre, dict) or not isinstance(pre.get("slots"), list):
        raise ValueError("A registered batch with slots is required")
    cache = read_json(out / ".observation_trace_cache.json", {})
    rows = result_rows(out, pre["slots"], cache)
    progress = read_json(out / "progress.json", {})
    all_recorded = len(rows) == len(pre["slots"])
    if scheduler_running is False:
        status = "completed" if all_recorded else "interrupted"
    elif scheduler_running is True:
        status = "running"
    else:
        status = "completed" if all_recorded and progress.get("finished") else progress.get("status", "prepared")
    summary = {"experiment_id": out.name, "updated_at": now(), "status": status,
               "code_commit": pre.get("code_commit"), "planned": len(pre["slots"]), "recorded": len(rows),
               "scheduler_running": scheduler_running, "progress": progress,
               "groups": grouped(rows, pre["slots"]), "rows": rows,
               "baseline": baseline_comparison(rows, baseline, cache),
               "model_requests_by_monitor": 0, "semantic_success_rate": None,
               "interpretation": "Unknown is neither success nor failure. Partial or bounded runs are not complete task evidence; no success-rate or causal claim."}
    write_json(out / ".observation_trace_cache.json", cache)
    write_json(out / "observation_summary.json", summary)
    (out / "report.md").write_text(render(summary))
    write_json(out / "evidence_index.json", {"experiment_id": out.name, "updated_at": summary["updated_at"],
               "report": "report.md", "summary": "observation_summary.json", "preregistration": "preregistration.json",
               "progress": "progress.json", "baseline": str(baseline) if baseline else None,
               "terminal_delivery": "delivery_status.json", "final_checksums": "observation_files.sha256",
               "runs": [{key: row[key] for key in ("slot", "variant", "scenario_id", "record_path", "run_path")} for row in rows]})
    return summary


def seal(out, summary):
    """Seal derived delivery only after the observed scheduler has exited."""
    out = Path(out)
    write_json(out / "delivery_status.json", {"status": summary["status"], "at": now(),
               "planned": summary["planned"], "recorded": summary["recorded"],
               "scheduler_exited": True, "automatic_restart": False, "model_requests_by_monitor": 0,
               "checksum_file": "observation_files.sha256", "checksum_exclusion": "checksum itself, running monitor logs and temporary files"})
    checksums = []
    for path in sorted(out.rglob("*")):
        if (not path.is_file() or path.name == "observation_files.sha256" or path.name.endswith(".tmp")
                or "monitor" in path.name.lower() and path.suffix == ".log"):
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        checksums.append(f"{digest.hexdigest()}  {path.relative_to(out)}")
    (out / "observation_files.sha256").write_text("\n".join(checksums) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scheduler-pid", type=int)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--interval", type=float, default=30)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval <= 0 or args.interval > 60:
        parser.error("--interval must be between 0 and 60 seconds")
    if not args.once and not args.scheduler_pid:
        parser.error("watch mode requires --scheduler-pid")
    while True:
        alive = scheduler_alive(args.scheduler_pid, args.output) if args.scheduler_pid else None
        summary = refresh(args.output, baseline=args.baseline, scheduler_running=alive)
        if not args.once and alive is False:
            seal(args.output, summary)
            return
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
