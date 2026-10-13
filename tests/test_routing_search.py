"""Fast tests for windowed search, via mask, and Kruskal pair ordering.

All synthetic (no board fixtures, no DCCF): small grids, each test <1s.
Contract under test: speed over identical paths — windowed routes must
succeed with valid endpoint-terminal chains and bounded expansions, not
match full-board paths cell-for-cell.
"""

import numpy as np
import pytest

from kicad_autorouter.routing.grid import RoutingGrid
from kicad_autorouter.routing.output import routes_to_tracks_vias
from kicad_autorouter.routing.pair_cost import (
    kruskal_connect,
    manhattan,
    ordered_pairs,
)
from kicad_autorouter.routing.router import CostMap, SingleNetRouter
from kicad_autorouter.routing.search_window import for_pair, full_board, grow
from kicad_autorouter.routing.via_sites import build_via_ok_mask


def _grid(w=80, h=40):
    return RoutingGrid(
        resolution_mm=1.0, layers=("F.Cu", "B.Cu"),
        origin_mm=(0.0, 0.0), width_cells=w, height_cells=h,
    )


def _router(grid, cost=None, **kw):
    if cost is None:
        cost = np.zeros((2, grid.height_cells, grid.width_cells), dtype=np.int16)
    kw.setdefault("max_expansions", 100_000)
    return SingleNetRouter(grid, cost, CostMap(), **kw), cost


def _valid_chain(path, start, goal):
    assert path[0] == start
    assert path[-1] == goal
    for a, b in zip(path, path[1:]):
        dc, dr, dl = abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2])
        assert (dc + dr == 1 and dl == 0) or (dc == 0 and dr == 0 and dl == 1)


class TestSearchWindow:
    def test_window_bounds_and_grows_to_full(self):
        W, H = 80, 40
        w = for_pair(10, 20, 50, 20, W, H, margin=8)
        assert (w.c0, w.r0, w.c1, w.r1) == (2, 12, 58, 28)
        g = grow(w, W, H, 2.0)
        assert g.c0 <= w.c0 and g.r0 <= w.r0 and g.c1 >= w.c1 and g.r1 >= w.r1
        f = full_board(W, H)
        assert (f.c0, f.r0, f.c1, f.r1) == (0, 0, W - 1, H - 1)

    def test_windowed_long_run_stays_inside_and_bounded(self):
        """VIN proxy: long 2-pad run succeeds; expansions fit the window."""
        grid = _grid(200, 80)
        router, _ = _router(grid)
        start, goal = (5, 40, 0), (194, 40, 0)
        full = router._a_star(start, goal)
        assert full.success
        window = for_pair(*start[:2], *goal[:2], grid.width_cells,
                          grid.height_cells, margin=16)
        capped = router._a_star(start, goal, window=window)
        assert capped.success
        _valid_chain(capped.path, start, goal)
        for c, r, _ in capped.path:
            assert window.c0 <= c <= window.c1 and window.r0 <= r <= window.r1
        cells = (window.c1 - window.c0 + 1) * (window.r1 - window.r0 + 1)
        assert capped.expansions <= 2 * cells
        assert capped.expansions <= full.expansions

    def test_window_grows_around_wall(self):
        """Wall seals the initial window; growth finds the gap, route succeeds."""
        grid = _grid(80, 40)
        cost = np.zeros((2, 40, 80), dtype=np.int16)
        cost[:, :, 30] = 100
        cost[:, 38, 30] = 0  # gap far below the terminals' row
        router, _ = _router(grid, cost, window_margin_cells=8, window_retries=3)
        res = router.route([(10, 20, 0), (50, 20, 0)])
        assert res.success
        assert res.path[0] == (10, 20, 0)
        assert res.path[-1] == (50, 20, 0)
        assert (30, 38, 0) in res.path or (30, 38, 1) in res.path


class TestViaMask:
    def test_mask_matches_legacy_scan_exactly(self):
        rng = np.random.default_rng(0)
        cost = (rng.random((2, 30, 30)) < 0.05).astype(np.int16) * 100
        via_block = np.zeros((30, 30), dtype=bool)
        via_block[5, 5] = True
        tht = np.zeros((30, 30), dtype=bool)
        tht[10, 10] = True
        secondary = np.zeros((2, 30, 30), dtype=np.int16)
        secondary[0, 12, 12] = 10000
        grid = _grid(30, 30)
        router, _ = _router(grid, cost, tht_via_mask=tht,
                            via_block_mask=via_block, via_clearance_cells=3)
        mask = build_via_ok_mask(cost, via_block, tht, secondary, 3, 100, 1000)
        assert mask.shape == (30, 30) and mask.dtype == bool
        for r in range(30):
            for c in range(30):
                # Legacy True = blocked; mask True = allowed.
                assert router._via_landing_blocked(r, c, secondary) != bool(mask[r, c])

    def test_mask_speed_path_used_on_route(self):
        grid = _grid(60, 40)
        router, _ = _router(grid, via_clearance_cells=5)
        res = router.route([(2, 20, 0), (57, 20, 1)])
        assert res.success
        assert res.expansions > 0


