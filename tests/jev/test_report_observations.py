"""Report evidence correctness, including actual scripted FAIRY irrigation."""
from copy import deepcopy
import json
from pathlib import Path
import socket

import pytest

from fairy.agents.Jev.report_observations import (
    VERSION, irrigation_recheck_evidence, render_irrigation_rechecks,
    validate_report_policy,
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("Offline report tests cannot use network")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def row(name, result, before, after=None, **kwargs):
    return {
        "tool_name": name, "tool_call_id": f"call_{before}", "result": result,
        "arguments": {}, "status": "ok", "executed": True,
        "sim_time_before": before, "sim_time_after": before if after is None else after,
        **kwargs,
    }


def irrigation(before=1, ready=20, ids=None):
    return row("FieldOpsApp__irrigate_range", {
        "status": "irrigation_started", "irrigated_ridges": list(range(43, 64)) if ids is None else ids,
        "duration_hours_per_ridge": .8, "effect_ready_at": ready,
    }, before)


def sensor(start, end, value=.2):
    return {"ridge_start": start, "ridge_end": end, "vwc": value}


def reading(before=21, sensors=None, **kwargs):
    return row("SensorApp__read_soil_sensors", {
        "soil_sensors": [sensor(33, 43), sensor(44, 53), sensor(54, 63)] if sensors is None else sensors
    }, before, **kwargs)


def state(*rows, now=100):
    return {"current_time": now, "observations": list(rows)}


def action(s):
    return irrigation_recheck_evidence(s, [0, 63])["actions"][0]


def test_sensor_zones_cover_actual_irrigation_without_claiming_individual_measurements():
    s = state(irrigation(), reading())
    a = action(s)
    assert a["coverage_status"] == "observed_after_ready"
    assert a["observed_ranges"] == [[43, 63]] and not a["unobserved_ranges"]
    assert all(o["measurement_kind"] == "sensor_zone_mean" for o in a["observations"])
    assert a["observations"][0]["range"] == [33, 43]
    assert a["observations"][0]["overlap_with_irrigated_ridges"] == [[43, 43]]
    assert a["observations"][0]["value"] == .2
    assert a["observations"][0]["result_path"] == "soil_sensors[0].vwc"
    text = render_irrigation_rechecks(s, [0, 63])
    assert "区域平均值" in text and "1 次已取得" in text
    assert not a["wait_call_ids"]  # No invented waiting call.


def test_requested_range_or_single_sensor_id_does_not_prove_returned_coverage():
    r = reading(sensors=[sensor(33, 43)])
    r["arguments"] = {"start": 0, "end": 63}
    a = action(state(irrigation(), r))
    assert a["coverage_status"] == "partial" and a["unobserved_ranges"] == [[44, 63]]
    r["result"] = {"soil_sensors": [{"sensor_id": "S6", "ridge_id": 60, "vwc": .3}]}
    assert action(state(irrigation(), r))["coverage_status"] == "not_observed"


@pytest.mark.parametrize("value", [None, True, float("nan"), float("inf"), -.1, 1.1])
def test_invalid_measurement_not_accepted(value):
    a = action(state(irrigation(), reading(sensors=[sensor(43, 63, value)])))
    assert a["coverage_status"] == "not_observed"


@pytest.mark.parametrize("changes", [
    {"status": "error"}, {"executed": False}, {"error": "read failed"},
    {"sim_time_before": None}, {"sim_time_after": 200},
])
def test_failed_unexecuted_untimed_or_future_read_not_evidence(changes):
    r = reading(); r.update(changes)
    assert action(state(irrigation(), r))["coverage_status"] == "not_observed"


def test_pre_response_read_and_missing_response_time_not_rewritten_as_complete():
    assert action(state(irrigation(), reading(before=19)))["coverage_status"] == "not_observed"
    i = irrigation(); i["result"].pop("effect_ready_at")
    assert action(state(i, reading()))["coverage_status"] == "unknown"


def test_operational_ridge_lookup_does_not_count_as_soil_measurement():
    r = row("FarmWorldApp__get_ridge_range_state",
            {"ridges": [{"ridge_id": r, "growth_stage": "R5"} for r in range(43, 64)]}, 21)
    assert action(state(irrigation(), r))["coverage_status"] == "not_observed"


def test_returned_single_probe_is_supported_and_sensor_errors_are_not():
    r = row("SensorApp__read_soil_sensor", sensor(43, 63), 21)
    a = action(state(irrigation(), r))
    assert a["coverage_status"] == "observed_after_ready"
    assert a["observations"][0]["result_path"] == "vwc"
    r["result"]["available"] = False
    assert action(state(irrigation(), r))["coverage_status"] == "not_observed"


def test_wait_is_separate_and_repeated_irrigation_keeps_new_obligation():
    wait = row("SystemApp__advance_time", {"status": "ok"}, 2, 20)
    s = state(irrigation(), wait, reading(), irrigation(before=30, ready=50))
    a, b = irrigation_recheck_evidence(s, [0, 63])["actions"]
    assert a["coverage_status"] == "observed_after_ready" and a["wait_call_ids"] == ["call_2"]
    assert b["coverage_status"] == "not_observed" and not b["wait_call_ids"]
    assert "其中 1 次已取得" in render_irrigation_rechecks(s, [0, 63])


def test_noncontiguous_irrigated_ranges_preserved_and_inputs_unchanged():
    s = state(irrigation(ids=[1, 3, 5]), reading(sensors=[sensor(1, 1), sensor(5, 5)]))
    before = deepcopy(s)
    assert action(s)["unobserved_ranges"] == [[3, 3]]
    assert action(s)["irrigated_ranges"] == [[1, 1], [3, 3], [5, 5]]
    assert s == before


def test_large_report_states_omission_and_retains_aggregate_incomplete_count():
    rows = [irrigation(before=i, ready=90+i, ids=[i]) for i in range(1, 11)]
    text = render_irrigation_rechecks(state(*rows), [0, 63])
    data = json.loads(text.split("\n", 1)[1])
    assert data["older_irrigation_details_omitted"] == 2 and len(data["actions"]) == 8
    assert data["coverage_status_counts"]["not_observed"] == 10


@pytest.mark.parametrize("config", [
    {"report_observation_policy": "unknown"},
    {"report_observation_policy": VERSION},
    {"report_observation_policy": VERSION, "parameter_strategy": "tool_contracts_v1"},
])
def test_incompatible_policy_rejected(config):
    with pytest.raises(ValueError):
        validate_report_policy(config)


@pytest.mark.parametrize("architecture,state_strategy", [("jev", "task_history_v1"), ("hybrid", "visible_state_v1")])
@pytest.mark.parametrize("enabled", [False, True])
def test_native_sensor_only_recheck_and_report_contract(tmp_path, monkeypatch, architecture, state_strategy, enabled):
    from fairy.experiments import episode

    class SensorRecheckPolicy:
        def __init__(self):
            self.index = -1
            self.calls = [
                ("WeatherApp__get_current_weather", {}),
                ("SensorApp__read_soil_sensors", {}),
                ("FarmWorldApp__get_inventory", {}),
                ("FarmWorldApp__get_ridge_range_state", {"start": 43, "end": 63}),
                ("FieldOpsApp__irrigate_range", {"start": 43, "end": 63, "duration_hours": .8}),
                ("SystemApp__advance_time", {"seconds": 7200, "minutes": 0, "hours": 0, "days": 0}),
                ("SensorApp__read_soil_sensors", {}),
                ("AgentUserInterface__send_message_to_user", None),
            ]

        def __call__(self, state, stage, instructions, options):
            if stage == "tool":
                self.index += 1
                return self.calls[self.index][0] if self.index < len(self.calls) else "FINISH"
            target = self.calls[self.index][1]
            if target is None:
                assert len(options) == 1
                return next(iter(options))
            return next(key for key, value in options.items()
                        if all(target.get(k) == v for k, v in value["arguments"].items()))

    monkeypatch.setattr(episode, "ScriptedPolicy", SensorRecheckPolicy)
    config = json.loads(Path("configs/jev/hn58_jev_tool_adapters_v2.json").read_text())
    config["state_strategy"] = state_strategy
    if enabled:
        config["report_observation_policy"] = VERSION
    path, result = episode.run_episode(config, tmp_path, architecture=architecture, mode="scripted")
    assert result["stop_reason"] == "model_finish", result
    assert result["task_success"] is result["live_integration_success"] is None
    checked = episode.verify_and_replay(path, config)
    assert checked["trace_verification"]["passed"], checked["trace_verification"]
    assert checked["replay_verification"]["passed"], checked["replay_verification"]
    assert checked["trace_verification"]["counts"]["http_attempts"] == 0
    # The real native environment and existing unchanged evaluator confirm the
    # same first four assertions, regardless of which report policy is enabled.
    assertions = checked["task_assertions"]
    assert all(v["passed"] for k, v in assertions.items() if k != "factual_final_report")
    assert assertions["factual_final_report"]["passed"] is enabled
    assert checked["scripted_task_success"] is enabled
    manifest = json.loads((path / "manifest.json").read_text())
    assert manifest.get("report_observation_policy") == (VERSION if enabled else None)
    events = [json.loads(line) for line in (path / "trace.jsonl").read_text().splitlines()]
    calls = [e for e in events if e["event"] == "tool_end"]
    assert len(calls) == 8 and all(e["status"] == "ok" for e in calls)
    content = calls[-1]["arguments"]["content"]
    if enabled:
        assert "1 次已取得" in content
        assert "尚未同时确认" not in content
    else:
        assert "尚未同时确认" in content
