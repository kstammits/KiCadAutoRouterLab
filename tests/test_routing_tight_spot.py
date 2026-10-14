"""Tight-spot routing: tiny pads inside cramped foreign clusters (DCCF VIN).

VIN marked as power fails on the *unrouted* DCCF fixture: Q2:1 is a
0.4x1.35mm SMD pad buried in a GND-shield cluster whose foreign halos
overlap its own terminal cell, and the ~1000-cell corridor to Q1/C6
threads hundreds of tolled halo cells. The search then floods the whole
board and quits at the expansion cap after tens of seconds.

Users cannot move pads or retune costs, so the open directions are
pushing/ripping lower-priority copper and halo retuning. These tests pin
the scenario with tight budgets (seconds, not minutes) and require
graceful bounded behavior — no hangs — plus the technical facts any
future fix must address. A small synthetic cluster covers the same
shape without the DCCF fixture cost.
"""

import time

import pytest

from kicad_autorouter.routing.grid import create_grid_from_model, board_to_grid
from kicad_autorouter.routing.obstacles import (
    build_occupancy_grid,
    build_fanout_cost,
    build_via_block_mask,
)
from kicad_autorouter.routing.pipeline import (
    _build_tht_via_mask,
    _terminals_for_net,
    resolve_clearance,
    resolve_track_width,
    via_halo_radius_cells,
)
from kicad_autorouter.routing.router import CostMap, SingleNetRouter
from kicad_autorouter.routing.scheduler import RipUpManager

from .helpers import make_fp as make_test_fp


def _router_for(model, grid, cost_grid, net, width_mm, clearance_mm,
                max_expansions, weight=3.0, window_retries=1):
    from kicad_autorouter.routing.pipeline import RoutingParams
    params = RoutingParams(grid_resolution_mm=grid.resolution_mm)
    tht = _build_tht_via_mask(model, grid)
    vbm = build_via_block_mask(
        model, grid, clearance_mm=clearance_mm,
        track_half_mm=width_mm / 2.0,
    )
    router = SingleNetRouter(
        grid, cost_grid, CostMap(heuristic_weight=weight),
        tht, vbm, max_expansions=max_expansions,
        window_retries=window_retries,
    )
    router.via_clearance_cells = via_halo_radius_cells(
        params.via_size_mm, clearance_mm, width_mm / 2.0,
        grid.resolution_mm,
    )
    secondary = build_fanout_cost(
        model, grid, net, clearance_mm=clearance_mm,
        track_half_mm=width_mm / 2.0,
    )
    return router, secondary


