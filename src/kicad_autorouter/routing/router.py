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


class SingleNetRouter:
    """Route a single net using A* on a 3D grid."""

    def __init__(
        self,
        grid: RoutingGrid,
        cost_grid: np.ndarray,
        cost_map: Optional[CostMap] = None,
    ):
        self.grid = grid
        self.cost_grid = cost_grid
        self.cost_map = cost_map or CostMap()
        self.n_layers = len(grid.layers)
        self.H = grid.height_cells
        self.W = grid.width_cells

    def route(
        self,
        terminals: List[Tuple[int, int, int]],  # [(col, row, layer_idx)]
    ) -> RouteResult:
        """Route a net connecting all terminals.

        Uses MST approximation: pairwise A* distances -> MST -> route edges sequentially.
        """
        if len(terminals) < 2:
            return RouteResult([], 0.0, 0, 0.0, False)

        # Build complete graph with A* distances
        n = len(terminals)
        dist_matrix = np.full((n, n), np.inf, dtype=np.float32)
        path_matrix = [[None] * n for _ in range(n)]

        for i in range(n):
            for j in range(i + 1, n):
                result = self._a_star(terminals[i], terminals[j])
                if result.success:
                    dist_matrix[i, j] = dist_matrix[j, i] = result.cost
                    path_matrix[i][j] = path_matrix[j][i] = result.path

        # Check connectivity
        if np.any(np.isinf(dist_matrix)):
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
                    full_path.extend(reversed(edge_path)[:-1])
                elif edge_path[0] == full_path[0]:
                    full_path = list(reversed(edge_path))[:-1] + full_path
                elif edge_path[-1] == full_path[0]:
                    full_path = edge_path[:-1] + full_path
                else:
                    # Disconnected - should not happen with MST
                    full_path.extend(edge_path[1:])

            # Compute cost for this edge
            for k in range(len(edge_path) - 1):
                c1, r1, l1 = edge_path[k]
                c2, r2, l2 = edge_path[k + 1]
                if l1 != l2:
                    total_vias += 1
                total_wire += 1

        total_cost = total_wire + total_vias * (self.cost_map.via_cost / self.cost_map.base_cost)
        wire_mm = total_wire * self.grid.resolution_mm

        return RouteResult(
            path=full_path,
            wire_mm=wire_mm,
            via_count=total_vias,
            cost=total_cost,
            success=True,
        )

    def _a_star(self, start: Tuple[int, int, int], goal: Tuple[int, int, int]) -> RouteResult:
        """A* search on 3D grid with via transitions."""
        sc, sr, sl = start
        gc, gr, gl = goal

        # Quick checks
        if (sc, sr, sl) == (gc, gr, gl):
            return RouteResult([start], 0.0, 0, 0.0, True)

        if self._is_blocked(sr, sc, sl) or self._is_blocked(gr, gc, gl):
            return RouteResult([], 0.0, 0, 0.0, False)

        # Heuristic: Manhattan distance * base_cost
        def heuristic(c, r, l):
            return (abs(c - gc) + abs(r - gr) + abs(l - gl)) * self.cost_map.base_cost

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

        while open_set:
            f, g, c, r, l, p = heapq.heappop(open_set)

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
                cell_cost = self._cell_cost(nr, nc, l)
                ng = g + cell_cost
                if ng < g_score[l, nr, nc]:
                    g_score[l, nr, nc] = ng
                    nf = ng + heuristic(nc, nr, l)
                    heapq.heappush(open_set, (nf, ng, nc, nr, l, (c, r, l)))

            # Via transitions (up/down)
            for nl in [l - 1, l + 1]:
                if 0 <= nl < self.n_layers:
                    if self._is_blocked(nr, nc, nl):  # Check same cell on other layer
                        continue
                    # Via cost
                    cell_cost = self.cost_map.via_cost
                    ng = g + cell_cost
                    if ng < g_score[nl, r, c]:
                        g_score[nl, r, c] = ng
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