"""Tests for routing grid creation and coordinate transforms."""

import pytest
import numpy as np

from kicad_autorouter.board_model import board_model
from kicad_autorouter.routing.grid import (
    RoutingGrid,
    create_grid_from_model,
    board_to_grid,
    grid_to_board,
    grid_segment_to_board,
)
from kicad_autorouter.sexpr import parse_file

FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures"
TUBE111_PCB = FIXTURES / "tube111.kicad_pcb"


@pytest.fixture(scope="module")
def tube111_model():
    return board_model(parse_file(TUBE111_PCB))


class TestGridCreation:
    """Tests for RoutingGrid creation from board models."""

    def test_create_grid_from_model_edge_cuts(self, minimal_model):
        """Grid created from Edge.Cuts covers board outline + margin."""
        grid = create_grid_from_model(minimal_model, resolution_mm=0.1, margin_mm=2.0)

        assert isinstance(grid, RoutingGrid)
        assert grid.resolution_mm == 0.1
        assert grid.layers == ("F.Cu", "B.Cu")
        assert grid.width_cells > 0
        assert grid.height_cells > 0
        assert grid.width_mm > 0
        assert grid.height_mm > 0

    def test_create_grid_different_resolutions(self, minimal_model):
        """Grid cell count scales with resolution."""
        grid_fine = create_grid_from_model(minimal_model, resolution_mm=0.05, margin_mm=2.0)
        grid_coarse = create_grid_from_model(minimal_model, resolution_mm=0.2, margin_mm=2.0)

        # Finer resolution = more cells
        assert grid_fine.width_cells > grid_coarse.width_cells
        assert grid_fine.height_cells > grid_coarse.height_cells
        # But physical size should be similar
        assert abs(grid_fine.width_mm - grid_coarse.width_mm) < 1.0


class TestCoordinateTransforms:
    """Tests for board<->grid coordinate transforms."""

    def test_board_to_grid_roundtrip(self, grid):
        """board_to_grid -> grid_to_board returns nearby point."""
        test_points = [
            (grid.origin_mm[0] + 10.0, grid.origin_mm[1] + 10.0),
            (grid.origin_mm[0] + grid.width_mm / 2, grid.origin_mm[1] + grid.height_mm / 2),
            (grid.origin_mm[0] + grid.width_mm - 0.05, grid.origin_mm[1] + grid.height_mm - 0.05),
        ]

        for x_mm, y_mm in test_points:
            col, row = board_to_grid(grid, x_mm, y_mm)
            x_back, y_back = grid_to_board(grid, col, row)

            # Should be within half cell (center of cell)
            assert abs(x_back - x_mm) <= grid.resolution_mm / 2 + 1e-6
            assert abs(y_back - y_mm) <= grid.resolution_mm / 2 + 1e-6

    def test_board_to_grid_clamping(self, grid):
        """Out-of-bounds coordinates clamp to grid edges."""
        # Beyond bottom-left
        col, row = board_to_grid(grid, grid.origin_mm[0] - 100, grid.origin_mm[1] - 100)
        assert col == 0
        assert row == 0

        # Beyond top-right
        col, row = board_to_grid(grid,
                                 grid.origin_mm[0] + grid.width_mm + 100,
                                 grid.origin_mm[1] + grid.height_mm + 100)
        assert col == grid.width_cells - 1
        assert row == grid.height_cells - 1


class TestDCCFGrid:
    """Tests specific to DCCF fixture (two board regions)."""

    def test_dccf_grid_covers_both_regions(self, dccf_model):
        """Grid spans both DCCF board regions."""
        grid = create_grid_from_model(dccf_model, resolution_mm=0.1, margin_mm=2.0)

        # DCCF has two boards side by side
        assert grid.width_cells > 1000  # ~100mm / 0.1mm
        assert grid.height_cells > 2000  # ~200mm / 0.1mm

    def test_dccf_known_footprint_in_grid(self, dccf_model):
        """Known footprint SW6 maps to valid grid cell."""
        grid = create_grid_from_model(dccf_model, resolution_mm=0.1, margin_mm=2.0)
        sw6 = next(fp for fp in dccf_model.footprints if fp.ref == "SW6")
        col, row = board_to_grid(grid, sw6.x_mm, sw6.y_mm)

        assert 0 <= col < grid.width_cells
        assert 0 <= row < grid.height_cells

        # Round-trip should be close
        x_back, y_back = grid_to_board(grid, col, row)
        assert x_back == pytest.approx(sw6.x_mm, abs=grid.resolution_mm / 2 + 1e-6)
        assert y_back == pytest.approx(sw6.y_mm, abs=grid.resolution_mm / 2 + 1e-6)