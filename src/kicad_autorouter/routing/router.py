"""Lee algorithm with A* heuristic for single-net routing."""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
from scipy import ndimage

from ..board_model import is_ground_net
from .corridor import find_corridor
from .grid import RoutingGrid
from .obstacles import POUR


@dataclass(frozen=True)
class RouteResult:
    """Result of routing a single net."""
    path: List[Tuple[int, int, int]]  # [(col, row, layer_idx)]
    wire_mm: float
    via_count: int
    cost: float
    success: bool
    expansions: int = 0  # A* node expansions spent (0 when trivially resolved)
    # Corridor guidance stats (aggregated across all pair-searches of this net).
    corridor_tried: int = 0   # pairs where initial window failed and corridor was attempted
    corridor_found: int = 0   # pairs where find_corridor returned a mask
    corridor_used: int = 0    # pairs where corridor-restricted search succeeded
    corridor_cells: int = 0   # sum of corridor mask cells across found corridors
    # Per-edge MST branch paths (one terminal-to-terminal A* path each).
    # ``path`` is the deduped union of these (first-seen order) for cell
    # blocking / terminal-coverage checks — NOT a continuous polyline when
    # >1 branch exists. Geometry consumers (smoothing, track emission,
    # corridor reservation) must iterate ``branches`` to avoid teleport
    # segments between spokes of a star.
    branches: Tuple[Tuple[int, int, int], ...] = ()
    # Per-A*-attempt trace (only populated when the router runs with
    # debug=True): one dict per attempt with pair, window kind/size,
    # expansions, success, and seconds. Timings live in return values so
    # benchmarks can record them without log scraping.
    debug_attempts: Tuple[dict, ...] = ()


@dataclass
class CostMap:
    """Routing cost parameters.

    ``via_cost_mm`` is resolution-independent (mm wire equivalent); the
    router scales it to cells via the grid resolution at construction.
    All other penalties are per-cell entry costs on top of ``base_cost``.
    """
    base_cost: int = 1              # per cell (wire length)
    via_cost_mm: float = 5.0        # per via transition (mm wire equivalent)
    courtyard_penalty: int = 5      # additional cost for COURTYARD cells
    pour_penalty: int = 10          # additional cost for POUR cells (cross if needed)
    high_cost_penalty: int = 20     # additional cost for HIGH_COST cells
    edge_keepout_penalty: int = 40  # additional cost for EDGE_KEEPOUT cells
    blocked_threshold: int = 100    # cells >= this are impassable
    # Weighted-A* greediness: f = g + weight * h. Old default 200 made
    # penalties invisible (200/cell gradient vs +20/+40 penalties); 3 keeps
    # the search penalty-respecting while still guided.
    heuristic_weight: float = 3.0

    def via_cost_cells(self, resolution_mm: float) -> int:
        """Via price in cells for a grid resolution (``int()`` truncation).

        Truncation (not round) preserves the historical values exactly at
        every tested resolution (5.0/0.1=50, 5.0/0.5=10); ``max(1, …)``
        guards absurd coarse grids from a free via.
        """
        if resolution_mm <= 0:
            return 1
        return max(1, int(self.via_cost_mm / resolution_mm))


# Secondary-grid values at/above this are impassable (foreign-THT exclusion
# rings). Soft halos (INNER 500 / OUTER 100) stay below, so they steer
# without ever trapping — see build_fanout_cost.
SECONDARY_BLOCKED = 1000


# Raised when a routing search is cancelled via ``should_cancel``.
# Caught by the pipeline/server job layer; never surfaces as a 500.
class RoutingCancelled(Exception):
    pass


