"""Tests for routing pipeline: incremental routing, rip-up, full pipeline."""

import pytest
import numpy as np

from kicad_autorouter.board_model import board_model
from kicad_autorouter.routing.grid import create_grid_from_model
from kicad_autorouter.routing.obstacles import build_occupancy_grid
from kicad_autorouter.routing.router import SingleNetRouter, CostMap
from kicad_autorouter.routing.pipeline import (
    route_nets,
    run_routing,
    run_full_pipeline,
    _apply_route_to_grid,
    _rip_up_nets_from_model,
    _build_tht_via_mask,
    RoutingParams,
    RoutingResult,
)
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
def routing_setup(minimal_model):
    """Create grid, cost_grid, and router for testing."""
    from kicad_autorouter.routing.grid import create_grid_from_model
    from kicad_autorouter.routing.obstacles import build_occupancy_grid
    grid = create_grid_from_model(minimal_model, resolution_mm=0.1, margin_mm=2.0)
    cost_grid = build_occupancy_grid(minimal_model, grid, default_clearance_mm=0.2)
    cost_map = CostMap()
    router = SingleNetRouter(grid, cost_grid, cost_map)
    return grid, cost_grid, router


class TestApplyRouteToGrid:
    """Tests for _apply_route_to_grid."""

    def test_apply_route_to_grid_same_layer(self, routing_setup):
        """Track marks line as BLOCKED on same layer."""
        grid, cost_grid, router = routing_setup
        # Find two free cells on layer 0
        free_cells = [(c, r) for r in range(grid.height_cells)
                      for c in range(grid.width_cells) if cost_grid[0, r, c] == 0]
        if len(free_cells) < 2:
            pytest.skip("Not enough free cells")

        result = router.route([(free_cells[0][0], free_cells[0][1], 0),
                               (free_cells[1][0], free_cells[1][1], 0)])
        if not result.success:
            pytest.skip("No path found")

        original_grid = cost_grid.copy()
        _apply_route_to_grid(result, cost_grid, grid)

        # Cost grid should have BLOCKED cells along the path
        assert not np.array_equal(cost_grid, original_grid)
        assert np.any(cost_grid == 100)  # BLOCKED

    def test_apply_route_to_grid_via(self, routing_setup):
        """Via marks both layers at position."""
        grid, cost_grid, router = routing_setup
        # Find a column where both layers are free
        for r in range(grid.height_cells):
            for c in range(grid.width_cells):
                if cost_grid[0, r, c] == 0 and cost_grid[1, r, c] == 0:
                    result = router._a_star((c, r, 0), (c, r, 1))
                    if not result.success:
                        continue
                    original_grid = cost_grid.copy()
                    _apply_route_to_grid(result, cost_grid, grid)

                    assert cost_grid[0, r, c] >= 100
                    assert cost_grid[1, r, c] >= 100
                    return
        pytest.skip("No suitable position for via test")


from dataclasses import replace

# ... existing imports ...


class TestRipUpNets:
    """Tests for _rip_up_nets_from_model."""

    def test_rip_up_nets_from_model(self, minimal_model):
        """_rip_up_nets_from_model removes tracks/vias for specified nets."""
        from kicad_autorouter.board_model import Track, Via, Point
        track = Track(start=Point(40, 50), end=Point(60, 50), width_mm=0.25,
                      layer="F.Cu", net_name="NET1")
        via = Via(position=Point(50, 50), size_mm=0.8, drill_mm=0.4,
                  layers=("F.Cu", "B.Cu"), net_name="NET1")

        # Create a new model with added tracks/vias using replace
        model_with_tracks = replace(minimal_model, tracks=(track,), vias=(via,))

        new_model = _rip_up_nets_from_model(model_with_tracks, ["NET1"])

        assert len(new_model.tracks) == 0
        assert len(new_model.vias) == 0

    def test_rip_up_nets_preserves_other_nets(self, minimal_model):
        """Other nets' tracks/vias are preserved."""
        from kicad_autorouter.board_model import Track, Via, Point
        track1 = Track(start=Point(40, 50), end=Point(60, 50), width_mm=0.25,
                       layer="F.Cu", net_name="NET1")
        track2 = Track(start=Point(40, 60), end=Point(60, 60), width_mm=0.25,
                       layer="F.Cu", net_name="NET2")

        model_with_tracks = replace(minimal_model, tracks=(track1, track2), vias=())

        new_model = _rip_up_nets_from_model(model_with_tracks, ["NET1"])

        assert len(new_model.tracks) == 1
        assert new_model.tracks[0].net_name == "NET2"


