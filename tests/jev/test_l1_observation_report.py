"""Offline report contracts: no requests, variant mixing or invented success."""
import json
from pathlib import Path
from types import SimpleNamespace

from fairy.experiments import l1_observation_report as report


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n")


def make_batch(tmp_path, variants=("jev", "jev_task_history")):
    slots = [{"slot": f"s__{v}", "scenario_id": "s", "architecture": "jev", "variant": v, "level": "L1"} for v in variants]
    put(tmp_path / "preregistration.json", {"slots": slots, "code_commit": "test"})
    return slots


def make_result(out, slot, **evaluation):
    run = out / "live" / slot["slot"]
    run.mkdir(parents=True, exist_ok=True)
    put(out / "results" / f"{slot['slot']}.json", {**slot, "path": str(run), "evaluation": evaluation})
    return run


def test_unknown_and_variants_stay_separate(tmp_path):
    slots = make_batch(tmp_path)
    make_result(tmp_path, slots[0], task_success=True, native_contract_success=True,
                weak_contract=True, live_integration_success=True)
    make_result(tmp_path, slots[1], task_success=False, live_integration_success=False)
    summary = report.refresh(tmp_path)
    assert summary["groups"]["jev"]["task_success"] == {"true": 0, "false": 0, "unknown": 1}
    assert summary["groups"]["jev_task_history"]["task_success"]["false"] == 1
    assert summary["rows"][0]["raw_task_success"] is True
    assert summary["semantic_success_rate"] is None
    assert "jev_task_history" in (tmp_path / "report.md").read_text()


def test_http_errors_and_tool_errors_keep_first_source_line(tmp_path):
    trace = tmp_path / "trace.jsonl"
    events = [
        {"event": "http_attempt_end", "event_id": 10, "status": "ok", "http_status": 200},
        {"event": "tool_end", "event_id": 12, "status": "ok", "result": {"error": "tank empty"}},
        {"event": "http_attempt_end", "event_id": 14, "status": "http_error", "http_status": 400,
         "parsed_response": {"detail": {"error_type": "max_tokens_exceeded"}}},
        {"event": "http_attempt_end", "event_id": 16, "status": "transport_error", "error_type": "ReadTimeout"},
    ]
    trace.write_text("".join(json.dumps(e) + "\n" for e in events) + '{"event":')
    summary = report.scan_trace(trace)
    assert summary["http_400_max_tokens_exceeded"] == 1
    assert summary["read_timeout_attempts"] == 1
    assert summary["tool_errors"] == 1
    assert summary["first_error"]["line"] == 2
    assert summary["first_http_error"]["event_id"] == 14
    assert summary["incomplete_final_line"] is True


def test_active_trace_without_result_never_scanned(tmp_path, monkeypatch):
    slots = make_batch(tmp_path)
    run = tmp_path / "live" / "active"
    put(run / "trace.jsonl", {"event": "http_attempt_end", "error_type": "ReadTimeout"})
    put(tmp_path / "progress.json", {"active": [slots[0]["slot"]], "pending": 1, "status": "running"})
    monkeypatch.setattr(report, "scan_trace", lambda _: (_ for _ in ()).throw(AssertionError("active trace read")))
    summary = report.refresh(tmp_path, scheduler_running=True)
    assert summary["recorded"] == 0 and summary["status"] == "running"


def test_trace_cache_is_invalidated_only_when_file_changes(tmp_path, monkeypatch):
    trace = tmp_path / "trace.jsonl"
    put(trace, {"event": "tool_end", "status": "ok"})
    cache = {}
    first = report.cached_trace(trace, cache)
    original = report.scan_trace
    calls = []
    monkeypatch.setattr(report, "scan_trace", lambda p: (calls.append(p), original(p))[1])
    assert report.cached_trace(trace, cache) == first
    assert calls == []
    with trace.open("a") as stream:
        stream.write(json.dumps({"event": "tool_end", "status": "error"}) + "\n")
    assert report.cached_trace(trace, cache)["tool_errors"] == 1
    assert len(calls) == 1


def test_baseline_comparison_never_fills_missing_new_results(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old_slot = make_batch(old, ("jev",))[0]
    make_result(old, old_slot, task_success=True)
    new_slots = make_batch(new)
    summary = report.refresh(new, baseline=old)
    assert summary["recorded"] == 0 and summary["baseline"]["matched"] == []
    make_result(new, new_slots[1], task_success=None)
    summary = report.refresh(new, baseline=old)
    assert summary["recorded"] == 1
    match = summary["baseline"]["matched"][0]
    assert match["new_variant"] == "jev_task_history" and match["old_variant"] == "jev"
    assert match["new"]["task_success"] is None and match["old"]["task_success"] is True


def test_baseline_reads_only_matched_scenario_and_variant(tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    old_slots = make_batch(old, ("jev", "hybrid"))
    old_slots.append({**old_slots[0], "slot": "other__jev", "scenario_id": "other"})
    put(old / "preregistration.json", {"slots": old_slots})
    for slot in old_slots:
        run = make_result(old, slot, task_success=None)
        put(run / "trace.jsonl", {"event": "tool_end", "status": "ok"})
    new_slot = make_batch(new, ("jev_task_history",))[0]
    make_result(new, new_slot, task_success=None)
    original = report.read_json
    reads = []

    def observe(path, default=None):
        reads.append(Path(path))
        return original(path, default)

    monkeypatch.setattr(report, "read_json", observe)
    summary = report.refresh(new, baseline=old)
    assert len(summary["baseline"]["matched"]) == 1
    assert old / "results" / "s__jev.json" in reads
    assert old / "results" / "s__hybrid.json" not in reads
    assert old / "results" / "other__jev.json" not in reads
    assert old / "live" / "s__hybrid" / "metrics.json" not in reads
    cache = original(new / ".observation_trace_cache.json")
    assert list(cache) == [str((old / "live" / "s__jev" / "trace.jsonl").resolve())]


def test_scheduler_identity_includes_action_and_exact_output(tmp_path, monkeypatch):
    def command(text):
        monkeypatch.setattr(report.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=text))
    command(f"python -m fairy.experiments.full_suite run --output {tmp_path}")
    assert report.scheduler_alive(123, tmp_path)
    command(f"python -m fairy.experiments.full_suite child --output {tmp_path}")
    assert not report.scheduler_alive(123, tmp_path)
    command(f"python -m fairy.experiments.full_suite run --output {tmp_path}/other")
    assert not report.scheduler_alive(123, tmp_path)
    command("sleep 500")
    assert not report.scheduler_alive(123, tmp_path)


def test_terminal_delivery_requires_every_registered_result(tmp_path):
    slots = make_batch(tmp_path)
    make_result(tmp_path, slots[0], execution_status="integration_error", task_success=None)
    summary = report.refresh(tmp_path, scheduler_running=False)
    assert summary["status"] == "interrupted"
    make_result(tmp_path, slots[1], execution_status="blocked_external_service", task_success=None)
    summary = report.refresh(tmp_path, scheduler_running=False)
    assert summary["status"] == "completed"
    (tmp_path / "monitor.log").write_text("still open")
    report.seal(tmp_path, summary)
    delivery = json.loads((tmp_path / "delivery_status.json").read_text())
    assert delivery["status"] == "completed" and delivery["model_requests_by_monitor"] == 0
    checksums = (tmp_path / "observation_files.sha256").read_text()
    assert "delivery_status.json" in checksums
    assert "monitor.log" not in checksums
    assert "observation_files.sha256" not in checksums
