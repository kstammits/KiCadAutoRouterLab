"""Plane-tap fanout: inner-zone nets stub to the plane in four-layer mode.

Two-layer mode forces fanout_planes=False (prior behavior retained verbatim);
four-layer mode forces True (short outer stubs + through-vias tap In*.Cu).

Markers: minimal
"""

from dataclasses import replace
from pathlib import Path

import pytest

from kicad_autorouter.board_model import Point, Zone, board_model
from kicad_autorouter.io import add_footprint_to_tree
from kicad_autorouter.routing.pipeline import RoutingParams, route_nets
from kicad_autorouter.sexpr import parse_file
from kicad_autorouter.strategy import MODE_PRESETS, apply_mode

from .helpers import make_fp

FIXTURES = Path("tests/fixtures")


def _gnd_board(net="GND"):
    """Two 1-pad SMD footprints on one net + an In2.Cu plane zone for it."""
    pcb_tree = parse_file(FIXTURES / "minimal.kicad_pcb")
    for ref, uuid, x in (("PFA", "plane-a-uuid-0001", 80.0),
                         ("PFB", "plane-b-uuid-0002", 120.0)):
        fp = make_fp(ref, uuid, x, 95.0, (net,), layer="F.Cu",
                     footprint_id="Test:R_0805_PF")
        pcb_tree = add_footprint_to_tree(pcb_tree, fp, "Test:R_0805_PF")
    model = board_model(pcb_tree)
    plane = Zone(
        uuid="plane-zone-uuid",
        layers=("In2.Cu",),
        polygon=(Point(40, 60), Point(160, 60), Point(160, 130), Point(40, 130)),
        net_name=net,
    )
    model = replace(
        model,
        copper_layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"),
        zones=model.zones + (plane,),
    )
    return pcb_tree, model


@pytest.mark.minimal
def test_fanout_params_round_trip_and_mode_presets():
    assert RoutingParams().fanout_planes is False
    assert RoutingParams().fanout_plane_vias_max == 16
    p = RoutingParams(fanout_planes=True, fanout_plane_vias_max=4)
    assert RoutingParams.from_dict(p.to_dict()).fanout_planes is True
    assert RoutingParams.from_dict(p.to_dict()).fanout_plane_vias_max == 4
    # Unknown keys still dropped.
    assert RoutingParams.from_dict(
        {"fanout_planes": True, "bogus": 1}).fanout_planes is True
    # Modes own the switch: two-layer ignores inners, four-layer uses them.
    assert apply_mode({}, "two-layer")["fanout_planes"] is False
    assert apply_mode({}, "four-layer")["fanout_planes"] is True
    assert MODE_PRESETS["two-layer"]["fanout_planes"] is False
    assert MODE_PRESETS["four-layer"]["fanout_planes"] is True


@pytest.mark.minimal
def test_two_layer_ignores_inner_zone():
    pcb_tree, model = _gnd_board()
    res, _, _ = route_nets(
        model, ["GND"], set(),
        params=RoutingParams(run_drc=False, grid_resolution_mm=0.5,
                             fanout_planes=False),
        original_pcb_tree=pcb_tree,
    )
    assert res.fanout_vias_added == 0
    assert res.fanout_tracks_added == 0
    assert res.nets_routed == 1, f"outer MST must still route: {res.failed_reasons}"


@pytest.mark.minimal
def test_four_layer_plane_fanout_stubs():
    pcb_tree, model = _gnd_board()
    res, _, _ = route_nets(
        model, ["GND"], set(),
        params=RoutingParams(run_drc=False, grid_resolution_mm=0.5,
                             fanout_planes=True),
        original_pcb_tree=pcb_tree,
    )
    assert res.nets_routed == 1, f"plane net must tap: {res.failed_reasons}"
    assert res.nets_failed == 0
    assert res.fanout_vias_added == 2, f"one stub via per SMD pad: {res.failed_reasons}"
    assert res.fanout_tracks_added == 2
    assert not res.failed_reasons


@pytest.mark.minimal
def test_signal_net_with_inner_zone_still_routes_outer():
    pcb_tree, model = _gnd_board(net="SIG1")
    res, _, _ = route_nets(
        model, ["SIG1"], set(),
        params=RoutingParams(run_drc=False, grid_resolution_mm=0.5,
                             fanout_planes=True),
        original_pcb_tree=pcb_tree,
    )
    assert res.fanout_vias_added == 0
    assert res.nets_routed == 1, f"signal must use MST: {res.failed_reasons}"
