"""Phase 3: pours are passable-at-toll + severance steers (no hard BLOCKED).

- Pour cells cost base + pour_penalty (mechanism pin).
- A net with both terminals inside a pour routes (regression: under the
  old BLOCKED model the escape steps were refused and routing failed).
- severance_cost math: exact penalties on narrow corridors, zero on wide
  copper and outside the pour, clipped below SECONDARY_BLOCKED stacking.
- End to end: route + auto-stitch on a severed board leaves one network.
"""

import numpy as np
import pytest

from kicad_autorouter.board_model import NetConnection
from kicad_autorouter.routing.grid import create_grid_from_model
from kicad_autorouter.routing.obstacles import POUR, build_occupancy_grid
from kicad_autorouter.routing.pipeline import RoutingParams, route_nets
from kicad_autorouter.routing.pour import pour_islands, severance_cost
from kicad_autorouter.routing.router import CostMap

from dataclasses import replace

from .test_pour import _board, _rect_zone, _track
from .helpers import make_fp


def _crossing_board():
    # Compact geometry (small grid = fast A*): 20x12 pour, S pads at x=3/17.
    fa = make_fp("A", "uuid-a", 3.0, 6.0, ("S",), hw=0.25, hh=0.25)
    fb = make_fp("B", "uuid-b", 17.0, 6.0, ("S",), hw=0.25, hh=0.25)
    model = _board(fps=[fa, fb],
                   zones=[_rect_zone("z1", "GND", ("F.Cu",), x1=20.0, y1=12.0)],
                   edge=25.0)
    return replace(model, nets={"S": (NetConnection("A", "1"), NetConnection("B", "1"))})


@pytest.mark.unit
class TestPourPassable:
    def test_pour_cell_cost_is_toll(self):
        from kicad_autorouter.routing.router import SingleNetRouter
        model = _crossing_board()
        grid = create_grid_from_model(model)
        cost_grid = build_occupancy_grid(model, grid, default_clearance_mm=0.2)
        assert np.any(cost_grid[0] == POUR)
        cm = CostMap()
        router = SingleNetRouter(grid, cost_grid, cm)
        from kicad_autorouter.routing.grid import board_to_grid
        c, r = board_to_grid(grid, 10.0, 5.0)  # pour interior, no pads
        assert cost_grid[0, r, c] == POUR
        assert router._cell_cost(r, c, 0) == cm.base_cost + cm.pour_penalty

    def test_terminals_inside_pour_route(self):
        # Regression: zones as BLOCKED refused every escape step, so any
        # net with pads inside a pour failed outright.
        model = _crossing_board()
        res, _, _ = route_nets(
            model, ["S"], set(),
            params=RoutingParams(stitch_gnd_vias=False, pour_severance_weight=0.0),
        )
        assert res.nets_routed == 1
        assert res.nets_failed == 0
        assert res.tracks_added > 0


@pytest.mark.unit
class TestSeveranceMath:
    def _cost(self, corridors, weight=100.0, width=0.25, clearance=0.2, thick=0.25):
        from kicad_autorouter.routing.grid import RoutingGrid
        grid = RoutingGrid(resolution_mm=0.1, layers=("F.Cu",),
                           origin_mm=(0.0, 0.0), width_cells=2, height_cells=1)
        return severance_cost({"F.Cu": np.array(corridors, dtype=np.float32)},
                              thick, grid, width, clearance, weight)[0]

    def test_exact_penalty_shape(self):
        # Threshold = (0.125 + 0.2 + 0.125) / 0.1 = 4.5 cells (int16-truncated).
        out = self._cost([[10.0, 2.0]])
        assert out[0, 0] == 0  # wide copper: no toll
        assert out[0, 1] == int(100.0 * (1.0 - 2.0 / 4.5))

    def test_outside_pour_untolled(self):
        # Corridor 0 outside copper must NOT toll (it would pave free space).
        out = self._cost([[0.0, 3.0]])
        assert out[0, 0] == 0
        assert out[0, 1] > 0

    def test_clip_below_blocked_stacking(self):
        out = self._cost([[1.0, 1.0]], weight=100000.0)
        assert out[0, 0] == 399  # +500 fanout halo can never reach 1000
        assert out[0, 1] == 399

    def test_zero_weight_disables(self):
        out = self._cost([[1.0]], weight=0.0)
        assert out[0, 0] == 0

    def test_pour_nets_skip_penalty(self):
        # GND tracks join the pour; only foreign nets pay severance. Pinned
        # at the pipeline level: pour nets never get a severance grid.
        from kicad_autorouter.routing.pour import pour_net_names
        model = _crossing_board()
        assert pour_net_names(model) == ["GND"]
        assert "S" not in pour_net_names(model)


@pytest.mark.unit
class TestEndToEnd:
    def test_route_and_stitch_leaves_one_network(self):
        # Single-layer sever: S routes around the wall end, the split pour
        # gets auto-bridged. (Compact board: keeps A* fast.)
        fa = make_fp("A", "uuid-a", 3.0, 6.0, ("S",), hw=0.25, hh=0.25)
        fb = make_fp("B", "uuid-b", 17.0, 6.0, ("S",), hw=0.25, hh=0.25)
        model = _board(
            fps=[fa, fb],
            zones=[_rect_zone("z1", "GND", ("F.Cu",), x1=20.0, y1=12.0)],
            tracks=[_track(10.0, -2.0, 10.0, 14.0, "X")],
            edge=25.0,
        )
        model = replace(model, nets={
            "S": (NetConnection("A", "1"), NetConnection("B", "1")),
        })
        res, _, grid = route_nets(model, ["S"], set(), params=RoutingParams())
        assert res.nets_routed == 1
        assert res.gnd_islands_before >= 2
        assert res.gnd_islands_after == 1
        assert res.stitch_vias_added + res.stitch_tracks_added >= 1
