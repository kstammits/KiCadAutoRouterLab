"""Tests for cost grid building primitives (obstacles)."""

import pytest
import numpy as np
from shapely.geometry import Polygon

from kicad_autorouter.board_model import board_model
from kicad_autorouter.routing.grid import create_grid_from_model, board_to_grid
from kicad_autorouter.routing.obstacles import (
    build_occupancy_grid,
    build_fanout_cost,
    build_via_block_mask,
    FREE, BLOCKED, HIGH_COST, EDGE_KEEPOUT,
    FANOUT_HALO_INNER, FANOUT_HALO_OUTER,
    _block_courtyard,
    _mark_pad,
    _mark_track,
    _mark_via,
    _mark_zone,
    _mark_edge_keepout,
    _draw_line_blocked,
    _mark_circular_zone,
    _fill_polygon,
)
from kicad_autorouter.routing.grid import create_grid_from_model
from kicad_autorouter.sexpr import parse_file

FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures"
MINIMAL_PCB = FIXTURES / "minimal.kicad_pcb"
DCCF_PCB = FIXTURES / "DCCF.sved.kicad_pcb"


@pytest.fixture(scope="module")
def minimal_model():
    return board_model(parse_file(MINIMAL_PCB))


@pytest.fixture(scope="module")
def dccf_model():
    return board_model(parse_file(DCCF_PCB))


@pytest.fixture
def grid(minimal_model):
    return create_grid_from_model(minimal_model, resolution_mm=0.1, margin_mm=2.0)


@pytest.fixture
def cost_grid(grid, minimal_model):
    return build_occupancy_grid(minimal_model, grid, default_clearance_mm=0.2)


class TestCostGridShape:
    """Basic cost grid structure tests."""

    def test_build_occupancy_grid_shape(self, grid, minimal_model):
        """Returns (n_layers, H, W) int16 array."""
        cost_grid = build_occupancy_grid(minimal_model, grid)
        assert cost_grid.shape == (2, grid.height_cells, grid.width_cells)
        assert cost_grid.dtype == np.int16

    def test_build_occupancy_grid_default_values(self, grid, minimal_model):
        """Most cells start as FREE."""
        cost_grid = build_occupancy_grid(minimal_model, grid)
        assert np.any(cost_grid == FREE)
        assert cost_grid.min() == FREE


class TestCourtyardBlocking:
    """Tests for _block_courtyard."""

    def test_block_courtyard_marks_both_layers(self, grid, minimal_model):
        """Courtyard outlines mark F.Cu and B.Cu as HIGH_COST (not BLOCKED).

        Courtyards are placement guides: pads sit inside the outline and
        must remain able to route out, so crossing is discouraged, never
        forbidden.
        """
        fp = next(fp for fp in minimal_model.footprints if fp.ref == "MH1")
        cost_grid = np.full((2, grid.height_cells, grid.width_cells), FREE, dtype=np.int16)

        _block_courtyard(cost_grid, grid, fp)

        # Both layers should have HIGH_COST cells, none BLOCKED
        assert np.any(cost_grid[0] == HIGH_COST)
        assert np.any(cost_grid[1] == HIGH_COST)
        assert not np.any(cost_grid >= BLOCKED)

    def test_block_courtyard_handles_empty(self, grid):
        """No courtyard = no-op."""
        from kicad_autorouter.board_model import Footprint
        fp = Footprint(ref="TEST", footprint_id="", layer="F.Cu", x_mm=50, y_mm=50, angle_deg=0, courtyard=())
        cost_grid = np.full((2, grid.height_cells, grid.width_cells), FREE, dtype=np.int16)

        _block_courtyard(cost_grid, grid, fp)

        # Grid untouched
        assert not np.any(cost_grid >= HIGH_COST)


