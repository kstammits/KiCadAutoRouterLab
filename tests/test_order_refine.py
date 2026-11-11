"""Order-space refine: several shuffled re-routes, keep the best.

Uses the X-crossing fixture (two diagonal nets that must cross): order
changes which net goes straight and which hops layers, so different
seeds score differently. All tests synthetic + fast (0.5mm grid).
"""

from pathlib import Path

import pytest

import kicad_autorouter.routing.pipeline as pipeline
from kicad_autorouter.board_model import board_model
from kicad_autorouter.io import add_footprint_to_tree
from kicad_autorouter.routing.pipeline import (
    RoutingParams,
    RoutingResult,
    refine_routing,
)
from kicad_autorouter.routing.route_raw import route_raw
from kicad_autorouter.sexpr import parse_file

from .helpers import make_fp

FIXTURES = Path("tests/fixtures")

CORNERS = {
    "XNW": (65.0, 55.0),
    "XNE": (205.0, 55.0),
    "XSW": (65.0, 135.0),
    "XSE": (205.0, 135.0),
}


pytestmark = pytest.mark.minimal


def _xcross_model():
    """Minimal fixture + 4 corner resistors wired into crossing X1/X2 nets."""
    pcb_tree = parse_file(FIXTURES / "minimal.kicad_pcb")
    specs = [
        ("XNW", "xcross-nw-uuid-0001", *CORNERS["XNW"], "X1", "STUB_NW"),
        ("XNE", "xcross-ne-uuid-0002", *CORNERS["XNE"], "X2", "STUB_NE"),
        ("XSW", "xcross-sw-uuid-0003", *CORNERS["XSW"], "X2", "STUB_SW"),
        ("XSE", "xcross-se-uuid-0004", *CORNERS["XSE"], "X1", "STUB_SE"),
    ]
    for spec in specs:
        pcb_tree = add_footprint_to_tree(
            pcb_tree,
            make_fp(spec[0], spec[1], spec[2], spec[3], (spec[4], spec[5]),
                    footprint_id="Test:R_0805_XCross"),
            "Test:R_0805_XCross")
    return pcb_tree, board_model(pcb_tree)


def _params(**kw):
    base = dict(grid_resolution_mm=0.5, run_drc=False, refill_zones=False)
    base.update(kw)
    return RoutingParams(**base)


def _winner(candidates):
    """Best candidate by (nets routed, then lowest score)."""
    return max(candidates, key=lambda c: (c["nets_routed"], -c["score"]))


def test_refine_explores_seeds_and_picks_best():
    tree, model = _xcross_model()
    result, _, _ = refine_routing(
        model, ["X1", "X2"], set(), params=_params(refine_seeds=3, refine_seed=7),
        original_pcb_tree=tree)
    # Seed 9 shuffles back to the identity order, so it is skipped silently.
    assert [c["seed"] for c in result.candidates] == [7, 8]
    # Trial 0 runs the input order; later trials permute the tangle group.
    assert result.candidates[0]["order"] == ["X1", "X2"]
    assert result.winner_seed in (7, 8)
    best = _winner(result.candidates)
    assert result.winner_seed == best["seed"]
    assert result.nets_routed + result.nets_failed == 2
    assert result.pcb_tree is not None


def test_refine_dedupes_two_net_orders(monkeypatch):
    """Five seeds on two nets collapse to at most two unique orders."""
    from kicad_autorouter.routing.pipeline import RawRoutes
    calls = []

    def _fake_route_raw(**kw):
        calls.append(tuple(kw["net_names"]))
        return RawRoutes(model=kw["model"], nets_routed=2, nets_failed=0,
                         total_wire_mm=1.0,
                         expansions_total=10), "CG", "RG"

    def _fake_finish(raw, params):
        return RoutingResult(
            tracks_added=1, vias_added=0, nets_routed=raw.nets_routed,
            nets_failed=0, drc_clean=True, drc_violations=0,
            pcb_tree="TREE"), "CG", "RG"

    monkeypatch.setattr(pipeline, "route_raw", _fake_route_raw)
    monkeypatch.setattr(pipeline, "finish_routes", _fake_finish)
    _, model = _xcross_model()
    result, _, _ = refine_routing(
        model, ["X1", "X2"], set(),
        params=_params(refine_seeds=5, refine_seed=0))
    assert len(result.candidates) <= 2
    assert len({tuple(c["order"]) for c in result.candidates}) == len(
        result.candidates)
    assert len(calls) == len(result.candidates)  # no wasted re-routes


