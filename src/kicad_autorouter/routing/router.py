"""Lee algorithm with A* heuristic for single-net routing."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from .grid import RoutingGrid


@dataclass(frozen=True)
class RouteResult:
    """Result of routing a single net."""
    path: List[Tuple[int, int, int]]  # [(col, row, layer_idx)]
    wire_mm: float
    via_count: int
    cost: float
    success: bool


@dataclass
class CostMap:
    """Routing cost parameters."""
    base_cost: int = 1              # per cell (wire length)
    via_cost: int = 50              # per via transition (5mm at 0.1mm)
    high_cost_penalty: int = 20     # additional cost for HIGH_COST cells
    edge_keepout_penalty: int = 40  # additional cost for EDGE_KEEPOUT cells
    blocked_threshold: int = 100    # cells >= this are impassable
    # Weighted-A* greediness: f = g + weight * h. Old default 200 made
    # penalties invisible (200/cell gradient vs +20/+40 penalties); 3 keeps
    # the search penalty-respecting while still guided.
    heuristic_weight: float = 3.0


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
    ):
        self.grid = grid
        self.cost_grid = cost_grid
        self.cost_map = cost_map or CostMap()
        self.n_layers = len(grid.layers)
        self.H = grid.height_cells
        self.W = grid.width_cells
        # Safety net: bound A* node expansions per search so a hard/blocked
        # net fails fast instead of hanging the server thread for minutes.
        self.max_expansions = max_expansions
        # THT via mask: True at (row, col) where through-hole pads allow free layer transition
        self.tht_via_mask = tht_via_mask if tht_via_mask is not None else np.zeros((self.H, self.W), dtype=bool)
        # Via-block mask: True at (row, col) where vias are forbidden (SMD
        # pad solder/fanout areas). THT-free transitions win ties.
        self.via_block_mask = via_block_mask if via_block_mask is not None else np.zeros((self.H, self.W), dtype=bool)

    def route(
        self,
        terminals: List[Tuple[int, int, int]],  # [(col, row, layer_idx)]
        secondary: Optional[np.ndarray] = None,  # per-net fanout halo, same shape as cost_grid
    ) -> RouteResult:
        """Route a net connecting all terminals.

        Uses MST approximation: pairwise A* distances -> MST -> route edges sequentially.

        ``secondary`` is an optional per-net cost overlay (e.g. fanout halos
        over other nets' pads). It is ADDED to entry costs but never blocks,
        so it steers around foreign copper without trapping terminals.
        """
        if len(terminals) < 2:
            return RouteResult([], 0.0, 0, 0.0, False)

        # Build complete graph with A* distances
        n = len(terminals)
        dist_matrix = np.full((n, n), np.inf, dtype=np.float32)
        path_matrix = [[None] * n for _ in range(n)]
        cost_matrix = np.full((n, n), np.inf, dtype=np.float32)  # Store actual A* costs

        for i in range(n):
            for j in range(i + 1, n):
                result = self._a_star(terminals[i], terminals[j], secondary)
                if result.success:
                    dist_matrix[i, j] = dist_matrix[j, i] = result.cost
                    path_matrix[i][j] = path_matrix[j][i] = result.path
                    cost_matrix[i, j] = cost_matrix[j, i] = result.cost

        # Check connectivity (ignore diagonal)
        if np.any(np.isinf(dist_matrix[np.triu_indices(n, k=1)])):
            return RouteResult([], 0.0, 0, 0.0, False)

        # Prim's MST
        mst_edges = self._prim_mst(dist_matrix)
        if not mst_edges:
            return RouteResult([], 0.0, 0, 0.0, False)

        # Route MST edges sequentially, merging paths
        full_path = []
        total_cost = 0.0
        total_wire = 0.0
        total_vias = 0

        for i, j in mst_edges:
            edge_path = path_matrix[i][j]
            if not edge_path:
                return RouteResult([], 0.0, 0, 0.0, False)
            
            # Merge with full path (avoid duplicating endpoints)
            if not full_path:
                full_path.extend(edge_path)
            else:
                # Check if edge_path[0] matches full_path[-1]
                if edge_path[0] == full_path[-1]:
                    full_path.extend(edge_path[1:])
                elif edge_path[-1] == full_path[-1]:
                    full_path.extend(list(reversed(edge_path))[:-1])
                elif edge_path[0] == full_path[0]:
                    full_path = list(reversed(edge_path))[:-1] + full_path
                elif edge_path[-1] == full_path[0]:
                    full_path = edge_path[:-1] + full_path
                else:
                    # Disconnected - should not happen with MST
                    full_path.extend(edge_path[1:])

            # Use the A* cost for this edge (includes free via handling)
            edge_cost = cost_matrix[i, j]
            total_cost += edge_cost
            
            # Count wire length and vias for reporting
            for k in range(len(edge_path) - 1):
                c1, r1, l1 = edge_path[k]
                c2, r2, l2 = edge_path[k + 1]
                if l1 != l2:
                    total_vias += 1
                total_wire += 1

        wire_mm = total_wire * self.grid.resolution_mm

        return RouteResult(
            path=full_path,
            wire_mm=wire_mm,
            via_count=total_vias,
            cost=total_cost,
            success=True,
        )

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

    def _a_star(
        self,
        start: Tuple[int, int, int],
        goal: Tuple[int, int, int],
        secondary: Optional[np.ndarray] = None,
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
        def heuristic(c, r, l):
            return (abs(c - gc) + abs(r - gr) + abs(l - gl)) * self.cost_map.base_cost * self.cost_map.heuristic_weight

        # A* with (f, g, col, row, layer, parent)
        # f = g + h
        open_set = []
        import heapq
        start_h = heuristic(sc, sr, sl)
        heapq.heappush(open_set, (start_h, 0, sc, sr, sl, None))

        # g_score array: [layer, row, col]
        INF = 1 << 30
        g_score = np.full((self.n_layers, self.H, self.W), INF, dtype=np.int32)
        g_score[sl, sr, sc] = 0

        # Parent pointers for path reconstruction
        parent = {}

        expansions = 0
        while open_set:
            f, g, c, r, l, p = heapq.heappop(open_set)
            expansions += 1
            if expansions > self.max_expansions:
                return RouteResult([], 0.0, 0, 0.0, False)

            if (c, r, l) == (gc, gr, gl):
                # Reconstruct path
                path = [(c, r, l)]
                while p is not None:
                    path.append(p[:3])
                    p = parent.get(p[:3])
                path.reverse()
                return RouteResult(path, 0.0, 0, g, True)

            if g > g_score[l, r, c]:
                continue

            # 4 neighbors on same layer
            for dc, dr in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
                nc, nr = c + dc, r + dr
                if not (0 <= nc < self.W and 0 <= nr < self.H):
                    continue
                if self._is_blocked(nr, nc, l):
                    continue
                cell_cost = self._cell_cost(nr, nc, l) + self._secondary_at(secondary, nr, nc, l)
                ng = g + cell_cost
                if ng < g_score[l, nr, nc]:
                    g_score[l, nr, nc] = ng
                    parent[(nc, nr, l)] = (c, r, l)
                    nf = ng + heuristic(nc, nr, l)
                    heapq.heappush(open_set, (nf, ng, nc, nr, l, (c, r, l)))

            # Via transitions (up/down)
            for nl in [l - 1, l + 1]:
                if 0 <= nl < self.n_layers:
                    # Check same cell on other layer
                    if self._is_blocked(r, c, nl):
                        continue
                    # Via cost: free at THT pad locations; forbidden inside
                    # SMD pad solder/fanout areas (THT wins ties).
                    if self.tht_via_mask[r, c]:
                        cell_cost = 0
                    elif self.via_block_mask[r, c]:
                        continue
                    else:
                        cell_cost = self.cost_map.via_cost
                    cell_cost += self._secondary_at(secondary, r, c, nl)
                    ng = g + cell_cost
                    if ng < g_score[nl, r, c]:
                        g_score[nl, r, c] = ng
                        parent[(c, r, nl)] = (c, r, l)
                        nf = ng + heuristic(c, r, nl)
                        heapq.heappush(open_set, (nf, ng, c, r, nl, (c, r, l)))

        return RouteResult([], 0.0, 0, 0.0, False)

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
        return base

    def _prim_mst(self, dist_matrix: np.ndarray) -> List[Tuple[int, int]]:
        """Prim's algorithm for MST on complete graph."""
        n = dist_matrix.shape[0]
        in_mst = np.zeros(n, dtype=bool)
        min_edge = np.full(n, np.inf, dtype=np.float32)
        parent = np.full(n, -1, dtype=np.int32)
        min_edge[0] = 0

        for _ in range(n):
            # Find vertex with minimum edge weight
            v = -1
            for j in range(n):
                if not in_mst[j] and (v == -1 or min_edge[j] < min_edge[v]):
                    v = j
            if min_edge[v] == np.inf:
                break  # disconnected
            in_mst[v] = True

            if parent[v] != -1:
                yield (parent[v], v)

            # Update edges
            for to in range(n):
                if not in_mst[to] and dist_matrix[v, to] < min_edge[to]:
                    min_edge[to] = dist_matrix[v, to]
                    parent[to] = v