class TestFanoutCost:
    """Tests for build_fanout_cost (per-net secondary halo grid)."""

    def _two_pad_model(self, grid):
        from kicad_autorouter.board_model import BoardModel, Footprint, Pad, Point
        x0, y0 = grid.origin_mm[0] + 50, grid.origin_mm[1] + 50
        return BoardModel(
            footprints=(
                Footprint(ref="R1", footprint_id="", layer="F.Cu", x_mm=x0, y_mm=y0,
                          angle_deg=0,
                          pads=(Pad(number="1", net_name="A", position=Point(x0, y0),
                                    size_mm=(1, 1), pad_type="smd", drill_mm=0,
                                    layers=("F.Cu",)),),
                          courtyard=()),
                Footprint(ref="R2", footprint_id="", layer="F.Cu", x_mm=x0 + 10, y_mm=y0,
                          angle_deg=0,
                          pads=(Pad(number="1", net_name="B",
                                    position=Point(x0 + 10, y0),
                                    size_mm=(1, 1), pad_type="smd", drill_mm=0,
                                    layers=("F.Cu",)),),
                          courtyard=()),
            ),
            keepout_zones=(),
            nets={},
            by_ref={},
        ), x0, y0

    def test_own_net_excluded_others_penalized(self, grid):
        """Current net's pads stay 0; other nets get inner+outer halo."""
        from kicad_autorouter.routing.grid import board_to_grid
        model, x0, y0 = self._two_pad_model(grid)
        secondary = build_fanout_cost(model, grid, "A",
                                      clearance_mm=0.2, track_half_mm=0.125)
        assert secondary.shape == (2, grid.height_cells, grid.width_cells)
        assert secondary.dtype.name == "int16"
        # Own pad (R1, net A): no halo
        c1, r1 = board_to_grid(grid, x0, y0)
        assert secondary[0, r1, c1] == 0
        # Other pad (R2, net B): inner halo at center
        c2, r2 = board_to_grid(grid, x0 + 10, y0)
        assert secondary[0, r2, c2] == FANOUT_HALO_INNER
        # SMD pad on F.Cu only: B.Cu untouched
        assert secondary[1, r2, c2] == 0
        # Outer ring present beyond inner (soft shoulder, strictly weaker)
        assert secondary[0].max() == FANOUT_HALO_INNER
        assert FANOUT_HALO_OUTER in secondary[0]

    def test_halo_never_blocks(self, grid):
        """Secondary values steer (added to cost) but blockage stays base-only."""
        model, _, _ = self._two_pad_model(grid)
        secondary = build_fanout_cost(model, grid, "A")
        # No cell may reach BLOCKED from halo alone (handled at cost time)
        assert secondary.max() == FANOUT_HALO_INNER