class SingleNetRouter:
    """Route a single net using A* on a 3D grid."""

    def __init__(
        self,
        grid: RoutingGrid,
        cost_grid: np.ndarray,
        cost_map: Optional[CostMap] = None,
        tht_via_mask: Optional[np.ndarray] = None,
        via_block_mask: Optional[np.ndarray] = None,
        max_expansions: int = 2_000_000,
        via_clearance_cells: int = 0,
        window_margin_cells: int = 32,
        window_growth: float = 2.0,
        window_retries: int = 3,
        corridor: bool = True,
        debug: bool = False,
        max_expansions_per_net: int = 0,
    ):
        self.grid = grid
        self.cost_grid = cost_grid
        self.cost_map = cost_map or CostMap()
        self.n_layers = len(grid.layers)
        self.H = grid.height_cells
        self.W = grid.width_cells
        # Via price scaled to this grid (mm → cells); resolution-independent
        # tradeoff between via and wire at every grid density.
        self._via_cost_cells = self.cost_map.via_cost_cells(grid.resolution_mm)
        # Safety net: bound A* node expansions per search so a hard/blocked
        # net fails fast instead of hanging the server thread for minutes.
        self.max_expansions = max_expansions
        # THT via mask: True at (row, col) where through-hole pads allow free layer transition
        self.tht_via_mask = tht_via_mask if tht_via_mask is not None else np.zeros((self.H, self.W), dtype=bool)
        # Via-block mask: True at (row, col) where vias are forbidden (SMD
        # pad solder/fanout areas). THT-free transitions win ties.
        self.via_block_mask = via_block_mask if via_block_mask is not None else np.zeros((self.H, self.W), dtype=bool)
        # Via clearance halo radius (cells): a drilled via's annulus plus
        # clearance to foreign copper. 0 = single-cell check.
        # Set per-net by the pipeline from via OD + clearance + track half.
        self.via_clearance_cells = max(0, int(via_clearance_cells))
        # Search-window policy: initial bbox dilation, growth factor per
        # retry, and retry count before the full-board fallback.
        self.window_margin_cells = max(0, int(window_margin_cells))
        self.window_growth = max(1.0, float(window_growth))
        self.window_retries = max(0, int(window_retries))
        self.corridor_enabled = bool(corridor)
        # Debug trace: per-A*-attempt records collected by route() and
        # returned on RouteResult.debug_attempts. Off by default (zero
        # overhead beyond one branch per attempt when disabled).
        self.debug_enabled = bool(debug)
        # Net-level expansion budget: stop starting new A* searches once
        # the running total reaches this (0 = no cap). Per-search
        # max_expansions still bounds each attempt; this bounds the
        # ~n-1 pair-searches × window-growth retries that make massive
        # nets burn tens of millions of expansions.
        self.max_expansions_per_net = max(0, int(max_expansions_per_net))

    def _budget_spent(self, expansions_total: int) -> bool:
        """True when the net-level expansion budget is exhausted."""
        return (self.max_expansions_per_net > 0
                and expansions_total >= self.max_expansions_per_net)

    def route(
        self,
        terminals: List[Tuple[int, int, int]],  # [(col, row, layer_idx)]
        secondary: Optional[np.ndarray] = None,  # per-net fanout halo, same shape as cost_grid
        progress_cb=None,  # fn(pairs_done, pairs_total, expansions) — best-effort
        should_cancel=None,  # fn() -> bool — checked between pairs + inside A*
        seed_cells: Optional[List[Tuple[int, int, int]]] = None,  # kept same-net copper to join
    ) -> RouteResult:
        """Route a net connecting all terminals.

        Uses MST approximation: pairwise A* distances -> MST -> route edges sequentially.

        ``secondary`` is an optional per-net cost overlay (e.g. fanout halos
        over other nets' pads). Values below SECONDARY_BLOCKED are ADDED to
        entry costs (steer without trapping); values at/above block entry
        and via landings (foreign-THT exclusion rings). The routed net's
        own pads are excluded at build time, so its terminals stay
        reachable.

        ``seed_cells`` is kept same-net copper from prior piecemeal chunks:
        searches may walk through it (not an obstacle) and terminate on it
        (joining the kept tree). Pair unions then distinguish terminal-tree
        joins from kept-tree joins via a synthetic SEED node.

        ``progress_cb``/``should_cancel`` are optional job hooks: cancel is
        honored between pair-searches (raising :class:`RoutingCancelled`)
        and inside each A* run; progress reports completed pairs plus the
        running expansion total.
        """
        if len(terminals) < 2:
            return RouteResult([], 0.0, 0, 0.0, False)
        if should_cancel is not None and should_cancel():
            raise RoutingCancelled("cancelled before first pair")

        # Pairs are searched in cheap Manhattan order (Kruskal) with one
        # via mask per net and a grown search window per pair: typical cost
        # is ~n-1 searches, not n*(n-1)/2 full-board floods.
        n = len(terminals)
        pairs_total = n * (n - 1) // 2
        pairs_done = 0
        expansions_total = 0
        pair_cstats: list[dict] = []
        attempt_log: list[dict] = []

        def _report():
            if progress_cb is not None:
                try:
                    progress_cb(pairs_done, pairs_total, expansions_total)
                except Exception:
                    pass

        via_ok = None
        if self.via_clearance_cells > 0:
            via_ok = build_via_ok_mask(
                self.cost_grid,
                self.via_block_mask,
                self.tht_via_mask,
                secondary,
                self.via_clearance_cells,
                self.cost_map.blocked_threshold,
                SECONDARY_BLOCKED,
            )

        def _search_pair(i: int, j: int, goal_mask: Optional[np.ndarray] = None):
            """Windowed A* with growth + full-board fallback; (ok, path, cost).

            When the first (smallest) window fails, a coarse corridor is
            computed once and the pair is retried restricted to it before
            the grown windows run: tight-spot corridors that unguided A*
            cannot find within budget route in seconds instead of hanging
            at the cap. A missing corridor or a failed restricted search
            falls through to the regular windows, so routability never
            regresses.
            """
            nonlocal pairs_done, expansions_total
            if should_cancel is not None and should_cancel():
                raise RoutingCancelled(f"cancelled before pair {(i, j)}")
            si, sj = terminals[i], terminals[j]
            window = for_pair(
                si[0], si[1], sj[0], sj[1], self.W, self.H,
                self.window_margin_cells,
            )
            windows = [window]
            for _ in range(max(0, self.window_retries - 1)):
                windows.append(grow(windows[-1], self.W, self.H, self.window_growth))
            windows.append(full_board(self.W, self.H))
            result = None

            cstats = {
                "tried": 0, "found": 0, "used": 0, "cells": 0,
                "corridor_time_s": 0.0,
                "window0_exp": 0, "corridor_exp": 0, "subsequent_exp": 0,
            }

            base = expansions_total

            def attempt(search_window, allow_mask=None, weight_override=None, kind="window"):
                """One A* attempt with progress accounting; returns result."""
                nonlocal base, result, pairs_done, expansions_total
                if self._budget_spent(expansions_total):
                    # Net budget spent: fail fast without launching another
                    # capped search. Later pairs/windows fail the same way,
                    # so the net reports ~budget expansions total.
                    result = RouteResult([], 0.0, 0, 0.0, False, 0)
                    pairs_done += 1
                    _report()
                    if self.debug_enabled:
                        attempt_log.append({
                            "pair": [i, j],
                            "kind": kind + ":budget",
                            "window_cells": 0,
                            "expansions": 0,
                            "success": False,
                            "seconds": 0.0,
                        })
                    return result
                search_cb = None
                if progress_cb is not None:
                    def search_cb(e, _base=base):
                        try:
                            progress_cb(pairs_done, pairs_total, _base + e)
                        except Exception:
                            pass
                t_attempt = time.perf_counter()
                if self.max_expansions_per_net > 0:
                    allowance: Optional[int] = max(
                        0, self.max_expansions_per_net - base)
                else:
                    allowance = None
                result = self._a_star(
                    si, sj, secondary,
                    progress_cb=search_cb,
                    should_cancel=should_cancel,
                    window=search_window,
                    via_ok=via_ok,
                    allow_mask=allow_mask,
                    heuristic_weight=weight_override,
                    goal_mask=goal_mask,
                    passable=seed_mask,
                    expansion_allowance=allowance,
                )
                dt_attempt = time.perf_counter() - t_attempt
                expansions_total += result.expansions
                base += result.expansions
                pairs_done += 1
                _report()
                if self.debug_enabled:
                    if search_window is None:
                        window_cells = self.W * self.H
                    else:
                        window_cells = ((search_window.c1 - search_window.c0 + 1)
                                        * (search_window.r1 - search_window.r0 + 1))
                    attempt_log.append({
                        "pair": [i, j],
                        "kind": kind,
                        "window_cells": window_cells,
                        "expansions": result.expansions,
                        "success": result.success,
                        "seconds": round(dt_attempt, 4),
                    })
                return result

            # Attempt 1: initial (smallest) window.
            attempt(window, kind="window0")
            cstats["window0_exp"] = result.expansions
            if result.success:
                pair_cstats.append(cstats)
                return result.success, result.path, result.cost

            # Corridor fallback (only when enabled and window 0 failed).
            # Skipped when the net budget is already spent: the restricted
            # search would instant-fail and find_corridor is wasted work.
            if self.corridor_enabled and not self._budget_spent(expansions_total):
                cstats["tried"] = 1
                if should_cancel is not None and should_cancel():
                    raise RoutingCancelled(f"cancelled before pair {(i, j)}")
                t_corr = time.perf_counter()
                corridor = find_corridor(
                    self.cost_grid, secondary, si, sj,
                    self.cost_map.blocked_threshold, SECONDARY_BLOCKED,
                )
                cstats["corridor_time_s"] = time.perf_counter() - t_corr
                if corridor is not None:
                    cstats["found"] = 1
                    cstats["cells"] = int(corridor.sum())
                    attempt(
                        full_board(self.W, self.H),
                        allow_mask=corridor,
                        weight_override=1.0,
                        kind="corridor",
                    )
                    cstats["corridor_exp"] = result.expansions
                    if result.success:
                        cstats["used"] = 1
                        pair_cstats.append(cstats)
                        return result.success, result.path, result.cost

            # Attempts 2..N: grown windows + full-board fallback.
            for wi in range(1, len(windows)):
                attempt(windows[wi],
                        kind="full" if wi == len(windows) - 1 else f"window{wi}")
                if result.success:
                    break
            cstats["subsequent_exp"] = result.expansions
            pair_cstats.append(cstats)
            return result.success, result.path, result.cost

        # Piecemeal seed: kept same-net copper from prior chunks. The
        # seed is walkable (passed to A* as ``passable``) and a valid goal,
        # so later chunks attach to the kept tree; unions distinguish
        # terminal-tree joins from kept-tree joins via a synthetic SEED node.
        SEED = n
        seed_set = set(map(tuple, seed_cells)) if seed_cells else None
        seed_mask = None
        if seed_set:
            seed_mask = np.zeros((self.n_layers, self.H, self.W), dtype=bool)
            for (c, r, l) in seed_set:
                if 0 <= l < self.n_layers and 0 <= r < self.H and 0 <= c < self.W:
                    seed_mask[l, r, c] = True

        # Component state for Steiner connections
        class UnionFindLocal:
            """Disjoint-set with path compression for Kruskal assembly."""
            def __init__(self, n: int):
                self.root = list(range(n))
                self.components = n

            def find(self, x: int) -> int:
                root = self.root
                while root[x] != x:
                    root[x] = root[root[x]]
                    x = root[x]
                return x

            def union(self, a: int, b: int) -> bool:
                ra, rb = self.find(a), self.find(b)
                if ra == rb:
                    return False
                self.root[rb] = ra
                self.components -= 1
                return True

        # Component root -> set of branch cells (for goal masks)
        component_branches: dict[int, set[Tuple[int, int, int]]] = {}

        # Build goal mask from component branches
        def build_goal_mask(comp_root: int) -> Optional[np.ndarray]:
            cells = component_branches.get(comp_root)
            if not cells:
                return None
            mask = np.zeros((self.n_layers, self.H, self.W), dtype=bool)
            for (c, r, l) in cells:
                if 0 <= l < self.n_layers and 0 <= r < self.H and 0 <= c < self.W:
                    mask[l, r, c] = True
            return mask

        # Custom Kruskal loop with Steiner support
        uf = UnionFindLocal(n + (1 if seed_set else 0))
        mst_edges: List[Tuple[int, int]] = []
        path_matrix: Dict[Tuple[int, int], List[Tuple[int, int, int]]] = {}
        cost_matrix: Dict[Tuple[int, int], float] = {}
        searches = 0

        for i, j in ordered_pairs(terminals):
            if uf.components == 1:
                break
            if self._budget_spent(expansions_total):
                break  # budget spent: remaining pairs fail without searches
            if uf.find(i) == uf.find(j):
                continue
            searches += 1

            # Build goal mask from j's component (if it has existing branches)
            # plus kept copper: searches terminate on either, joining the
            # new tree to the kept one wherever they touch.
            comp_j = uf.find(j)
            goal_mask = build_goal_mask(comp_j)
            if seed_mask is not None:
                goal_mask = seed_mask if goal_mask is None else (goal_mask | seed_mask)

            success, path, cost = _search_pair(i, j, goal_mask)
            if success:
                end = tuple(path[-1]) if path else None
                # Fresh root: comp_j may be stale (j's component can merge
                # between pairs, popping its old entry).
                j_cells = component_branches.get(uf.find(j))
                if end is not None and (
                    end == tuple(terminals[j])
                    or (j_cells is not None and end in j_cells)
                ):
                    uf.union(i, j)
                if seed_set is not None and end in seed_set:
                    uf.union(i, SEED)
                mst_edges.append((i, j))
                path_matrix[(i, j)] = path_matrix[(j, i)] = path
                cost_matrix[(i, j)] = cost_matrix[(j, i)] = cost

                # Merge branch cells of every joined component (generic:
                # correct for terminal-terminal, terminal-seed, and
                # terminal-terminal+seed joins alike).
                new_root = uf.find(i)
                merged_cells = set()
                for root in list(component_branches.keys()):
                    if uf.find(root) == new_root:
                        merged_cells.update(component_branches.pop(root))
                merged_cells.update(path)
                component_branches[new_root] = merged_cells

        connected = all(uf.find(i) == uf.find(0) for i in range(n))
        seed_touched = (
            seed_set is not None
            and any(uf.find(i) == uf.find(SEED) for i in range(n))
        )
        extra_branches: List[Tuple[list, float]] = []
        if seed_set is not None and connected and not seed_touched:
            # One trunk search from the new tree to kept copper: start at
            # the terminal nearest kept copper, walk out along own branches
            # (walkable) and terminate on kept copper (goal). Without this,
            # adjacent chunks that route cleanly side-by-side never join.
            def _manhattan(a, b):
                return abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])

            start_t = tuple(min(
                terminals,
                key=lambda t: min(_manhattan(tuple(t), s) for s in seed_set),
            ))
            goal_s = min(seed_set, key=lambda s: _manhattan(start_t, s))
            branch_mask = np.zeros((self.n_layers, self.H, self.W), dtype=bool)
            for cells in component_branches.values():
                for (c, r, l) in cells:
                    if 0 <= l < self.n_layers and 0 <= r < self.H and 0 <= c < self.W:
                        branch_mask[l, r, c] = True
            walkable = branch_mask | seed_mask
            if self.max_expansions_per_net > 0:
                trunk_allowance: Optional[int] = max(
                    0, self.max_expansions_per_net - expansions_total)
            else:
                trunk_allowance = None
            trunk = self._a_star(
                start_t, tuple(goal_s), secondary,
                progress_cb=None,
                should_cancel=should_cancel,
                window=None,
                via_ok=via_ok,
                allow_mask=None,
                heuristic_weight=1.0,
                goal_mask=seed_mask,
                passable=walkable,
                expansion_allowance=trunk_allowance,
            )
            expansions_total += trunk.expansions
            _report()
            if trunk.success and trunk.path:
                extra_branches.append((list(trunk.path), trunk.cost))
                for i in range(n):
                    uf.union(i, SEED)
                seed_touched = True
        if not connected or (seed_set is not None and not seed_touched):
            return RouteResult([], 0.0, 0, 0.0, False, expansions_total,
                               debug_attempts=tuple(attempt_log))
        if not mst_edges and not extra_branches:
            return RouteResult([], 0.0, 0, 0.0, False, expansions_total)

        # Branch model: keep each MST edge path separate.
        branches: List[List[Tuple[int, int, int]]] = []
        total_cost = 0.0
        total_wire = 0.0
        total_vias = 0

        edge_items = (
            [(path_matrix[(i, j)], cost_matrix[(i, j)]) for i, j in mst_edges]
            + extra_branches
        )
        for edge_path, edge_cost in edge_items:
            if not edge_path:
                return RouteResult([], 0.0, 0, 0.0, False, expansions_total)

            branches.append(list(edge_path))

            # Use the A* cost for this edge (includes free via handling)
            total_cost += edge_cost

            # Count wire length and vias for reporting
            for k in range(len(edge_path) - 1):
                c1, r1, l1 = edge_path[k]
                c2, r2, l2 = edge_path[k + 1]
                if l1 != l2:
                    total_vias += 1
                total_wire += 1

        wire_mm = total_wire * self.grid.resolution_mm

        # Deduped union (first-seen order) for cell blocking and
        # terminal-coverage checks. NOT a routable polyline for >1
        # branch — geometry consumers must use ``branches``.
        seen = set()
        union_path: List[Tuple[int, int, int]] = []
        for branch in branches:
            for cell in branch:
                if cell not in seen:
                    seen.add(cell)
                    union_path.append(cell)

        # Aggregate corridor stats across all pair-searches of this net.
        ctried = sum(c["tried"] for c in pair_cstats)
        cfnd = sum(c["found"] for c in pair_cstats)
        cused = sum(c["used"] for c in pair_cstats)
        ccells = sum(c["cells"] for c in pair_cstats)

        return RouteResult(
            path=union_path,
            wire_mm=total_wire,
            via_count=total_vias,
            cost=total_cost,
            success=True,
            expansions=expansions_total,
            debug_attempts=tuple(attempt_log),
            corridor_tried=ctried,
            corridor_found=cfnd,
            corridor_used=cused,
            corridor_cells=ccells,
            branches=tuple(tuple(b) for b in branches),
        )

    def _secondary_blocked(
        self,
        secondary: Optional[np.ndarray],
        row: int,
        col: int,
        layer: int,
    ) -> bool:
        """True when the per-net secondary grid forbids this cell.

        Fail-safe with no goal exemption: the secondary grid excludes the
        routed net's own pads at build time, so a blocked goal would mean
        pathological pad overlap — "no path" beats a short.
        """
        if secondary is None:
            return False
        if not (0 <= row < self.H and 0 <= col < self.W and 0 <= layer < self.n_layers):
            return False
        return int(secondary[layer, row, col]) >= SECONDARY_BLOCKED

    def _secondary_at(
        self,
        secondary: Optional[np.ndarray],
        row: int,
        col: int,
        layer: int,
    ) -> int:
        """Per-net halo value at a cell (0 when no secondary grid)."""
        if secondary is None:
            return 0
        if not (0 <= row < self.H and 0 <= col < self.W and 0 <= layer < self.n_layers):
            return 0
        return int(secondary[layer, row, col])

    def _via_landing_blocked(
        self,
        row: int,
        col: int,
        secondary: Optional[np.ndarray] = None,
    ) -> bool:
        """True when a via halo at (row, col) would violate foreign copper.

        Scans the ``via_clearance_cells`` disc on every layer for
        ``BLOCKED`` cost cells (prior tracks/vias/holes), secondary
        exclusion rings (foreign THT), and via-block solder areas. The
        center cell's own THT drill disc is exempt (free hop reuses the
        barrel); SMD solder areas still forbid even THT-adjacent sites
        unless the center itself is a drill hole.
        """
        radius = self.via_clearance_cells
        if radius <= 0:
            return False
        # Free hop at its own drill hole: the barrel already exists, so the
        # annulus needs no new clearance beyond what the pad rings enforce.
        if self.tht_via_mask[row, col]:
            return False
        r2 = radius * radius
        for l in range(self.n_layers):
            layer_grid = self.cost_grid[l]
            for r in range(max(0, row - radius), min(self.H, row + radius + 1)):
                dr = r - row
                dc_max = int(math.sqrt(max(0, r2 - dr * dr)))
                for c in range(max(0, col - dc_max), min(self.W, col + dc_max + 1)):
                    if layer_grid[r, c] >= self.cost_map.blocked_threshold:
                        return True
                    if secondary is not None and int(secondary[l, r, c]) >= SECONDARY_BLOCKED:
                        return True
                    if self.via_block_mask[r, c]:
                        return True
        return False

    def terminal_escape_counts(
        self,
        terminals: List[Tuple[int, int, int]],
        secondary: Optional[np.ndarray] = None,
        limit: int = 4096,
    ) -> List[int]:
        """Bounded flood-fill reachable-cell count per terminal (debug aid).

        Upper bound on escape room: layer changes are allowed wherever the
        target cell is passable (via rules ignored), 4-neighborhood moves
        must clear both the cost grid and the secondary exclusion rings. A
        tiny count means the terminal is sealed by blocked/exclusion cells
        and its pair-searches will burn the expansion cap. Runs in
        milliseconds (bounded by ``limit`` pops per terminal).
        """
        from collections import deque

        counts = []
        for (sc, sr, sl) in terminals:
            seen = {(sc, sr, sl)}
            queue = deque([(sc, sr, sl)])
            while queue and len(seen) < limit:
                c, r, l = queue.popleft()
                for nc, nr, nl in ((c + 1, r, l), (c - 1, r, l),
                                   (c, r + 1, l), (c, r - 1, l),
                                   (c, r, l - 1), (c, r, l + 1)):
                    if not (0 <= nr < self.H and 0 <= nc < self.W
                            and 0 <= nl < self.n_layers):
                        continue
                    if (nc, nr, nl) in seen:
                        continue
                    if self.cost_grid[nl, nr, nc] >= self.cost_map.blocked_threshold:
                        continue
                    if (secondary is not None
                            and int(secondary[nl, nr, nc]) >= SECONDARY_BLOCKED):
                        continue
                    seen.add((nc, nr, nl))
                    queue.append((nc, nr, nl))
            counts.append(len(seen))
        return counts

    def _a_star(
        self,
        start: Tuple[int, int, int],
        goal: Tuple[int, int, int],
        secondary: Optional[np.ndarray] = None,
        progress_cb=None,  # fn(expansions) — called every 4096 pops, best-effort
        should_cancel=None,  # fn() -> bool — checked every 4096 pops
        window: Optional[Window] = None,  # search clip; None = full board
        via_ok: Optional[np.ndarray] = None,  # precomputed landing mask (H, W)
        allow_mask: Optional[np.ndarray] = None,  # corridor restriction (H, W); None = anywhere
        heuristic_weight: Optional[float] = None,  # override CostMap weight for this search
        goal_mask: Optional[np.ndarray] = None,  # (n_layers, H, W) bool: any True cell is a valid goal
        passable: Optional[np.ndarray] = None,  # (n_layers, H, W) bool: kept same-net copper — walkable + valid goal
        expansion_allowance: Optional[int] = None,  # stop this search after this many pops (net budget remainder; None = no cap)
    ) -> RouteResult:
        """A* search on 3D grid with via transitions.

        If ``goal_mask`` is provided (3D bool array ``n_layers × H × W``), the
        search terminates when ANY cell in the mask is expanded, not just the
        single ``goal`` cell. The path is reconstructed to the actual goal cell
        reached. The heuristic still uses the original ``goal`` for guidance.

        ``passable`` marks kept same-net copper (piecemeal): those cells are
        enterable despite BLOCKED stamps and also terminate the search, so
        later chunks walk out along and attach to earlier copper.
        """
        sc, sr, sl = start
        gc, gr, gl = goal

        # Quick checks
        if (sc, sr, sl) == (gc, gr, gl):
            return RouteResult([start], 0.0, 0, 0.0, True)

        # Allow start/goal on blocked cells (e.g., existing pads/tracks)
        # Only check bounds, not blocked status for terminals
        if not (0 <= sr < self.H and 0 <= sc < self.W and 0 <= sl < self.n_layers):
            return RouteResult([], 0.0, 0, 0.0, False)
        if not (0 <= gr < self.H and 0 <= gc < self.W and 0 <= gl < self.n_layers):
            return RouteResult([], 0.0, 0, 0.0, False)

        # Heuristic: Weighted A* (weight from CostMap; must stay small
        # enough that cell penalties still steer the search).
        # Admissible heuristic is Manhattan * base_cost (min step cost = 1).
        # Corridor-restricted searches use weight 1.0: inside a narrow
        # tolled corridor an inflated heuristic thrashes (repeated
        # re-expansion) instead of guiding.
        hw = self.cost_map.heuristic_weight if heuristic_weight is None else heuristic_weight
        def heuristic(c, r, l):
            return (abs(c - gc) + abs(r - gr) + abs(l - gl)) * self.cost_map.base_cost * hw

        # Quick goal_mask check for start cell (passable alone does not
        # trivially succeed: connector searches start on kept copper and
        # must still walk out to a goal).
        if goal_mask is not None and goal_mask[sl, sr, sc]:
            return RouteResult([start], 0.0, 0, 0.0, True)

        # A* with (f, g, col, row, layer, parent)
        # f = g + h
        open_set = []
        import heapq
        start_h = heuristic(sc, sr, sl)
        heapq.heappush(open_set, (start_h, 0, sc, sr, sl, None))

        # g_score array clipped to the window: [layer, row - r0, col - c0].
        # Small windows shrink both the allocation and the expansion flood.
        INF = 1 << 30
        if window is None:
            c0, r0, c1, r1 = 0, 0, self.W - 1, self.H - 1
        else:
            c0, r0, c1, r1 = window.c0, window.r0, window.c1, window.r1
        g_score = np.full(
            (self.n_layers, r1 - r0 + 1, c1 - c0 + 1), INF, dtype=np.int32
        )
        g_score[sl, sr - r0, sc - c0] = 0

        # Parent pointers for path reconstruction
        parent = {}

        expansions = 0
        while open_set:
            f, g, c, r, l, p = heapq.heappop(open_set)
            expansions += 1
            if expansions > self.max_expansions:
                return RouteResult([], 0.0, 0, 0.0, False, expansions)
            if expansion_allowance is not None and expansions >= expansion_allowance:
                return RouteResult([], 0.0, 0, 0.0, False, expansions)
            if expansions % 4096 == 0:
                if should_cancel is not None and should_cancel():
                    raise RoutingCancelled(f"cancelled at {expansions} expansions")
                if progress_cb is not None:
                    try:
                        progress_cb(expansions)
                    except Exception:
                        pass

            # Goal check: if goal_mask provided, any cell in mask is a valid goal
            # (callers OR kept-copper joins into the mask; ``passable`` only
            # controls movement, never termination).
            if goal_mask is not None and goal_mask[l, r, c]:
                # Reconstruct path to this goal cell
                path = [(c, r, l)]
                while p is not None:
                    path.append(p[:3])
                    p = parent.get(p[:3])
                path.reverse()
                return RouteResult(path, 0.0, 0, g, True, expansions)

            if (c, r, l) == (gc, gr, gl):
                # Reconstruct path
                path = [(c, r, l)]
                while p is not None:
                    path.append(p[:3])
                    p = parent.get(p[:3])
                path.reverse()
                return RouteResult(path, 0.0, 0, g, True, expansions)

            if g > g_score[l, r - r0, c - c0]:
                continue

            # 4 neighbors on same layer
            for dc, dr in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
                nc, nr = c + dc, r + dr
                if not (c0 <= nc <= c1 and r0 <= nr <= r1):
                    continue
                if allow_mask is not None and not allow_mask[nr, nc]:
                    continue
                if self._is_blocked(nr, nc, l) and not (
                        passable is not None and passable[l, nr, nc]):
                    continue
                if self._secondary_blocked(secondary, nr, nc, l) and not (
                        passable is not None and passable[l, nr, nc]):
                    continue
                cell_cost = self._cell_cost(nr, nc, l) + self._secondary_at(secondary, nr, nc, l)
                ng = g + cell_cost
                if ng < g_score[l, nr - r0, nc - c0]:
                    g_score[l, nr - r0, nc - c0] = ng
                    parent[(nc, nr, l)] = (c, r, l)
                    nf = ng + heuristic(nc, nr, l)
                    heapq.heappush(open_set, (nf, ng, nc, nr, l, (c, r, l)))

            # Via transitions (up/down)
            for nl in [l - 1, l + 1]:
                if 0 <= nl < self.n_layers:
                    # Check same cell on other layer
                    if allow_mask is not None and not allow_mask[r, c]:
                        continue
                    if self._is_blocked(r, c, nl) and not (
                            passable is not None and passable[nl, r, c]):
                        continue
                    if self._secondary_blocked(secondary, r, c, nl) and not (
                            passable is not None and passable[nl, r, c]):
                        continue
                    # Landing on kept same-net copper is always legal (the
                    # trunk joins it), so via-block and halo checks are
                    # waived for passable targets only.
                    target_kept = passable is not None and passable[nl, r, c]
                    # Via cost: free at THT pad locations; forbidden inside
                    # SMD pad solder/fanout areas (THT wins ties).
                    if self.tht_via_mask[r, c]:
                        cell_cost = 0
                    elif self.via_block_mask[r, c] and not target_kept:
                        continue
                    elif via_ok is not None:
                        # Precomputed clearance halo (same disc semantics as
                        # _via_landing_blocked, evaluated once per net).
                        if not via_ok[r, c] and not target_kept:
                            continue
                        cell_cost = self._via_cost_cells
                    else:
                        # Fallback for direct _a_star callers without a mask:
                        # scan the halo now (slow, exact).
                        if self._via_landing_blocked(r, c, secondary):
                            continue
                        cell_cost = self._via_cost_cells
                    cell_cost += self._secondary_at(secondary, r, c, nl)
                    ng = g + cell_cost
                    if ng < g_score[nl, r - r0, c - c0]:
                        g_score[nl, r - r0, c - c0] = ng
                        parent[(c, r, nl)] = (c, r, l)
                        nf = ng + heuristic(c, r, nl)
                        heapq.heappush(open_set, (nf, ng, c, r, nl, (c, r, l)))

        return RouteResult([], 0.0, 0, 0.0, False, expansions)

    def _is_blocked(self, row: int, col: int, layer: int) -> bool:
        if not (0 <= row < self.H and 0 <= col < self.W and 0 <= layer < self.n_layers):
            return True
        return self.cost_grid[layer, row, col] >= self.cost_map.blocked_threshold

    def _cell_cost(self, row: int, col: int, layer: int) -> int:
        """Cost to enter this cell (excluding via cost)."""
        cell_val = self.cost_grid[layer, row, col]
        if cell_val >= self.cost_map.blocked_threshold:
            return 1 << 30  # effectively infinite
        base = self.cost_map.base_cost
        if cell_val >= 80:  # EDGE_KEEPOUT
            base += self.cost_map.edge_keepout_penalty
        elif cell_val >= 50:  # HIGH_COST
            base += self.cost_map.high_cost_penalty
        elif cell_val >= POUR:  # POUR (35)
            base += self.cost_map.pour_penalty
        elif cell_val >= 25:  # COURTYARD
            base += self.cost_map.courtyard_penalty
        return base


