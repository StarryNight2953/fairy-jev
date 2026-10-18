"""Append-only, flushed events; secrets never belong in run artifacts."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from pathlib import Path
from typing import Any


def json_safe(value: Any):
    """Preserve invalid numeric input as an explicit tag in error evidence."""
    if isinstance(value, float) and not math.isfinite(value):
        return {"__nonfinite_float__": repr(value)}
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_safe(v) for v in value]
    return value


def canonical(value: Any) -> str:
    return json.dumps(json_safe(value), ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def write_json(path: str | Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(value), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


class TraceWriter:
    def __init__(self, path: str | Path, run_id: str, secrets=()):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.secrets = tuple(s for s in secrets if s)
        self.events: list[dict] = []
        self._file = self.path.open("x", encoding="utf-8")

    def sanitize(self, value):
        if isinstance(value, float) and not math.isfinite(value):
            return json_safe(value)
        if isinstance(value, dict):
            return {str(k): ("[REDACTED]" if str(k).lower() in {"api_key", "authorization", "proxy-authorization"}
                             else self.sanitize(v)) for k, v in value.items()}
        if isinstance(value, (tuple, list)):
            return [self.sanitize(v) for v in value]
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
            return re.sub(r"(?i)Bearer\s+[^\s\"']+", "Bearer [REDACTED]", value)
        return value

    def emit(self, event: str, **fields) -> dict:
        row = self.sanitize({"run_id": self.run_id, "event_id": len(self.events), "event": event,
                             "wall_timestamp": time.time(), **fields})
        self._file.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        self._file.flush()
        self.events.append(row)
        return row

    def close(self):
        self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