@pytest.mark.dccf
class TestVinTightSpot:
    """DCCF VIN (power-marked) technical facts + bounded behavior."""

    def test_q2_terminal_sits_inside_foreign_halo(self, dccf_model):
        """Q2:1's own cell is tolled by a neighboring foreign halo.

        The routed net's own pads are excluded from halo generation, so a
        nonzero secondary value here means an *unavoidable* foreign overlap
        — the escape starts inside tolled terrain through no fault of
        placement. Any terminal-exemption or retune fix must clear this.
        """
        from kicad_autorouter.routing.pipeline import RoutingParams
        params = RoutingParams(grid_resolution_mm=0.1)
        grid = create_grid_from_model(dccf_model, 0.1, 2.0)
        clearance_mm = resolve_clearance(dccf_model, params, "VIN")
        secondary = build_fanout_cost(
            dccf_model, grid, "VIN", clearance_mm=clearance_mm,
            track_half_mm=0.25,
        )
        vbm = build_via_block_mask(
            dccf_model, grid, clearance_mm=clearance_mm,
            track_half_mm=0.25,
        )
        terminals = _terminals_for_net(dccf_model, grid, "VIN")
        assert len(terminals) == 3
        # Q2:1 is the far tiny SMD terminal (matches fixture survey).
        qc, qr, ql = terminals[2]
        assert ql == 0  # F.Cu
        assert int(secondary[ql, qr, qc]) > 0
        assert bool(vbm[qr, qc])

    def test_q1_q2_pair_fails_fast_with_bounded_expansions(self, dccf_model):
        """The hard leg quits at the cap instead of hanging.

        Budget: 15k expansions x 2 windows (initial + full board).
        Passes whether the pair routes (future fix) or fails gracefully.
        """
        from kicad_autorouter.routing.pipeline import RoutingParams
        params = RoutingParams(grid_resolution_mm=0.1)
        grid = create_grid_from_model(dccf_model, 0.1, 2.0)
        cost_grid = build_occupancy_grid(
            dccf_model, grid, default_clearance_mm=params.clearance_mm
        )
        width_mm = resolve_track_width(dccf_model, params, "VIN", ("VIN",))
        clearance_mm = resolve_clearance(dccf_model, params, "VIN")
        router, secondary = _router_for(
            dccf_model, grid, cost_grid, "VIN", width_mm, clearance_mm,
            max_expansions=15_000,
        )
        terminals = _terminals_for_net(dccf_model, grid, "VIN")
        t0 = time.perf_counter()
        result = router.route([terminals[1], terminals[2]], secondary)
        elapsed = time.perf_counter() - t0
        # Two windows + one corridor attempt max: bounded work, seconds
        # not minutes (+1 per search: the cap trips after the increment).
        assert result.expansions <= 3 * (15_000 + 1)
        assert elapsed < 20.0
        if result.success:
            assert result.path[0][:2] == terminals[1][:2]
            assert result.path[-1][:2] == terminals[2][:2]

    def test_corridor_routes_q1_q2_pair(self, dccf_model):
        """Corridor guidance routes the tight leg previous tests cap out on.

        With room to finish (200k/search), the corridor-restricted
        admissible search finds the walkable path in ~60k expansions.
        """
        from kicad_autorouter.routing.corridor import find_corridor
        from kicad_autorouter.routing.pipeline import RoutingParams
        from kicad_autorouter.routing.router import SECONDARY_BLOCKED
        params = RoutingParams(grid_resolution_mm=0.1)
        grid = create_grid_from_model(dccf_model, 0.1, 2.0)
        cost_grid = build_occupancy_grid(
            dccf_model, grid, default_clearance_mm=params.clearance_mm
        )
        width_mm = resolve_track_width(dccf_model, params, "VIN", ("VIN",))
        clearance_mm = resolve_clearance(dccf_model, params, "VIN")
        router, secondary = _router_for(
            dccf_model, grid, cost_grid, "VIN", width_mm, clearance_mm,
            max_expansions=200_000,
        )
        terminals = _terminals_for_net(dccf_model, grid, "VIN")
        corridor = find_corridor(
            cost_grid, secondary, terminals[1], terminals[2], 100,
            SECONDARY_BLOCKED,
        )
        assert corridor is not None
        # Corridor is a small fraction of the board.
        assert corridor.sum() < 0.05 * corridor.size
        t0 = time.perf_counter()
        result = router.route([terminals[1], terminals[2]], secondary)
        elapsed = time.perf_counter() - t0
        assert result.success
        assert elapsed < 20.0
        assert result.path[0][:2] == terminals[1][:2]
        assert result.path[-1][:2] == terminals[2][:2]

    def test_full_vin_route_terminates_bounded(self, dccf_model):
        """Whole-net VIN attempt terminates with bounded total expansions."""
        from kicad_autorouter.routing.pipeline import RoutingParams
        params = RoutingParams(grid_resolution_mm=0.1)
        grid = create_grid_from_model(dccf_model, 0.1, 2.0)
        cost_grid = build_occupancy_grid(
            dccf_model, grid, default_clearance_mm=params.clearance_mm
        )
        width_mm = resolve_track_width(dccf_model, params, "VIN", ("VIN",))
        clearance_mm = resolve_clearance(dccf_model, params, "VIN")
        router, secondary = _router_for(
            dccf_model, grid, cost_grid, "VIN", width_mm, clearance_mm,
            max_expansions=10_000,
        )
        terminals = _terminals_for_net(dccf_model, grid, "VIN")
        t0 = time.perf_counter()
        result = router.route(terminals, secondary)
        elapsed = time.perf_counter() - t0
        # 3 pairs x (2 windows + 1 corridor) x 10k cap: hard upper
        # bound on work (+1 per search: the cap trips after the increment).
        assert result.expansions <= 3 * 3 * (10_000 + 1)
        assert elapsed < 30.0
        assert isinstance(result.success, bool)