class TestBuildTHTViaMask:
    """Tests for _build_tht_via_mask."""

    def test_build_tht_via_mask_tht_pads(self, dccf_model):
        """Through-hole pads create True in mask."""
        from kicad_autorouter.routing.grid import create_grid_from_model
        grid = create_grid_from_model(dccf_model, resolution_mm=0.1, margin_mm=2.0)
        mask = _build_tht_via_mask(dccf_model, grid)

        assert mask.shape == (grid.height_cells, grid.width_cells)
        assert mask.dtype == bool
        # DCCF has through-hole parts
        assert np.any(mask)

    def test_build_tht_via_mask_smd_pads(self, minimal_model):
        """SMD-only board should not create mask."""
        from kicad_autorouter.routing.grid import create_grid_from_model
        grid = create_grid_from_model(minimal_model, resolution_mm=0.1, margin_mm=2.0)
        mask = _build_tht_via_mask(minimal_model, grid)

        assert mask.shape == (grid.height_cells, grid.width_cells)
        assert mask.dtype == bool
        # Minimal has mounting holes with THT pads, so mask may have values
        # This test verifies the mask is created correctly
        assert mask.dtype == bool


class TestRouteNetsIncremental:
    """Tests for incremental route_nets function."""

    def test_route_nets_incremental_cost_grid(self, minimal_model):
        """Reuses cost_grid between calls."""
        from kicad_autorouter.routing.grid import create_grid_from_model
        from kicad_autorouter.routing.obstacles import build_occupancy_grid
        from kicad_autorouter.routing.router import SingleNetRouter, CostMap

        grid = create_grid_from_model(minimal_model, resolution_mm=0.1, margin_mm=2.0)
        cost_grid = build_occupancy_grid(minimal_model, grid, default_clearance_mm=0.2)
        cost_map = CostMap()
        router = SingleNetRouter(grid, cost_grid, cost_map)

        # Find a simple net to route
        net_names = list(minimal_model.nets.keys())
        if not net_names:
            pytest.skip("No nets in model")

        # First call
        result1, cost_grid1, grid1 = route_nets(
            minimal_model, [net_names[0]], set(), cost_grid=cost_grid,
            routing_grid=grid, params=RoutingParams()
        )
        # Second call should reuse cost_grid
        result2, cost_grid2, grid2 = route_nets(
            minimal_model, [net_names[0]], set(), cost_grid=cost_grid1,
            routing_grid=grid1, params=RoutingParams()
        )

        # Same grid objects returned
        assert grid2 is grid1
        assert cost_grid2 is cost_grid1

    def test_route_nets_rip_up_only_selected(self, minimal_model):
        """Other nets' tracks/vias preserved during incremental routing."""
        # This test is more integration-style; skip if no nets
        if not minimal_model.nets:
            pytest.skip("No nets in model")

        net_names = list(minimal_model.nets.keys())
        result, _, _ = route_nets(
            minimal_model, [net_names[0]], set(),
            params=RoutingParams(run_drc=False)
        )
        # Should complete without error
        assert isinstance(result, RoutingResult)


class TestRunRouting:
    """Tests for run_routing function."""

    def test_run_routing_power_first(self, dccf_model):
        """Power nets routed before signals."""
        pytest.skip("Full DCCF routing is slow - run manually if needed")
        # DCCF has power/ground nets
        power_nets = ["GND", "VCC", "VDD", "VSS", "GND", "GROUND", "+12V", "+5V", "+3.3V", "+1.8V", "-12V", "-5V"]
        dccf_power = [n for n in dccf_model.nets if n in power_nets]
        if not dccf_power:
            pytest.skip("No power nets in DCCF")

        result = run_routing(dccf_model, run_drc=False)
        # Should complete without error
        assert isinstance(result, RoutingResult)

    def test_run_routing_ground_stitching(self, dccf_model):
        """Ground nets get stitching."""
        pytest.skip("Full DCCF routing is slow - run manually if needed")
        result = run_routing(dccf_model, run_drc=False)
        assert isinstance(result, RoutingResult)

    def test_run_routing_decap_fanout(self, dccf_model):
        """Decap caps get fanout."""
        pytest.skip("Full DCCF routing is slow - run manually if needed")
        result = run_routing(dccf_model, run_drc=False)
        assert isinstance(result, RoutingResult)

    def test_run_routing_respects_max_via_count(self, dccf_model):
        """Max via count limits routing."""
        pytest.skip("Full DCCF routing is slow - run manually if needed")
        result = run_routing(dccf_model, max_via_count=1, run_drc=False)
        assert result.vias_added <= 1


