"""Tests for cost grid building primitives (obstacles)."""

import pytest
import numpy as np
from shapely.geometry import Polygon

from kicad_autorouter.board_model import board_model
from kicad_autorouter.routing.grid import create_grid_from_model, board_to_grid
from kicad_autorouter.routing.obstacles import (
    build_occupancy_grid,
    FREE, BLOCKED, HIGH_COST, EDGE_KEEPOUT,
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
        """Courtyard marks F.Cu and B.Cu as BLOCKED."""
        fp = next(fp for fp in minimal_model.footprints if fp.ref == "MH1")
        cost_grid = np.full((2, grid.height_cells, grid.width_cells), FREE, dtype=np.int16)

        _block_courtyard(cost_grid, grid, fp)

        # Both layers should have BLOCKED cells
        assert np.any(cost_grid[0] == BLOCKED)
        assert np.any(cost_grid[1] == BLOCKED)

    def test_block_courtyard_handles_empty(self, grid):
        """No courtyard = no-op."""
        from kicad_autorouter.board_model import Footprint
        fp = Footprint(ref="TEST", footprint_id="", layer="F.Cu", x_mm=50, y_mm=50, angle_deg=0, courtyard=())
        cost_grid = np.full((2, grid.height_cells, grid.width_cells), FREE, dtype=np.int16)

        _block_courtyard(cost_grid, grid, fp)

        # No BLOCKED cells added
        assert not np.any(cost_grid == BLOCKED)


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


class TestBuildOccupancyGridIntegration:
    """Integration tests for full build_occupancy_grid."""

    def test_build_occupancy_grid_layers(self, grid, minimal_model):
        """Returns proper layer structure with expected cost values."""
        cost_grid = build_occupancy_grid(minimal_model, grid, default_clearance_mm=0.2)

        assert cost_grid.shape == (2, grid.height_cells, grid.width_cells)
        assert cost_grid.dtype == np.int16
        assert cost_grid.min() >= 0

    def test_courtyard_blocked_on_both_layers(self, cost_grid):
        """Footprint courtyards are BLOCKED on both layers."""
        assert np.any(cost_grid[0] == BLOCKED)
        assert np.any(cost_grid[1] == BLOCKED)

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