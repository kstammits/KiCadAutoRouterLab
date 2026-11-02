"""Capsule-exact pad exclusion stamps + graded halo value tiers.

Elongated pads stamp oriented stadiums (spine along the major axis) instead
of discs sized by pad length; the secondary halo is three tiers — hard core
on copper only, steep-but-passable toll over the clearance margin, soft
shoulder beyond. Markers: unit (synthetic) + dccf (one DCCF regression).
"""

from dataclasses import replace

import numpy as np
import pytest

from kicad_autorouter.board_model import (
    BoardModel,
    NetConnection,
    Pad,
    Point,
)
from kicad_autorouter.routing.grid import board_to_grid, create_grid_from_model
from kicad_autorouter.routing.obstacles import (
    FANOUT_HALO_OUTER,
    FANOUT_HALO_STEEP,
    THT_EXCLUSION,
    _pad_rect_corners_mm,
    _pad_spine_mm,
    _stamp_capsule_zone,
    _stamp_pad_zone,
    build_fanout_cost,
)
from kicad_autorouter.routing.router import SECONDARY_BLOCKED
from tests.helpers import make_fp

pytestmark = pytest.mark.unit


def _pad(x, y, w, h, shape="oval", angle=0.0, net="B",
         pad_type="smd", drill=0.0):
    return Pad(number="1", net_name=net, position=Point(x, y),
               size_mm=(w, h), shape=shape, angle_deg=angle,
               pad_type=pad_type, drill_mm=drill, layers=("F.Cu",))


def _rect_cuts(x0, y0, x1, y1):
    c = [Point(x0, y0), Point(x1, y0), Point(x1, y1), Point(x0, y1)]
    return tuple((c[i], c[(i + 1) % 4]) for i in range(4))


def _one_pad_model(x0, y0, pad, net="B", extent=30.0):
    fp = replace(make_fp("U1", "uuid-u1", x0, y0, nets=(net,)),
                 pads=(replace(pad, position=Point(x0, y0)),))
    return BoardModel(
        footprints=(fp,),
        keepout_zones=(),
        nets={net: (NetConnection(ref="U1", pad_number="1"),)},
        by_ref={"U1": fp},
        edge_cuts=_rect_cuts(x0 - extent, y0 - extent,
                             x0 + extent, y0 + extent),
        footprint_region={"uuid-u1": 0},
    )


class TestPadSpine:
    def test_oval_horizontal(self):
        x1, y1, x2, y2, minor = _pad_spine_mm(_pad(50, 50, 6, 2))
        assert (x1, y1, x2, y2) == pytest.approx((48, 50, 52, 50))
        assert minor == pytest.approx(1.0)

    def test_oval_vertical(self):
        x1, y1, x2, y2, minor = _pad_spine_mm(_pad(50, 50, 2, 6))
        assert (x1, y1, x2, y2) == pytest.approx((50, 48, 50, 52))
        assert minor == pytest.approx(1.0)

    def test_oval_rotated_90(self):
        # R(-90): local X maps to vertical — same spine as vertical oval.
        x1, y1, x2, y2, minor = _pad_spine_mm(_pad(50, 50, 6, 2, angle=90.0))
        assert sorted((round(v, 9) for v in (x1, x2))) == [50, 50]
        assert sorted((round(v, 9) for v in (y1, y2))) == [48, 52]
        assert minor == pytest.approx(1.0)

    def test_rect_spine_degenerate_rect_covers_it(self):
        # Rect-family pads inflate via _pad_rect_corners_mm, not a spine:
        # the spine is degenerate (a full-length capsule would balloon
        # past square pins like D6/Q1 and U2 pin 1).
        x1, y1, x2, y2, minor = _pad_spine_mm(_pad(50, 50, 4, 2, shape="rect"))
        assert (x1, y1, x2, y2) == pytest.approx((50, 50, 50, 50))
        assert minor == pytest.approx(1.0)

    def test_rect_corners_axis_aligned(self):
        corners = _pad_rect_corners_mm(_pad(50, 50, 4, 2, shape="rect"), 0.5)
        assert corners == pytest.approx(
            [(47.5, 48.5), (52.5, 48.5), (52.5, 51.5), (47.5, 51.5)])

    def test_rect_corners_rotated_90(self):
        corners = _pad_rect_corners_mm(
            _pad(50, 50, 4, 2, shape="rect", angle=90.0), 0.0)
        xs = sorted(round(x, 9) for x, _ in corners)
        ys = sorted(round(y, 9) for _, y in corners)
        assert xs == [49, 49, 51, 51]
        assert ys == [48, 48, 52, 52]

    def test_circle_degenerates_to_disc(self):
        x1, y1, x2, y2, minor = _pad_spine_mm(_pad(50, 50, 2, 2, shape="circle"))
        assert (x1, y1, x2, y2) == pytest.approx((50, 50, 50, 50))
        assert minor == pytest.approx(1.0)

    def test_unknown_shape_falls_back_to_disc(self):
        x1, y1, x2, y2, minor = _pad_spine_mm(_pad(50, 50, 6, 2, shape="foo"))
        assert (x1, y1, x2, y2) == pytest.approx((50, 50, 50, 50))
        assert minor == pytest.approx(3.0)


