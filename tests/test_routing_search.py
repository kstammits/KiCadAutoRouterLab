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
from kicad_autorouter.routing.router import (
    CostMap,
    SingleNetRouter,
    for_pair,
    full_board,
    grow,
    kruskal_connect,
    manhattan,
    ordered_pairs,
)


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


class TestTrunkGrowth:
    """Trunk-rooted growth for ground nets: supply-outward attach-to-tree
    instead of Kruskal chaining."""

    def _line_terms(self):
        return [(10, 20, 0), (20, 20, 0), (30, 20, 0), (40, 20, 0)]

    def _pairs_of(self, res):
        return [tuple(a["pair"]) for a in res.debug_attempts]

    def test_trunk_attaches_each_terminal_to_root(self):
        grid = _grid()
        router, _ = _router(grid, debug=True)
        terms = self._line_terms()
        res = router.route(terms, trunk_root=terms[0])
        assert res.success
        for t in terms:
            assert t in res.path
        # One attach search per non-root terminal, supply-outward.
        assert self._pairs_of(res) == [(1, 0), (2, 0), (3, 0)]

    def test_trunk_root_absent_falls_back_to_kruskal(self):
        grid = _grid()
        router, _ = _router(grid, debug=True)
        terms = self._line_terms()
        res = router.route(terms, trunk_root=(999, 999, 0))
        assert res.success
        assert self._pairs_of(res) == [(0, 1), (1, 2), (2, 3)]

    def test_trunk_deterministic(self):
        grid = _grid()
        terms = self._line_terms()
        paths = []
        for _ in range(2):
            router, _ = _router(grid, debug=True)
            res = router.route(terms, trunk_root=terms[0])
            assert res.success
            paths.append(res.path)
        assert paths[0] == paths[1]


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
        """Track emission follows branches: every track is covered by
        branch cells (merged collinear spokes are one segment now, but
        no segment may jump between spokes)."""
        import math

        grid = _grid(60, 40)
        router, _ = _router(grid)
        terms = [(30, 20, 0), (10, 20, 0), (50, 20, 0), (30, 5, 0)]
        res = router.route(terms)
        assert res.success
        tracks, _, _ = routes_to_tracks_vias({"S": res}, grid)
        assert tracks  # all spokes produce copper
        cells = {(c, r) for b in res.branches for c, r, _ in b}

        def to_cell(x, y):
            return (int(math.floor(x / grid.resolution_mm - 0.5 + 1e-9)),
                    int(math.floor(y / grid.resolution_mm - 0.5 + 1e-9)))

        for t in tracks:
            length = math.hypot(t.end[0] - t.start[0], t.end[1] - t.start[1])
            steps = max(1, int(math.ceil(length / grid.resolution_mm * 2.0)))
            for i in range(steps + 1):
                x = t.start[0] + (t.end[0] - t.start[0]) * i / steps
                y = t.start[1] + (t.end[1] - t.start[1]) * i / steps
                assert to_cell(x, y) in cells, f"teleport sample {(x, y)} not on any branch"


class TestMergeCollinear:
    """Post-emission merge: chains collapse, spokes survive, copper is kept."""

    def _t(self, a, b, layer="F.Cu", net="S"):
        from kicad_autorouter.routing.output import Track
        return Track(start=a, end=b, width_mm=0.2, layer=layer, net_name=net)

    def test_chain_merges_head_to_tail(self):
        from kicad_autorouter.routing.output import _merge_collinear_tracks
        out = _merge_collinear_tracks(
            [self._t((0, 0), (1, 0)), self._t((1, 0), (2, 0)), self._t((2, 0), (3, 0))])
        assert [(t.start, t.end) for t in out] == [((0, 0), (3.0, 0.0))]

    def test_opposite_spokes_unite_not_drop(self):
        """Shared start, opposite directions: union is one segment, both kept."""
        from kicad_autorouter.routing.output import _merge_collinear_tracks
        out = _merge_collinear_tracks([self._t((0, 0), (1, 0)), self._t((0, 0), (-1, 0))])
        assert len(out) == 1
        assert (out[0].start, out[0].end) == ((-1, 0), (1.0, 0.0))

    def test_tee_stem_and_disjoint_untouched(self):
        from kicad_autorouter.routing.output import _merge_collinear_tracks
        out = _merge_collinear_tracks([self._t((0, 0), (2, 0)), self._t((1, 0), (1, 1))])
        assert len(out) == 2  # T stem joins mid-segment: no shared endpoint
        out = _merge_collinear_tracks([self._t((0, 0), (1, 0)), self._t((2, 0), (3, 0))])
        assert len(out) == 2  # disjoint collinear: no shared endpoint
        out = _merge_collinear_tracks([self._t((0, 0), (1, 0)), self._t((1, 0), (1, 1))])
        assert len(out) == 2  # bent joint: not collinear

    def test_duplicates_collapse_and_layers_split(self):
        from kicad_autorouter.routing.output import _merge_collinear_tracks
        out = _merge_collinear_tracks([self._t((0, 0), (1, 0)), self._t((0, 0), (1, 0))])
        assert len(out) == 1
        out = _merge_collinear_tracks(
            [self._t((0, 0), (1, 0), layer="F.Cu"),
             self._t((1, 0), (2, 0), layer="B.Cu")])
        assert len(out) == 2


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