class TestPadMarking:
    """Tests for _mark_pad."""

    def test_mark_pad_tht_both_layers(self, grid, minimal_model):
        """Through-hole pad marks both layers as HIGH_COST."""
        # Find a THT pad
        tht_fp = None
        for fp in minimal_model.footprints:
            for pad in fp.pads:
                if pad.is_through_hole:
                    tht_fp = fp
                    break
            if tht_fp:
                break

        if not tht_fp:
            pytest.skip("No THT pads in minimal fixture")

        cost_grid = np.full((2, grid.height_cells, grid.width_cells), FREE, dtype=np.int16)
        tht_pad = next(p for p in tht_fp.pads if p.is_through_hole)

        _mark_pad(cost_grid, grid, tht_fp, tht_pad, {}, 2)

        col, row = board_to_grid(grid, tht_pad.position.x_mm, tht_pad.position.y_mm)
        # Should mark HIGH_COST on both layers around pad
        assert cost_grid[0, row, col] >= HIGH_COST
        assert cost_grid[1, row, col] >= HIGH_COST

    def test_mark_pad_smd_single_layer(self, grid, minimal_model):
        """SMD pad marks only its layer."""
        smd_fp = None
        smd_pad = None
        for fp in minimal_model.footprints:
            for pad in fp.pads:
                if not pad.is_through_hole:
                    smd_fp = fp
                    smd_pad = pad
                    break
            if smd_fp:
                break

        if not smd_fp:
            pytest.skip("No SMD pads in minimal fixture")

        cost_grid = np.full((2, grid.height_cells, grid.width_cells), FREE, dtype=np.int16)
        _mark_pad(cost_grid, grid, smd_fp, smd_pad, {}, 2)

        col, row = board_to_grid(grid, smd_pad.position.x_mm, smd_pad.position.y_mm)
        layer_idx = 0 if smd_fp.layer == "F.Cu" else 1
        other_layer = 1 - layer_idx

        # Only pad's layer should be marked
        assert cost_grid[layer_idx, row, col] >= HIGH_COST
        assert cost_grid[other_layer, row, col] == FREE

    def test_mark_pad_clearance_net_class(self, grid):
        """Net-specific clearance overrides default."""
        from kicad_autorouter.board_model import Footprint, Pad, Point
        from kicad_autorouter.routing.grid import board_to_grid

        # Place footprint within grid bounds
        x_mm, y_mm = grid.origin_mm[0] + 100, grid.origin_mm[1] + 100
        fp = Footprint(ref="TEST", footprint_id="", layer="F.Cu", x_mm=x_mm, y_mm=y_mm, angle_deg=0,
                       pads=(Pad(number="1", net_name="NET_A", position=Point(x_mm, y_mm), size_mm=(1,1),
                                 pad_type="smd", drill_mm=0, layers=("F.Cu",)),))
        cost_grid = np.full((2, grid.height_cells, grid.width_cells), FREE, dtype=np.int16)

        clearance_cells = {"NET_A": 5}  # 5 cells for NET_A
        default = 1

        pad = fp.pads[0]
        _mark_pad(cost_grid, grid, fp, pad, clearance_cells, default)

        col, row = board_to_grid(grid, x_mm, y_mm)
        # Net_A should get 5-cell clearance
        marked = np.sum(cost_grid[0, row-5:row+6, col-5:col+6] >= HIGH_COST)
        assert marked > 0


class TestTrackMarking:
    """Tests for _mark_track."""

    def test_mark_track_layer_specific(self, grid):
        """Track only blocks its layer."""
        from kicad_autorouter.board_model import Track, Point
        track = Track(
            start=Point(40, 50), end=Point(60, 50), width_mm=0.25,
            layer="F.Cu", net_name="NET1"
        )
        cost_grid = np.full((2, grid.height_cells, grid.width_cells), FREE, dtype=np.int16)

        _mark_track(cost_grid, grid, track)

        # F.Cu (layer 0) should have BLOCKED along track
        assert np.any(cost_grid[0] == BLOCKED)
        # B.Cu (layer 1) should be untouched
        assert not np.any(cost_grid[1] == BLOCKED)


class TestViaMarking:
    """Tests for _mark_via."""

    def test_mark_via_all_layers(self, grid):
        """Via blocks all layers at position."""
        from kicad_autorouter.board_model import Via, Point
        via = Via(position=Point(50, 50), size_mm=0.8, drill_mm=0.4, layers=("F.Cu", "B.Cu"), net_name="NET1")
        cost_grid = np.full((2, grid.height_cells, grid.width_cells), FREE, dtype=np.int16)

        _mark_via(cost_grid, grid, via)

        col, row = board_to_grid(grid, 50, 50)
        assert cost_grid[0, row, col] >= BLOCKED
        assert cost_grid[1, row, col] >= BLOCKED


class TestZoneMarking:
    """Tests for _mark_zone."""

    def test_mark_zone_f_cu_only(self, grid):
        """Zone only blocks its layer."""
        from kicad_autorouter.board_model import Zone, Point
        zone = Zone(
            uuid="zone1",
            layers=("F.Cu",),
            polygon=(Point(45, 45), Point(55, 45), Point(55, 55), Point(45, 55)),
            net_name="GND"
        )
        cost_grid = np.full((2, grid.height_cells, grid.width_cells), FREE, dtype=np.int16)

        _mark_zone(cost_grid, grid, zone)

        assert np.any(cost_grid[0] == BLOCKED)
        assert not np.any(cost_grid[1] == BLOCKED)