class TestCapsuleStamp:
    def test_slot_covers_ends_stays_tight_across(self, grid):
        # 6x1 slot at res 0.1 with 0.5 halo: radius 1.0 across the width.
        x0 = grid.origin_mm[0] + 30
        y0 = grid.origin_mm[1] + 30
        layer = np.zeros((grid.height_cells, grid.width_cells), dtype=np.int16)
        _stamp_capsule_zone(layer, grid, x0 - 2.5, y0, x0 + 2.5, y0, 1.0, 100)
        col, row = board_to_grid(grid, x0, y0)
        assert layer[row, col] == 100  # copper center
        # Past the slot end but inside end-cap + halo: covered.
        col_e, row_e = board_to_grid(grid, x0 + 3.2, y0)
        assert layer[row_e, col_e] == 100
        # Sideways past copper + halo: free (the old max/2 disc stains this).
        col_s, row_s = board_to_grid(grid, x0, y0 + 1.6)
        assert layer[row_s, col_s] == 0

    def test_degenerate_spine_matches_disc(self, grid):
        x0 = grid.origin_mm[0] + 40
        y0 = grid.origin_mm[1] + 40
        layer = np.zeros((grid.height_cells, grid.width_cells), dtype=np.int16)
        _stamp_capsule_zone(layer, grid, x0, y0, x0, y0, 0.5, 100)
        col, row = board_to_grid(grid, x0, y0)
        assert layer[row, col] == 100
        col_f, row_f = board_to_grid(grid, x0 + 2.0, y0)
        assert layer[row_f, col_f] == 0

    def test_square_rect_stays_box_shaped(self, grid):
        # Regression for square pins (D6/Q1, U2 pin 1) ballooning into
        # capsules: a 1x1 rect with 0.5 halo stamps a ~2x2 box, not a
        # capsule stretching a half-width past the copper.
        x0 = grid.origin_mm[0] + 50
        y0 = grid.origin_mm[1] + 50
        layer = np.zeros((grid.height_cells, grid.width_cells), dtype=np.int16)
        _stamp_pad_zone(
            layer, grid, _pad(x0, y0, 1, 1, shape="rect"), 0.5, 100)
        col, row = board_to_grid(grid, x0, y0)
        assert layer[row, col] == 100
        # Past copper (0.5) + halo (0.5) + a cell of slack: free on all
        # four sides — no capsule overhang along either axis.
        for dx, dy in ((1.6, 0.0), (-1.6, 0.0), (0.0, 1.6), (0.0, -1.6)):
            c, r = board_to_grid(grid, x0 + dx, y0 + dy)
            assert layer[r, c] == 0, (dx, dy)

    def test_rect_slot_covers_ends_without_sideways_balloon(self, grid):
        # 6x1 rect slot, 0.5 halo: ends covered, flanks tight.
        x0 = grid.origin_mm[0] + 60
        y0 = grid.origin_mm[1] + 60
        layer = np.zeros((grid.height_cells, grid.width_cells), dtype=np.int16)
        _stamp_pad_zone(
            layer, grid, _pad(x0, y0, 6, 1, shape="rect"), 0.5, 100)
        col_e, row_e = board_to_grid(grid, x0 + 3.4, y0)
        assert layer[row_e, col_e] == 100  # end + halo covered
        col_s, row_s = board_to_grid(grid, x0, y0 + 1.6)
        assert layer[row_s, col_s] == 0  # flank past halo is free


