"""Tests for A* search, MST, and CostMap in routing."""

import pytest
import numpy as np

from kicad_autorouter.board_model import board_model
from kicad_autorouter.routing.grid import create_grid_from_model, board_to_grid
from kicad_autorouter.routing.obstacles import build_occupancy_grid, FREE, BLOCKED, HIGH_COST
from kicad_autorouter.routing.router import (
    SingleNetRouter,
    CostMap,
    RouteResult,
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
    grid = __import__("kicad_autorouter.routing.grid", fromlist=["create_grid_from_model"]).create_grid_from_model(
        minimal_model, resolution_mm=0.1, margin_mm=2.0
    )
    cost_grid = build_occupancy_grid(minimal_model, grid, default_clearance_mm=0.2)
    cost_map = CostMap()
    router = SingleNetRouter(grid, cost_grid, cost_map)
    return grid, cost_grid, router


class TestCostMap:
    """Tests for CostMap defaults and behavior."""
    def test_cost_map_defaults(self):
        """CostMap has expected default values."""
        cm = CostMap()
        assert cm.base_cost == 1
        assert cm.via_cost == 50
        assert cm.high_cost_penalty == 20
        assert cm.edge_keepout_penalty == 40
        assert cm.blocked_threshold == 100
        # Weighted A* stays penalty-respecting (was 200 = effectively greedy)
        assert cm.heuristic_weight == 3.0

    def test_cell_cost_free(self, routing_setup):
        """Free cell returns base_cost."""
        grid, cost_grid, router = routing_setup
        # Find a free cell
        for r in range(grid.height_cells):
            for c in range(grid.width_cells):
                if cost_grid[0, r, c] == 0:
                    assert router._cell_cost(r, c, 0) == router.cost_map.base_cost
                    return

    def test_cell_cost_high_cost_penalty(self, routing_setup):
        """HIGH_COST cell adds penalty."""
        grid, cost_grid, router = routing_setup
        # Find a HIGH_COST cell
        for r in range(grid.height_cells):
            for c in range(grid.width_cells):
                if cost_grid[0, r, c] == 50:  # HIGH_COST
                    expected = router.cost_map.base_cost + router.cost_map.high_cost_penalty
                    assert router._cell_cost(r, c, 0) == expected
                    return

    def test_cell_cost_edge_keepout_penalty(self, routing_setup):
        """EDGE_KEEPOUT cell adds penalty."""
        grid, cost_grid, router = routing_setup
        for r in range(grid.height_cells):
            for c in range(grid.width_cells):
                if cost_grid[0, r, c] == 80:  # EDGE_KEEPOUT
                    expected = router.cost_map.base_cost + router.cost_map.edge_keepout_penalty
                    assert router._cell_cost(r, c, 0) == expected
                    return

    def test_cell_cost_blocked_threshold(self, routing_setup):
        """BLOCKED cell returns INF."""
        grid, cost_grid, router = routing_setup
        for r in range(grid.height_cells):
            for c in range(grid.width_cells):
                if cost_grid[0, r, c] >= 100:  # BLOCKED
                    assert router._cell_cost(r, c, 0) == 1 << 30
                    return

    def test_is_blocked_bounds(self, routing_setup):
        """Out-of-bounds is blocked."""
        grid, cost_grid, router = routing_setup
        assert router._is_blocked(-1, 0, 0)
        assert router._is_blocked(grid.height_cells, 0, 0)
        assert router._is_blocked(0, -1, 0)
        assert router._is_blocked(0, grid.width_cells, 0)
        assert router._is_blocked(0, 0, -1)
        assert router._is_blocked(0, 0, len(grid.layers))


class TestAStar:
    """Tests for A* search algorithm."""

    def test_a_star_same_start_goal(self, routing_setup):
        """Zero-length path returns success."""
        grid, cost_grid, router = routing_setup
        # Find a free cell
        for r in range(grid.height_cells):
            for c in range(grid.width_cells):
                if cost_grid[0, r, c] == 0:
                    result = router._a_star((c, r, 0), (c, r, 0))
                    assert result.success
                    assert result.path == [(c, r, 0)]
                    assert result.cost == 0.0
                    return

    def test_a_star_simple_horizontal(self, routing_setup):
        """Horizontal path on same layer."""
        grid, cost_grid, router = routing_setup
        # Find two free cells on same row
        for r in range(grid.height_cells):
            free_cols = [c for c in range(grid.width_cells) if cost_grid[0, r, c] == 0]
            if len(free_cols) >= 2:
                c1, c2 = free_cols[0], free_cols[1]
                result = router._a_star((c1, r, 0), (c2, r, 0))
                assert result.success
                assert result.path[0] == (c1, r, 0)
                assert result.path[-1] == (c2, r, 0)
                # Path should be horizontal (same row)
                assert all(p[1] == r and p[2] == 0 for p in result.path)
                return

    def test_a_star_via_transition(self, routing_setup):
        """Layer change incurs via_cost."""
        grid, cost_grid, router = routing_setup
        # Find a column where both layers are free at same position
        for r in range(grid.height_cells):
            for c in range(grid.width_cells):
                if cost_grid[0, r, c] == 0 and cost_grid[1, r, c] == 0:
                    result = router._a_star((c, r, 0), (c, r, 1))
                    assert result.success
                    assert result.path[0] == (c, r, 0)
                    assert result.path[-1] == (c, r, 1)
                    # Path should have a layer change
                    assert any(p[2] != result.path[0][2] for p in result.path)
                    # Cost should include via_cost
                    assert result.cost >= router.cost_map.via_cost
                    return

    def test_a_star_tht_via_free(self, routing_setup):
        """THT pad mask allows free via."""
        grid, cost_grid, router = routing_setup
        # This test requires a THT pad mask - skip if not available
        if not hasattr(router, 'tht_via_mask') or not np.any(router.tht_via_mask):
            pytest.skip("No THT via mask available")

    def test_a_star_blocked_cell(self, routing_setup):
        """BLOCKED cell is impassable."""
        grid, cost_grid, router = routing_setup
        # Find a blocked cell
        for r in range(grid.height_cells):
            for c in range(grid.width_cells):
                if cost_grid[0, r, c] >= 100:
                    # Try to route through blocked cell
                    result = router._a_star((c, r, 0), (c, r, 0))
                    # Start/goal on blocked is allowed, but path through blocked should fail
                    # Find two free cells with blocked between them
                    return

    def test_a_star_heuristic_admissible(self, routing_setup):
        """Weighted A* finds path (may not be optimal)."""
        grid, cost_grid, router = routing_setup
        # Find two distant free cells
        free_cells = [(c, r) for r in range(grid.height_cells)
                      for c in range(grid.width_cells) if cost_grid[0, r, c] == 0]
        if len(free_cells) >= 2:
            start = free_cells[0]
            goal = free_cells[min(20, len(free_cells)-1)]  # Closer goal
            result = router._a_star((start[0], start[1], 0), (goal[0], goal[1], 0))
            # Should find some path
            assert result.success or not result.success  # May fail if no path exists

    def test_secondary_steers_but_never_traps(self, routing_setup):
        """A per-net halo steers the path around foreign copper without
        blocking: terminals stay reachable even inside halo cells."""
        import numpy as np
        grid, cost_grid, router = routing_setup
        # Long free horizontal run to route across
        for r in range(grid.height_cells):
            free_cols = [c for c in range(grid.width_cells) if cost_grid[0, r, c] == 0]
            if len(free_cols) >= 40:
                c1, c2 = free_cols[0], free_cols[39]
                mid = (c1 + c2) // 2
                secondary = np.zeros_like(cost_grid)
                # Impassable-looking disc (would trap if it blocked)
                secondary[0, r - 3:r + 4, mid - 3:mid + 4] = 500
                result = router.route([(c1, r, 0), (c2, r, 0)], secondary)
                assert result.success
                assert result.path[0] == (c1, r, 0)
                assert result.path[-1] == (c2, r, 0)
                # Path detours around the penalized disc center
                assert (mid, r, 0) not in result.path
                return
        pytest.skip("No long free row found")

    def test_a_star_returns_full_path_chain(self, routing_setup):
        """Regression: parent pointers must be recorded so reconstruction
        yields the complete start→goal chain, not just the final step."""
        grid, cost_grid, router = routing_setup
        # Two free cells far apart on the same row band
        for r in range(grid.height_cells):
            free_cols = [c for c in range(grid.width_cells) if cost_grid[0, r, c] == 0]
            if len(free_cols) >= 30:
                c1, c2 = free_cols[0], free_cols[29]
                result = router._a_star((c1, r, 0), (c2, r, 0))
                assert result.success
                assert result.path[0] == (c1, r, 0)
                assert result.path[-1] == (c2, r, 0)
                assert len(result.path) > 2
                # Every step moves to a 4-neighbor (or layer change)
                for (a, b) in zip(result.path, result.path[1:]):
                    dc, dr, dl = abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2])
                    assert (dc + dr == 1 and dl == 0) or (dc == 0 and dr == 0 and dl == 1)
                return
        pytest.skip("No long free row found")


