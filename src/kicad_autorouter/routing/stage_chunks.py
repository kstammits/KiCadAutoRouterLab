"""Stage A helpers: terminals, chunking/splicing, rip-up (split from route_raw)."""
from __future__ import annotations
import math
import time
from dataclasses import replace
from typing import Collection, Dict, List, Optional, Tuple
import numpy as np
from ..board_model import BoardModel, is_ground_net
from ..io import rip_up_nets
from .grid import RoutingGrid, board_to_grid
from .obstacles import bresenham_cells, stamp_square, BLOCKED
from .router import RouteResult as NetRouteResult
from .params import RoutingParams



def _terminals_for_net(
    model: BoardModel,
    grid: RoutingGrid,
    net_name: str,
    allowed_layers: Optional[Collection[str]] = None,
) -> List[Tuple[int, int, int]]:
    """Grid terminals ``[(col, row, layer_idx)]`` for a net's pads.

    When ``allowed_layers`` is set, pads on other layers are skipped
    (never silently remapped): callers check
    :func:`_disallowed_terminal_layers` first and fail the net with an
    explicit reason.
    """
    terminals = []
    for conn in model.nets.get(net_name, []):
        fp = model.by_ref.get(conn.ref)
        if not fp:
            continue
        pad = next((p for p in fp.pads if p.number == conn.pad_number), None)
        if not pad:
            continue
        if allowed_layers is not None and fp.layer not in allowed_layers:
            continue
        col, row = board_to_grid(grid, pad.position.x_mm, pad.position.y_mm)
        layer_idx = grid.layer_to_idx.get(fp.layer, 0)
        terminals.append((col, row, layer_idx))
    return terminals


# Public alias for consumers outside the package (e.g. UI server).
terminals_for_net = _terminals_for_net


def _group_terminals_by_region(
    model: BoardModel,
    grid: RoutingGrid,
    net_name: str,
    allowed_layers: Optional[Collection[str]] = None,
) -> List[Tuple[int, List[Tuple[int, int, int]]]]:
    """Grid terminals grouped by board region (panel-aware routing).

    Each group routes independently inside its own region — copper must
    never cross the inter-panel gap. Bridge / header mates are assembly
    joints: each side just routes up to its local pads, so no cross-gap
    portal is needed in the router. Single-file boards (no
    ``board_regions``) return one group, preserving prior behavior.
    Pads outside every polygon (region -1) form their own unconstrained
    group.
    """
    grouped: Dict[int, List[Tuple[int, int, int]]] = {}
    for conn in model.nets.get(net_name, []):
        fp = model.by_ref.get(conn.ref)
        if not fp:
            continue
        pad = next((p for p in fp.pads if p.number == conn.pad_number), None)
        if not pad:
            continue
        if allowed_layers is not None and fp.layer not in allowed_layers:
            continue
        col, row = board_to_grid(grid, pad.position.x_mm, pad.position.y_mm)
        layer_idx = grid.layer_to_idx.get(fp.layer, 0)
        if model.board_regions:
            region_idx = model.footprint_region.get(fp.uuid, -1)
        else:
            region_idx = 0
        grouped.setdefault(region_idx, []).append((col, row, layer_idx))
    # Deterministic region order; net (model) order inside each group so a
    # single-region board routes exactly as before. Piecemeal chunking
    # sorts its own copy row-major (adjacent bands).
    return [(region_idx, grouped[region_idx]) for region_idx in sorted(grouped)]


def _branch_paths(result) -> List[List[int]]:
    """Branch geometries for a route (fallback to ``[result.path]``).

    Legacy/foreign ``RouteResult``-likes without ``branches`` (e.g. direct
    ``_a_star`` results) carry a single continuous path; multi-terminal
    routes carry one branch per MST edge.
    """
    branches = getattr(result, "branches", ())
    if branches:
        return [list(b) for b in branches]
    if getattr(result, "path", None):
        return [list(result.path)]
    return []


