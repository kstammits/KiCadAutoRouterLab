"""Via crossing: two diagonal nets must cross each other to route.

Four 2-pad SMD resistors in the corners of the minimal fixture board form
an X: net X1 joins NW-SE, net X2 joins NE-SW. The diagonals of a rectangle
must intersect in plan view, so whatever paths the router finds, the two
nets' copper crosses somewhere; with both nets' SMD pads on F.Cu the only
sane way through is a layer hop. X2 routes first and takes F.Cu straight;
X1 must drop to B.Cu and come back (2 vias).

Route order matters and is load-bearing: the second net is the one that
hops. (X1 first lets X2 dodge around the empty board edge instead — on a
4-directional grid every monotone path costs the same 440 cells, and the
hop adds 2 via costs on top, so grid-optimal A* can never prefer it. The
via saving only materializes after any-angle smoothing, which A* cannot
see. Routing the diagonal first pins the middle and forces the hop.)

Thresholds calibrated by running (0.5mm grid routes both nets in <0.1s
with exactly 2 vias); the properties are structural, not numeric.
"""

import pytest

from dataclasses import replace as dc_replace
from pathlib import Path

from shapely.geometry import Polygon

from kicad_autorouter.board_model import Footprint, KeepoutZone, Point, board_model
from kicad_autorouter.board_model import vias as board_vias
from kicad_autorouter.io import add_footprint_to_tree
from kicad_autorouter.routing.pipeline import RoutingParams, route_nets
from kicad_autorouter.sexpr import parse_file

from .helpers import make_fp, plan_segments

FIXTURES = Path("tests/fixtures")

# Minimal fixture outline is (55,45)-(215,145); 10mm inset corners.
CORNERS = {
    "XNW": (65.0, 55.0),
    "XNE": (205.0, 55.0),
    "XSW": (65.0, 135.0),
    "XSE": (205.0, 135.0),
}


pytestmark = pytest.mark.minimal


def _corner_resistor(ref: str, uuid: str, x_mm: float, y_mm: float,
                     net_a: str, net_b: str) -> Footprint:
    """2-pad SMD resistor at board position; pads/courtyard in board coords."""
    return make_fp(ref, uuid, x_mm, y_mm, (net_a, net_b),
                   footprint_id="Test:R_0805_XCross")


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
        pcb_tree = add_footprint_to_tree(pcb_tree, _corner_resistor(*spec), "Test:R_0805_XCross")
    return pcb_tree, board_model(pcb_tree)


# Central keepout sits exactly on the X intersection (135, 95): both
# straight diagonals pass through it. F.Cu-only, so X1 must detour around
# it on F.Cu while X2's B.Cu bridge can still dive underneath — proving
# the block is layer-specific and neither net goes wonky (through it,
# around the board, or via inside it).
KEEPOUT_X0, KEEPOUT_X1 = 130.0, 140.0
KEEPOUT_Y0, KEEPOUT_Y1 = 90.0, 100.0


def _central_keepout() -> KeepoutZone:
    return KeepoutZone(
        uuid="xcross-kz-center",
        layers=("F.Cu",),
        polygon=(
            Point(KEEPOUT_X0, KEEPOUT_Y0),
            Point(KEEPOUT_X1, KEEPOUT_Y0),
            Point(KEEPOUT_X1, KEEPOUT_Y1),
            Point(KEEPOUT_X0, KEEPOUT_Y1),
        ),
        tracks_allowed=False,
        vias_allowed=False,
        pads_allowed=False,
        copper_pour_allowed=False,
        footprints_allowed=False,
    )


def _xcross_model_with_keepout():
    """X-crossing model plus the central F.Cu keepout (model-level inject).

    Writeback only carries tracks/vias, so the tree needs no zone writer —
    same pattern as the THT-free-via walls.
    """
    pcb_tree, model = _xcross_model()
    model = dc_replace(model, keepout_zones=model.keepout_zones + (_central_keepout(),))
    return pcb_tree, model


@pytest.mark.parametrize("net,pad_a,pad_b", [
    ("X1", "XNW", "XSE"),
    ("X2", "XNE", "XSW"),
])
def test_solo_diagonal_shortcuts_to_near_euclidean(net, pad_a, pad_b):
    """A single diagonal must route near-Euclidean, not Manhattan.

    Each net alone on an empty board: the straight corner-to-corner line
    is 161.2mm; the Manhattan grid walk is 220mm. A* only emits Manhattan
    stairs, so the diagonal comes entirely from the smoother — and the
    emit gate must not veto it on raster dust (the 0.5mm raster cannot
    resolve a legal pad entry, and the old fallback emitted the raw
    220mm staircase). 1.1x Euclidean leaves room for pad entries and one
    fillet while failing a full-Manhattan walk (1.37x).
    """
    import math

    pcb_tree, model = _xcross_model()
    assert {c.ref for c in model.nets[net]} == {pad_a, pad_b}
    result, _, _ = route_nets(
        model, [net], set(),
        params=RoutingParams(run_drc=False, grid_resolution_mm=0.5),
        original_pcb_tree=pcb_tree,
    )
    assert result.nets_routed == 1, f"solo diagonal must route: {result}"
    assert result.nets_failed == 0, f"no net may fail: {result}"
    straight = math.hypot(
        CORNERS[pad_a][0] - CORNERS[pad_b][0],
        CORNERS[pad_a][1] - CORNERS[pad_b][1])
    assert result.total_wire_mm <= 1.1 * straight, (
        f"solo diagonal {net} {result.total_wire_mm:.1f}mm should approach "
        f"the {straight:.1f}mm straight line, not the 220mm Manhattan walk"
    )