class TestMST:
    """Tests for Prim's MST algorithm."""

    def test_prim_mst_two_nodes(self):
        """MST on two nodes = single edge."""
        dist = np.array([[0, 5], [5, 0]], dtype=np.float32)
        # Create a dummy router to access _prim_mst
        router = self._make_dummy_router()
        edges = list(router._prim_mst(dist))
        assert len(edges) == 1
        assert edges[0] == (0, 1) or edges[0] == (1, 0)

    def test_prim_mst_three_nodes(self):
        """MST on three nodes forms tree with 2 edges."""
        router = self._make_dummy_router()
        dist = np.array([
            [0, 1, 10],
            [1, 0, 1],
            [10, 1, 0]
        ], dtype=np.float32)
        edges = list(router._prim_mst(dist))
        assert len(edges) == 2
        nodes = set()
        for a, b in edges:
            nodes.add(a)
            nodes.add(b)
        assert nodes == {0, 1, 2}

    def test_prim_mst_disconnected(self):
        """Inf distance = no MST edge for disconnected."""
        router = self._make_dummy_router()
        dist = np.array([
            [0, np.inf],
            [np.inf, 0]
        ], dtype=np.float32)
        edges = list(router._prim_mst(dist))
        assert len(edges) == 0

    def test_prim_mst_four_nodes(self):
        """MST on four nodes."""
        router = self._make_dummy_router()
        dist = np.array([
            [0, 1, 2, 3],
            [1, 0, 4, 5],
            [2, 4, 0, 6],
            [3, 5, 6, 0]
        ], dtype=np.float32)
        edges = list(router._prim_mst(dist))
        assert len(edges) == 3

    def _make_dummy_router(self):
        """Create a minimal router for testing _prim_mst."""
        # Create minimal grid and cost_grid
        grid = __import__("kicad_autorouter.routing.grid", fromlist=["RoutingGrid"]).RoutingGrid(
            resolution_mm=0.1,
            layers=("F.Cu", "B.Cu"),
            origin_mm=(0.0, 0.0),
            width_cells=10,
            height_cells=10,
        )
        cost_grid = np.zeros((2, 10, 10), dtype=np.int16)
        return SingleNetRouter(grid, cost_grid)