def _apply_route_to_grid(
    result,
    cost_grid: np.ndarray,
    grid: RoutingGrid,
    half_width_cells: int = 1,
    extra_radius: int = 0,
    via_clearance_cells: int = 0,
) -> None:
    """Mark routed tracks and vias as BLOCKED in the cost grid.

    The corridor covers ``max(half_width_cells - 1, extra_radius)`` around
    the centerline so emitted tracks reserve their true footprint *plus*
    clearance to neighboring copper for subsequent nets. Via landings
    stamp a via-specific halo (via annulus + clearance + track half-width)
    on both layers. Every MST branch is reserved (not just a flattened union),
    so later nets keep clearance to the actual copper. Updates the cost_grid
    in-place.

    Args:
        result: RouteResult containing branch paths [(col, row, layer_idx)]
        cost_grid: (n_layers, H, W) int16 array to update
        grid: RoutingGrid for coordinate transforms
        half_width_cells: corridor half-width in cells (>= 1)
        extra_radius: clearance halo in cells (>= 0) for tracks
        via_clearance_cells: via halo radius in cells (via size/2 + clearance + track half-width)
    """
    if not result.success or not result.path:
        return

    n_layers = cost_grid.shape[0]
    H, W = cost_grid.shape[1], cost_grid.shape[2]
    track_radius = max(max(0, half_width_cells - 1), max(0, extra_radius))
    via_radius = max(1, via_clearance_cells)

    # Arc-fitted results reserve their true curved copper (sampled arcs +
    # stubs) instead of the pre-fillet chords, so later nets keep clearance
    # to what is actually emitted.
    fitted = getattr(result, "arc_pieces", ())
    branches = _branch_paths(result)
    if fitted and len(fitted) == len(branches) and any(fitted):
        for path, branch_pieces in zip(branches, fitted):
            if branch_pieces:
                by_layer: Dict[int, list] = {}
                for p in branch_pieces:
                    if p[0] in ("seg", "arc"):
                        by_layer.setdefault(p[-1], []).append(p)
                for layer_idx, pieces in by_layer.items():
                    if 0 <= layer_idx < n_layers:
                        _stamp_fitted_pieces(cost_grid[layer_idx], pieces,
                                             track_radius)
            else:
                # Arc-less branch: fall back to chord stamping so its
                # straight copper is still reserved.
                for i in range(len(path) - 1):
                    c1, r1, l1 = path[i]
                    c2, r2, l2 = path[i + 1]
                    if l1 == l2:
                        _mark_line_blocked(cost_grid[l1], r1, c1, r2, c2,
                                           track_radius)
        for path in branches:
            for i in range(len(path) - 1):
                c1, r1, l1 = path[i]
                c2, r2, l2 = path[i + 1]
                if l1 != l2:
                    for _l, _r, _c in ((l1, r1, c1), (l2, r2, c2)):
                        if 0 <= _l < n_layers and 0 <= _r < H and 0 <= _c < W:
                            stamp_square(cost_grid[_l], _r, _c, via_radius, BLOCKED)
        return

    for path in branches:
        for i in range(len(path) - 1):
            c1, r1, l1 = path[i]
            c2, r2, l2 = path[i + 1]

            # Mark the segment cells as blocked
            if l1 == l2:
                # Same layer - mark track corridor along the line
                _mark_line_blocked(cost_grid[l1], r1, c1, r2, c2, track_radius)
            else:
                # Layer transition - mark via halo at both layers
                for _l, _r, _c in ((l1, r1, c1), (l2, r2, c2)):
                    if 0 <= _l < n_layers and 0 <= _r < H and 0 <= _c < W:
                        stamp_square(cost_grid[_l], _r, _c, via_radius, BLOCKED)


def _chunk_terminals(
    terminals: List[Tuple[int, int, int]], size: int
) -> List[List[Tuple[int, int, int]]]:
    """Split terminals into disjoint chunks of at most ``size``.

    Later chunks route against earlier chunks' stamped copper, which stays
    walkable and is a valid search goal, so sub-trees join at kept copper
    instead of a shared terminal. A trailing singleton merges into the
    previous chunk (a chunk must connect at least a pair); values < 2
    route whole.
    """
    if size < 2 or len(terminals) <= size:
        return [list(terminals)]
    chunks = [list(terminals[i:i + size]) for i in range(0, len(terminals), size)]
    if len(chunks) > 1 and len(chunks[-1]) < 2:
        chunks[-2].extend(chunks[-1])
        chunks.pop()
    return chunks


def union_paths(branches, coerce_int: bool = False) -> list:
    """First-seen union of branch cells, matching single-flight shape."""
    seen = set()
    union: list = []
    for b in branches:
        for cell in b:
            c = ((int(cell[0]), int(cell[1]), int(cell[2]))
                 if coerce_int else cell)
            if c not in seen:
                seen.add(c)
                union.append(c)
    return union


