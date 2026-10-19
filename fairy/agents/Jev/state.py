"""Visible information only: no Scenario, Engine or physics objects accepted."""
from __future__ import annotations

from copy import deepcopy

DEFAULT_STATE_STRATEGY = "visible_state_v1"
TASK_HISTORY_STRATEGY = "task_history_v1"
STATE_STRATEGIES = (DEFAULT_STATE_STRATEGY, TASK_HISTORY_STRATEGY)


def validate_state_strategy(config):
    """Return the pure-Jev model projection, rejecting unknown configuration."""
    strategy = (config or {}).get("state_strategy", DEFAULT_STATE_STRATEGY)
    if strategy not in STATE_STRATEGIES:
        raise ValueError(f"Unknown pure Jev state_strategy: {strategy!r}; expected one of {STATE_STRATEGIES}")
    return strategy


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

    def task_history_snapshot(self, current_time):
        """Project each raw tool observation once, without history reduction.

        This alternative changes the model input only. The bounded ``snapshot``
        remains the candidate resolver's evidence contract. Observation IDs
        preserve ordering between actions and separately listed notifications;
        timestamps and all controller feedback remain visible as well.
        """
        actions, notifications = [], []
        for row in self.observations:
            if row.get("tool_name"):
                action = {
                    "observation_id": row["observation_id"],
                    "action": row["tool_name"],
                    "arguments": row.get("arguments", {}),
                    "result": row.get("result"),
                    "status": row.get("status"),
                    "sim_time_before": row.get("sim_time_before"),
                    "sim_time_after": row.get("sim_time_after"),
                }
                for field in ("error", "error_stage", "executed"):
                    if row.get(field) is not None:
                        action[field] = row[field]
                actions.append(action)
            else:
                # These are public environment observations, not simulator state.
                notifications.append(row)
        return deepcopy({"task": self.task, "current_time": current_time,
            "system_context": self.system_context, "actions_so_far": actions,
            "environment_notifications": notifications, "controller_feedback": self.feedback})

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