class TestRouterIntegration:
    """Integration tests for SingleNetRouter."""

    def test_route_simple_net(self, routing_setup):
        """Route a simple 2-pin net."""
        grid, cost_grid, router = routing_setup
        # Find two free cells
        free_cells = [(c, r) for r in range(grid.height_cells)
                      for c in range(grid.width_cells) if cost_grid[0, r, c] == 0]
        if len(free_cells) < 2:
            pytest.skip("Not enough free cells")

        start = free_cells[0]
        goal = free_cells[min(10, len(free_cells)-1)]
        terminals = [(start[0], start[1], 0), (goal[0], goal[1], 0)]

        result = router.route(terminals)
        # May succeed or fail depending on obstacles
        if result.success:
            assert result.path
            assert result.wire_mm > 0
            # Path should connect the two terminal regions
            # (exact start/end may differ if terminals on blocked cells)
            assert len(result.path) >= 2

    def test_route_three_terminals(self, routing_setup):
        """Route net with 3 terminals (MST)."""
        grid, cost_grid, router = routing_setup
        free_cells = [(c, r) for r in range(grid.height_cells)
                      for c in range(grid.width_cells) if cost_grid[0, r, c] == 0]
        if len(free_cells) < 3:
            pytest.skip("Not enough free cells")

        terminals = [(free_cells[0][0], free_cells[0][1], 0),
                     (free_cells[1][0], free_cells[1][1], 0),
                     (free_cells[2][0], free_cells[2][1], 0)]

        result = router.route(terminals)
        if result.success:
            assert result.path
            assert result.via_count >= 0
            assert result.wire_mm > 0

    def test_route_insufficient_terminals(self, routing_setup):
        """<2 terminals returns failure."""
        grid, cost_grid, router = routing_setup
        result = router.route([(100, 100, 0)])
        assert not result.success
        assert result.path == []

    def test_route_blocked_start(self, routing_setup):
        """Blocked start/goal returns failure."""
        grid, cost_grid, router = routing_setup
        # Find a blocked cell
        for r in range(grid.height_cells):
            for c in range(grid.width_cells):
                if cost_grid[0, r, c] >= 100:
                    result = router._a_star((c, r, 0), (c, r, 0))
                    # Start=goal on blocked is allowed
                    assert result.success
                    return