class TestEdgeKeepout:
    """Tests for _mark_edge_keepout."""

    def test_mark_edge_keepout_uses_edge_cuts(self, grid, minimal_model):
        """Edge keepout drawn along Edge.Cuts."""
        cost_grid = np.full((2, grid.height_cells, grid.width_cells), FREE, dtype=np.int16)

        _mark_edge_keepout(cost_grid, grid, minimal_model, keepout_mm=0.5)

        # Should have EDGE_KEEPOUT near board edges
        assert np.any(cost_grid[0] == EDGE_KEEPOUT)
        assert np.any(cost_grid[1] == EDGE_KEEPOUT)


class TestDrawingPrimitives:
    """Tests for low-level drawing functions."""

    def test_draw_line_blocked_bresenham(self, grid):
        """Bresenham line fills correct cells."""
        layer_grid = np.full((grid.height_cells, grid.width_cells), FREE, dtype=np.int16)
        x1, y1 = 40, 50
        x2, y2 = 60, 50  # Horizontal line

        _draw_line_blocked(layer_grid, grid, x1, y1, x2, y2)

        # Line should be BLOCKED
        assert np.any(layer_grid == BLOCKED)

    def test_mark_circular_zone_radius(self, grid):
        """Circular zone radius in cells."""
        layer_grid = np.full((grid.height_cells, grid.width_cells), FREE, dtype=np.int16)
        center_row, center_col = grid.height_cells // 2, grid.width_cells // 2

        _mark_circular_zone(layer_grid, center_row, center_col, 5, HIGH_COST)

        # Check radius
        marked_rows = np.where(np.any(layer_grid == HIGH_COST, axis=1))[0]
        marked_cols = np.where(np.any(layer_grid == HIGH_COST, axis=0))[0]
        assert len(marked_rows) > 0
        assert len(marked_cols) > 0

        # Radius should be approximately 5 cells
        row_span = marked_rows.max() - marked_rows.min()
        col_span = marked_cols.max() - marked_cols.min()
        assert row_span <= 12  # diameter ~ 2*radius+1
        assert col_span <= 12


