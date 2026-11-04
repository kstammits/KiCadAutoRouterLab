"""Generic disjoint-set (Union-Find) with path compression.

Single home for the Kruskal/steiner component tracking in ``router.py``
and the island grouping in ``stitch.py``. Keys are lazy: unknown keys
join as singletons on first ``find``. Pass ``items`` up front when the
universe is known so ``components`` counts unseen members too (Kruskal
early-exit relies on this).
"""

from __future__ import annotations

from typing import Dict, Generic, Hashable, Iterable, TypeVar

T = TypeVar("T", bound=Hashable)


class DisjointSet(Generic[T]):
    """Disjoint-set with path compression and a live component count."""

    def __init__(self, items: Iterable[T] = ()) -> None:
        self.parent: Dict[T, T] = {}
        self.components = 0
        for item in items:
            self.find(item)

    def find(self, x: T) -> T:
        parent = self.parent
        if x not in parent:
            parent[x] = x
            self.components += 1
            return x
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(self, a: T, b: T) -> bool:
        """Merge sets; True when this connected two components."""
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        self.parent[rb] = ra
        self.components -= 1
        return True
