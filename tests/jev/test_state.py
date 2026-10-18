"""Keep visible requests bounded without losing physical actions or errors."""

import json

from fairy.agents.Jev.state import VisibleState


REPORT = "AgentUserInterface__send_message_to_user"


def observation(index, tool, *, result=None, write=False, status="ok", arguments=None):
    return {"tool_call_id": f"tool_{index:04d}", "tool_name": tool,
            "arguments": arguments or {}, "result": result or {"fact": index},
            "write_operation": write, "status": status,
            "sim_time_before": 1000.0 + index, "sim_time_after": 1000.1 + index}


def add_reports(state, count, start=0):
    text = "已启动目标区灌溉；等待后的完整目标区复查仍未确认，不能宣称完成。" * 12
    for index in range(start, start + count):
        state.add_observation(observation(index, REPORT, write=True,
                              arguments={"content": text}, result={"message": text}))


def test_ten_to_forty_reports_does_not_grow_visible_request_with_history():
    state = VisibleState()
    state.task = "Only irrigate ridges 43–63 and recheck"
    add_reports(state, 10)
    ten = state.snapshot(2000)
    add_reports(state, 30, start=10)
    forty = state.snapshot(2000)
    size_ten = len(json.dumps(ten, ensure_ascii=False))
    size_forty = len(json.dumps(forty, ensure_ascii=False))
    assert abs(size_forty - size_ten) < 1000
    assert len(ten["observations"]) == len(forty["observations"]) == 1
    assert len(ten["recent_actions"]) == len(forty["recent_actions"]) == 6
    assert ten["action_facts"] == forty["action_facts"] == []
    assert ten["report_history"]["sent_count"] == 10
    assert forty["report_history"]["sent_count"] == 40
    assert forty["observations"][0]["tool_call_id"] == "tool_0039"
    assert len(state.observations) == 40  # Original records remain available to full trace/history.
    assert forty["task"] == state.task


def test_physical_writes_and_all_errors_survive_forty_successful_reports():
    state = VisibleState(max_observations=2)
    originals = [
        observation(0, "FieldOpsApp__irrigate", write=True,
                    arguments={"start": 43, "end": 63, "hours": 0.8}, result={"status": "irrigation_started", "dose": 0.8}),
        observation(1, "FieldOpsApp__irrigate", write=True,
                    arguments={"start": 43, "end": 63, "hours": 0.4}, result={"status": "irrigation_started", "dose": 0.4}),
        observation(2, "FarmWorldApp__get_inventory", status="error", result={"error": "read unavailable"}),
        observation(3, REPORT, write=True, status="error", result={"error": "report failed"}),
    ]
    for row in originals:
        state.add_observation(row)
    add_reports(state, 40, start=4)
    snapshot = state.snapshot(2100)
    facts = {row["tool_call_id"]: row for row in snapshot["action_facts"]}
    assert set(facts) == {row["tool_call_id"] for row in originals}
    assert facts["tool_0000"]["arguments"]["hours"] == 0.8
    assert facts["tool_0001"]["arguments"]["hours"] == 0.4
    assert facts["tool_0003"]["result"] == {"error": "report failed"}
    observed = {row["observation_id"]: row for row in snapshot["observations"]}
    for original in originals:
        fact = facts[original["tool_call_id"]]
        if "same_as_observation" in fact["result"]:
            assert observed[fact["result"]["same_as_observation"]]["result"] == original["result"]
        else:
            assert fact["result"] == original["result"]
    assert snapshot["report_history"]["sent_count"] == 41
    assert len(snapshot["recent_actions"]) == 6
    assert len(state.observations) == 44


def test_latest_facts_omitted_ids_and_references_are_explicit():
    state = VisibleState(max_observations=2, max_recent_actions=2, max_feedback=2)
    state.task = "Preserve latest facts"
    state.system_context.append("Visible system policy")
    state.add_observation(observation(0, "WeatherApp__get_current_weather", result={"rain": "old"}))
    state.add_observation(observation(1, "FarmWorldApp__get_inventory", result={"quota": 1.4}))
    state.add_observation(observation(2, "WeatherApp__get_current_weather", result={"rain": "new"}))
    state.add_observation({"kind": "environment_notification", "result": {"ready": True}})
    for index in range(5):
        state.add_feedback({"status": "needs_context", "attempt": index})
    snapshot = state.snapshot(2200)
    assert [row["observation_id"] for row in snapshot["observations"]] == [2, 3]
    assert snapshot["observations"][0]["result"] == {"rain": "new"}
    assert snapshot["recent_actions"][0]["result"] == {"quota": 1.4}
    assert snapshot["recent_actions"][1]["result"] == {"same_as_observation": 2}
    assert snapshot["history_retention"]["omitted_observation_ids"] == [0, 1]
    assert snapshot["history_retention"]["omitted_recent_action_ids"] == [0]
    assert snapshot["history_retention"]["omitted_feedback_count"] == 3
    assert [row["attempt"] for row in snapshot["controller_feedback"]] == [3, 4]
    assert "same_as_observation" in snapshot["history_retention"]["result_deduplication"]
    assert snapshot["history_retention"]["full_history"] == "trace.jsonl"
    assert snapshot["system_context"] == ["Visible system policy"]


def test_successful_report_latest_result_and_recent_six_remain_visible():
    state = VisibleState()
    add_reports(state, 8)
    snapshot = state.snapshot(2300)
    assert [row["tool_call_id"] for row in snapshot["recent_actions"]] == [f"tool_{index:04d}" for index in range(2, 8)]
    latest = snapshot["observations"][0]
    assert latest["result"] == state.observations[-1]["result"]
    assert snapshot["recent_actions"][-1]["result"] == {"same_as_observation": latest["observation_id"]}
    assert all("same_as_observation" not in row["result"] for row in snapshot["recent_actions"][:-1])
    assert snapshot["history_retention"]["omitted_observation_ids"] == list(range(7))


def test_input_and_snapshot_mutation_cannot_rewrite_stored_visible_facts():
    state = VisibleState()
    source = observation(0, "FieldOpsApp__irrigate", write=True, result={"nested": {"dose": 0.8}})
    state.add_observation(source)
    source["result"]["nested"]["dose"] = 9.0
    snapshot = state.snapshot(2400)
    assert snapshot["observations"][0]["result"]["nested"]["dose"] == 0.8
    snapshot["observations"][0]["result"]["nested"]["dose"] = 99.0
    assert state.snapshot(2401)["observations"][0]["result"]["nested"]["dose"] == 0.8