# ---------------------------------------------------------------------------
# Pair ordering for multi-terminal nets: Kruskal by Manhattan proximity.
#
# Eager MST ran A* on all ``n*(n-1)/2`` terminal pairs, then kept ``n-1``.
# Since rip/re-lay favors speed over bit-identical paths, pairs are visited
# in cheap Manhattan order and unioned on success, stopping once the tree is
# complete. Typical cost: ``~n-1`` searches instead of ``~n^2/2``. A failing
# pair is simply skipped; later pairs bridge the remaining components.

def manhattan(a: Tuple[int, int, int], b: Tuple[int, int, int]) -> int:
    """Lower bound on wire steps between terminals (layers count as steps)."""
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])


def ordered_pairs(terminals: List[Tuple[int, int, int]]) -> List[Tuple[int, int]]:
    """All ``(i, j)`` pairs, shortest Manhattan distance first."""
    pairs = [
        (manhattan(terminals[i], terminals[j]), i, j)
        for i in range(len(terminals))
        for j in range(i + 1, len(terminals))
    ]
    pairs.sort()
    return [(i, j) for _, i, j in pairs]


class UnionFind:
    """Disjoint-set with path compression for Kruskal assembly."""

    def __init__(self, n: int):
        self.root = list(range(n))
        self.components = n

    def find(self, x: int) -> int:
        root = self.root
        while root[x] != x:
            root[x] = root[root[x]]
            x = root[x]
        return x

    def union(self, a: int, b: int) -> bool:
        """Merge sets; True when this connected two components."""
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        self.root[rb] = ra
        self.components -= 1
        return True


