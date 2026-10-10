"""Via crossing: two diagonal nets must cross each other to route.

Four 2-pad SMD resistors in the corners of the minimal fixture board form
an X: net X1 joins NW-SE, net X2 joins NE-SW. The diagonals of a rectangle
must intersect in plan view, so whatever paths the router finds, the two
nets' copper crosses somewhere; with both nets' SMD pads on F.Cu the only
sane way through is a layer hop. X1 routes first and takes F.Cu straight;
X2 must drop to B.Cu and come back (2 vias).

Thresholds calibrated by running (0.5mm grid routes both nets in <0.1s
with exactly 2 vias); the properties are structural, not numeric.
"""

from pathlib import Path

from kicad_autorouter.board_model import Footprint, board_model
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


def test_diagonal_nets_route_with_via_crossing():
    pcb_tree, model = _xcross_model()
    assert {c.ref for c in model.nets["X1"]} == {"XNW", "XSE"}
    assert {c.ref for c in model.nets["X2"]} == {"XNE", "XSW"}

    result, _, _ = route_nets(
        model, ["X1", "X2"], set(),
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
    x1_front = plan_segments(result.pcb_tree, "X1", "F.Cu")
    x2_back = plan_segments(result.pcb_tree, "X2", "B.Cu")
    assert x1_front, "X1 should hold F.Cu (routed first, straight through)"
    assert x2_back, "X2 should spend part of the route on B.Cu"
    # The B.Cu span must genuinely bridge X1's F.Cu copper in plan view —
    # not a pointless hop elsewhere.
    assert any(
        back.intersects(front) for back in x2_back for front in x1_front
    ), "X2's B.Cu span must cross X1's F.Cu copper in plan view"
