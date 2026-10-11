"""Pour continuity: copper masks and GND-island detection (Phase 1).

No routing behavior changes here — this suite pins the analysis model:
zone outline + clearance pullback around foreign copper + same-net copper
add-back + min_thickness sliver removal, then 4-connectivity components.
"""

import numpy as np
import pytest

from kicad_autorouter.board_model import (
    BoardModel,
    Footprint,
    Pad,
    Point,
    Track,
    Via,
    Zone,
    board_model,
)
from kicad_autorouter.routing.grid import create_grid_from_model
from kicad_autorouter.routing.pour import (
    gnd_islands,
    ground_nets_with_zones,
    pour_copper_mask,
    pour_islands,
)
from kicad_autorouter.sexpr import parse_file

from .helpers import make_fp

FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures"


def _rect_zone(uuid, net, layers=("F.Cu",), x0=0.0, y0=0.0, x1=40.0, y1=30.0,
               clearance=0.5, min_thickness=0.25):
    return Zone(
        uuid=uuid, layers=tuple(layers),
        polygon=(Point(x0, y0), Point(x1, y0), Point(x1, y1), Point(x0, y1)),
        net_name=net, clearance_mm=clearance, min_thickness_mm=min_thickness,
    )


def _board(fps=(), zones=(), tracks=(), vias=(), edge=50.0):
    fps = tuple(fps)
    return BoardModel(
        footprints=fps,
        keepout_zones=(),
        zones=tuple(zones),
        tracks=tuple(tracks),
        vias=tuple(vias),
        nets={},
        by_ref={fp.ref: fp for fp in fps},
        edge_cuts=(
            (Point(-edge, -edge), Point(edge, -edge)),
            (Point(edge, -edge), Point(edge, edge)),
            (Point(edge, edge), Point(-edge, edge)),
            (Point(-edge, edge), Point(-edge, -edge)),
        ),
        footprint_region={fp.uuid: 0 for fp in fps},
    )


def _track(x1, y1, x2, y2, net, layer="F.Cu", width=0.25):
    return Track(start=Point(x1, y1), end=Point(x2, y2),
                 width_mm=width, layer=layer, net_name=net)


@pytest.mark.unit
class TestCopperMask:
    def test_empty_zone_seeds_copper(self):
        model = _board(zones=[_rect_zone("z1", "GND")])
        grid = create_grid_from_model(model)
        mask = pour_copper_mask(model, grid, "GND", "F.Cu")
        assert mask.any()
        # Other layer untouched (single-layer zone).
        assert not pour_copper_mask(model, grid, "GND", "B.Cu").any()

    def test_foreign_track_cuts_pour(self):
        # A full-height foreign track splits the pour into left/right.
        model = _board(
            zones=[_rect_zone("z1", "GND")],
            tracks=[_track(20.0, -5.0, 20.0, 35.0, "SIG")],
        )
        grid = create_grid_from_model(model)
        islands = pour_islands(model, grid, "GND")["F.Cu"]
        assert len(islands) == 2
        assert all(not isl.has_connection for isl in islands)

    def test_same_net_pad_feeds_island(self):
        gnd_pad_fp = make_fp("J1", "uuid-j1", 10.0, 15.0, ("GND",))
        model = _board(
            fps=[gnd_pad_fp],
            zones=[_rect_zone("z1", "GND")],
            tracks=[_track(20.0, -5.0, 20.0, 35.0, "SIG")],
        )
        grid = create_grid_from_model(model)
        islands = pour_islands(model, grid, "GND")["F.Cu"]
        assert len(islands) == 2
        fed = [isl for isl in islands if isl.has_connection]
        assert len(fed) == 1
        assert fed[0].centroid_mm[0] < 20.0

    def test_tht_barrel_bridges_layers(self):
        # THT GND pad: copper on both layers even for a single-layer zone
        # pair (two zone outlines, one per layer, same net).
        fp = make_fp("J1", "uuid-j1", 20.0, 15.0, ("GND",), tht=True)
        model = _board(
            fps=[fp],
            zones=[_rect_zone("z1", "GND", ("F.Cu",)),
                   _rect_zone("z2", "GND", ("B.Cu",))],
        )
        grid = create_grid_from_model(model)
        for layer in ("F.Cu", "B.Cu"):
            islands = pour_islands(model, grid, "GND")[layer]
            assert len(islands) == 1
            assert islands[0].has_connection

    def test_non_ground_net_ignored_by_gnd(self):
        model = _board(zones=[_rect_zone("z1", "VCC")])
        grid = create_grid_from_model(model)
        assert gnd_islands(model, grid)["F.Cu"] == []

    def test_zone_without_outline_skipped(self):
        from dataclasses import replace
        z = replace(_rect_zone("z1", "GND"), polygon=())
        model = _board(zones=[z])
        grid = create_grid_from_model(model)
        assert pour_islands(model, grid, "GND")["F.Cu"] == []


@pytest.mark.unit
class TestGroundNetDiscovery:
    def test_finds_zoned_ground_nets(self):
        model = _board(zones=[_rect_zone("z1", "GND"),
                              _rect_zone("z2", "GND_PWR"),
                              _rect_zone("z3", "VCC")])
        assert ground_nets_with_zones(model) == ["GND", "GND_PWR"]

    def test_empty_without_zones(self):
        assert ground_nets_with_zones(_board()) == []


@pytest.mark.unit
class TestIslandOrdering:
    def test_largest_first(self):
        model = _board(
            zones=[_rect_zone("z1", "GND")],
            tracks=[_track(30.0, -5.0, 30.0, 35.0, "SIG")],
        )
        grid = create_grid_from_model(model)
        islands = pour_islands(model, grid, "GND")["F.Cu"]
        assert len(islands) == 2
        counts = [isl.cell_count for isl in islands]
        assert counts == sorted(counts, reverse=True)
        assert islands[0].area_mm2 > islands[1].area_mm2
        assert islands[0].zone_uuids == ("z1",)


class TestFixtureCharacterization:
    """Recorded island counts on real fixtures (characterization, not spec).

    If a deliberate copper-model change moves these, update the numbers
    AND justify — same convention as test_placement_parity.py. Recorded
    2026-10-11: tube111's 487 tracks carve its 6 GND zones into 10 (F.Cu)
    + 26 (B.Cu) islands; unrouted DCCF fragments on pad cutouts alone.
    """

    def test_tube111_island_counts(self):
        model = board_model(parse_file(FIXTURES / "tube111.kicad_pcb"))
        grid = create_grid_from_model(model)
        out = gnd_islands(model, grid)
        assert len(out["F.Cu"]) == 10
        assert len(out["B.Cu"]) == 26
        # Biggest island per layer is fed; small fragments include dead copper.
        assert out["F.Cu"][0].has_connection
        assert out["B.Cu"][0].has_connection
        assert any(not isl.has_connection for isl in out["F.Cu"])

    def test_dccf_island_counts(self):
        model = board_model(parse_file(FIXTURES / "DCCF.sved.kicad_pcb"))
        grid = create_grid_from_model(model)
        out = gnd_islands(model, grid)
        assert len(out["F.Cu"]) == 54
        assert len(out["B.Cu"]) == 50
