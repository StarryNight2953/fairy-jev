"""Explicit per-provider credential requirements for comparison runs.

An unauthenticated local vLLM endpoint is an intentional configuration choice.
Its public SDK placeholder is injected only into a copied construction dict.
"""
from __future__ import annotations

from copy import deepcopy
import os


_PROVIDER_ENVIRONMENTS = {
    "default": ("OPENAI_API_KEY",), "openai": ("OPENAI_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",), "deepseek-json": ("DEEPSEEK_API_KEY",),
    "qwen": ("QWEN_API_KEY", "DASHSCOPE_API_KEY"),
    "qwen-json": ("QWEN_API_KEY", "DASHSCOPE_API_KEY"),
    "llama-api": ("LLAMA_API_KEY", "OPENAI_API_KEY"),
    "openai-compatible": ("LLAMA_API_KEY", "OPENAI_API_KEY"),
    "vllm": ("VLLM_API_KEY",),
}


def _mode(config):
    mode = config.get("authentication_mode", "environment")
    if mode not in {"environment", "none"}:
        raise ValueError("authentication_mode must be environment or none")
    if mode == "none":
        if str(config.get("provider", "openai")).lower() != "vllm":
            raise ValueError("authentication_mode=none is only supported for explicit vllm endpoints")
        if not isinstance(config.get("base_url"), str) or not config["base_url"].strip():
            raise ValueError("authentication_mode=none requires an explicit base_url")
    return mode


def _environment_name(config):
    if "api_key_env" in config:
        name = config["api_key_env"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError("api_key_env must be a nonempty environment-variable name")
        return name
    provider = str(config.get("provider", "openai")).lower()
    if provider not in _PROVIDER_ENVIRONMENTS:
        raise ValueError(f"No credential mapping for provider {provider}")
    alternatives = _PROVIDER_ENVIRONMENTS[provider]
    # Keep the clients' existing fallback order; do not demand both alternatives.
    return next((name for name in alternatives if os.environ.get(name)), alternatives[0])


def required_credential_names(config, architecture):
    """Return variable names (never their values) needed by this architecture."""
    if architecture not in {"llm_native", "jev", "hybrid"}:
        raise ValueError("unknown architecture")
    names = []
    if architecture in {"jev", "hybrid"}:
        name = config.get("api_key_env", "TYPESAFE_API_KEY")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("TypeSafe api_key_env must be a nonempty name")
        names.append(name)
    if architecture in {"llm_native", "hybrid"}:
        llm = config.get("llm", {})
        if _mode(llm) != "none" and not llm.get("api_key"):
            names.append(_environment_name(llm))
    return list(dict.fromkeys(names))


def prepare_llm_config(config):
    """Resolve authentication into a copy; never mutate saved effective config."""
    prepared = deepcopy(config)
    if _mode(prepared) == "none":
        # Do not let VLLMClient inherit any real VLLM_API_KEY for this endpoint.
        # OpenAI SDK requires a nonempty key even when the server does not.
        prepared["api_key"] = "EMPTY"
    elif not prepared.get("api_key"):
        value = os.environ.get(_environment_name(prepared))
        if value:
            prepared["api_key"] = value
    return prepared