class TestTinyPadCluster:
    """Synthetic tight spot: tiny SMD terminal inside a foreign cluster.

    Small grid (pads within ~15mm) so the search terminates in well
    under a second either way; asserts the cramped precondition plus
    bounded graceful behavior.
    """

    def _cluster_model(self):
        from dataclasses import replace

        from kicad_autorouter.board_model import (
            BoardModel,
            NetConnection,
            Point,
        )
        from .helpers import make_smd_pad

        # Target net T: tiny 0.4x1.35 pad at (0, 0), partner 15mm away.
        tiny = replace(
            make_smd_pad("1", "T", size_mm=(0.4, 1.35)),
            position=Point(0.0, 0.0),
        )
        far = replace(
            make_smd_pad("1", "T", size_mm=(1.6, 1.6)),
            position=Point(15.0, 0.0),
        )
        # Foreign cluster around the tiny pad (halos overlap it).
        foreigners = []
        for k, (dx, dy, net) in enumerate(
            [(0.9, 0.0, "GND"), (-0.9, 0.4, "F2"), (0.2, -1.0, "F3")]
        ):
            foreigners.append(replace(
                make_smd_pad(str(k + 1), net, size_mm=(1.0, 1.0)),
                position=Point(dx, dy),
            ))
        fp_t0 = make_test_fp("T0", "uuid-t0", 0.0, 0.0, ("T",))
        fp_t0 = replace(fp_t0, pads=(tiny,), courtyard=())
        fp_t1 = make_test_fp("T1", "uuid-t1", 15.0, 0.0, ("T",))
        fp_t1 = replace(fp_t1, pads=(far,), courtyard=())
        fp_f = make_test_fp("F", "uuid-f", 0.0, 0.0, ("GND", "F2", "F3"))
        fp_f = replace(fp_f, pads=tuple(foreigners), courtyard=())
        fps = (fp_t0, fp_t1, fp_f)
        return BoardModel(
            footprints=fps,
            keepout_zones=(),
            nets={
                "T": (NetConnection(ref="T0", pad_number="1"),
                      NetConnection(ref="T1", pad_number="1")),
                "GND": (NetConnection(ref="F", pad_number="1"),),
                "F2": (NetConnection(ref="F", pad_number="2"),),
                "F3": (NetConnection(ref="F", pad_number="3"),),
            },
            by_ref={fp.ref: fp for fp in fps},
        )

    def test_foreign_halo_overlaps_tiny_terminal(self):
        """Precondition: the tiny terminal cell is foreign-tolled."""
        model = self._cluster_model()
        grid = create_grid_from_model(model, 0.1, 2.0)
        secondary = build_fanout_cost(
            model, grid, "T", clearance_mm=0.2, track_half_mm=0.125
        )
        terminals = _terminals_for_net(model, grid, "T")
        assert len(terminals) == 2
        c, r, lyr = terminals[0]
        assert int(secondary[lyr, r, c]) > 0

    def test_tiny_escape_terminates_bounded(self):
        """Routing the cluster terminates fast, success or graceful fail."""
        model = self._cluster_model()
        grid = create_grid_from_model(model, 0.1, 2.0)
        cost_grid = build_occupancy_grid(model, grid, default_clearance_mm=0.2)
        router, secondary = _router_for(
            model, grid, cost_grid, "T", 0.25, 0.2, max_expansions=20_000
        )
        terminals = _terminals_for_net(model, grid, "T")
        t0 = time.perf_counter()
        result = router.route(terminals, secondary)
        elapsed = time.perf_counter() - t0
        # Single pair x (2 windows + 1 corridor) x cap.
        assert result.expansions <= 3 * (20_000 + 1)
        assert elapsed < 10.0
        if result.success:
            assert result.path[0] == terminals[0]
            assert result.path[-1] == terminals[1]


class TestTightSpotRipupPolicy:
    """Today's eviction policy, as it applies to a VIN-like tight spot.

    Only GND-family copper may be evicted (cheaply rebuilt via pours +
    stitching); signal/power copper is never ripped. A future push/shove
    or priority-rip feature changes this gate — these tests frame it.
    """

    def test_gnd_victims_rippable_by_anyone(self):
        ripup = RipUpManager()
        assert ripup.can_rip("VIN", "GND")
        assert ripup.can_rip("VIN", "GND_PWR")

    def test_signal_victims_not_rippable(self):
        ripup = RipUpManager()
        assert not ripup.can_rip("VIN", "Net-(R1-Pad1)")
        assert not ripup.can_rip("VIN", "SDA")

    def test_same_net_reroute_allowed(self):
        ripup = RipUpManager()
        assert ripup.can_rip("VIN", "VIN")
