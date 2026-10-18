"""Bounded context updates through FAIRY's already configured LLM provider."""
from __future__ import annotations

import json
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path

from ..trace import canonical, digest
from .context import StateBudgetExceeded, lengths, normalized_limits, validate_material


class LLMUpdateError(ValueError):
    pass


def _transient(error):
    status = getattr(error, "status_code", None)
    return (status in {408, 409, 429} or isinstance(status, int) and status >= 500
            or isinstance(error, (TimeoutError, ConnectionError))
            or type(error).__name__ in {"APITimeoutError", "APIConnectionError"})


class LLMUpdater:
    def __init__(self, llm, config=None, trace=None):
        self.llm, self.trace = llm, trace
        self.config = config or {}
        self.limits = normalized_limits(self.config)
        self.prompt = Path(__file__).with_name("prompt.md").read_text(encoding="utf-8")
        self.prompt_hash = digest(self.prompt)
        self.update_count = 0

    def _emit(self, event, **fields):
        if self.trace is not None:
            return self.trace.emit(event, **fields)

    def update(self, update_input):
        runtime = getattr(self.llm, "_comparison_runtime", None)
        # A format repair or transient retry is another HTTP attempt for the
        # same logical update. The provider wrapper reuses this role/request.
        scope = runtime.logical("llm_context_update") if runtime is not None else nullcontext()
        with scope:
            return self._update(update_input)

    def _update(self, update_input):
        self.update_count += 1
        update_id = f"llm_update_{self.update_count:04d}"
        messages = [{"role": "system", "content": self.prompt},
                    {"role": "user", "content": canonical({**update_input,
                        "max_brief_chars": self.limits["max_brief_chars"]})}]
        self._emit("llm_update_start", update_id=update_id, input=update_input,
                   prompt_hash=self.prompt_hash, max_actual_attempts=2,
                   output_token_budget=getattr(self.llm, "max_output_tokens", None))
        for attempt in (1, 2):
            size = lengths(messages)
            if size["utf8_bytes"] > self.limits["max_updater_input_bytes"]:
                measurement = {"role": "llm_context_update", "complete_messages": size,
                    "components": {key: lengths(value) for key, value in update_input.items()},
                    "limit_bytes": self.limits["max_updater_input_bytes"], "token_count": None}
                self._emit("state_budget_exceeded", measurements=measurement)
                raise StateBudgetExceeded(measurement)
            self._emit("llm_update_attempt", update_id=update_id, attempt=attempt,
                       messages=deepcopy(messages), size=size)
            try:
                response = self.llm.chat_completion(messages, tools=None)
            except Exception as error:
                self._emit("llm_update_attempt_end", update_id=update_id, attempt=attempt,
                    status="error", error=f"{type(error).__name__}: {error}",
                    source_request_id=getattr(self.llm, "last_request_id", None))
                if attempt == 1 and _transient(error):
                    continue
                raise LLMUpdateError(f"llm_update_error: {type(error).__name__}") from error
            content = None
            try:
                content = response.choices[0].message.content
                payload = json.loads(content)
                payload = validate_material(payload, update_input["input_event_ids"], self.limits["max_brief_chars"])
            except (ValueError, TypeError, AttributeError, IndexError) as error:
                self._emit("llm_update_attempt_end", update_id=update_id, attempt=attempt,
                    status="invalid_format", content=content, error=str(error),
                    source_request_id=getattr(self.llm, "last_request_id", None))
                if attempt == 2:
                    raise LLMUpdateError("llm_update_error: invalid material after bounded repair") from error
                messages.extend([{"role": "assistant", "content": content or ""},
                    {"role": "user", "content": "Repair only the JSON response format or invalid references. "
                        f"Previous output error: {error}. Allowed evidence_refs: {update_input['input_event_ids']}. "
                        "Return exactly brief and evidence_refs; obey the same evidence and length limits."}])
                continue
            result = {**payload, "source_request_id": getattr(self.llm, "last_request_id", None)}
            self._emit("llm_update_attempt_end", update_id=update_id, attempt=attempt,
                       status="ok", content=content, parsed=payload,
                       source_request_id=result["source_request_id"])
            self._emit("llm_update_end", update_id=update_id, status="ok", material=result,
                       actual_provider_calls=attempt)
            return result
        raise AssertionError("unreachable")


class ScriptedUpdater:
    """Generic offline material fixture; never a source of live model advice."""
    def __init__(self, trace=None):
        self.trace, self.update_count = trace, 0
        self.prompt_hash = digest("scripted_context_fixture_v1")

    def update(self, update_input):
        self.update_count += 1
        result = {"brief": "Offline context fixture. Follow the original public task, verify prerequisites and actual action effects, and treat new tool feedback as evidence. No action is inferred to have succeeded without its result.",
            "evidence_refs": list(update_input["input_event_ids"]),
            "source_request_id": f"scripted_update_{self.update_count:04d}"}
        if self.trace is not None:
            self.trace.emit("scripted_context_update", input=update_input, material=result,
                            model_http_attempts=0)
        return result
