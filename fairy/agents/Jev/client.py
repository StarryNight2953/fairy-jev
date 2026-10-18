"""Small injectable decision boundary backed by the official synchronous SDK."""
from __future__ import annotations

import importlib.metadata
import os
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class Decision:
    selected_option: str
    record: dict[str, Any]


class DecisionError(RuntimeError):
    def __init__(self, message, record=None):
        super().__init__(message)
        self.record = record or {}


class DecisionBackend(Protocol):
    name: str

    def decide(self, state, question_id, instructions, options) -> Decision: ...


class OfficialBackend:
    name = "typesafe_sdk"

    def __init__(self, config, *, transport=None):
        from typesafe_sdk import RetryPolicy, TypeSafeClient
        key = os.environ.get(config.get("api_key_env", "TYPESAFE_API_KEY"))
        if not key:
            raise ValueError("missing_api_key")
        self.requested_model = config.get("model", "jev-1.13.0")
        self.sdk_version = importlib.metadata.version("typesafe-sdk")
        self.client = TypeSafeClient(api_key=key, base_url=config.get("base_url", "https://api.typesafe.ai"),
            model=self.requested_model, timeout=float(config.get("request_timeout_seconds", 30)),
            retry=RetryPolicy(max_retries=0), compression=False, transport=transport)

    def decide(self, state, question_id, instructions, options):
        from typesafe_sdk import Choice
        if not 1 <= len(options) <= 255:
            raise DecisionError("Choice requires 1–255 options")
        record = {"backend": self.name, "requested_model": self.requested_model,
                  "response_model": None, "sdk_version": self.sdk_version, "request_id": None,
                  "http_status": None, "raw_response_body": None, "parsed_response": None,
                  "probabilities": None, "confidence": None, "usage": None,
                  "missing_metadata_reason": "No response received yet", "retry_max_retries": 0}
        try:
            response = self.client.system_one(state=state, questions={question_id: Choice(
                instructions=instructions, criteria=options)})
            raw = response.raw_http_response
            record.update(response_model=response.model, parsed_response=response.model_dump(mode="json"),
                          raw_response_body=raw.text, http_status=raw.status_code,
                          request_id=raw.headers.get("x-typesafe-request-id"),
                          usage=response.usage.model_dump(mode="json"), missing_metadata_reason=None)
            answer = response.choices[question_id]
            record.update(probabilities=answer.probabilities, confidence=answer.confidence,
                          selected_option=answer.choice)
            if answer.choice not in options:
                raise DecisionError("invalid_return_option", record)
            if record["request_id"] is None or any(v is None for v in record["usage"].values()):
                record["missing_metadata_reason"] = "Unreported fields are null; the SDK/API did not supply them"
            return Decision(answer.choice, record)
        except DecisionError:
            raise
        except Exception as exc:
            record.update(error_type=type(exc).__name__, error=str(exc))
            if hasattr(exc, "status"):
                record["http_status"] = exc.status
                record["missing_metadata_reason"] = "HTTP error or invalid response; parsed model answer and token usage unavailable"
            if hasattr(exc, "body"):
                record["raw_response_body"] = exc.body
            headers = getattr(exc, "headers", {}) or {}
            record["request_id"] = headers.get("x-typesafe-request-id")
            raise DecisionError(f"{type(exc).__name__}: {exc}", record) from exc

    def close(self):
        self.client.close()


class ScriptedBackend:
    """A deterministic test backend, never reported as a real model decision."""
    name = "scripted"

    def __init__(self, policy):
        self.policy = policy

    def decide(self, state, question_id, instructions, options):
        selection = self.policy(state, question_id, instructions, options)
        if selection not in options:
            raise DecisionError("invalid_return_option", {"selected_option": selection, "backend": self.name})
        return Decision(selection, {"backend": self.name, "requested_model": None, "response_model": None,
            "sdk_version": None, "request_id": None, "http_status": None, "raw_response_body": None,
            "parsed_response": None, "probabilities": None, "confidence": None, "usage": None,
            "missing_metadata_reason": "Deterministic scripted decision; no SDK or HTTP request made"})

    def close(self):
        pass
