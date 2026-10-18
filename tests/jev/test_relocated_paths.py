import json
from pathlib import Path

import pytest

from fairy.experiments import paths
from fairy.experiments.budget import LiveLedger


@pytest.fixture
def relocated(tmp_path, monkeypatch):
    workspace = tmp_path / "Fairy-Jev"
    workspace.mkdir()
    (workspace / "jev-mvp").mkdir()
    (workspace / "llm-jev-hybrid").mkdir()
    mapping = workspace / "workspace-paths.json"
    mapping.write_text(json.dumps({"path_mapping": {
        str(workspace): "jev-mvp",
        str(tmp_path / "Fairy-Jev-hybrid"): "llm-jev-hybrid",
    }}))
    monkeypatch.setattr(paths, "WORKSPACE_MAP", mapping)
    return workspace, tmp_path / "Fairy-Jev-hybrid"


def test_historical_paths_resolve_without_rewriting_evidence(relocated):
    workspace, old_hybrid = relocated
    original = b'{"historical_path":"kept verbatim"}\n'
    target = workspace / "llm-jev-hybrid/manifest.json"
    target.write_bytes(original)
    assert paths.resolve_saved_path(old_hybrid / "manifest.json") == target
    assert paths.resolve_saved_path(target) == target
    assert target.read_bytes() == original
    main = workspace / "jev-mvp/trace.jsonl"
    main.write_text("{}\n")
    assert paths.resolve_saved_path(workspace / "trace.jsonl") == main


def test_existing_budget_identity_and_consumption_survive_move(relocated):
    workspace, old_hybrid = relocated
    actual = workspace / "llm-jev-hybrid/ledger.json"
    ledger = LiveLedger(actual)
    ledger.reserve_episode("old-run", "hybrid", "smoke", "smoke_hybrid")
    ledger.reserve_http("old-run")
    before = ledger.snapshot()
    moved = LiveLedger(old_hybrid / "ledger.json")
    assert moved.path == actual
    assert moved.snapshot() == before
    from fairy.experiments.budget import BudgetStop
    with pytest.raises(BudgetStop, match="episode_slot_already_consumed"):
        moved.reserve_episode("another-run", "hybrid", "smoke", "smoke_hybrid")
    assert not old_hybrid.exists()


def test_missing_mapped_ledger_fails_closed(relocated):
    workspace, old_hybrid = relocated
    with pytest.raises(FileNotFoundError, match="Relocated evidence is missing"):
        LiveLedger(old_hybrid / "missing-ledger.json")
    assert not (workspace / "llm-jev-hybrid/missing-ledger.json").exists()
    assert not old_hybrid.exists()


def test_relative_and_unmapped_paths_keep_their_meaning(relocated, tmp_path):
    workspace, _ = relocated
    assert paths.resolve_saved_path("runs/new", root=workspace) == workspace / "runs/new"
    assert paths.resolve_saved_path(tmp_path / "unrelated") == tmp_path / "unrelated"


def test_mapping_cannot_escape_workspace(relocated):
    workspace, old_hybrid = relocated
    (workspace / "workspace-paths.json").write_text(json.dumps({
        "path_mapping": {str(old_hybrid): "../outside"}}))
    with pytest.raises(ValueError):
        paths.resolve_saved_path(old_hybrid / "ledger.json")
