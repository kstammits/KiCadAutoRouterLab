"""Local GND shove: evict-split-restore retry for GND-walled nets.

Synthetic board: signal S must cross a full-height GND wall (both
layers, spanning past the grid) next to its left pin. S fails on the
intact board, routes after per-pin GND eviction, GND pad-components
stay at 1, and only the conflicting middle piece stays evicted (the
rest restores). Rollback paths: nothing evicted -> board untouched;
signal-owned walls are never victims.
"""

import pytest

from dataclasses import replace

from kicad_autorouter.board_model import NetConnection, board_model
from kicad_autorouter.io import add_footprint_to_tree
from kicad_autorouter.routing.pipeline import RoutingParams, route_nets
from kicad_autorouter.routing.shove import (
    boxes_for_net,
    evict_gnd_in_boxes,
    gnd_pad_components,
    retry_failed_with_shove,
)
from kicad_autorouter.sexpr import SExpr, parse_file

from .helpers import make_fp, stamp_edge_cuts
from .test_pour import _board, _track

FIXTURES = "tests/fixtures"


def _wall_board(wall_net="GND"):
    fa = make_fp("A", "uuid-a", 3.0, 6.0, ("S",), hw=0.25, hh=0.25)
    fb = make_fp("B", "uuid-b", 17.0, 6.0, ("S",), hw=0.25, hh=0.25)
    g1 = make_fp("G1", "uuid-g1", 2.0, -15.0, ("GND",), hw=0.25, hh=0.25,
                 layer="B.Cu")
    g2 = make_fp("G2", "uuid-g2", 17.0, -15.0, ("GND",), hw=0.25, hh=0.25,
                 layer="B.Cu")
    model = _board(
        fps=[fa, fb, g1, g2],
        tracks=[
            _track(5.0, -30.0, 5.0, 30.0, wall_net, layer="F.Cu", width=0.5),
            _track(5.0, -30.0, 5.0, 30.0, wall_net, layer="B.Cu", width=0.5),
            _track(2.0, -15.0, 17.0, -15.0, "GND", layer="B.Cu", width=0.5),
        ],
        edge=25.0,
    )
    model = replace(model, nets={
        "S": (NetConnection("A", "1"), NetConnection("B", "1")),
    })
    tree = parse_file(f"{FIXTURES}/minimal.kicad_pcb")
    for fp, lib in ((fa, "Test:R_0805"), (fb, "Test:R_0805"),
                    (g1, "Test:R_0805"), (g2, "Test:R_0805")):
        tree = add_footprint_to_tree(tree, fp, lib)
    tree = stamp_edge_cuts(tree, half=25.0)
    for t in model.tracks:
        tree = SExpr(tree.head, tree.args + (SExpr("segment", (
            SExpr("start", (t.start.x_mm, t.start.y_mm)),
            SExpr("end", (t.end.x_mm, t.end.y_mm)),
            SExpr("width", (t.width_mm,)),
            SExpr("layer", (t.layer,)),
            SExpr("net", (t.net_name,)),
        )),))
    return model, tree


def _rail_board():
    # GND continuity depends on one full-width rail: S must cross it,
    # and the crossing middle cannot restore, so the fracture gate
    # fails without gap healing (G1 | G2 split). The B.Cu K-wall seals
    # S's via-dive escape (padless obstacle net, never evicted), so S
    # also fails on the intact board.
    fa = make_fp("A", "uuid-a", 9.0, 4.0, ("S",), hw=0.25, hh=0.25)
    fb = make_fp("B", "uuid-b", 9.0, 10.0, ("S",), hw=0.25, hh=0.25)
    g1 = make_fp("G1", "uuid-g1", -20.0, 6.0, ("GND",), hw=0.25, hh=0.25)
    g2 = make_fp("G2", "uuid-g2", 20.0, 6.0, ("GND",), hw=0.25, hh=0.25)
    model = _board(
        fps=[fa, fb, g1, g2],
        tracks=[
            _track(-30.0, 6.0, 30.0, 6.0, "GND", layer="F.Cu", width=0.5),
            _track(-30.0, 6.0, 30.0, 6.0, "K", layer="B.Cu", width=0.5),
        ],
        edge=25.0,
    )
    model = replace(model, nets={
        "S": (NetConnection("A", "1"), NetConnection("B", "1")),
    })
    tree = parse_file(f"{FIXTURES}/minimal.kicad_pcb")
    for fp, lib in ((fa, "Test:R_0805"), (fb, "Test:R_0805"),
                    (g1, "Test:R_0805"), (g2, "Test:R_0805")):
        tree = add_footprint_to_tree(tree, fp, lib)
    tree = stamp_edge_cuts(tree, half=25.0)
    for t in model.tracks:
        tree = SExpr(tree.head, tree.args + (SExpr("segment", (
            SExpr("start", (t.start.x_mm, t.start.y_mm)),
            SExpr("end", (t.end.x_mm, t.end.y_mm)),
            SExpr("width", (t.width_mm,)),
            SExpr("layer", (t.layer,)),
            SExpr("net", (t.net_name,)),
        )),))
    return model, tree