def kruskal_connect(
    terminals: List[Tuple[int, int, int]],
    search_fn: Callable[[int, int], Tuple[bool, object, float]],
) -> Tuple[List[Tuple[int, int]], Dict[Tuple[int, int], object], Dict[Tuple[int, int], float], int, bool]:
    """Search pairs in Manhattan order until connected (or pairs exhaust).

    ``search_fn(i, j)`` returns ``(success, path, cost)``. Returns
    ``(edges, paths, costs, searches, connected)`` where ``paths``/``costs``
    are keyed both ``(i, j)`` and ``(j, i)`` for drop-in MST use.
    """
    n = len(terminals)
    edges: List[Tuple[int, int]] = []
    paths: Dict[Tuple[int, int], object] = {}
    costs: Dict[Tuple[int, int], float] = {}
    if n < 2:
        return edges, paths, costs, 0, False

    uf = UnionFind(n)
    searches = 0
    for i, j in ordered_pairs(terminals):
        if uf.components == 1:
            break
        if uf.find(i) == uf.find(j):
            continue
        searches += 1
        success, path, cost = search_fn(i, j)
        if success:
            uf.union(i, j)
            edges.append((i, j))
            paths[(i, j)] = paths[(j, i)] = path
            costs[(i, j)] = costs[(j, i)] = cost
    return edges, paths, costs, searches, uf.components == 1


