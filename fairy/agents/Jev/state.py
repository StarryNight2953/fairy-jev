"""Visible information only: no Scenario, Engine or physics objects accepted."""
from __future__ import annotations

from copy import deepcopy


class VisibleState:
    def __init__(self, *, max_observations=24, max_recent_actions=6, max_feedback=12):
        if min(max_observations, max_recent_actions, max_feedback) < 1:
            raise ValueError("history bounds must be positive")
        self.task = ""
        self.system_context = []
        self.observations = []
        self.feedback = []
        self.max_observations = max_observations
        self.max_recent_actions = max_recent_actions
        self.max_feedback = max_feedback

    def add_observation(self, observation):
        row = deepcopy(observation)
        row["observation_id"] = len(self.observations)
        self.observations.append(row)

    def add_feedback(self, feedback):
        self.feedback.append(deepcopy(feedback))

    def snapshot(self, current_time):
        # Retain latest facts, not an ever-growing series of repeated reports/reads.
        latest = {}
        for row in self.observations:
            latest[row.get("tool_name", "environment")] = row["observation_id"]
        keep = sorted(latest.values(), reverse=True)[:self.max_observations]
        keep = set(keep)
        omitted = [r["observation_id"] for r in self.observations if r["observation_id"] not in keep]
        actions = [r for r in self.observations if r.get("tool_name")]
        def reference_result(row):
            if row["observation_id"] in keep:
                return {**row, "result": {"same_as_observation": row["observation_id"]}}
            return row
        return deepcopy({"task": self.task, "current_time": current_time,
            "observations": [r for r in self.observations if r["observation_id"] in keep],
            "recent_actions": [reference_result(r) for r in actions[-self.max_recent_actions:]],
            "action_facts": [reference_result(r) for r in actions
                             if (r.get("write_operation") and r.get("tool_name") != "AgentUserInterface__send_message_to_user")
                             or r.get("status") == "error"],
            "report_history": {"sent_count": sum(r.get("tool_name") == "AgentUserInterface__send_message_to_user" for r in actions),
                               "rule": "Latest report remains in observations; recent reports in recent_actions; all originals in trace.jsonl"},
            "controller_feedback": self.feedback[-self.max_feedback:], "system_context": self.system_context,
            "history_retention": {"rule": "latest per tool, newest first up to max_observations; recent actions bounded; all physical writes/errors retained separately",
                "max_observations": self.max_observations, "omitted_observation_ids": omitted,
                "omitted_recent_action_ids": [r["observation_id"] for r in actions[:-self.max_recent_actions]],
                "omitted_feedback_count": max(0, len(self.feedback)-self.max_feedback),
                "result_deduplication": "same_as_observation references the complete result in observations by observation_id; no data omitted",
                "full_history": "trace.jsonl"}})