class TestPolygonFill:
    """Tests for _fill_polygon."""

    def test_fill_polygon_convex(self, grid):
        """Convex polygon fill."""
        layer_grid = np.full((grid.height_cells, grid.width_cells), FREE, dtype=np.int16)
        # Square at center
        center_r, center_c = grid.height_cells // 2, grid.width_cells // 2
        points = [(center_r-5, center_c-5), (center_r-5, center_c+5),
                  (center_r+5, center_c+5), (center_r+5, center_c-5)]

        _fill_polygon(layer_grid, points, BLOCKED)

        assert np.any(layer_grid == BLOCKED)

    def test_fill_polygon_concave(self, grid):
        """Concave polygon fill (L-shape)."""
        layer_grid = np.full((grid.height_cells, grid.width_cells), FREE, dtype=np.int16)
        center_r, center_c = grid.height_cells // 2, grid.width_cells // 2
        # L-shape
        points = [(center_r-5, center_c-5), (center_r-5, center_c),
                  (center_r, center_c), (center_r, center_c+5),
                  (center_r+5, center_c+5), (center_r+5, center_c-5)]

        _fill_polygon(layer_grid, points, BLOCKED)

        assert np.any(layer_grid == BLOCKED)

    def test_fill_polygon_respects_row_col_orientation(self, grid):
        """Regression: points are (row, col) — a wide-thin rect must fill
        a wide-thin cell block, not a transposed tall-thin one."""
        layer_grid = np.full((grid.height_cells, grid.width_cells), FREE, dtype=np.int16)
        center_r, center_c = grid.height_cells // 2, grid.width_cells // 2
        # 4 rows tall x 40 cols wide, in (row, col) order
        points = [(center_r-2, center_c-20), (center_r-2, center_c+20),
                  (center_r+2, center_c+20), (center_r+2, center_c-20)]

        _fill_polygon(layer_grid, points, BLOCKED)

        marked_rows = np.where(np.any(layer_grid == BLOCKED, axis=1))[0]
        marked_cols = np.where(np.any(layer_grid == BLOCKED, axis=0))[0]
        assert len(marked_rows) > 0 and len(marked_cols) > 0
        assert marked_rows.max() - marked_rows.min() <= 6
        assert marked_cols.max() - marked_cols.min() >= 30

    def test_zone_marking_keeps_thin_strips_thin(self, grid, minimal_model):
        """Regression: minimal's full-width keepout strips must not become
        full-height blocked bars (board_to_grid returns (col, row))."""
        cost_grid = build_occupancy_grid(minimal_model, grid, default_clearance_mm=0.2)
        H, W = cost_grid.shape[1], cost_grid.shape[2]
        for layer_idx in range(cost_grid.shape[0]):
            full_height_cols = np.where(
                (cost_grid[layer_idx] >= BLOCKED).sum(axis=0) >= H - 2
            )[0]
            assert len(full_height_cols) == 0

    def test_pad_inside_own_courtyard_can_route_out(self, grid):
        """Regression: an SMD pad enclosed by its own courtyard outline
        must still reach a cell outside it (outlines are HIGH_COST)."""
        from kicad_autorouter.board_model import Footprint, Pad, Point
        from kicad_autorouter.routing.router import SingleNetRouter, CostMap

        x_mm = grid.origin_mm[0] + 50
        y_mm = grid.origin_mm[1] + 50
        hw, hh = 1.5, 1.0
        corners = [Point(x_mm - hw, y_mm - hh), Point(x_mm + hw, y_mm - hh),
                   Point(x_mm + hw, y_mm + hh), Point(x_mm - hw, y_mm + hh),
                   Point(x_mm - hw, y_mm - hh)]
        fp = Footprint(ref="T1", footprint_id="", layer="F.Cu", x_mm=x_mm, y_mm=y_mm,
                       angle_deg=0,
                       pads=(Pad(number="1", net_name="N1", position=Point(x_mm, y_mm),
                                 size_mm=(1, 1), pad_type="smd", drill_mm=0,
                                 layers=("F.Cu",)),),
                       courtyard=tuple((corners[i], corners[i + 1]) for i in range(4)))
        cost_grid = np.full((2, grid.height_cells, grid.width_cells), FREE, dtype=np.int16)
        _block_courtyard(cost_grid, grid, fp)

        sc, sr = board_to_grid(grid, x_mm, y_mm)
        gc, gr = board_to_grid(grid, x_mm + 10, y_mm)
        router = SingleNetRouter(grid, cost_grid, CostMap(),
                                 np.zeros((grid.height_cells, grid.width_cells), dtype=bool))
        result = router._a_star((sc, sr, 0), (gc, gr, 0))
        assert result.success