# ---------------------------------------------------------------------------
# Search windows: bound each A* run to a sub-rectangle of the board.
#
# A long 2-pad net (e.g. VIN across 20cm of DCCF) otherwise floods the full
# ``H x W x layers`` grid: every pair-search allocates a full-size ``g_score``
# array and may expand far from the straight-line corridor. A window clips
# both the allocation and the expansion to ``bbox(start, goal) + margin``,
# grown on failure with a full-board fallback so routability never regresses.

@dataclass(frozen=True)
class Window:
    """Inclusive board-cell rectangle ``[c0..c1] x [r0..r1]``."""

    c0: int
    r0: int
    c1: int
    r1: int


def for_pair(
    sc: int,
    sr: int,
    gc: int,
    gr: int,
    W: int,
    H: int,
    margin: int,
) -> Window:
    """Bounding box of both terminals dilated by ``margin`` and clipped."""
    m = max(0, int(margin))
    return Window(
        c0=max(0, min(sc, gc) - m),
        r0=max(0, min(sr, gr) - m),
        c1=min(W - 1, max(sc, gc) + m),
        r1=min(H - 1, max(sr, gr) + m),
    )


def grow(window: Window, W: int, H: int, factor: float = 2.0) -> Window:
    """Expand about the center by ``factor`` (each side), clipped to board."""
    f = max(1.0, float(factor))
    cw = (window.c0 + window.c1) / 2.0
    rw = (window.r0 + window.r1) / 2.0
    hw = (window.c1 - window.c0 + 1) * f / 2.0
    hh = (window.r1 - window.r0 + 1) * f / 2.0
    return Window(
        c0=max(0, int(cw - hw)),
        r0=max(0, int(rw - hh)),
        c1=min(W - 1, int(cw + hw)),
        r1=min(H - 1, int(rw + hh)),
    )


