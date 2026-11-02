"""Panel-aware routing: one net spanning two board regions.

Regression for the tube111 +12V case — a netlist name shared across
panels must route per region (each side up to its own pads; assembly
bridges join the boards), never as one copper track across the gap.
"""

from pathlib import Path

from shapely.geometry import Point as ShapelyPoint
from shapely.geometry import Polygon as ShapelyPolygon

from kicad_autorouter.board_model import (
    BoardModel,
    BoardRegion,
    NetConnection,
    Point,
    board_model,
    tracks,
)
from kicad_autorouter.io import parse_file as parse_tree
from kicad_autorouter.routing.pipeline import (
    RoutingParams,
    _group_terminals_by_region,
    create_grid_from_model,
    route_nets,
)
from kicad_autorouter.sexpr import parse_file
from tests.helpers import make_fp

TUBE111_PCB = Path(__file__).parent / "fixtures" / "tube111.kicad_pcb"
MINIMAL_PCB = Path(__file__).parent / "fixtures" / "minimal.kicad_pcb"


def _rect_cuts(x0, y0, x1, y1):
    c = [Point(x0, y0), Point(x1, y0), Point(x1, y1), Point(x0, y1)]
    return tuple((c[i], c[(i + 1) % 4]) for i in range(4))


def _two_panel_model() -> BoardModel:
    """Two 20x20mm panels with a 10mm gap; SIG has two pads per panel."""
    poly_a = (Point(0, 0), Point(20, 0), Point(20, 20), Point(0, 20))
    poly_b = (Point(30, 0), Point(50, 0), Point(50, 20), Point(30, 20))
    regions = (
        BoardRegion(uuid="reg-a", polygon=poly_a, bbox=(0, 20, 0, 20)),
        BoardRegion(uuid="reg-b", polygon=poly_b, bbox=(30, 50, 0, 20)),
    )
    fps = (
        make_fp("U1", "uuid-a1", 5.0, 10.0, nets=("SIG",)),
        make_fp("U2", "uuid-a2", 15.0, 10.0, nets=("SIG",)),
        make_fp("U3", "uuid-b1", 35.0, 10.0, nets=("SIG",)),
        make_fp("U4", "uuid-b2", 45.0, 10.0, nets=("SIG",)),
    )
    return BoardModel(
        footprints=fps,
        keepout_zones=(),
        nets={"SIG": tuple(NetConnection(ref=fp.ref, pad_number="1") for fp in fps)},
        by_ref={fp.ref: fp for fp in fps},
        edge_cuts=_rect_cuts(0, 0, 20, 20) + _rect_cuts(30, 0, 50, 20),
        board_regions=regions,
        footprint_region={
            "uuid-a1": 0, "uuid-a2": 0, "uuid-b1": 1, "uuid-b2": 1,
        },
    )


def test_group_terminals_by_region_splits_panels():
    model = _two_panel_model()
    grid = create_grid_from_model(model, 0.5, 2.0)
    groups = _group_terminals_by_region(model, grid, "SIG", None)
    assert [ri for ri, _ in groups] == [0, 1]
    assert [len(t) for _, t in groups] == [2, 2]


def test_tube111_plus12v_spans_two_regions():
    """tube111 +12V pads live on two panels — routing must split, not join."""
    model = board_model(parse_file(TUBE111_PCB))
    assert len(model.board_regions) == 3
    grid = create_grid_from_model(model, 0.5, 2.0)
    groups = dict(
        (ri, len(t)) for ri, t in _group_terminals_by_region(model, grid, "+12V", None)
    )
    assert set(groups) == {1, 2}
    assert sum(groups.values()) == len(model.nets["+12V"])


def test_two_panel_net_never_crosses_gap():
    """SIG routes per panel; no emitted copper touches the void between."""
    model = _two_panel_model()
    tree = parse_tree(MINIMAL_PCB)
    result, _, _ = route_nets(
        model, ["SIG"], set(),
        params=RoutingParams(run_drc=False, grid_resolution_mm=0.25),
        original_pcb_tree=tree,
    )
    assert result.nets_routed == 1, f"SIG must route per-region: {result.failed_reasons}"
    assert result.nets_failed == 0
    polys = [
        ShapelyPolygon([(p.x_mm, p.y_mm) for p in r.polygon])
        for r in model.board_regions
    ]
    assert result.pcb_tree is not None
    checked = 0
    for tr in tracks(result.pcb_tree):
        if tr.net_name != "SIG":
            continue
        mid = ShapelyPoint(
            (tr.start.x_mm + tr.end.x_mm) / 2.0,
            (tr.start.y_mm + tr.end.y_mm) / 2.0,
        )
        assert any(p.covers(mid) for p in polys), (
            f"cross-region copper across the panel gap: {tr.start}-{tr.end}"
        )
        checked += 1
    assert checked > 0, "expected per-region SIG copper"