def _merge_chunk_results(results: List[NetRouteResult]) -> NetRouteResult:
    """Concatenate per-chunk routes into one net result (piecemeal splice).

    Branches concatenate (geometry consumers already iterate branches);
    wire/via/cost/expansions/corridor stats sum. The union path dedupes
    in first-seen order, matching single-flight shape.
    """
    branches: List[Tuple[int, int, int]] = []
    arc_pieces: List[tuple] = []
    for r in results:
        brs = _branch_paths(r)
        branches.extend(brs)
        # Fitted arc chains ride with their branches (1:1, same order);
        # unfitted results pad empty so alignment survives the splice.
        fitted = getattr(r, "arc_pieces", ())
        if fitted and len(fitted) == len(brs):
            arc_pieces.extend(fitted)
        else:
            arc_pieces.extend([() for _ in brs])
    union = union_paths(branches)
    return NetRouteResult(
        path=union,
        wire_mm=sum(r.wire_mm for r in results),
        via_count=sum(r.via_count for r in results),
        cost=sum(r.cost for r in results),
        success=True,
        expansions=sum(r.expansions for r in results),
        corridor_tried=sum(r.corridor_tried for r in results),
        corridor_found=sum(r.corridor_found for r in results),
        corridor_used=sum(r.corridor_used for r in results),
        corridor_cells=sum(r.corridor_cells for r in results),
        branches=tuple(tuple(b) for b in branches),
        arc_pieces=tuple(arc_pieces),
        debug_attempts=tuple(a for r in results for a in r.debug_attempts),
    )


def _branch_cells(result) -> List[Tuple[int, int, int]]:
    """Dense raster cells of every branch (centerline + via landings).

    Smoothed branches are waypoint polylines — sparse points, not copper.
    Rasterizing the same way ``_apply_route_to_grid`` stamps gives the
    actual cells for join targets and connectivity checks.
    """
    cells: List[Tuple[int, int, int]] = []
    for branch in _branch_paths(result):
        if len(branch) == 1:
            cells.append(tuple(branch[0]))
        for i in range(len(branch) - 1):
            c1, r1, l1 = branch[i]
            c2, r2, l2 = branch[i + 1]
            if l1 == l2:
                cells.extend((c, r, l1) for c, r in bresenham_cells(c1, r1, c2, r2))
            else:
                cells.append((c1, r1, l1))
                cells.append((c2, r2, l2))
    return cells


def _flood_cover(
    terminals: List[Tuple[int, int, int]], cells, kind: str, copper: str
) -> Optional[str]:
    """Every terminal mutually reachable through ``cells`` (or a reason).

    Shared flood behind chunk and seeded coverage: 8-neighbourhood steps
    plus layer vias, from the first terminal through walkable cells only.
    Membership alone is not enough — disjoint sub-trees each cover their
    own terminals. 8-connectivity (not 4) because Bresenham waypoint
    chords step diagonally.
    """
    walk = set(cells)
    missing = sum(1 for t in terminals if tuple(t) not in walk)
    if missing:
        return (f"{kind} splice disconnected: {missing} terminal(s) "
                f"outside {copper}")
    seen = {tuple(terminals[0])}
    stack = [tuple(terminals[0])]
    while stack:
        c, r, l = stack.pop()
        for dc in (-1, 0, 1):
            for dr in (-1, 0, 1):
                if dc == 0 and dr == 0:
                    continue
                nb = (c + dc, r + dr, l)
                if nb in walk and nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        for nl in (l - 1, l + 1):
            nb = (c, r, nl)
            if nb in walk and nb not in seen:
                seen.add(nb)
                stack.append(nb)
    unreachable = sum(1 for t in terminals if tuple(t) not in seen)
    if unreachable:
        return (f"{kind} splice disconnected: {unreachable} terminal(s) "
                f"unreachable in {copper}")
    return None


def _check_chunk_coverage(
    terminals: List[Tuple[int, int, int]], merged: NetRouteResult
) -> Optional[str]:
    """Merged copper must connect every terminal (BFS over branch cells)."""
    return _flood_cover(terminals, set(_branch_cells(merged)),
                        "piecemeal", "routed copper")