class TestKruskalPairs:
    def test_order_and_chain_helpers(self):
        terms = [(0, 0, 0), (10, 0, 0), (5, 5, 0), (5, 0, 1)]
        pairs = ordered_pairs(terms)
        assert len(pairs) == 6
        dists = [manhattan(terms[i], terms[j]) for i, j in pairs]
        assert dists == sorted(dists)

    def test_kruskal_stops_at_tree_complete(self):
        """5 terminals connect with 4 searches, not 10."""
        terms = [(0, 0, 0), (8, 0, 0), (16, 0, 0), (24, 0, 0), (32, 0, 0)]
        calls = []

        def fake(i, j):
            calls.append((i, j))
            return True, [(terms[i], terms[j])], float(manhattan(terms[i], terms[j]))

        edges, paths, costs, searches, connected = kruskal_connect(terms, fake)
        assert connected
        assert searches == 4 < 5 * 4 // 2
        assert len(edges) == 4
        assert len(paths) == 8 and len(costs) == 8

    def test_kruskal_skips_failed_pair(self):
        terms = [(0, 0, 0), (1, 0, 0), (50, 50, 0)]

        def fake(i, j):
            if {i, j} == {0, 1}:
                return False, [], 0.0
            return True, [], 1.0

        edges, _, _, _, connected = kruskal_connect(terms, fake)
        assert connected
        assert len(edges) == 2

    def test_multipad_route_uses_fewer_searches_than_eager(self):
        grid = _grid(60, 40)
        router, _ = _router(grid)
        terms = [(2, 20, 0), (15, 20, 0), (30, 20, 0), (45, 20, 0), (57, 20, 0)]
        res = router.route(terms)
        assert res.success
        for t in terms:
            assert t in res.path
        # Eager would run 10 pair-searches; Kruskal needs ~4 windowed ones.
        assert res.expansions < 10 * 60  # generous: < one full row-scan per pair


class TestStarBranches:
    """Regression for the O3 3/4-pad miss: a degree-3 hub must keep every
    spoke as its own continuous branch (no teleport merge, no dropped
    terminal)."""

    def test_star_all_terminals_connected_no_teleport(self):
        grid = _grid(60, 40)
        router, _ = _router(grid)
        terms = [(30, 20, 0), (10, 20, 0), (50, 20, 0), (30, 5, 0)]
        res = router.route(terms)
        assert res.success
        # One branch per MST edge.
        assert len(res.branches) == len(terms) - 1
        # Every terminal is covered by the union ...
        for t in terms:
            assert t in res.path
        # ... and every branch is internally continuous (4-neighbour
        # steps or single-cell via hops — never a teleport jump).
        for branch in res.branches:
            assert len(branch) >= 2
            for a, b in zip(branch, branch[1:]):
                dc, dr, dl = abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2])
                assert (dc + dr == 1 and dl == 0) or (dc == 0 and dr == 0 and dl == 1)

    def test_star_branches_share_hub_no_duplicate_copper(self):
        """Spokes meet at shared hub cells instead of dangling."""
        grid = _grid(60, 40)
        router, _ = _router(grid)
        terms = [(30, 20, 0), (10, 20, 0), (50, 20, 0), (30, 5, 0)]
        res = router.route(terms)
        assert res.success
        endpoints = {b[0] for b in res.branches} | {b[-1] for b in res.branches}
        for t in terms:
            assert t in endpoints
        # Union is strictly shorter than naive concatenation (hub shared).
        concat_len = sum(len(b) for b in res.branches)
        assert len(res.path) < concat_len

    def test_two_pad_route_unchanged_single_branch(self):
        grid = _grid(60, 40)
        router, _ = _router(grid)
        start, goal = (2, 20, 0), (57, 20, 0)
        res = router.route([start, goal])
        assert res.success
        assert len(res.branches) == 1
        assert list(res.branches[0]) == res.path
        _valid_chain(res.path, start, goal)

    def test_star_emits_no_teleport_tracks(self):
        """Track emission follows branches: no single segment may jump
        between spokes (the old merged polyline drew one)."""
        import math

        grid = _grid(60, 40)
        router, _ = _router(grid)
        terms = [(30, 20, 0), (10, 20, 0), (50, 20, 0), (30, 5, 0)]
        res = router.route(terms)
        assert res.success
        tracks, _ = routes_to_tracks_vias({"S": res}, grid)
        assert tracks  # all spokes produce copper
        cell = grid.resolution_mm * math.sqrt(2) + 1e-6
        for t in tracks:
            dx = t.end[0] - t.start[0]
            dy = t.end[1] - t.start[1]
            assert math.hypot(dx, dy) <= cell


class TestRipRelay:
    def test_reroute_after_block_differs_but_succeeds(self):
        grid = _grid(60, 40)
        router, cost = _router(grid)
        start, goal = (2, 20, 0), (57, 20, 0)
        first = router.route([start, goal])
        assert first.success
        # Rip up by walling off the middle of the first path (keep pads free).
        mid = first.path[len(first.path) // 2]
        cost[:, mid[1] - 2:mid[1] + 3, mid[0] - 2:mid[0] + 3] = 100
        second = router.route([start, goal])
        assert second.success
        _valid_chain(second.path, start, goal)
        assert second.path != first.path