def full_board(W: int, H: int) -> Window:
    """Fallback window covering the whole board."""
    return Window(c0=0, r0=0, c1=W - 1, r1=H - 1)


# ---------------------------------------------------------------------------
# Precomputed via-landing mask: hoist the clearance scan out of A*.
#
# ``SingleNetRouter._via_landing_blocked`` scanned a ``radius``-disc on every
# layer for every via candidate — up to twice per expansion, in pure Python.
# The predicate depends only on static-per-net inputs (cost grid, secondary
# exclusion rings, via-block solder areas, clearance radius), so it is
# evaluated once per net here with identical disc semantics, by reverse
# stamping: each obstacle cell forbids all via centers within ``radius``.
#
# Returns ``(H, W)`` bool where True = via landing allowed (before the
# THT-free / via-block checks the search still applies per cell).

def _disc_structure(radius: int) -> np.ndarray:
    """Boolean ``(2*radius+1, 2*radius+1)`` disc (``dr²+dc² <= r²``).

    Matches the historical per-cell ``_stamp`` scan exactly (integer
    arithmetic, no float error): a via center is forbidden when an
    obstacle cell lies within the Euclidean ``radius`` disc.
    """
    y, x = np.ogrid[-radius : radius + 1, -radius : radius + 1]
    return (x * x + y * y <= radius * radius)


