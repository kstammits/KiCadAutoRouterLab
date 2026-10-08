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
MINIMAL_PCB = FIXTURES / "minimal.kicad_pcb"
DCCF_PCB = FIXTURES / "DCCF.sved.kicad_pcb"
TUBE111_PCB = FIXTURES / "tube111.kicad_pcb"


@pytest.fixture(scope="module")
def minimal_model():
    return board_model(parse_file(MINIMAL_PCB))


@pytest.fixture(scope="module")
def dccf_model():
    return board_model(parse_file(DCCF_PCB))


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

    def test_create_grid_from_model_fallback(self):
        """Fallback to footprint extents when no Edge.Cuts."""
        # Minimal has Edge.Cuts, so test with empty model-like object
        from kicad_autorouter.board_model import BoardModel, Footprint, Point
        from kicad_autorouter.routing.grid import create_grid_from_model

        # Create a model with footprints but no edge cuts
        fp = Footprint(
            ref="U1",
            footprint_id="Test",
            layer="F.Cu",
            x_mm=50.0,
            y_mm=50.0,
            angle_deg=0.0,
            pads=(),
            courtyard=((Point(49, 49), Point(51, 49)), (Point(51, 49), Point(51, 51)),
                       (Point(51, 51), Point(49, 51)), (Point(49, 51), Point(49, 49))),
        )
        model = BoardModel(
            footprints=(fp,),
            keepout_zones=(),
            nets={},
            by_ref={"U1": fp},
            edge_cuts=(),
            edge_arcs=(),
        )

        grid = create_grid_from_model(model, resolution_mm=0.5, margin_mm=1.0)
        assert grid.width_cells > 0
        assert grid.height_cells > 0

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

    @pytest.fixture
    def grid(self, minimal_model):
        return create_grid_from_model(minimal_model, resolution_mm=0.1, margin_mm=2.0)

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

    def test_grid_bbox_mm(self, grid):
        """bbox_mm() matches origin + size."""
        min_x, min_y, max_x, max_y = grid.bbox_mm()
        assert min_x == pytest.approx(grid.origin_mm[0])
        assert min_y == pytest.approx(grid.origin_mm[1])
        assert max_x == pytest.approx(grid.origin_mm[0] + grid.width_mm)
        assert max_y == pytest.approx(grid.origin_mm[1] + grid.height_mm)

    def test_grid_layer_to_idx(self, grid):
        """Layer name to index mapping."""
        mapping = grid.layer_to_idx
        assert mapping["F.Cu"] == 0
        assert mapping["B.Cu"] == 1
        assert len(mapping) == 2

    def test_grid_segment_to_board(self, grid):
        """Convert grid path to board coordinates."""
        path = [(10, 20, 0), (11, 20, 0), (11, 21, 1)]
        board_path = grid_segment_to_board(grid, path)

        assert len(board_path) == 3
        for (x, y, layer) in board_path:
            assert isinstance(x, float)
            assert isinstance(y, float)
            assert layer in ("F.Cu", "B.Cu")

        # First point should be near grid cell (10, 20) center
        expected_x = grid.origin_mm[0] + (10 + 0.5) * grid.resolution_mm
        expected_y = grid.origin_mm[1] + (20 + 0.5) * grid.resolution_mm
        assert board_path[0][0] == pytest.approx(expected_x)
        assert board_path[0][1] == pytest.approx(expected_y)


class TestGridProperties:
    """Tests for RoutingGrid derived properties."""

    def test_grid_dimensions(self, minimal_model):
        """width_mm, height_mm computed from cells * resolution."""
        grid = create_grid_from_model(minimal_model, resolution_mm=0.2, margin_mm=1.0)
        assert grid.width_mm == pytest.approx(grid.width_cells * 0.2)
        assert grid.height_mm == pytest.approx(grid.height_cells * 0.2)

    def test_grid_two_regions(self, tube111_model):
        """Tube111 has two disconnected boards - grid covers both with margin."""
        grid = create_grid_from_model(tube111_model, resolution_mm=0.1, margin_mm=2.0)
        # Should cover the span of both boards
        assert grid.width_mm > 100  # Both boards span >100mm
        assert grid.height_mm > 100


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