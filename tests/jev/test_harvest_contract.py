"""Offline evidence lifecycle and conditional range coverage checks."""
from copy import deepcopy
import socket
import pytest

from fairy.agents.Jev.harvest_contract import harvest_range_menu, observed_unplanted


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("Offline harvest contract tests must not access network")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def result(eid, method, value, **kwargs):
    return {
        "event_id": eid, "event": "tool_end", "tool_name": "TractorApp__" + method,
        "executed": True, "status": "ok", "result": value,
        "arguments": {"start_ridge": 0, "end_ridge": 3}, **kwargs,
    }


def harvest(eid=1, ids=None):
    return result(eid, "harvest", {"status": "ok", "harvested_ridges": [0, 1, 2, 3] if ids is None else ids})


def menu(events):
    return harvest_range_menu(events, [0, 63], 4)


def test_unknown_retains_all_structurally_legal_ranges():
    m = menu([])
    assert m["start_candidates"] == list(range(64))
    assert m["end_candidates_by_start"]["20"] == [20, 21, 22, 23]
    assert m["end_candidates_by_start"]["63"] == [63]
    assert not m["runtime_enabled"]


def test_exclusion_applies_to_start_and_all_endpoints_without_crossing_holes():
    m = menu([harvest(ids=[2, 3, 9])])
    assert 2 not in m["start_candidates"]
    assert m["end_candidates_by_start"]["0"] == [0, 1]
    assert m["end_candidates_by_start"]["7"] == [7, 8]
    assert m["facts"]["evidence"]["3"]["event_id"] == 1
    actual = {(int(s), e) for s, ends in m["end_candidates_by_start"].items() for e in ends}
    expected = {(s, e) for s in range(64) for e in range(s, min(64, s+4))
                if not set(range(s, e+1)) & {2, 3, 9}}
    assert actual == expected  # Neither unsafe combinations nor silent coverage loss.


@pytest.mark.parametrize("method,field", [
    ("plant_seeds", "planted_ridges"), ("replant_seeds", "replanted_ridges"),
])
def test_new_crop_cycle_reopens_only_returned_ridges(method, field):
    m = menu([harvest(), result(2, method, {"status": "ok", field: [1, 2]})])
    assert m["facts"]["excluded_ridges"] == [0, 3]
    assert m["end_candidates_by_start"]["1"] == [1, 2]
    # Reopened means unknown. It does not assert maturity or quota feasibility.
    m2 = menu([harvest(), result(2, method, {"status": "ok", field: [1, 2]}),
               harvest(3, [1])])
    assert m2["facts"]["excluded_ridges"] == [0, 1, 3]


def test_failed_or_ambiguous_reset_does_not_keep_stale_block():
    m = menu([harvest(), result(2, "plant_seeds", {"error": "execution uncertain"},
                               status="error")])
    assert not m["facts"]["excluded_ridges"]
    assert m["facts"]["invalidations"][0]["forgotten_ridges"] == [0, 1, 2, 3]


def test_unexecuted_plant_does_not_invalidate():
    m = menu([harvest(), result(2, "plant_seeds", None, executed=False)])
    assert m["facts"]["excluded_ridges"] == [0, 1, 2, 3]


@pytest.mark.parametrize("kind", ["environment_notification", "crop_state_invalidated", "episode_start"])
def test_environment_or_episode_boundary_forgets_old_prohibitions(kind):
    assert not menu([harvest(), {"event_id": 2, "event": kind}])["facts"]["excluded_ridges"]


def test_form_ridges_invalidates_previous_lifecycle():
    assert not menu([harvest(), result(2, "form_ridges", {"status": "ok"})])["facts"]["excluded_ridges"]


@pytest.mark.parametrize("ids", [[True], [-1], [64], [0, "1"], None])
def test_malformed_harvest_output_not_partially_accepted(ids):
    e = result(1, "harvest", {"status": "ok", "harvested_ridges": ids})
    assert not menu([e])["facts"]["excluded_ridges"]


def test_failed_harvest_is_not_success_and_preserves_prior_confirmed_return():
    failed = result(2, "harvest", {"error": "All ridges must be planted before harvest"},
                    status="error", error_stage="tool_return")
    assert not menu([failed])["facts"]["excluded_ridges"]
    assert menu([harvest(), failed])["facts"]["excluded_ridges"] == [0, 1, 2, 3]
    assert not menu([harvest(), {**failed, "error_stage": "tool_exception"}])["facts"]["excluded_ridges"]


def test_explicit_public_planted_observation_overrides_old_evidence():
    obs = result(2, "ignored", {"ridges": [
        {"ridge_id": 1, "planted": True}, {"ridge_id": 5, "planted": False},
        {"ridge_id": 2, "planted": "true"}, {"ridge_id": 3, "harvested": False},
    ]}, tool_name="FarmWorldApp__get_ridge_range_state")
    m = menu([harvest(), obs])
    assert m["facts"]["excluded_ridges"] == [0, 2, 3, 5]
    assert m["facts"]["evidence"]["5"]["result_path"] == "ridges[1].planted"


def test_all_excluded_yields_context_need_without_finishing_or_synthesizing_action():
    m = menu([harvest(ids=list(range(64)))])
    assert m["status"] == "needs_context" and not m["start_candidates"]
    assert not m["end_candidates_by_start"]
    assert "action" not in m and "task_success" not in m


def test_no_argument_inference_or_model_summary_evidence():
    e = result(1, "harvest", {"status": "ok"})
    llm = {"event_id": 2, "event": "decision_end", "result": {"harvested_ridges": [0, 1, 2, 3]}}
    assert not menu([e, llm])["facts"]["excluded_ridges"]


def test_input_immutable_and_chronology_not_silently_reordered():
    events = [harvest(), result(2, "unload_grain", {"status": "ok"})]
    before = deepcopy(events)
    assert menu(events)["facts"]["excluded_ridges"] == [0, 1, 2, 3]
    assert events == before
    for bad in ([harvest(), harvest()], [harvest(3), harvest(1)]):
        with pytest.raises(ValueError, match="strictly increasing"):
            menu(bad)


@pytest.mark.parametrize("bounds,width", [([2, 1], 4), ([0, True], 4), ([0, 63], 0), ([0, 63], True)])
def test_bad_declared_contract_rejected(bounds, width):
    with pytest.raises(ValueError):
        harvest_range_menu([], bounds, width)