def _resume_progress(grouped_chunks, group_progress: Dict[int, List[NetRouteResult]]) -> int:
    """Recompute ``done_base`` so a retry continues where it stopped.

    Counts pair-work for chunks already routed in each region group, so the
    progress window reflects prior copper rather than restarting at zero.
    """
    return sum(
        len(c) * (len(c) - 1) // 2
        for ri, chunks in grouped_chunks
        for c in chunks[:len(group_progress.get(ri, []))]
    )


def _rip_up_nets_from_model(model: BoardModel, net_names: List[str]) -> BoardModel:
    """Return a new BoardModel with tracks/vias for specified nets removed.

    This allows the router to find new paths without being blocked by
    existing tracks for the nets being re-routed.
    """
    if not net_names:
        return model

    net_set = set(net_names)
    kept_tracks = tuple(t for t in model.tracks if t.net_name not in net_set)
    kept_arcs = tuple(a for a in model.arcs if a.net_name not in net_set)
    kept_vias = tuple(v for v in model.vias if v.net_name not in net_set)

    return replace(model, tracks=kept_tracks, arcs=kept_arcs, vias=kept_vias)


def _strip_gnd_blockers_from_model(model: BoardModel) -> Tuple[BoardModel, int]:
    """Return (model without GND copper, count removed).

    GND-only eviction: every track/via on a ground net (``is_ground_net``)
    is a blocker candidate — pre-existing zone-pour stitch vias, old GND
    tracks, and prior stitcher bridges. Zones themselves are kept (KiCad
    refills them); ``plan_stitches()`` rebuilds inter-layer continuity
    afterwards. Signal/power copper is never touched here — that is what
    stops tracks ripping each other out. Deliberately ignores
    ``protected_nets``: GND is rebuildable, so protection still means
    "never rip signal/power" (see ``strip_gnd_blockers`` param).
    """
    kept_tracks = tuple(t for t in model.tracks if not is_ground_net(t.net_name))
    kept_arcs = tuple(a for a in model.arcs if not is_ground_net(a.net_name))
    kept_vias = tuple(v for v in model.vias if not is_ground_net(v.net_name))
    removed = (len(model.tracks) - len(kept_tracks)) + (len(model.arcs) - len(kept_arcs)) + (len(model.vias) - len(kept_vias))
    if removed == 0:
        return model, 0
    return replace(model, tracks=kept_tracks, arcs=kept_arcs, vias=kept_vias), removed


def _mark_line_blocked(layer_grid: np.ndarray, r1: int, c1: int, r2: int, c2: int, radius: int = 0) -> None:
    """Bresenham line drawing to mark cells as BLOCKED (plus ``radius`` halo)."""
    H, W = layer_grid.shape

    for c, r in bresenham_cells(c1, r1, c2, r2):
        if 0 <= r < H and 0 <= c < W:
            if radius:
                stamp_square(layer_grid, r, c, radius, BLOCKED)
            else:
                layer_grid[r, c] = BLOCKED

def _stamp_fitted_pieces(layer_grid, pieces, radius: int) -> None:
    """Reserve fitted seg/arc pieces on a cost layer (arc-aware stamping).

    Pieces are grid-float coords; arcs are densely sampled (every ~0.25
    cell) so the stamped corridor follows the true curve instead of the
    pre-fillet chord. Via markers are skipped here — via halos are
    stamped by the branch path scan.
    """
    from ..board_model import sample_circular_arc as _sample_arc
    H, W = layer_grid.shape
    dense: List[Tuple[float, float]] = []
    for p in pieces:
        if p[0] == "seg":
            length = math.hypot(p[3] - p[1], p[4] - p[2])
            n = max(1, int(math.ceil(length / 0.25)))
            for i in range(n + 1):
                t = i / n
                dense.append((p[1] + (p[3] - p[1]) * t, p[2] + (p[4] - p[2]) * t))
        elif p[0] == "arc":
            s, m, e = (p[1], p[2]), (p[3], p[4]), (p[5], p[6])
            chord = math.hypot(e[0] - s[0], e[1] - s[1])
            n = max(4, int(math.ceil(chord / 0.25)) + 1)
            dense.extend(_sample_arc(s, m, e, n))
    for cf, rf in dense:
        c = int(round(cf))
        r = int(round(rf))
        if 0 <= r < H and 0 <= c < W:
            if radius:
                stamp_square(layer_grid, r, c, radius, BLOCKED)
            else:
                layer_grid[r, c] = BLOCKED