def _params(**kw):
    base = dict(grid_resolution_mm=0.5, run_drc=False,
                strip_gnd_blockers=False, stitch_gnd_vias=False)
    base.update(kw)
    return RoutingParams(**base)


@pytest.mark.unit
class TestShoveRetry:
    def test_walled_signal_fails_then_routes_with_shove(self):
        model, tree = _wall_board()
        params = _params()
        res, _, _ = route_nets(model, ["S"], set(), params=params,
                               original_pcb_tree=tree)
        assert res.nets_failed == 1  # sealed by the GND wall
        assert gnd_pad_components(model) == 1

        model2, tree2, reports = retry_failed_with_shove(
            model, tree, ["S"], set(), params, radius_mm=4.0)
        rep = reports["S"]
        assert rep.routed, rep.reason
        assert rep.evicted > 0
        # The hole piece conflicts with the new path and stays out;
        # everything else restores.
        assert 0 <= rep.restored < rep.evicted
        assert gnd_pad_components(model2) == 1
        final = board_model(tree2)
        assert any(t.net_name == "S" for t in final.tracks)
        # GND link far from the pins survives untouched.
        assert any(t.net_name == "GND" and t.start.y_mm == -15.0
                   for t in final.tracks)

    def test_no_gnd_in_boxes_leaves_board_untouched(self):
        model, tree = _wall_board()
        params = _params()
        model2, tree2, reports = retry_failed_with_shove(
            model, tree, ["S"], set(), params, radius_mm=0.05)
        rep = reports["S"]
        assert not rep.routed
        assert rep.evicted == 0
        assert rep.reason == "no GND in shove boxes"
        assert tree2 is tree and model2 is model

    def test_signal_wall_is_never_a_victim(self):
        model, tree = _wall_board(wall_net="K")
        params = _params()
        res, _, _ = route_nets(model, ["S"], set(), params=params,
                               original_pcb_tree=tree)
        assert res.nets_failed == 1
        _, _, reports = retry_failed_with_shove(
            model, tree, ["S"], set(), params, radius_mm=4.0)
        assert not reports["S"].routed
        assert reports["S"].evicted == 0

    def test_evict_splits_tracks_not_amputates(self):
        model, tree = _wall_board()
        boxes = boxes_for_net(model, "S", 4.0)
        assert len(boxes) == 2
        ev = evict_gnd_in_boxes(model, tree, boxes)
        assert ev.evicted_count > 0
        # Outside-box wall pieces survive on both layers.
        assert any(t.net_name == "GND" and t.layer == "F.Cu" for t in ev.model.tracks)
        assert any(t.net_name == "GND" and t.layer == "B.Cu" for t in ev.model.tracks)
        # The untouched GND link survives whole.
        assert any(t.net_name == "GND" and t.start.y_mm == -15.0
                   for t in ev.model.tracks)

    def test_gnd_gap_healed_adopt(self):
        # The crossing middle cannot restore (S routes through it), so
        # the fracture gate fails — but a go-around exists and the gap
        # flight bridges it: adopted with gap_routed set.
        model, tree = _rail_board()
        params = _params()
        res, _, _ = route_nets(model, ["S"], set(), params=params,
                               original_pcb_tree=tree)
        assert res.nets_failed == 1  # sealed by the full-width GND rail
        assert gnd_pad_components(model) == 1

        model2, tree2, reports = retry_failed_with_shove(
            model, tree, ["S"], set(), params, radius_mm=4.0)
        rep = reports["S"]
        assert rep.routed, rep.reason
        assert rep.gap_routed is True
        # Partial restore: the crossing middle stays evicted.
        assert rep.evicted > rep.restored
        assert gnd_pad_components(model2) == 1
        final = board_model(tree2)
        assert any(t.net_name == "S" for t in final.tracks)
        # Bridge copper joins the split stubs on GND (stubs + bridge
        # outnumber the two kept stubs).
        assert sum(1 for t in final.tracks if t.net_name == "GND") >= 3

    def test_gnd_gap_failure_rolls_back(self):
        # A failed gap flight keeps the fracture rollback: the board is
        # untouched and the reason names the flight failure.
        import unittest.mock as mock

        model, tree = _rail_board()
        params = _params()
        with mock.patch(
                "kicad_autorouter.routing.shove.route_gap_terminals",
                return_value=(None, "boom", None, 0.25, None)):
            model2, tree2, reports = retry_failed_with_shove(
                model, tree, ["S"], set(), params, radius_mm=4.0)
        rep = reports["S"]
        assert not rep.routed
        assert rep.gap_routed is False
        assert "boom" in rep.reason
        assert tree2 is tree and model2 is model
        assert not any(t.net_name == "S" for t in model2.tracks)
