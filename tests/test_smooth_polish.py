"""Smoothing effort levels, the retry ladder, Polish, and relay ordering.

Covers ``smooth_effort`` (float 0..3, default 2.0): the resolver anchors,
the deterministic attempt ladder, effort-0 raw emission, and the Polish
re-smoothing pass (gate-clean, wire never grows). Plus failure-first
relay ordering in refine. All synthetic + fast (0.5mm grid).
"""

import math

import pytest

from kicad_autorouter.board_model import board_model
from kicad_autorouter.io import add_footprint_to_tree
from kicad_autorouter.routing.output import routes_to_tracks_vias
from kicad_autorouter.routing.pipeline import RoutingParams, refine_routing
from kicad_autorouter.routing.route_raw import route_raw
from kicad_autorouter.routing.router import RipUpManager
from kicad_autorouter.routing.polish import polish_routes
from kicad_autorouter.routing.refine import _refine_order
from kicad_autorouter.routing.smooth import effort_rungs, resolve_config
from kicad_autorouter.sexpr import parse_file

from .helpers import make_fp

FIXTURES = "tests/fixtures"

CORNERS = {
    "XNW": (65.0, 55.0),
    "XNE": (205.0, 55.0),
    "XSW": (65.0, 135.0),
    "XSE": (205.0, 135.0),
}


def _xcross_model():
    pcb_tree = parse_file(f"{FIXTURES}/minimal.kicad_pcb")
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


# --- resolver anchors -------------------------------------------------------


def test_resolve_config_default_is_legacy():
    cfg = resolve_config(2.0, _params())
    assert cfg == {"fit_arcs": True, "max_radius_mm": 10.0,
                   "min_leg_mm": 0.3, "sample_mm": 0.05,
                   "keep_min_radius_mm": 0.5}


def test_resolve_config_quick_has_no_arcs_or_keep():
    cfg = resolve_config(1.0, _params())
    assert cfg["fit_arcs"] is False
    assert cfg["keep_min_radius_mm"] == 0.0


def test_resolve_config_fractional_interpolates_keep():
    assert resolve_config(1.75, _params())["keep_min_radius_mm"] == pytest.approx(0.25)
    assert resolve_config(1.75, _params())["fit_arcs"] is True


def test_resolve_config_hot_rung():
    cfg = resolve_config(3.0, _params())
    assert cfg["fit_arcs"] is True
    assert cfg["keep_min_radius_mm"] == pytest.approx(1.0)
    assert cfg["sample_mm"] == pytest.approx(0.025)


def test_effort_rungs_shapes():
    assert effort_rungs(2.0) == [2.0]
    assert effort_rungs(1.0) == [1.0]
    assert effort_rungs(2.5) == [2.0, 2.5]
    assert effort_rungs(3.0) == [2.0, 2.5, 3.0]
    assert max(len(effort_rungs(e)) for e in (0.0, 1.0, 2.0, 2.9, 3.0)) <= 3


def test_smooth_effort_validation():
    assert RoutingParams.from_dict({"smooth_effort": 3}).smooth_effort == 3
    assert RoutingParams().to_dict()["smooth_effort"] == 2.0
    for bad in (-0.5, 3.5, float("nan"), True, "high"):
        with pytest.raises(ValueError):
            RoutingParams.from_dict({"smooth_effort": bad})


# --- effort in the route path ------------------------------------------------


def _max_step(branches):
    steps = []
    for branch in branches:
        for (c1, r1, _), (c2, r2, _) in zip(branch, branch[1:]):
            steps.append(max(abs(c2 - c1), abs(r2 - r1)))
    return max(steps) if steps else 0


def test_effort_zero_emits_raw_manhattan():
    _, model = _xcross_model()
    raw, _, _ = route_raw(model, ["X1", "X2"], set(),
                          params=_params(smooth_effort=0.0))
    assert raw.nets_routed == 2
    for net, result in raw.all_results.items():
        pieces = tuple(getattr(result, "arc_pieces", ()) or ())
        assert all(p == () for p in pieces)  # no fitted arcs emitted
        branches = list(getattr(result, "branches", ()))
        assert _max_step(branches) <= 1  # no shortcut chords


def test_effort_one_emits_no_arcs():
    _, model = _xcross_model()
    raw, _, _ = route_raw(model, ["X1", "X2"], set(),
                          params=_params(smooth_effort=1.0))
    assert raw.nets_routed == 2
    for result in raw.all_results.values():
        pieces = tuple(getattr(result, "arc_pieces", ()) or ())
        assert all(p == () for p in pieces)


def test_effort_three_beats_or_matches_default():
    # The ladder's first rung is the default config, so best-of-rungs
    # can never lose to the single pass on the same deterministic search.
    _, model = _xcross_model()
    base, _, _ = route_raw(model, ["X1", "X2"], set(), params=_params())
    hot, _, _ = route_raw(model, ["X1", "X2"], set(),
                          params=_params(smooth_effort=3.0))
    assert hot.nets_routed == 2
    base_wire = sum(r.wire_mm for r in base.all_results.values())
    hot_wire = sum(r.wire_mm for r in hot.all_results.values())
    assert hot_wire <= base_wire + 1e-9


# --- polish ------------------------------------------------------------------


def test_polish_noop_at_zero_effort():
    tree, model = _xcross_model()
    raw, _, rg = route_raw(model, ["X1", "X2"], set(), params=_params())
    new, report = polish_routes(raw.all_results, raw.width_map, raw.model,
                                rg, _params(), ["X1", "X2"], 0.0)
    assert report["skipped"] == 2
    assert new == raw.all_results


def test_polish_never_grows_wire_and_converts():
    tree, model = _xcross_model()
    raw, _, rg = route_raw(model, ["X1", "X2"], set(),
                           params=_params(smooth_effort=1.0))
    before = {n: r.wire_mm for n, r in raw.all_results.items()}
    new, report = polish_routes(raw.all_results, raw.width_map, raw.model,
                                rg, _params(), ["X1", "X2", "NOPE"], 3.0)
    assert report["polished"] + report["kept"] + report["reverted"] + report["skipped"] == 3
    assert report["nets"]["NOPE"]["status"] == "skipped"
    assert report["wire_saved_mm"] >= 0.0
    for net, entry in report["nets"].items():
        if net == "NOPE":
            continue
        assert entry["wire_after_mm"] <= entry["wire_before_mm"] + 1e-9
        assert entry["wire_before_mm"] == pytest.approx(before[net])
    # Polished branches still convert to board copper.
    tracks, vias, arcs = routes_to_tracks_vias(
        new, rg, net_class_widths=raw.width_map)
    assert tracks and all(t.net_name in ("X1", "X2") for t in tracks)


# --- relay ordering ------------------------------------------------------------


def test_refine_order_failed_first():
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
