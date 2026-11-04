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
from kicad_autorouter.sexpr import parse_file

from .helpers import make_fp

FIXTURES = Path("tests/fixtures")

CORNERS = {
    "XNW": (65.0, 55.0),
    "XNE": (205.0, 55.0),
    "XSW": (65.0, 135.0),
    "XSE": (205.0, 135.0),
}


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
    assert len(result.candidates) == 4  # baseline + 3 shuffles
    assert [c["seed"] for c in result.candidates] == [-1, 7, 8, 9]
    assert result.winner_seed in (-1, 7, 8, 9)
    best = _winner(result.candidates)
    assert result.winner_seed == best["seed"]
    assert result.nets_routed + result.nets_failed == 2
    assert result.pcb_tree is not None


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
    baseline = next(c for c in result.candidates if c["seed"] == -1)
    winner = next(c for c in result.candidates if c["seed"] == result.winner_seed)
    assert (winner["nets_routed"], -winner["score"]) >= \
           (baseline["nets_routed"], -baseline["score"])


def test_refine_refuses_protected_and_overcap():
    tree, model = _xcross_model()
    result, _, _ = refine_routing(
        model, ["X1", "X2"], {"X1"},
        params=_params(refine_seeds=2, refine_seed=0),
        original_pcb_tree=tree)
    assert result.failed_reasons["X1"].startswith("protected")
    assert result.nets_routed == 1  # eligible X2 still routed
    assert result.pcb_tree is not None

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
    calls = []

    def _fake_route_nets(**kw):
        order = tuple(kw["net_names"])
        calls.append(order)
        # Seed 5->(A,B,C), 6->(B,A,C), 7->(C,A,B): the cheap routed-1
        # candidate must lose to routed-2 on count; among routed-2 the
        # cheaper score wins.
        routed = 1 if order[0] == "C" else 2
        score = 0.5 if order[0] == "C" else (5.0 if order == ("B", "A", "C") else 10.0)
        return RoutingResult(
            tracks_added=2, vias_added=1, nets_routed=routed,
            nets_failed=0, drc_clean=True, drc_violations=0,
            pcb_tree="TREE", total_wire_mm=score,
        ), "CG", "RG"

    monkeypatch.setattr(pipeline, "route_nets", _fake_route_nets)
    _, model = _xcross_model()
    result, cost_grid, routing_grid = refine_routing(
        model, ["A", "B", "C"], set(), params=_params(refine_seeds=3, refine_seed=5))
    assert result.winner_seed == 6
    assert calls[-1] == ("B", "A", "C")  # final replay uses the winner order
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