def test_refine_singleton_groups_route_once(monkeypatch):
    """Isolated nets never permute: one routing call regardless of seeds."""
    from kicad_autorouter.routing.pipeline import RawRoutes
    calls = []

    def _fake_route_raw(**kw):
        calls.append(tuple(kw["net_names"]))
        return RawRoutes(model=kw["model"], nets_routed=1, nets_failed=0,
                         total_wire_mm=1.0,
                         expansions_total=10), "CG", "RG"

    def _fake_finish(raw, params):
        return RoutingResult(
            tracks_added=1, vias_added=0, nets_routed=raw.nets_routed,
            nets_failed=0, drc_clean=True, drc_violations=0,
            pcb_tree="TREE"), "CG", "RG"

    monkeypatch.setattr(pipeline, "route_raw", _fake_route_raw)
    monkeypatch.setattr(pipeline, "finish_routes", _fake_finish)
    monkeypatch.setattr(pipeline, "_tangle_groups",
                        lambda nets, model: [[n] for n in nets])
    _, model = _xcross_model()
    result, _, _ = refine_routing(
        model, ["X1", "X2"], set(),
        params=_params(refine_seeds=5, refine_seed=0))
    assert [c["order"] for c in result.candidates] == [["X1", "X2"]]
    assert len(calls) == 1


def test_refine_deterministic_same_seeds():
    kwargs = dict(net_names=["X1", "X2"], protected_nets=set(),
                   params=_params(refine_seeds=3, refine_seed=42))
    tree_a, model_a = _xcross_model()
    tree_b, model_b = _xcross_model()
    ra, _, _ = refine_routing(model_a, original_pcb_tree=tree_a, **kwargs)
    rb, _, _ = refine_routing(model_b, original_pcb_tree=tree_b, **kwargs)
    assert ra.winner_seed == rb.winner_seed
    assert [(c["seed"], c["nets_routed"], c["score"]) for c in ra.candidates] == \
           [(c["seed"], c["nets_routed"], c["score"]) for c in rb.candidates]
    assert (ra.tracks_added, ra.vias_added) == (rb.tracks_added, rb.vias_added)


def test_refine_never_worse_than_current():
    tree, model = _xcross_model()
    result, _, _ = refine_routing(
        model, ["X1", "X2"], set(), params=_params(refine_seeds=3, refine_seed=1),
        original_pcb_tree=tree)
    winner = next(c for c in result.candidates if c["seed"] == result.winner_seed)
    if result.winner_seed != -1:
        assert result.nets_routed == winner["nets_routed"]
    for c in result.candidates:
        if c["seed"] == result.winner_seed:
            continue
        assert (winner["nets_routed"], -winner["score"]) >= \
               (c["nets_routed"], -c["score"])


def test_refine_keeps_current_when_all_trials_fail():
    tree, model = _xcross_model()
    result, cost_grid, routing_grid = refine_routing(
        model, ["X1", "X2"], set(),
        params=_params(refine_seeds=2, refine_seed=0, max_expansions_per_net=1),
        original_pcb_tree=tree)
    assert result.winner_seed == -1
    assert result.tracks_added == 0 and result.vias_added == 0
    # Relay: trial 0 fails every net, so trial 1's failed-first order is
    # the identity order again — dedup skips the provably-identical
    # re-route (routing is deterministic per order on a fresh grid).
    assert len(result.candidates) == 1
    assert result.candidates[0]["nets_routed"] == 0
    assert cost_grid is None and routing_grid is None