def test_diagonal_nets_route_with_via_crossing():
    pcb_tree, model = _xcross_model()
    assert {c.ref for c in model.nets["X1"]} == {"XNW", "XSE"}
    assert {c.ref for c in model.nets["X2"]} == {"XNE", "XSW"}

    result, _, _ = route_nets(
        model, ["X2", "X1"], set(),
        params=RoutingParams(run_drc=False, grid_resolution_mm=0.5),
        original_pcb_tree=pcb_tree,
    )

    assert result.nets_routed == 2, f"both diagonals must route: {result}"
    assert result.nets_failed == 0, f"no net may fail: {result}"
    assert result.tracks_added > 0
    # Crossing on a single layer is illegal (clearance), and walking around
    # a corner-to-corner track costs far more than a layer hop at the
    # default via price, so the crossing must go multi-layer.
    assert result.vias_added >= 2, (
        f"expected a via pair hopping the crossing, got {result.vias_added}"
    )

    assert result.pcb_tree is not None
    x2_front = plan_segments(result.pcb_tree, "X2", "F.Cu")
    x1_back = plan_segments(result.pcb_tree, "X1", "B.Cu")
    assert x2_front, "X2 should hold F.Cu (routed first, straight through)"
    assert x1_back, "X1 should spend part of the route on B.Cu"
    # The B.Cu span must genuinely bridge X2's F.Cu copper in plan view —
    # not a pointless hop elsewhere.
    assert any(
        back.intersects(front) for back in x1_back for front in x2_front
    ), "X1's B.Cu span must cross X2's F.Cu copper in plan view"


def test_diagonal_nets_respect_keepout():
    """Central F.Cu keepout: routing still succeeds, block stays clean.

    The keepout covers the ideal-diagonal intersection, so a straight
    shot would have to detour (F.Cu) or dive under it (B.Cu). X2 holds
    F.Cu around the block while X1 threads across on F.Cu and bridges
    down the far side on B.Cu, crossing X2's copper in plan view. This
    guards the valuable property: an unrelated keepout neither breaks
    the crossing nor collects copper/vias, and wire stays bounded
    versus the no-keepout baseline (nothing goes wonky). Plan-view
    checks run on densely sampled arcs (see helpers.plan_segments):
    long sweeps bow far off their chords, so chord approximation would
    misplace the copper.
    """
    params = RoutingParams(run_drc=False, grid_resolution_mm=0.5)
    base_tree, base_model = _xcross_model()
    base, _, _ = route_nets(
        base_model, ["X2", "X1"], set(),
        params=params, original_pcb_tree=base_tree,
    )
    assert base.nets_routed == 2, f"baseline must route: {base}"

    pcb_tree, model = _xcross_model_with_keepout()
    result, _, _ = route_nets(
        model, ["X2", "X1"], set(),
        params=params, original_pcb_tree=pcb_tree,
    )

    assert result.nets_routed == 2, f"keepout must not break routing: {result}"
    assert result.nets_failed == 0, f"no net may fail: {result}"
    assert result.vias_added >= 2, (
        f"expected the via-pair crossing to survive the keepout, got {result.vias_added}"
    )

    assert result.pcb_tree is not None
    x2_front = plan_segments(result.pcb_tree, "X2", "F.Cu")
    x1_back = plan_segments(result.pcb_tree, "X1", "B.Cu")
    assert x2_front, "X2 should still hold F.Cu"
    assert x1_back, "X1 should still bridge on B.Cu"
    assert any(
        back.intersects(front) for back in x1_back for front in x2_front
    ), "X1's B.Cu span must still cross X2's F.Cu copper in plan view"

    # The block is F.Cu-only: no F.Cu copper and no via annulus may enter
    # it; B.Cu stays free to pass underneath.
    block = Polygon([
        (KEEPOUT_X0, KEEPOUT_Y0), (KEEPOUT_X1, KEEPOUT_Y0),
        (KEEPOUT_X1, KEEPOUT_Y1), (KEEPOUT_X0, KEEPOUT_Y1),
    ])
    fcu_segs = (
        plan_segments(result.pcb_tree, "X1", "F.Cu")
        + plan_segments(result.pcb_tree, "X2", "F.Cu")
    )
    assert fcu_segs, "expected F.Cu copper to check against the keepout"
    assert not any(seg.intersects(block) for seg in fcu_segs), (
        "no F.Cu copper may enter the keepout"
    )
    for via in board_vias(result.pcb_tree):
        assert not block.intersects(
            Polygon([
                (via.position.x_mm - via.size_mm / 2.0, via.position.y_mm - via.size_mm / 2.0),
                (via.position.x_mm + via.size_mm / 2.0, via.position.y_mm - via.size_mm / 2.0),
                (via.position.x_mm + via.size_mm / 2.0, via.position.y_mm + via.size_mm / 2.0),
                (via.position.x_mm - via.size_mm / 2.0, via.position.y_mm + via.size_mm / 2.0),
            ])
        ), f"via {via.net_name} drilled inside the keepout at {via.position}"

    # Bounded detour: the keepout must not send the router off the rails.
    assert result.total_wire_mm <= 1.5 * base.total_wire_mm, (
        f"keepout wire {result.total_wire_mm:.1f}mm blew up vs "
        f"baseline {base.total_wire_mm:.1f}mm"
    )
