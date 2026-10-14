"""Lee algorithm with A* heuristic for single-net routing."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from .corridor import find_corridor
from .grid import RoutingGrid
from .obstacles import POUR
from .pair_cost import kruskal_connect
from .search_window import Window, for_pair, full_board, grow
from .via_sites import build_via_ok_mask


@dataclass(frozen=True)
class RouteResult:
    """Result of routing a single net."""
    path: List[Tuple[int, int, int]]  # [(col, row, layer_idx)]
    wire_mm: float
    via_count: int
    cost: float
    success: bool
    expansions: int = 0  # A* node expansions spent (0 when trivially resolved)
    # Per-edge MST branch paths (one terminal-to-terminal A* path each).
    # ``path`` is the deduped union of these (first-seen order) for cell
    # blocking / terminal-coverage checks — NOT a continuous polyline when
    # >1 branch exists. Geometry consumers (smoothing, track emission,
    # corridor reservation) must iterate ``branches`` to avoid teleport
    # segments between spokes of a star.
    branches: Tuple[Tuple[int, int, int], ...] = ()


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
        # clearance to foreign copper. 0 = legacy single-cell check.
        # Set per-net by the pipeline from via OD + clearance + track half.
        self.via_clearance_cells = max(0, int(via_clearance_cells))
        # Search-window policy: initial bbox dilation, growth factor per
        # retry, and retry count before the full-board fallback.
        self.window_margin_cells = max(0, int(window_margin_cells))
        self.window_growth = max(1.0, float(window_growth))
        self.window_retries = max(0, int(window_retries))

    def route(
        self,
        terminals: List[Tuple[int, int, int]],  # [(col, row, layer_idx)]
        secondary: Optional[np.ndarray] = None,  # per-net fanout halo, same shape as cost_grid
        progress_cb=None,  # fn(pairs_done, pairs_total, expansions) — best-effort
        should_cancel=None,  # fn() -> bool — checked between pairs + inside A*
    ) -> RouteResult:
        """Route a net connecting all terminals.

        Uses MST approximation: pairwise A* distances -> MST -> route edges sequentially.

        ``secondary`` is an optional per-net cost overlay (e.g. fanout halos
        over other nets' pads). Values below SECONDARY_BLOCKED are ADDED to
        entry costs (steer without trapping); values at/above block entry
        and via landings (foreign-THT exclusion rings). The routed net's
        own pads are excluded at build time, so its terminals stay
        reachable.

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

        def _search_pair(i: int, j: int):
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
            base = expansions_total
            result = None

            def attempt(search_window, allow_mask=None, weight_override=None):
                """One A* attempt with progress accounting; returns result."""
                nonlocal base, result, pairs_done, expansions_total
                search_cb = None
                if progress_cb is not None:
                    def search_cb(e, _base=base):
                        try:
                            progress_cb(pairs_done, pairs_total, _base + e)
                        except Exception:
                            pass
                result = self._a_star(
                    si, sj, secondary,
                    progress_cb=search_cb,
                    should_cancel=should_cancel,
                    window=search_window,
                    via_ok=via_ok,
                    allow_mask=allow_mask,
                    heuristic_weight=weight_override,
                )
                expansions_total += result.expansions
                base += result.expansions
                pairs_done += 1
                _report()
                return result

            for wi, window in enumerate(windows):
                attempt(window)
                if result.success:
                    break
                if wi == 0:
                    if should_cancel is not None and should_cancel():
                        raise RoutingCancelled(f"cancelled before pair {(i, j)}")
                    corridor = find_corridor(
                        self.cost_grid, secondary, si, sj,
                        self.cost_map.blocked_threshold, SECONDARY_BLOCKED,
                    )
                    if corridor is not None:
                        attempt(
                            full_board(self.W, self.H),
                            allow_mask=corridor,
                            weight_override=1.0,
                        )
                        if result.success:
                            break
            return result.success, result.path, result.cost

        mst_edges, path_matrix, cost_matrix, connected = None, {}, {}, False
        mst_edges, path_matrix, cost_matrix, _, connected = kruskal_connect(
            terminals, _search_pair
        )
        if not connected:
            return RouteResult([], 0.0, 0, 0.0, False, expansions_total)
        if not mst_edges:
            return RouteResult([], 0.0, 0, 0.0, False, expansions_total)

        # Branch model: keep each MST edge path separate. A single
        # polyline cannot represent a degree-3+ star without either a
        # teleport jump (the old endpoint-only merge) or wasteful
        # backtracking over the hub. Branches share hub cells; each is
        # individually continuous terminal-to-terminal copper.
        branches: List[List[Tuple[int, int, int]]] = []
        total_cost = 0.0
        total_wire = 0.0
        total_vias = 0

        for i, j in mst_edges:
            edge_path = path_matrix.get((i, j))
            if not edge_path:
                return RouteResult([], 0.0, 0, 0.0, False, expansions_total)

            branches.append(list(edge_path))

            # Use the A* cost for this edge (includes free via handling)
            edge_cost = cost_matrix[(i, j)]
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

        return RouteResult(
            path=union_path,
            wire_mm=wire_mm,
            via_count=total_vias,
            cost=total_cost,
            success=True,
            expansions=expansions_total,
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
    ) -> RouteResult:
        """A* search on 3D grid with via transitions."""
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
            if expansions % 4096 == 0:
                if should_cancel is not None and should_cancel():
                    raise RoutingCancelled(f"cancelled at {expansions} expansions")
                if progress_cb is not None:
                    try:
                        progress_cb(expansions)
                    except Exception:
                        pass

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
                if self._is_blocked(nr, nc, l):
                    continue
                if self._secondary_blocked(secondary, nr, nc, l):
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
                    if self._is_blocked(r, c, nl):
                        continue
                    if self._secondary_blocked(secondary, r, c, nl):
                        continue
                    # Via cost: free at THT pad locations; forbidden inside
                    # SMD pad solder/fanout areas (THT wins ties).
                    if self.tht_via_mask[r, c]:
                        cell_cost = 0
                    elif self.via_block_mask[r, c]:
                        continue
                    elif via_ok is not None:
                        # Precomputed clearance halo (same disc semantics as
                        # _via_landing_blocked, evaluated once per net).
                        if not via_ok[r, c]:
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