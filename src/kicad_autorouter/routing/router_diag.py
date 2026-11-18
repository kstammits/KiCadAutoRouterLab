"""Routing diagnostics (split from router.py)."""
from __future__ import annotations
from typing import Callable, Dict, List, Optional, Tuple
from .dsu import DisjointSet
import numpy as np



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

    uf = DisjointSet(range(n))
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


def diagnose_attempts(
    attempts,
    *,
    max_expansions: int = 0,
    corridor_enabled: bool = True,
    net_budget: int = 0,
    has_seed: bool = False,
) -> dict:
    """Machine-usable summary of a failed search ladder.

    ``attempts`` are the debug records logged by ``route()`` (pair, kind,
    window box, expansions, success). The returned dict names what was
    tried per pair — window boxes, corridor outcome, full-board fallback —
    plus ``hints``: factual one-liners a second try can act on (widen the
    margin, raise a cap, or accept the box is sealed and join differently).
    No policy lives here; this only reports so a retry can differ.
    """
    records = [dict(a) for a in (attempts or ())]
    pair_keys = []
    for a in records:
        p = a.get("pair")
        key = tuple(p) if isinstance(p, (list, tuple)) else p
        if key not in pair_keys:
            pair_keys.append(key)
    ok_pairs = {
        tuple(a["pair"]) for a in records
        if a.get("success") and isinstance(a.get("pair"), (list, tuple))
    }
    failed_pairs = [
        list(k) for k in pair_keys
        if isinstance(k, tuple) and k not in ok_pairs
    ]
    kinds = sorted({str(a.get("kind", "")) for a in records})
    base_kinds = {k.split(":")[0] for k in kinds}
    first_failure = next((a for a in records if not a.get("success")), None)
    trunk = next((a for a in records if a.get("kind") == "trunk"), None)
    w0_failed = any(
        not a.get("success") and a.get("kind") == "window0" for a in records
    )
    capped = [
        a for a in records
        if (not a.get("success") and max_expansions > 0
                and a.get("expansions", 0) >= max_expansions)
    ]
    budget_cut = any(str(a.get("kind", "")).endswith(":budget") for a in records)
    hints = []
    if trunk is not None and not trunk.get("success"):
        hints.append(
            "seed join (trunk) failed: chunk tree and kept copper have no "
            "legal connector in the searched box — retry needs a different "
            "join (stub/relay), not a wider pair window"
        )
    if not failed_pairs and has_seed and trunk is None:
        hints.append(
            "pair searches reached kept copper but the tree never fully "
            "joined (a terminal dangles): retry the seed join (trunk) even "
            "though the local tree is incomplete — wider pair windows "
            "cannot help"
        )
    if failed_pairs and "full" not in base_kinds and not budget_cut:
        hints.append(
            f"full-board window never tried for pairs {failed_pairs}: "
            "retry with larger window_retries or window_margin_cells"
        )
    if capped:
        hints.append(
            f"{len(capped)} attempt(s) hit the per-search cap "
            f"max_expansions={max_expansions}: retry with a larger cap"
        )
    if net_budget > 0 and budget_cut:
        hints.append(
            "net expansion budget exhausted "
            f"(max_expansions_per_net={net_budget}): retry with a larger budget"
        )
    if corridor_enabled and w0_failed and "corridor" not in base_kinds:
        hints.append(
            "corridor fallback never ran after a failed window0: retry with "
            "corridor enabled and budget unspent"
        )
    if (failed_pairs and "full" in base_kinds and not capped and not budget_cut
            and (trunk is None or trunk.get("success"))):
        hints.append(
            "all windows incl. full board failed within budget: sealed by "
            "obstacles, not by search limits — move copper or join differently"
        )
    return {
        "pairs_tried": [list(k) if isinstance(k, tuple) else k for k in pair_keys],
        "pairs_failed": failed_pairs,
        "kinds_tried": kinds,
        "full_board_tried": "full" in base_kinds,
        "first_failure": first_failure,
        "hints": hints,
    }