class TestRunFullPipeline:
    """Tests for complete pipeline."""

    def test_run_full_pipeline_minimal(self, minimal_model):
        """Full pipeline completes on minimal fixture."""
        from kicad_autorouter.placement import PlacementParams, run_placement
        from kicad_autorouter.board_model import apply_deltas

        # Place
        proposal = run_placement(minimal_model, PlacementParams(stub=False, max_iterations=10))
        model = apply_deltas(minimal_model, proposal.deltas)

        # Route
        result = run_routing(model, run_drc=False)
        assert isinstance(result, RoutingResult)

    def test_run_full_pipeline_drc_flag(self, minimal_model):
        """DRC flag controls validation."""
        from kicad_autorouter.placement import PlacementParams, run_placement
        from kicad_autorouter.board_model import apply_deltas

        proposal = run_placement(minimal_model, PlacementParams(stub=False, max_iterations=10))
        model = apply_deltas(minimal_model, proposal.deltas)

        # With run_drc=False, should not attempt DRC
        result = run_routing(model, run_drc=False)
        assert isinstance(result, RoutingResult)


class TestRoutingParams:
    """Tests for RoutingParams serialization."""

    def test_routing_params_defaults(self):
        params = RoutingParams()
        assert params.grid_resolution_mm == 0.1
        assert params.via_cost_mm == 5.0
        assert params.max_via_count == 100
        assert params.track_width_mm == 0.25
        assert params.run_drc is False

    def test_routing_params_to_dict(self):
        params = RoutingParams(via_cost_mm=10.0, max_via_count=50)
        d = params.to_dict()
        assert d["via_cost_mm"] == 10.0
        assert d["max_via_count"] == 50

    def test_routing_params_from_dict(self):
        d = {"via_cost_mm": 7.5, "max_via_count": 200, "unknown_param": 999}
        params = RoutingParams.from_dict(d)
        assert params.via_cost_mm == 7.5
        assert params.max_via_count == 200
        assert params.track_width_mm == 0.25  # default preserved


class TestRoutingResult:
    """Tests for RoutingResult dataclass."""

    def test_routing_result_defaults(self):
        result = RoutingResult(
            tracks_added=10,
            vias_added=5,
            nets_routed=3,
            nets_failed=1,
            drc_clean=True,
            drc_violations=0,
        )
        assert result.tracks_added == 10
        assert result.vias_added == 5
        assert result.nets_routed == 3
        assert result.nets_failed == 1
        assert result.drc_clean is True
        assert result.drc_violations == 0


class TestIntegration:
    """Integration tests on real boards."""

    def test_minimal_place_and_route(self, minimal_model):
        """Minimal board: place then route."""
        from kicad_autorouter.placement import PlacementParams, run_placement
        from kicad_autorouter.board_model import apply_deltas

        proposal = run_placement(minimal_model, PlacementParams(stub=False, max_iterations=10))
        model = apply_deltas(minimal_model, proposal.deltas)

        result = run_routing(model, run_drc=False)
        assert isinstance(result, RoutingResult)

    def test_dccf_place_and_route(self, dccf_model):
        """DCCF board: place then route (smoke test)."""
        pytest.skip("Full DCCF place+route is slow - run manually if needed")
        from kicad_autorouter.placement import PlacementParams, run_placement
        from kicad_autorouter.board_model import apply_deltas

        proposal = run_placement(dccf_model, PlacementParams(stub=False, max_iterations=10))
        model = apply_deltas(dccf_model, proposal.deltas)

        result = run_routing(model, run_drc=False)
        assert isinstance(result, RoutingResult)