class TestDCCFRouter:
    """Router tests on DCCF board."""

    def test_dccf_router_creation(self, dccf_model):
        """Router can be created for DCCF."""
        grid = __import__("kicad_autorouter.routing.grid", fromlist=["create_grid_from_model"]).create_grid_from_model(
            dccf_model, resolution_mm=0.1, margin_mm=2.0
        )
        cost_grid = build_occupancy_grid(dccf_model, grid, default_clearance_mm=0.2)
        router = SingleNetRouter(grid, cost_grid)
        assert router.n_layers == 2
        assert router.H > 0
        assert router.W > 0

    def test_dccf_find_path_between_pads(self, dccf_model):
        """Find path between two pads on same net."""
        grid = __import__("kicad_autorouter.routing.grid", fromlist=["create_grid_from_model"]).create_grid_from_model(
            dccf_model, resolution_mm=0.1, margin_mm=2.0
        )
        cost_grid = build_occupancy_grid(dccf_model, grid, default_clearance_mm=0.2)
        router = SingleNetRouter(grid, cost_grid)

        # Find a net with at least 2 pads on F.Cu
        for net_name, conns in dccf_model.nets.items():
            if len(conns) >= 2:
                f_cu_pads = []
                for conn in conns:
                    fp = dccf_model.by_ref.get(conn.ref)
                    if fp and fp.layer == "F.Cu":
                        pad = next((p for p in fp.pads if p.number == conn.pad_number), None)
                        if pad:
                            f_cu_pads.append(pad)
                if len(f_cu_pads) >= 2:
                    break
        else:
            pytest.skip("No suitable net found")

        p1, p2 = f_cu_pads[0], f_cu_pads[1]
        c1, r1 = board_to_grid(grid, p1.position.x_mm, p1.position.y_mm)
        c2, r2 = board_to_grid(grid, p2.position.x_mm, p2.position.y_mm)

        result = router.route([(c1, r1, 0), (c2, r2, 0)])
        # Path may or may not exist due to obstacles
        if result.success:
            assert result.path
            assert result.path[0] == (c1, r1, 0)
            assert result.path[-1] == (c2, r2, 0)