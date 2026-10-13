"""Pair ordering for multi-terminal nets: Kruskal by Manhattan proximity.

Eager MST ran A* on all ``n*(n-1)/2`` terminal pairs, then kept ``n-1``.
Since rip/re-lay favors speed over bit-identical paths, pairs are visited
in cheap Manhattan order and unioned on success, stopping once the tree is
complete. Typical cost: ``~n-1`` searches instead of ``~n^2/2``. A failing
pair is simply skipped; later pairs bridge the remaining components.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Tuple


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