def build_via_ok_mask(
    cost_grid: np.ndarray,
    via_block_mask: np.ndarray,
    tht_via_mask: np.ndarray,
    secondary,
    radius: int,
    blocked_threshold: int,
    secondary_blocked: int,
) -> np.ndarray:
    """Boolean ``(H, W)`` mask of allowed via centers."""
    n_layers, H, W = cost_grid.shape
    radius = max(0, int(radius))
    if radius <= 0:
        return np.ones((H, W), dtype=bool)

    # Union of all obstacle sources across layers (2D): a via halo on
    # any layer forbids the (row, col) center on every layer. OR-then-
    # dilate equals dilate-then-OR, so one dilation replaces the old
    # per-obstacle-cell Python ``_stamp`` loop with identical results.
    obstacle = np.zeros((H, W), dtype=bool)
    for l in range(n_layers):
        layer = cost_grid[l]
        sec = secondary[l] if secondary is not None else None
        if sec is not None:
            obstacle |= (layer >= blocked_threshold) | (sec >= secondary_blocked)
        else:
            obstacle |= layer >= blocked_threshold
    obstacle |= np.asarray(via_block_mask, dtype=bool)

    if not obstacle.any():
        forbidden = np.zeros((H, W), dtype=bool)
    else:
        forbidden = ndimage.binary_dilation(
            obstacle, structure=_disc_structure(radius)
        )
        forbidden = np.asarray(forbidden, dtype=bool)

    ok = ~forbidden
    # Drilled holes stay legal hop sites (the barrel already exists).
    ok |= np.asarray(tht_via_mask, dtype=bool)
    return ok


