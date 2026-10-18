"""Observe actual synchronous HTTP sends beneath the existing provider SDKs.

No authentication headers are recorded. The run's TraceWriter additionally
redacts known secret values, including any provider text that echoes a secret.
"""
from __future__ import annotations

import json
import time
from contextlib import nullcontext
from functools import wraps
from urllib.parse import urlsplit, urlunsplit


def _body(value):
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


def _safe_endpoint(value):
    parsed = urlsplit(str(value))
    host = parsed.hostname or ""
    if parsed.port:
        host += f":{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


class ObservedTransport:
    """Duck-typed BaseTransport for both httpx and TypeSafe's httpx2.

    Wrapping the existing transport retains connection/proxy behavior and SDK
    retries. Each retry reaches handle_request independently and reserves its
    own allowance. BudgetStop inherits BaseException and bypasses SDK retries.
    """

    def __init__(self, inner, runtime):
        self.inner, self.runtime = inner, runtime

    def handle_request(self, request):
        runtime = self.runtime
        runtime.check()
        role = runtime.current_role
        logical_id = runtime.current_request_id
        attempt_id = runtime.reserve_http(role, logical_id)
        remaining = runtime.remaining_seconds()
        timeouts = dict(request.extensions.get("timeout") or {})
        request.extensions["timeout"] = {
            phase: min(float(timeouts[phase]), remaining) if timeouts.get(phase) is not None else remaining
            for phase in ("connect", "read", "write", "pool")
        }
        request_body = _body(request.read())
        context = {"role": role, "logical_request_id": logical_id,
                   "attempt_id": attempt_id, "endpoint": _safe_endpoint(request.url),
                   "requested_model": request_body.get("model") if isinstance(request_body, dict) else None}
        started = time.perf_counter()
        runtime.trace.emit("http_attempt_start", **context, method=request.method,
                           request_body=request_body,
                           timeout_seconds=max(request.extensions["timeout"].values()),
                           timeout_by_phase=dict(request.extensions["timeout"]),
                           remaining_deadline_seconds=remaining,
                           sdk_retry_index=request.headers.get("x-stainless-retry-count"))
        try:
            response = self.inner.handle_request(request)
            raw = response.read().decode("utf-8", errors="replace")
            parsed = _body(raw)
            usage = parsed.get("usage") if isinstance(parsed, dict) else None
            runtime.trace.emit("http_attempt_end", **context,
                status="ok" if 200 <= response.status_code < 300 else "http_error",
                http_status=response.status_code,
                server_request_id=response.headers.get("x-request-id") or response.headers.get("x-typesafe-request-id"),
                response_model=parsed.get("model") if isinstance(parsed, dict) else None,
                raw_response_body=raw, parsed_response=parsed, usage=usage,
                usage_status="reported" if usage is not None else "unavailable",
                wall_elapsed_seconds=time.perf_counter()-started)
        except BaseException as exc:
            runtime.trace.emit("http_attempt_end", **context, status="error", http_status=None,
                error_type=type(exc).__name__, error=str(exc), usage=None, usage_status="unavailable",
                wall_elapsed_seconds=time.perf_counter()-started)
            raise
        runtime.check()
        return response

    def close(self):
        return self.inner.close()

    def __enter__(self):
        self.inner.__enter__()
        return self

    def __exit__(self, *args):
        return self.inner.__exit__(*args)


def instrument_http_client(client, runtime):
    """Wrap primary and proxy transports without replacing SDK client settings."""
    wrapped = {}

    def observe(transport):
        if transport is None or isinstance(transport, ObservedTransport):
            return transport
        key = id(transport)
        if key not in wrapped:
            wrapped[key] = ObservedTransport(transport, runtime)
        return wrapped[key]

    client._transport = observe(client._transport)
    client._mounts = {pattern: observe(transport) for pattern, transport in client._mounts.items()}
    return client


def build_instrumented_llm(config, runtime, role="llm_native_decision"):
    """Construct the configured FAIRY client and observe its unchanged request path."""
    from fairy.agents.llm.base_llm import BaseLLM
    from .credentials import prepare_llm_config

    llm = BaseLLM.llm_builder(prepare_llm_config(config))
    if role == "llm_context_update":
        # Updater owns its single retry/format repair; no nested SDK retries.
        llm.api_client.max_retries = 0
    instrument_http_client(llm.api_client._client, runtime)
    original = llm.chat_completion

    @wraps(original)
    def chat_completion(messages, tools=None):
        # An updater's format repair/transport retry belongs to the same logical
        # material update; the transport still counts each HTTP send separately.
        logical = (nullcontext(runtime.current_request_id)
                   if runtime.current_role == role and runtime.current_request_id
                   else runtime.logical(role))
        with logical:
            llm.last_request_id = runtime.current_request_id
            runtime.trace.emit("llm_request", role=role, logical_request_id=llm.last_request_id,
                messages=messages, tools=tools, requested_model=llm.model,
                provider=llm.provider, sdk_max_retries=llm.api_client.max_retries,
                max_output_tokens=llm.max_output_tokens)
            result = original(messages, tools)
            runtime.trace.emit("llm_response", role=role, logical_request_id=llm.last_request_id,
                parsed_response=result.model_dump(mode="json") if hasattr(result, "model_dump") else str(result))
            return result

    llm.chat_completion = chat_completion
    llm.last_request_id = None
    llm._comparison_runtime = runtime
    return llm
