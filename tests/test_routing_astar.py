"""Tests for A* search behavioral properties."""

import pytest
import numpy as np

from kicad_autorouter.routing.grid import create_grid_from_model, board_to_grid
from kicad_autorouter.routing.obstacles import build_occupancy_grid, FREE, BLOCKED, HIGH_COST
from kicad_autorouter.routing.router import (
    SingleNetRouter,
    CostMap,
    RouteResult,
)


class TestAStar:
    """Tests for A* search algorithm behavioral properties."""

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