def test_refine_refuses_protected_and_overcap():
    tree, model = _xcross_model()
    result, _, _ = refine_routing(
        model, ["X1", "X2"], {"X1"},
        params=_params(refine_seeds=2, refine_seed=0),
        original_pcb_tree=tree)
    assert result.failed_reasons["X1"].startswith("protected")
    # X2's diagonal used to die at the emit gate: the keep rule kinked a
    # walkable chord into foreign 0805 copper (smoother-manufactured trip).
    # Keep-cut kinks are walkability-gated now, so X2 routes cleanly alone
    # (verified DRC-clean outside this test) — the gate still guards real
    # touches, it just no longer fires on manufactured geometry.
    assert result.nets_routed == 1
    assert result.tracks_added > 0
    assert result.pcb_tree is not None
    _, model_probe = _xcross_model()
    raw, _, _ = route_raw(model_probe, ["X2"], set(),
                          params=_params(refine_seeds=2, refine_seed=0))
    assert raw.nets_routed == 1
    assert "X2" not in raw.ripup.net_failures
    # Single attempt, no retry burn (deterministic success first try).
    assert raw.ripup.attempts["X2"] == 0

    result2, cost_grid2, routing_grid2 = refine_routing(
        model, ["X1", "X2"], set(),
        params=_params(refine_seeds=2, refine_seed=0, max_terminals_per_net=1))
    assert set(result2.failed_reasons) == {"X1", "X2"}
    assert all("over max_terminals_per_net" in r
               for r in result2.failed_reasons.values())
    # Nothing routed, nothing committed: grids pass through untouched.
    assert cost_grid2 is None and routing_grid2 is None


def test_refine_cancel_keeps_current():
    _, model = _xcross_model()
    calls = {"n": 0}

    def _cancel():
        calls["n"] += 1
        return True

    result, cost_grid, routing_grid = refine_routing(
        model, ["X1", "X2"], set(),
        params=_params(refine_seeds=3, refine_seed=0),
        should_cancel=_cancel)
    assert result.cancelled
    assert result.winner_seed == -1
    assert result.tracks_added == 0 and result.vias_added == 0
    assert cost_grid is None and routing_grid is None
    assert calls["n"] >= 1


def test_refine_picks_best_order_with_stubbed_router(monkeypatch):
    """Winner rule: most nets routed wins; ties break on lowest score."""
    from kicad_autorouter.routing.pipeline import RawRoutes
    calls = []

    def _fake_route_raw(**kw):
        order = tuple(kw["net_names"])
        calls.append(order)
        # Seed 5->identity (A,B,C); 6->(B,A,C); 7->(C,A,B): the cheap
        # routed-1 candidate must lose to routed-2 on count; among routed-2
        # the cheaper score wins.
        routed = 1 if order[0] == "C" else 2
        score = 0.5 if order[0] == "C" else (5.0 if order == ("B", "A", "C") else 10.0)
        return RawRoutes(model=kw["model"], nets_routed=routed,
                         nets_failed=3 - routed, total_wire_mm=score,
                         expansions_total=100), "CG", "RG"

    finished = []

    def _fake_finish(raw, params):
        finished.append(raw)
        return RoutingResult(
            tracks_added=9, vias_added=0, nets_routed=raw.nets_routed,
            nets_failed=0, drc_clean=True, drc_violations=0,
            pcb_tree="TREE"), "CG", "RG"

    monkeypatch.setattr(pipeline, "route_raw", _fake_route_raw)
    monkeypatch.setattr(pipeline, "finish_routes", _fake_finish)
    monkeypatch.setattr(pipeline, "_tangle_groups",
                        lambda nets, model: [list(nets)])
    _, model = _xcross_model()
    result, cost_grid, routing_grid = refine_routing(
        model, ["A", "B", "C"], set(), params=_params(refine_seeds=3, refine_seed=5))
    assert result.winner_seed == 6
    assert calls[0] == ("A", "B", "C")  # trial 0 runs the input order
    assert finished[0].total_wire_mm == 5.0  # winner goes to finish
    assert result.expansions == 300
    assert (cost_grid, routing_grid) == ("CG", "RG")
    assert result.pcb_tree == "TREE"


def test_refine_params_validation():
    assert RoutingParams().refine_seeds == 3
    assert RoutingParams().refine_seed == 0
    assert RoutingParams.from_dict(
        {"refine_seeds": 5, "refine_seed": 11}).refine_seeds == 5
    with pytest.raises(ValueError, match="capped at 8"):
        RoutingParams.from_dict({"refine_seeds": 9})
    with pytest.raises(ValueError, match="must be >= 0"):
        RoutingParams.from_dict({"refine_seed": -1})


