"""Resolve relocated evidence without rewriting historical manifests or indices."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_MAP = ROOT.parent / "workspace-paths.json"


def resolve_saved_path(value, *, root=ROOT, mapping_file=None):
    path = Path(value)
    if not path.is_absolute():
        return Path(root) / path
    mapping_file = Path(mapping_file) if mapping_file is not None else WORKSPACE_MAP
    if not mapping_file.is_file():
        return path
    mappings = json.loads(mapping_file.read_text(encoding="utf-8"))["path_mapping"]
    # The new parent keeps the old main-checkout name. Already migrated paths
    # must not be mistaken for paths into that former main checkout.
    for current in mappings.values():
        current_root = (mapping_file.parent / current).resolve()
        current_root.relative_to(mapping_file.parent.resolve())
        if path.is_relative_to(current_root):
            return path
    for original, current in sorted(mappings.items(), key=lambda item: len(Path(item[0]).parts), reverse=True):
        try:
            suffix = path.relative_to(original)
        except ValueError:
            continue
        target = (mapping_file.parent / current / suffix).resolve()
        target.relative_to(mapping_file.parent.resolve())
        if not target.exists():
            # Especially for the live ledger: a stale mapped path must never
            # silently create a fresh budget and reset already consumed slots.
            raise FileNotFoundError(f"Relocated evidence is missing: {target}")
        return target
    return path