class TestHaloTiers:
    def _model(self):
        # Foreign 6x1 oval slot at (20, 20), own 1x1 pad at (60, 20).
        ffp = replace(make_fp("U1", "uuid-u1", 20.0, 20.0, nets=("B",)),
                      pads=(_pad(20.0, 20.0, 6, 1, net="B"),))
        ofp = replace(make_fp("U2", "uuid-u2", 60.0, 60.0, nets=("A",)),
                      pads=(_pad(60.0, 60.0, 1, 1, net="A"),))
        model = BoardModel(
            footprints=(ffp, ofp),
            keepout_zones=(),
            nets={"A": (NetConnection(ref="U2", pad_number="1"),),
                  "B": (NetConnection(ref="U1", pad_number="1"),)},
            by_ref={"U1": ffp, "U2": ofp},
            edge_cuts=_rect_cuts(0, 0, 80, 80),
            footprint_region={"uuid-u1": 0, "uuid-u2": 0},
        )
        return model

    def test_tiers_core_margin_shoulder_free(self):
        model = self._model()
        grid = create_grid_from_model(model, 0.1, 2.0)
        sec = build_fanout_cost(model, grid, "A", clearance_mm=0.2,
                                track_half_mm=0.125)

        def at(x, y):
            c, r = board_to_grid(grid, x, y)
            return sec[0, r, c]

        # Core (copper, r=0.5): hard. Margin (0.5..0.825): steep toll,
        # passable. Shoulder (..1.025): soft. Beyond: free.
        assert at(20.0, 20.0) == THT_EXCLUSION
        assert at(20.0, 20.6) == FANOUT_HALO_STEEP
        assert at(20.0, 20.9) == FANOUT_HALO_OUTER
        assert at(20.0, 22.0) == 0
        # Own-net pad is excluded entirely.
        assert at(60.0, 60.0) == 0

    def test_steep_tier_is_passable(self):
        assert FANOUT_HALO_STEEP < SECONDARY_BLOCKED


@pytest.mark.dccf
def test_dccf_vin_terminal_no_longer_sealed(dccf_model):
    """VIN terminal 2's own cell drops below the blocking threshold.

    It read THT_EXCLUSION (10000, sealed) under disc halos sized by pad
    length; capsule stamps + the copper-only hard core must unseal it.
    """
    from kicad_autorouter.routing.grid import create_grid_from_model
    from kicad_autorouter.routing.params import (
        RoutingParams,
        resolve_clearance,
        resolve_track_width,
    )
    from kicad_autorouter.routing.pipeline import _terminals_for_net

    model = dccf_model
    params = RoutingParams(grid_resolution_mm=0.1)
    grid = create_grid_from_model(model, 0.1, 2.0)
    width_mm = resolve_track_width(model, params, "VIN", ("VIN",))
    clearance_mm = resolve_clearance(model, params, "VIN")
    sec = build_fanout_cost(model, grid, "VIN", clearance_mm=clearance_mm,
                            track_half_mm=width_mm / 2.0)
    terminals = _terminals_for_net(model, grid, "VIN")
    c, r, lyr = terminals[2]
    assert sec[lyr, r, c] < SECONDARY_BLOCKED