# ---------------------------------------------------------------------------
# Net scheduling and rip-up management for multi-net routing.

def is_rippupable_victim(net_name: Optional[str]) -> bool:
    """True when a net's copper may be evicted to make way for another net.

    GND-only rule: ground-pour copper (GND/GND_PWR/VGND, ...) is cheaply
    replicated elsewhere (zones refill + ``plan_stitches()`` rebuilds
    inter-layer continuity), so it is always fair game. Signal/power
    copper is never evicted except when explicitly requested or
    placement-moved — this is what stops tracks ripping each other out.
    """
    return is_ground_net(net_name)


class RipUpManager:
    """Manages rip-up and retry for failed nets."""

    def __init__(self, max_attempts: int = 3):
        self.max_attempts = max_attempts
        self.attempts: Dict[str, int] = defaultdict(int)
        self.routed_paths: Dict[str, List[Tuple[int, int, int]]] = {}
        self.net_failures: Dict[str, str] = {}

    def can_rip(self, requestor_net: str, victim_net: Optional[str]) -> bool:
        """True when ``requestor_net`` may evict ``victim_net``'s copper.

        Priority rule ("don't rip anyone higher than you"): only GND-family
        victims are evictable, regardless of requestor. Same-net re-route
        (requestor == victim) is always allowed and handled by the
        pipeline's requested-net rip-up, not by this gate.
        """
        if victim_net is None:
            return False
        if requestor_net == victim_net:
            return True
        return is_rippupable_victim(victim_net)

    def record_success(self, net_name: str, path: List[Tuple[int, int, int]]) -> None:
        """Record successful routing."""
        self.routed_paths[net_name] = path
        self.net_failures.pop(net_name, None)

    def record_failure(self, net_name: str, reason: str) -> None:
        """Record failed routing attempt."""
        self.attempts[net_name] += 1
        self.net_failures[net_name] = reason

    def can_retry(self, net_name: str) -> bool:
        return self.attempts[net_name] < self.max_attempts

    def get_failed_nets(self) -> List[str]:
        return [n for n, a in self.attempts.items() if a >= self.max_attempts]