def test_tangle_groups_clusters_overlapping_nets():
    from kicad_autorouter.routing.pipeline import _refine_order, _tangle_groups
    _, model = _xcross_model()
    # Diagonal X nets span the same board area: one tangle group.
    assert _tangle_groups(["X1", "X2"], model) == [["X1", "X2"]]
    # Unknown (pad-less) nets never merge: singleton groups hold position.
    assert _tangle_groups(["X1", "NOPE"], model) == [["X1"], ["NOPE"]]
    assert _refine_order(["A", "X1", "B"], [["X1"]], 3) == ["A", "X1", "B"]
    assert _refine_order(["A", "X1", "B"], [["X1"]], 99) == ["A", "X1", "B"]
    # Multi-net groups permute deterministically.
    o = _refine_order(["X1", "X2"], [["X1", "X2"]], 6)
    assert sorted(o) == ["X1", "X2"]
    assert o == _refine_order(["X1", "X2"], [["X1", "X2"]], 6)


def test_split_route_raw_finish_matches_route_nets():
    """Stage split guard: route_nets == finish_routes(route_raw(...))."""
    from kicad_autorouter.routing.pipeline import finish_routes, route_raw
    from kicad_autorouter.sexpr import to_sexpr
    tree, model = _xcross_model()
    kw = dict(net_names=["X1", "X2"], protected_nets=set(),
               params=_params(), original_pcb_tree=tree)
    r1, _, _ = pipeline.route_nets(model, **kw)
    raw, _, _ = route_raw(model, **kw)
    r2, _, _ = finish_routes(raw, _params())
    assert to_sexpr(r1.pcb_tree) == to_sexpr(r2.pcb_tree)
    for field in ("tracks_added", "vias_added", "nets_routed", "nets_failed",
                  "total_wire_mm", "expansions", "stitch_vias_added",
                  "stitch_tracks_added", "arcs_added", "fanout_vias_added",
                  "gnd_islands_before", "gnd_islands_after", "gnd_stripped"):
        assert getattr(r1, field) == getattr(r2, field), field


def test_refine_order_failed_first():
    # (Moved from test_smooth_polish.py: failure-first relay ordering
    # belongs to the refine contract, not the smoother.)
    from kicad_autorouter.routing.pipeline import _refine_order

    groups = [["X1", "X2", "X3"]]
    legacy = _refine_order(["X1", "X2", "X3"], groups, 7)
    assert _refine_order(["X1", "X2", "X3"], groups, 7, ()) == legacy
    assert _refine_order(["X1", "X2", "X3"], groups, 7, {"X9"}) == legacy
    order = _refine_order(["X1", "X2", "X3"], groups, 7, {"X3"})
    assert order[0] == "X3"
    assert sorted(order) == ["X1", "X2", "X3"]


def test_refine_relays_previous_failures(monkeypatch):
    """Trial k+1 routes trial k's failed nets first."""
    import kicad_autorouter.routing.pipeline as pipeline
    from kicad_autorouter.routing.pipeline import RawRoutes, RoutingResult
    from kicad_autorouter.routing.router import RipUpManager

    calls = []

    def _fake_route_raw(**kw):
        order = tuple(kw["net_names"])
        calls.append(order)
        rip = RipUpManager()
        if len(calls) == 1:
            rip.record_failure("X2", "boom")  # first trial fails X2
            routed, failed = 1, 1
        else:
            routed, failed = 2, 0
        return RawRoutes(model=kw["model"], nets_routed=routed,
                         nets_failed=failed, ripup=rip,
                         total_wire_mm=1.0, expansions_total=5), "CG", "RG"

    def _fake_finish(raw, params):
        return RoutingResult(
            tracks_added=1, vias_added=0, nets_routed=raw.nets_routed,
            nets_failed=0, drc_clean=True, drc_violations=0,
            pcb_tree="TREE"), "CG", "RG"

    monkeypatch.setattr(pipeline, "route_raw", _fake_route_raw)
    monkeypatch.setattr(pipeline, "finish_routes", _fake_finish)
    _, model = _xcross_model()
    refine_routing(model, ["X1", "X2"], set(),
                   params=_params(refine_seeds=2, refine_seed=0),
                   original_pcb_tree="TREE")
    assert calls[0] == ("X1", "X2")  # identity trial first
    assert calls[1][0] == "X2"  # relay: failed net first