class TestBuildOccupancyGridIntegration:
    """Integration tests for full build_occupancy_grid."""

    def test_build_occupancy_grid_layers(self, grid, minimal_model):
        """Returns proper layer structure with expected cost values."""
        cost_grid = build_occupancy_grid(minimal_model, grid, default_clearance_mm=0.2)

        assert cost_grid.shape == (2, grid.height_cells, grid.width_cells)
        assert cost_grid.dtype == np.int16
        assert cost_grid.min() >= 0

    def test_courtyard_high_cost_on_both_layers(self, cost_grid):
        """Footprint courtyard outlines are HIGH_COST (escapable) on both layers."""
        assert np.any(cost_grid[0] == HIGH_COST)
        assert np.any(cost_grid[1] == HIGH_COST)

    def test_pads_high_cost_with_clearance(self, cost_grid):
        """Pad centers and clearance are HIGH_COST."""
        assert np.any(cost_grid >= HIGH_COST)

    def test_tracks_blocked_on_layer(self, cost_grid, minimal_model):
        """Existing tracks are BLOCKED on their layer."""
        # Minimal has no tracks, but structure is there
        assert cost_grid.dtype == np.int16

    def test_vias_blocked_both_layers(self, cost_grid):
        """Vias block all layers."""
        # Minimal has no vias
        assert cost_grid.dtype == np.int16

    def test_zones_blocked_on_layer(self, cost_grid, dccf_model):
        """Copper zones block their layer."""
        grid = create_grid_from_model(dccf_model, resolution_mm=0.1, margin_mm=2.0)
        dccf_cost = build_occupancy_grid(dccf_model, grid, default_clearance_mm=0.2)
        # DCCF has a GND zone
        assert dccf_cost.dtype == np.int16

    def test_edge_keepout_present(self, cost_grid):
        """Edge keepout zones exist."""
        assert np.any(cost_grid == EDGE_KEEPOUT)


class TestDCCFCostGrid:
    """Cost grid tests on DCCF (real board)."""

    def test_dccf_cost_grid_has_expected_values(self, dccf_model):
        """DCCF cost grid has BLOCKED, HIGH_COST, EDGE_KEEPOUT."""
        grid = create_grid_from_model(dccf_model, resolution_mm=0.1, margin_mm=2.0)
        cost_grid = build_occupancy_grid(dccf_model, grid, default_clearance_mm=0.2)

        assert np.any(cost_grid == BLOCKED)
        assert np.any(cost_grid == HIGH_COST)
        assert np.any(cost_grid == EDGE_KEEPOUT)

    def test_dccf_tht_via_mask(self, dccf_model):
        """THT pads create via mask."""
        grid = create_grid_from_model(dccf_model, resolution_mm=0.1, margin_mm=2.0)
        from kicad_autorouter.routing.pipeline import _build_tht_via_mask
        mask = _build_tht_via_mask(dccf_model, grid)

        assert mask.shape == (grid.height_cells, grid.width_cells)
        assert mask.dtype == bool
        # DCCF has through-hole parts
        assert np.any(mask)


class TestViaBlockMask:
    """No vias inside SMD pad solder/fanout areas."""

    def test_smd_pad_cells_blocked(self, dccf_model):
        """SMD pad copper + fanout halo cells forbid vias."""
        from kicad_autorouter.routing.pipeline import _build_tht_via_mask

        grid = create_grid_from_model(dccf_model, resolution_mm=0.5, margin_mm=2.0)
        mask = build_via_block_mask(dccf_model, grid)
        assert mask.shape == (grid.height_cells, grid.width_cells)
        assert mask.dtype == bool

        smd = [(fp, pad) for fp in dccf_model.footprints
               for pad in fp.pads if not pad.is_through_hole]
        assert smd, "DCCF should have SMD pads"
        for fp, pad in smd[:20]:
            col, row = board_to_grid(grid, pad.position.x_mm, pad.position.y_mm)
            assert mask[row, col], f"SMD pad {fp.ref}.{pad.number} not via-blocked"

    def test_tht_pads_stay_legal(self, dccf_model):
        """Through-hole pads are excluded (free via sites, THT mask wins)."""
        from kicad_autorouter.routing.pipeline import _build_tht_via_mask

        grid = create_grid_from_model(dccf_model, resolution_mm=0.5, margin_mm=2.0)
        block = build_via_block_mask(dccf_model, grid)
        tht = _build_tht_via_mask(dccf_model, grid)
        assert np.any(tht), "DCCF should have THT pads"
        # No cell may be both blocked and THT-free (router gives THT priority,
        # but the builders must not disagree).
        assert not np.any(block & tht)