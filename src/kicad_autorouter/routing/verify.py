"""Emit gate: verify emitted copper against hard exclusion, exactly.

The fitter's stitching loop emits straight connector stubs without
re-checking them; a stub that skips collapsed vertices can cut straight
across foreign copper the search and smoother both refused (tube111 pin
run: 0.5 mm power tracks through foreign SMD pads). This module checks
the *emitted* geometry — smoothed branch chords plus fitted segs/arcs —
against the hard-exclusion cells with exact segment-vs-cell geometry
(no sampling gaps, no endpoint allowances) and reports violations so
the caller can fail loudly instead of writing DRC-dirty copper.

Pure grid geometry (shapely + numpy); no kicad-cli, no DRC, no gate
coupling: the check is "does copper touch the exclusion", independent
of whatever the search/smoother decided.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from shapely import box as _vbox
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

# Per-mask spatial-index cache. The hard-exclusion mask is stable across
# every chunk/retry of a net's region group, but ``fitted_pieces_violate``
# runs per chunk — rebuilding a ~45k-box STRtree per chunk (scalar
# ``box()`` per cell + index build ≈ seconds) dominated route time
# (single GND net: >30 min). Keyed by mask bytes hash + layer + cell
# geometry; LRU-capped (a full run touches dozens of distinct masks).
_TREE_CACHE: "OrderedDict[tuple, STRtree | None]" = OrderedDict()
_TREE_CACHE_MAX = 16

# Cross-net shared index. Per-net masks in one (board, grid, halo-params,
# confinement) group differ only by own-pad carve-outs, so the un-carved
# base mask is a superset of every net's mask and one STRtree serves the
# whole group: per-net own cells (``own_net_hard_cells``) filter query
# hits. Entries hold the base layer + index + row/col index arrays for
# hit→cell mapping; LRU-capped (each entry is ~10 MB on DCCF-class
# boards: base layer + int32 index arrays + the tree itself).
_SHARED_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()
_SHARED_CACHE_MAX = 8


def share_group_key(model_fingerprint: int, vias_fingerprint: int,
                    grid, clearance_mm: float, track_half_mm: float,
                    extra_margin_mm: float, region_tag) -> tuple:
    """Cache key for one cross-net index group.

    Covers every input of the un-carved base mask: model pads/vias
    (placement commits change footprints; stamping adds vias), grid
    geometry, halo sizing, and confinement identity. Same key ⟺ same
    base mask, so the first net's ``mask | own`` build serves all later
    nets in the group.
    """
    ox, oy = grid.origin_mm
    return (int(model_fingerprint), int(vias_fingerprint),
            tuple(grid.layers), float(ox), float(oy),
            float(grid.resolution_mm), int(grid.height_cells),
            int(grid.width_cells), float(clearance_mm),
            float(track_half_mm), float(extra_margin_mm), region_tag)


def _cell_boxes(blocked_2d: np.ndarray, ox: float, oy: float,
                res: float) -> STRtree:
    """Spatial index of blocked-cell boxes for one layer.

    One vectorized ``box()`` call (not one scalar call per cell), cached
    by mask content: repeat checks against the same mask reuse the index.
    """
    key = (hash(np.asarray(blocked_2d, dtype=bool).tobytes()),
           float(ox), float(oy), float(res))
    try:
        tree = _TREE_CACHE.pop(key)
    except KeyError:
        pass
    else:
        _TREE_CACHE[key] = tree  # re-insert: mark recently used
        return tree
    rows, cols = np.nonzero(np.asarray(blocked_2d, dtype=bool))
    tree = None
    if len(rows):
        r = np.asarray(rows, dtype=np.float64)
        c = np.asarray(cols, dtype=np.float64)
        tree = STRtree(_vbox(ox + c * res, oy + r * res,
                             ox + (c + 1) * res, oy + (r + 1) * res))
    _TREE_CACHE[key] = tree
    while len(_TREE_CACHE) > _TREE_CACHE_MAX:
        _TREE_CACHE.popitem(last=False)
    return tree


def _shared_entry(group_key: tuple, layer: int, base_layer: np.ndarray,
                  ox: float, oy: float, res: float,
                  timings: Optional[dict] = None) -> tuple:
    """Shared ``(entry, built)`` for one group + layer.

    ``base_layer`` is the un-carved base mask (first net's ``mask |
    own``); the STRtree + row/col index arrays are built once per group
    and reused by every later net, whose own cells filter query hits.
    """
    key = (group_key, int(layer), float(ox), float(oy), float(res))
    try:
        entry = _SHARED_CACHE.pop(key)
    except KeyError:
        pass
    else:
        _SHARED_CACHE[key] = entry  # re-insert: mark recently used
        return entry, False
    t0 = time.perf_counter()
    base = np.ascontiguousarray(np.asarray(base_layer, dtype=bool))
    rows, cols = np.nonzero(base)
    tree = None
    if len(rows):
        r = np.asarray(rows, dtype=np.float64)
        c = np.asarray(cols, dtype=np.float64)
        tree = STRtree(_vbox(ox + c * res, oy + r * res,
                             ox + (c + 1) * res, oy + (r + 1) * res))
    entry = {"tree": tree,
             "rows": np.asarray(rows, dtype=np.int32),
             "cols": np.asarray(cols, dtype=np.int32),
             "base": base}
    _SHARED_CACHE[key] = entry
    while len(_SHARED_CACHE) > _SHARED_CACHE_MAX:
        _SHARED_CACHE.popitem(last=False)
    if timings is not None:
        timings["smooth_verify_build_s"] = timings.get(
            "smooth_verify_build_s", 0.0) + (time.perf_counter() - t0)
    return entry, True


def _hit_cells_to_keep(hits, rows: np.ndarray, cols: np.ndarray,
                       own_2d: np.ndarray, ox: float, oy: float,
                       res: float) -> np.ndarray:
    """Boolean keep-mask over query hit indices (drop own-cell hits).

    ``hits`` are STRtree result indices into the shared box arrays;
    ``own_2d`` is the calling net's own-cell layer. Vectorized: clean
    checks (no hits) never reach here.
    """
    hits = np.asarray(hits)
    if hits.size == 0:
        return hits
    try:
        idx = np.asarray(hits, dtype=np.intp)
    except (TypeError, ValueError):
        # Old-shapely geometry results: recover cells from box bounds
        # (boxes are exact cell boxes: box(ox + c*res, oy + r*res, ...)).
        cells = []
        for h in hits:
            try:
                minx, miny, _maxx, _maxy = h.bounds
            except AttributeError:
                continue
            c = int(round((minx - ox) / res))
            r = int(round((miny - oy) / res))
            cells.append((r, c))
        if not cells:
            return np.asarray(hits)[:0]
        r = np.array([rc[0] for rc in cells], dtype=np.intp)
        c = np.array([rc[1] for rc in cells], dtype=np.intp)
        H, W = own_2d.shape
        valid = (r >= 0) & (r < H) & (c >= 0) & (c < W)
        drop = np.zeros(len(cells), dtype=bool)
        drop[valid] = np.asarray(own_2d, dtype=bool)[r[valid], c[valid]]
        return np.asarray(hits)[~drop]
    r = rows[idx]
    c = cols[idx]
    H, W = own_2d.shape
    valid = (r >= 0) & (r < H) & (c >= 0) & (c < W)
    drop = np.zeros(idx.shape, dtype=bool)
    drop[valid] = np.asarray(own_2d, dtype=bool)[r[valid], c[valid]]
    return hits[~drop]


def _flatten_arc(c1: float, r1: float, cm: float, rm: float, c2: float,
                 r2: float) -> List[Tuple[float, float]]:
    """Dense polyline along the fitted arc (grid coords)."""
    from .biarc import sample_circular_arc

    length = abs(c2 - c1) + abs(r2 - r1) + abs(cm - c1) + abs(rm - r1)
    n = max(32, int(length / 0.1) + 1)
    return [(float(x), float(y))
            for x, y in sample_circular_arc((c1, r1), (cm, rm), (c2, r2), n)]


def fitted_pieces_violate(
    branches: Sequence[Sequence[Tuple[float, float, int]]],
    arc_pieces: Sequence[Sequence[tuple]],
    blocked_mask,
    grid,
    timings: Optional[dict] = None,
    share: Optional[tuple] = None,
) -> List[str]:
    """Return human-readable violations for emitted copper in exclusion.

    Args:
        branches: smoothed branch waypoint lists ``[(c, r, layer)]``.
        arc_pieces: per-branch fitted piece tuples (``seg``/``arc``/``via``).
        blocked_mask: ``(n_layers, H, W)`` bool hard-exclusion grid.
        grid: RoutingGrid (origin + resolution for board-mm reporting).
        timings: optional dict accumulating ``smooth_verify_build_s``,
            ``smooth_verify_segs`` and ``shared_fallbacks``.
        share: None for the legacy per-mask index, else
            ``(group_key, own_3d)``: ``group_key`` identifies one
            (board, grid, halo-params, confinement) group whose un-carved
            base mask is a superset of every net's mask, and ``own_3d``
            is the calling net's own-cell ``(n_layers, H, W)`` bool
            (``own_net_hard_cells``). The shared STRtree is built once
            per group (from the first caller's ``mask | own``) and only
            hits on cells the calling mask does not contain
            (``own & ~mask``) are dropped — a cell holding both own and
            foreign copper stays a violation. A per-call subset guard
            falls back to the legacy index if the mask ever escapes
            the shared base.

    Empty list means clean.
    """
    ox, oy = grid.origin_mm
    res = grid.resolution_mm
    if not (res and res > 0):
        return ["unusable grid resolution"]
    n_layers = blocked_mask.shape[0]
    trees = {}
    shared_entries: Dict[int, Optional[dict]] = {}
    problems: List[str] = []

    def tree_for(layer: int):
        if layer not in trees:
            t0 = time.perf_counter() if timings is not None else 0.0
            trees[layer] = (
                _cell_boxes(blocked_mask[layer], ox, oy, res)
                if 0 <= layer < n_layers else None
            )
            if timings is not None:
                timings["smooth_verify_build_s"] = (
                    timings.get("smooth_verify_build_s", 0.0)
                    + (time.perf_counter() - t0))
        return trees[layer]

    def shared_for(layer: int):
        """Shared entry for one layer, or None to use the legacy index."""
        if layer in shared_entries:
            return shared_entries[layer]
        if share is None or not (0 <= layer < n_layers):
            shared_entries[layer] = None
            return None
        group_key, own_3d = share
        if own_3d is None:
            shared_entries[layer] = None
            return None
        own_layer = np.asarray(own_3d[layer], dtype=bool)
        mask_layer = np.asarray(blocked_mask[layer], dtype=bool)
        entry, built = _shared_entry(group_key, layer, mask_layer | own_layer,
                                     ox, oy, res, timings)
        if not built:
            # Subset guard: the shared base must cover this mask; else
            # the un-carved-base invariant broke — fall back to legacy
            # for this layer rather than risk a missed violation.
            if bool(np.any(mask_layer & ~entry["base"])):
                if timings is not None:
                    timings["shared_fallbacks"] = (
                        timings.get("shared_fallbacks", 0) + 1)
                shared_entries[layer] = None
                return None
        shared_entries[layer] = entry
        return entry

    def query_hits(tree, line):
        try:
            return tree.query(line, predicate="intersects")
        except TypeError:
            bare = tree.query(line)
            return [h for h in bare
                    if line.intersects(h if hasattr(h, "intersects") else h)]

    def check_seg(c1: float, r1: float, c2: float, r2: float, layer: int,
                  what: str) -> None:
        if c1 == c2 and r1 == r2:
            return
        if timings is not None:
            timings["smooth_verify_segs"] = (
                timings.get("smooth_verify_segs", 0) + 1)
        x1, y1 = ox + (c1 + 0.5) * res, oy + (r1 + 0.5) * res
        x2, y2 = ox + (c2 + 0.5) * res, oy + (r2 + 0.5) * res
        line = LineString([(x1, y1), (x2, y2)])
        if share is None:
            tree = tree_for(layer)
            if tree is None:
                return
            n = len(query_hits(tree, line))
        else:
            entry = shared_for(layer)
            if entry is None:
                tree = tree_for(layer)
                if tree is None:
                    return
                n = len(query_hits(tree, line))
            else:
                tree = entry["tree"]
                if tree is None:
                    return
                hits = query_hits(tree, line)
                if len(hits) == 0:
                    return
                _group_key, own_3d = share
                # Drop only hits on cells the calling mask does NOT
                # contain: ``own & ~mask``. A cell holding both own and
                # foreign copper stays True in the mask (foreign stamp)
                # and must still trip — filtering on raw own cells
                # would wrongly silence those overlaps.
                mask_2d = np.asarray(blocked_mask[layer], dtype=bool)
                if own_3d is not None:
                    own_2d = np.asarray(own_3d[layer], dtype=bool)
                    if bool(np.any(own_2d)):
                        drop_2d = own_2d & ~mask_2d
                        if bool(np.any(drop_2d)):
                            hits = _hit_cells_to_keep(hits, entry["rows"],
                                                      entry["cols"], drop_2d,
                                                      ox, oy, res)
                n = len(hits)
        if n:
            problems.append(
                f"{what} ({x1:.2f},{y1:.2f})->({x2:.2f},{y2:.2f})@{layer} "
                f"crosses hard exclusion ({n} cells)"
            )

    for bi, branch in enumerate(branches or ()):
        pts = list(branch)
        for i in range(len(pts) - 1):
            c1, r1, l1 = pts[i]
            c2, r2, l2 = pts[i + 1]
            if l1 == l2:
                check_seg(c1, r1, c2, r2, l1, f"branch {bi} chord")

    for bi, pieces in enumerate(arc_pieces or ()):
        for p in pieces or ():
            if not p:
                continue
            kind = p[0]
            if kind == "seg":
                _, c1, r1, c2, r2, layer = p
                check_seg(c1, r1, c2, r2, layer, f"branch {bi} fitted seg")
            elif kind == "arc":
                _, c1, r1, cm, rm, c2, r2, layer = p
                pts = _flatten_arc(c1, r1, cm, rm, c2, r2)
                for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                    before = len(problems)
                    check_seg(ax, ay, bx, by, layer,
                              f"branch {bi} fitted arc")
                    if len(problems) > before:
                        # Repair the message to name the arc, not the sub-seg.
                        problems[-1] = (
                            f"branch {bi} fitted arc "
                            f"({c1:.1f},{r1:.1f})->({c2:.1f},{r2:.1f})@{layer} "
                            f"crosses hard exclusion")
                        break
    return problems


def _emitted_segs(branches, arc_pieces):
    """Yield ``(c1, r1, c2, r2, layer)`` for the same copper the gate checks.

    Same-layer branch chords plus fitted seg pieces plus flattened arcs,
    zero-length segments skipped — mirrors ``fitted_pieces_violate``.
    """
    for branch in branches or ():
        pts = list(branch)
        for i in range(len(pts) - 1):
            c1, r1, l1 = pts[i]
            c2, r2, l2 = pts[i + 1]
            if l1 == l2 and not (c1 == c2 and r1 == r2):
                yield (c1, r1, c2, r2, l1)
    for pieces in arc_pieces or ():
        for p in pieces or ():
            if not p:
                continue
            if p[0] == "seg":
                _, c1, r1, c2, r2, layer = p
                if not (c1 == c2 and r1 == r2):
                    yield (c1, r1, c2, r2, layer)
            elif p[0] == "arc":
                _, c1, r1, cm, rm, c2, r2, layer = p
                pts = _flatten_arc(c1, r1, cm, rm, c2, r2)
                for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                    if not (ax == bx and ay == by):
                        yield (ax, ay, bx, by, layer)


def gate_touch_margin(branches, arc_pieces, grid, model, net_name,
                      track_half_mm: float):
    """Classify an emit-gate trip as real copper touch vs raster graze.

    For each emitted segment (same set ``fitted_pieces_violate`` checks),
    buffers the centerline by ``track_half_mm`` (flat caps) and measures
    the edge distance to the nearest *foreign* copper polygon (other nets'
    pads in true shape, other nets' via annuli) on the same layer. The
    routed net's own copper is excluded — terminals are expected touches.

    Returns ``(is_touch, min_margin_mm)``: ``is_touch`` is True when any
    emitted track touches (or overlaps) foreign copper (margin <= 0);
    otherwise the gate fired on raster dust (guard-ring graze or cell-
    corner clip with copper > track edge away). ``min_margin_mm`` is inf
    when there is no foreign copper on the routed layers.

    Built lazily per call (only gate-tripped chunks pay), so the hot path
    stays untouched.
    """
    # Lazy import: placement owns pad geometry; routing must not pull it
    # in at module load (keeps this leaf module light and cycle-free).
    from ..placement.contact import pad_copper_polygon

    ox, oy = grid.origin_mm
    res = grid.resolution_mm
    if not (res and res > 0):
        return True, 0.0
    layers = list(getattr(grid, "layers", ()) or ())
    layer_to_idx = dict(getattr(grid, "layer_to_idx", None)
                        or {name: i for i, name in enumerate(layers)})
    n_layers = len(layers)

    per_layer: dict = {i: [] for i in range(n_layers)}
    for fp in getattr(model, "footprints", ()):
        fp_layer = getattr(fp, "layer", "")
        for pad in getattr(fp, "pads", ()):
            if getattr(pad, "net_name", None) == net_name:
                continue
            try:
                poly = pad_copper_polygon(pad)
            except Exception:
                continue
            if getattr(pad, "is_through_hole", False):
                for i in range(n_layers):
                    per_layer[i].append(poly)
            elif fp_layer in layer_to_idx:
                per_layer[layer_to_idx[fp_layer]].append(poly)
            # Pads on layers outside the grid never meet emitted copper.
    for via in getattr(model, "vias", ()):
        if getattr(via, "net_name", None) == net_name:
            continue
        try:
            pos = via.position
            poly = Point(float(pos.x_mm), float(pos.y_mm)).buffer(
                float(via.size_mm) / 2.0)
        except Exception:
            continue
        via_layers = getattr(via, "layers", ())
        if via_layers:
            idxs = [layer_to_idx[name] for name in via_layers
                    if name in layer_to_idx]
        else:
            idxs = list(range(n_layers))  # legacy unspanned via: every layer
        for i in idxs:
            per_layer[i].append(poly)

    min_margin = float("inf")
    for c1, r1, c2, r2, layer in _emitted_segs(branches, arc_pieces):
        if not (0 <= layer < n_layers):
            continue
        polys = per_layer.get(layer, ())
        if not polys:
            continue
        x1, y1 = ox + (c1 + 0.5) * res, oy + (r1 + 0.5) * res
        x2, y2 = ox + (c2 + 0.5) * res, oy + (r2 + 0.5) * res
        track = LineString([(x1, y1), (x2, y2)]).buffer(
            float(track_half_mm), cap_style="flat")
        if track.is_empty:
            continue
        for poly in polys:
            try:
                d = float(track.distance(poly))
            except Exception:
                continue
            if d < min_margin:
                min_margin = d
                if min_margin <= 1e-9:
                    return True, 0.0
    if min_margin == float("inf"):
        return False, float("inf")
    return False, min_margin