def _sealed_grid(w=40, h=20, wall_c=20):
    """Two-layer grid split by a full-height wall (no gap anywhere)."""
    from kicad_autorouter.routing.router import diagnose_attempts
    grid = _grid(w, h)
    cost = np.zeros((2, h, w), dtype=np.int16)
    cost[:, :, wall_c] = 100  # blocked_threshold: impassable both layers
    router, _ = _router(grid, cost, debug=True, window_margin_cells=4)
    return grid, router, diagnose_attempts


class TestNeighborJoins:
    def test_pair_stops_at_near_pin_not_far_root(self):
        """Opportunistic termination: pins join neighbors, not the root.

        R40-shaped: N sits 2mm from M and 5mm from the supply root. The
        pair M->root must end on N (nearest goal) instead of trekking
        past it to the root, so total wire stays near the 2+7 minimum.
        """
        grid = _grid(40, 10)
        router, _ = _router(grid, debug=True)
        root = (25, 5, 0)
        res = router.route([(20, 5, 0), (18, 5, 0), root], trunk_root=root)
        assert res.success
        assert res.wire_mm <= 10.0
        cells = {c for b in res.branches for c in b}
        assert (20, 5, 0) in cells and (18, 5, 0) in cells


class TestFailureDiagnostics:
    def test_failed_route_names_its_window(self):
        """A sealed pair logs boxes + pin mm, and the diagnosis says sealed."""
        _, router, diagnose_attempts = _sealed_grid()
        start, goal = (5, 10, 0), (34, 10, 0)
        res = router.route([start, goal])
        assert not res.success
        attempts = list(res.debug_attempts)
        assert attempts, "failed route must keep its attempt trace"
        for a in attempts:
            assert "window_box" in a and "window_box_mm" in a
            assert "pins_mm" in a
        w0 = next(a for a in attempts if a["kind"] == "window0")
        assert w0["window_cells"] < 40 * 20  # initial window, not full board
        assert w0["window_box_mm"][0] < w0["window_box_mm"][2]
        # Pin positions are board mm (resolution 1.0, origin 0,0).
        assert w0["pins_mm"][0][:2] == [5.5, 10.5]
        assert w0["pins_mm"][1][:2] == [34.5, 10.5]
        assert any(a["kind"] == "corridor:none" for a in attempts)
        diag = diagnose_attempts(
            attempts, max_expansions=router.max_expansions,
            corridor_enabled=router.corridor_enabled)
        assert diag["pairs_failed"] == [[0, 1]]
        assert diag["full_board_tried"]
        assert any("sealed by obstacles" in h for h in diag["hints"])

    def test_failed_trunk_names_seed_join(self):
        """Chunk tree forms but kept copper is unreachable: trunk logged."""
        _, router, diagnose_attempts = _sealed_grid()
        res = router.route([(5, 10, 0), (8, 10, 0)],
                           seed_cells=[(30, 10, 0), (31, 10, 0)])
        assert not res.success
        attempts = list(res.debug_attempts)
        trunk = next(a for a in attempts if a["kind"] == "trunk")
        assert trunk["success"] is False
        assert trunk["expansions"] > 0
        assert len(trunk["pins_mm"]) == 2
        diag = diagnose_attempts(
            attempts, max_expansions=router.max_expansions,
            corridor_enabled=router.corridor_enabled)
        assert any("seed join" in h for h in diag["hints"])

    def test_dangling_terminal_rescued_by_seed_join(self):
        """Searches end on kept copper, stranding a terminal: join saves it."""
        grid = _grid(30, 10)
        cost = np.zeros((2, 10, 30), dtype=np.int16)
        cost[:, :, 15] = 100  # wall between the terminals
        router, _ = _router(grid, cost, debug=True)
        start, goal = (5, 5, 0), (25, 5, 0)
        res = router.route([start, goal],
                           seed_cells=[(6, 5, 0), (24, 5, 0)])
        assert res.success
        cells = {c for b in res.branches for c in b}
        assert start in cells and goal in cells
        assert any(c in cells for c in [(6, 5, 0), (24, 5, 0)])
        kinds = [a["kind"] for a in res.debug_attempts]
        assert "trunk" in kinds

    def test_dangling_signature_still_diagnosed(self):
        """The all-ok-but-unjoined trace keeps its name for the log."""
        from kicad_autorouter.routing.router import diagnose_attempts
        attempts = [{"pair": [0, 1], "kind": "window0",
                     "window_cells": 100, "expansions": 2, "success": True,
                     "seconds": 0.0}]
        diag = diagnose_attempts(attempts, has_seed=True)
        assert diag["pairs_failed"] == []
        assert any("dangles" in h for h in diag["hints"])
        assert not any("full-board" in h for h in diag["hints"])

    def test_boxed_in_terminal_still_fails(self):
        """No path at all (not a join problem): sealed hint, no rescue."""
        grid = _grid(30, 10)
        cost = np.zeros((2, 10, 30), dtype=np.int16)
        cost[:, :, 15] = 100
        # Box the first terminal in on both layers (its own cell stays free).
        for dc, dr in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            cost[:, 5 + dr, 5 + dc] = 100
        router, _ = _router(grid, cost, debug=True)
        res = router.route([(5, 5, 0), (25, 5, 0)])
        assert not res.success
        from kicad_autorouter.routing.router import diagnose_attempts
        diag = diagnose_attempts(list(res.debug_attempts),
                                 max_expansions=router.max_expansions,
                                 corridor_enabled=router.corridor_enabled)
        assert diag["pairs_failed"] == [[0, 1]]
        assert any("sealed by obstacles" in h for h in diag["hints"])
        assert not any("dangles" in h for h in diag["hints"])

    def test_neighbor_pins_join_directly_despite_far_seed(self):
        """Phase 1: adjacent pins join each other, not distant kept copper."""
        grid = _grid(40, 10)
        cost = np.zeros((2, 10, 40), dtype=np.int16)
        router, _ = _router(grid, cost, debug=True)
        res = router.route([(5, 5, 0), (8, 5, 0)],
                           seed_cells=[(9, 5, 0), (35, 5, 0)])
        assert res.success
        pair_recs = [a for a in res.debug_attempts
                     if a["pair"] == [0, 1] and a["success"]]
        assert pair_recs and all(a["goal"] == "term" for a in pair_recs)
        assert not any(a.get("goal") == "seed" and a["pair"] == [0, 1]
                       for a in res.debug_attempts)
        assert res.wire_mm <= 8.0  # 3mm pair + short trunk, not a 27mm trek

    def test_walled_pair_falls_back_to_seed(self):
        """Phase 2: pins that cannot join alone still join via kept copper."""
        grid = _grid(30, 10)
        cost = np.zeros((2, 10, 30), dtype=np.int16)
        cost[:, :, 15] = 100
        router, _ = _router(grid, cost, debug=True)
        res = router.route([(5, 5, 0), (25, 5, 0)],
                           seed_cells=[(6, 5, 0), (24, 5, 0)])
        assert res.success
        assert any(a.get("goal") == "seed" and a["success"]
                   for a in res.debug_attempts)
        assert any(a["kind"] == "trunk" and a["success"]
                   for a in res.debug_attempts)

    def test_trunk_records_its_window(self):
        """Windowed joins name their box instead of logging full-board."""
        grid = _grid(30, 10)
        cost = np.zeros((2, 10, 30), dtype=np.int16)
        cost[:, :, 15] = 100
        router, _ = _router(grid, cost, debug=True)
        res = router.route([(5, 5, 0), (25, 5, 0)],
                           seed_cells=[(6, 5, 0), (24, 5, 0)])
        assert res.success
        trunks = [a for a in res.debug_attempts if a["kind"] == "trunk"]
        assert trunks and all(t["success"] for t in trunks)
        assert any(t["window_box"] for t in trunks)

    def test_capped_search_suggests_cap_not_sealed(self):
        from kicad_autorouter.routing.router import diagnose_attempts
        grid = _grid(200, 80)
        router, _ = _router(grid, debug=True, max_expansions=50)
        res = router.route([(5, 40, 0), (194, 40, 0)])
        assert not res.success
        diag = diagnose_attempts(
            list(res.debug_attempts), max_expansions=50,
            corridor_enabled=router.corridor_enabled)
        assert any("per-search cap" in h for h in diag["hints"])
        assert not any("sealed by obstacles" in h for h in diag["hints"])
