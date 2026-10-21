"""Public-briefing control policy for offline integration validation only."""
from __future__ import annotations

from .candidates import extract_task_parameters


class ScriptedPolicy:
    sequence = ["WeatherApp__get_current_weather", "SensorApp__read_soil_sensors",
                "FarmWorldApp__get_ridge_range_state", "FarmWorldApp__get_inventory",
                "FieldOpsApp__irrigate", "SystemApp__advance_time",
                "FarmWorldApp__get_ridge_range_state", "SensorApp__read_soil_sensors",
                "FarmWorldApp__get_inventory", "AgentUserInterface__send_message_to_user", "FINISH"]

    def __init__(self):
        self.index = 0
        self.selected = None

    def __call__(self, state, question_id, instructions, options):
        if question_id == "tool":
            if self.index >= len(self.sequence):
                raise ValueError("scripted_sequence_exhausted")
            self.selected = self.sequence[self.index]
            self.index += 1
            return self.selected
        target = extract_task_parameters(state["task"])
        region, hours = target["target_range"], target["irrigation_hours"]
        if region is None or hours is None:
            raise ValueError("scripted policy requires visible target range and duration")
        if question_id in {"parameter_start", "parameter_start_ridge", "parameter_end", "parameter_end_ridge"}:
            parameter = question_id.removeprefix("parameter_")
            value = region[0] if parameter in {"start", "start_ridge"} else region[1]
            return next(key for key, candidate in options.items() if candidate["arguments"] == {parameter: value})
        for candidate_id, candidate in options.items():
            args = candidate["arguments"]
            if self.selected == "FarmWorldApp__get_ridge_range_state" and args == {"start": region[0], "end": region[1]}:
                return candidate_id
            if self.selected == "FieldOpsApp__irrigate" and args == {"start": region[0], "end": region[1], "hours": hours}:
                return candidate_id
            if self.selected == "SystemApp__advance_time" and any(":effect_ready_at" in ref or ":eta_minutes_to_confirmation" in ref for ref in candidate["source_refs"]):
                return candidate_id
            if self.selected == "AgentUserInterface__send_message_to_user" and "content" in args:
                return candidate_id
        raise ValueError(f"scripted public-fact candidate unavailable for {self.selected}")
