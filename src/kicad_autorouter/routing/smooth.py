"""Any-angle path smoothing (string-pulling) for routed nets.

The A* router moves 4-directionally, so angled runs come out as staircases:
one grid step per track segment. This module post-processes a successful
route's grid path, skipping intermediate nodes wherever the straight
line-of-sight is walkable. Surviving corners keep their grid coordinates,
so output stays as straight KiCad ``(segment)`` tracks — just fewer,
longer, any-angle ones that pack tighter.

Safety rules:
- Only shortcuts within a single layer run; via (layer-transition) nodes
  are never moved or removed.
- A shortcut is accepted only if every cell under its corridor footprint
  is FREE. Penalized-but-crossable costs (HIGH_COST pad rings,
  EDGE_KEEPOUT) also block shortcuts — the router may wade through them
  deliberately, but a blind shortcut would clip other nets' clearance
  (this showed up as DRC shorts/clearance errors). The only exception is
  the routed net's own pads/terminals, passed via ``soft_mask``.
- Cells within one cell of either shortcut endpoint are exempt, mirroring
  the router's allowance for terminals sitting on blocked pad cells.
"""

from __future__ import annotations

import math
from typing import List, Optional, Set, Tuple

import numpy as np

try:
    from numba import njit
except ImportError:  # pragma: no cover - Numba is a hard dependency.

    def njit(*args, **kwargs):
        """No-op fallback when Numba is unavailable (slow but correct)."""
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]

        def wrap(fn):
            return fn

        return wrap

# Shared dummy for absent optional masks (Numba has no None-arrays: the
# accompanying ``has_*`` flag short-circuits before any indexing).
_EMPTY_BOOL_2D = np.zeros((0, 0), dtype=bool)

# Cost bands (mirrors routing/obstacles.py + router.CostMap).
SOFT_THRESHOLD = 50  # HIGH_COST pad rings / EDGE_KEEPOUT: no shortcut through
                         # COURTYARD (25) is below this — shortcuts allowed


def own_net_soft_mask(model, grid, net_name: str, clearance_mm: float, track_half_mm: float):
    """Boolean ``(n_layers, H, W)`` mask of the net's own pad areas.

    Inside the mask, penalized cells may be crossed by shortcuts (the
    track must escape its own pads). Everywhere else shortcuts require
    FREE cells. BLOCKED cells are impassable regardless of the mask.
    """
    n_layers = len(grid.layers)
    mask = np.zeros((n_layers, grid.height_cells, grid.width_cells), dtype=bool)
    res_mm = grid.resolution_mm
    if res_mm <= 0:
        return mask
    from .grid import board_to_grid

    for fp in model.footprints:
        for pad in fp.pads:
            if not pad.net_name or pad.net_name != net_name:
                continue
            try:
                w, h = float(pad.size_mm[0]), float(pad.size_mm[1])
            except (TypeError, ValueError, IndexError):
                w, h = 0.0, 0.0
            shape = (getattr(pad, "shape", "") or "").lower()
            if shape in ("rect", "roundrect", "chamfered", "trapezoid", "custom"):
                pad_half = math.hypot(w, h) / 2.0
            else:
                pad_half = max(w, h) / 2.0
            radius = max(1, int(math.ceil((pad_half + clearance_mm + track_half_mm) / res_mm)))
            col, row = board_to_grid(grid, pad.position.x_mm, pad.position.y_mm)
            layers = (0, 1) if pad.is_through_hole else (0 if fp.layer == "F.Cu" else 1,)
            for li in layers:
                if li >= n_layers:
                    continue
                r0, r1 = max(0, row - radius), min(grid.height_cells - 1, row + radius)
                c0, c1 = max(0, col - radius), min(grid.width_cells - 1, col + radius)
                mask[li, r0:r1 + 1, c0:c1 + 1] = True
    return mask


@njit(cache=True)
def _footprint_clear_nb(
    cost_layer,
    has_soft: bool,
    soft_layer,
    c_f: float,
    r_f: float,
    radius: int,
    blocked_threshold: int,
    soft_threshold: int,
    has_blocked: bool,
    blocked_layer,
    hr: int,
) -> bool:
    """Compiled corridor-footprint check (see :func:`_footprint_clear`).

    Same scan with the same square-BLOCKED / Euclidean-soft semantics;
    ``None`` masks arrive as ``(0, 0)`` dummies guarded by ``has_*``
    short-circuits (never indexed when False).
    """
    H, W = cost_layer.shape
    c0 = int(math.floor(c_f))
    r0 = int(math.floor(r_f))
    hr2 = hr * hr
    for rr in range(r0 - radius, r0 + radius + 1):
        if rr < 0 or rr >= H:
            return False
        dr = rr - r0
        for cc in range(c0 - radius, c0 + radius + 1):
            if cc < 0 or cc >= W:
                return False
            dc = cc - c0
            # Plain BLOCKED copper keeps the conservative square footprint
            # (see _footprint_clear).
            if int(cost_layer[rr, cc]) >= blocked_threshold:
                return False
            dd = dr * dr + dc * dc
            if has_blocked and dd <= hr2 and bool(blocked_layer[rr, cc]):
                return False
            if dd > hr2:
                continue
            val = int(cost_layer[rr, cc])
            if val >= soft_threshold and (not has_soft or not soft_layer[rr, cc]):
                return False
    return True


def _footprint_clear(
    cost_layer,
    soft_layer,
    c_f: float,
    r_f: float,
    radius: int,
    blocked_threshold: int,
    blocked_layer=None,
    hard_radius: Optional[int] = None,
) -> bool:
    """Check the corridor footprint around a sample point.

    The footprint is a Euclidean disc (``dr²+dc² <= radius²``), not a
    square: square corners overstate diagonal clearance by up to √2 and
    needlessly forbid 45° shortcuts that keep true DRC distance.

    Cells >= ``blocked_threshold`` are always impassable. Penalized cells
    (>= SOFT_THRESHOLD: pad rings, edge keepout) are impassable unless
    covered by ``soft_layer`` (the routed net's own pad areas).
    ``blocked_layer`` is an optional boolean mask of per-net hard exclusion
    (foreign-THT rings from the secondary grid): any True cell in the
    footprint blocks the shortcut with no soft exemption, since the A*
    router itself treats those cells as impassable. The exclusion disc
    already bakes pad copper + clearance + track half-width, so it is
    tested with ``hard_radius`` (track copper half-width only, defaults to
    ``radius``) to avoid counting clearance twice; the cost grid keeps the
    full ``radius``.
    """
    hr = radius if hard_radius is None else hard_radius
    has_soft = soft_layer is not None
    has_blocked = blocked_layer is not None
    return bool(
        _footprint_clear_nb(
            np.asarray(cost_layer),
            has_soft,
            np.asarray(soft_layer) if has_soft else _EMPTY_BOOL_2D,
            float(c_f),
            float(r_f),
            int(radius),
            int(blocked_threshold),
            int(SOFT_THRESHOLD),
            has_blocked,
            np.asarray(blocked_layer) if has_blocked else _EMPTY_BOOL_2D,
            int(hr),
        )
    )


@njit(cache=True)
def _blocked_footprint_hit_nb(blocked_layer, c_f: float, r_f: float, radius: int) -> bool:
    """Compiled exclusion-footprint check (see :func:`_blocked_footprint_hit`)."""
    H, W = blocked_layer.shape
    c0 = int(math.floor(c_f))
    r0 = int(math.floor(r_f))
    r2 = radius * radius
    for rr in range(r0 - radius, r0 + radius + 1):
        if rr < 0 or rr >= H:
            return True
        dr = rr - r0
        for cc in range(c0 - radius, c0 + radius + 1):
            if cc < 0 or cc >= W:
                return True
            if dr * dr + (cc - c0) * (cc - c0) > r2:
                continue
            if bool(blocked_layer[rr, cc]):
                return True
    return False


def _blocked_footprint_hit(blocked_layer, c_f: float, r_f: float, radius: int) -> bool:
    """True when the corridor footprint around a sample touches exclusion.

    Euclidean disc (``dr²+dc² <= radius²``); see :func:`_footprint_clear`.
    """
    return bool(
        _blocked_footprint_hit_nb(
            np.asarray(blocked_layer), float(c_f), float(r_f), int(radius)
        )
    )


@njit(cache=True)
def _line_walkable_nb(
    c1: int,
    r1: int,
    c2: int,
    r2: int,
    cost_layer,
    has_soft: bool,
    soft_layer,
    blocked_threshold: int,
    soft_threshold: int,
    corridor_radius: int,
    has_blocked: bool,
    blocked_layer,
    hr: int,
) -> bool:
    """Compiled any-angle line check (see :func:`_line_walkable`)."""
    dist = math.hypot(float(c2 - c1), float(r2 - r1))
    if dist == 0.0:
        if has_blocked:
            H, W = blocked_layer.shape
            if 0 <= r1 < H and 0 <= c1 < W and bool(blocked_layer[r1, c1]):
                return False
        return True
    steps = int(math.ceil(dist * 2.0))
    if steps < 1:
        steps = 1
    for s in range(steps + 1):
        t = float(s) / float(steps)
        c_f = float(c1) + float(c2 - c1) * t
        r_f = float(r1) + float(r2 - r1) * t
        if has_blocked and not (
            math.hypot(c_f - float(c1), r_f - float(r1)) < 0.5
            or math.hypot(c_f - float(c2), r_f - float(r2)) < 0.5
        ):
            # Hard exclusion (foreign-THT rings) has no endpoint
            # allowance: the routed net's own pads read 0 in this mask,
            # so any footprint hit means clipping foreign copper. The
            # cost grid keeps its own 1-cell allowance below.
            if _blocked_footprint_hit_nb(blocked_layer, c_f, r_f, hr):
                return False
        # Terminal allowance for the cost grid: pads live on blocked
        # cells, so skip within one cell of either endpoint.
        if (
            math.hypot(c_f - float(c1), r_f - float(r1)) < 1.0
            or math.hypot(c_f - float(c2), r_f - float(r2)) < 1.0
        ):
            continue
        if not _footprint_clear_nb(
            cost_layer,
            has_soft,
            soft_layer,
            c_f,
            r_f,
            corridor_radius,
            blocked_threshold,
            soft_threshold,
            has_blocked,
            blocked_layer,
            hr,
        ):
            return False
    return True


def _line_walkable(
    c1: int,
    r1: int,
    c2: int,
    r2: int,
    cost_layer,
    soft_layer,
    blocked_threshold: int,
    corridor_radius: int,
    blocked_layer=None,
    hard_radius: Optional[int] = None,
) -> bool:
    """Check an any-angle straight line between two grid cells.

    Samples at half-cell steps; each sample's corridor footprint must be
    clear, except near the endpoints (pad allowance). ``blocked_layer``
    (foreign-THT exclusion) is enforced at every sample including the
    endpoint allowance: the routed net's own pads are excluded at build
    time, so any exclusion hit means clipping foreign copper.
    ``hard_radius`` is the footprint used against ``blocked_layer`` (track
    copper half-width only — the exclusion already contains clearance);
    the cost grid keeps the full ``corridor_radius``.
    """
    hr = corridor_radius if hard_radius is None else hard_radius
    has_soft = soft_layer is not None
    has_blocked = blocked_layer is not None
    return bool(
        _line_walkable_nb(
            int(c1),
            int(r1),
            int(c2),
            int(r2),
            np.asarray(cost_layer),
            has_soft,
            np.asarray(soft_layer) if has_soft else _EMPTY_BOOL_2D,
            int(blocked_threshold),
            int(SOFT_THRESHOLD),
            int(corridor_radius),
            has_blocked,
            np.asarray(blocked_layer) if has_blocked else _EMPTY_BOOL_2D,
            int(hr),
        )
    )


def _point_clear(
    c: int,
    r: int,
    cost_layer,
    soft_layer,
    blocked_threshold: int,
    corridor_radius: int,
    blocked_layer=None,
    hard_radius: Optional[int] = None,
) -> bool:
    """True when a candidate Steiner waypoint's own footprint is clear."""
    hr = corridor_radius if hard_radius is None else hard_radius
    H, W = cost_layer.shape
    if not (0 <= r < H and 0 <= c < W):
        return False
    if blocked_layer is not None and _blocked_footprint_hit(blocked_layer, float(c), float(r), hr):
        return False
    return _footprint_clear(cost_layer, soft_layer, float(c), float(r), corridor_radius, blocked_threshold, None)


def _greedy_pass(
    run: List[Tuple[int, int, int]],
    cost_layer,
    soft_layer,
    blocked_threshold: int,
    corridor_radius: int,
    blocked_layer=None,
    hard_radius: Optional[int] = None,
    angle_step_rad: float = math.pi / 18,
) -> List[Tuple[int, int, int]]:
    """One greedy furthest-visible pass over a same-layer run."""
    n = len(run)
    if n < 3:
        return list(run)
    out = [run[0]]
    i = 0
    while i < n - 1:
        # Furthest reachable node wins (fewest, longest segments).
        # Among equally-far nodes, prefer angle closest to step_rad multiple.
        j = n - 1
        best_j = None
        best_penalty = float('inf')
        while j > i + 1:
            if _line_walkable(
                run[i][0], run[i][1], run[j][0], run[j][1],
                cost_layer, soft_layer, blocked_threshold, corridor_radius,
                blocked_layer, hard_radius,
            ):
                dist = math.hypot(run[j][0] - run[i][0], run[j][1] - run[i][1])
                if best_j is None or dist > best_dist or (dist == best_dist and _angle_penalty(run[i][0], run[i][1], run[j][0], run[j][1], angle_step_rad) < best_penalty):
                    best_j = j
                    best_dist = dist
                    best_penalty = _angle_penalty(run[i][0], run[i][1], run[j][0], run[j][1], angle_step_rad)
            j -= 1
        if best_j is None:
            best_j = i + 1
        out.append(run[best_j])
        i = best_j
    return out


def _angle_penalty(c1: int, r1: int, c2: int, r2: int, step_rad: float) -> float:
    """Distance of a leg's angle to the nearest step_rad multiple, in radians."""
    ang = math.atan2(r2 - r1, c2 - c1)
    nearest = round(ang / step_rad) * step_rad
    return abs(ang - nearest)


def _steiner_candidates(
    a_c: int, a_r: int, b_c: int, b_r: int,
    cost_layer, soft_layer, blocked_layer,
    corridor_radius: int,
    H: int, W: int,
    hard_radius: Optional[int] = None,
) -> List[Tuple[int, int]]:
    """Outward waypoint candidates to route around a blocking lump.

    Subset-only shortcutting can only pull the string tighter — never push
    it outward. When the router hugs an exclusion disc (e.g. a horizontal
    run detouring around a foreign pad), every tighter chord clips the
    disc, so greedy keeps the whole staircase. The fix is new vertices
    outside the original path: expanded bounding-box corners of the
    blocking cells between A and B (the natural tangent-style 45° waypoints).

    Two offset rings are generated: the track-copper ring (``hard_radius +
    1``, enough to clear pad-clearance rings whose halos already contain
    clearance) and the full-clearance ring (``corridor_radius + 1``, for
    plain copper blockers). Nearer candidates come first so tight spaces
    prefer the smallest outward push that still verifies walkable.
    """
    hr = corridor_radius if hard_radius is None else hard_radius
    pads = [hr + 1, corridor_radius + 1] if corridor_radius != hr else [hr + 1]
    c0, c1 = (a_c, b_c) if a_c <= b_c else (b_c, a_c)
    r0, r1 = (a_r, b_r) if a_r <= b_r else (b_r, a_r)
    c0 = max(0, c0 - 2)
    r0 = max(0, r0 - 2)
    c1 = min(W - 1, c1 + 2)
    r1 = min(H - 1, r1 + 2)
    # Bounding box of blocking cells in the window. Cost cells at/above
    # SOFT_THRESHOLD count (outside the soft exemption they forbid
    # shortcuts just like hard exclusion does).
    omin_c, omin_r, omax_c, omax_r = None, None, None, None
    for rr in range(r0, r1 + 1):
        for cc in range(c0, c1 + 1):
            block = False
            if blocked_layer is not None and bool(blocked_layer[rr, cc]):
                block = True
            else:
                val = int(cost_layer[rr, cc])
                if val >= SOFT_THRESHOLD and (soft_layer is None or not bool(soft_layer[rr, cc])):
                    block = True
            if block:
                if omin_c is None:
                    omin_c, omin_r, omax_c, omax_r = cc, rr, cc, rr
                else:
                    omin_c = min(omin_c, cc)
                    omin_r = min(omin_r, rr)
                    omax_c = max(omax_c, cc)
                    omax_r = max(omax_r, rr)
    if omin_c is None:
        return []
    out = []
    for pad in pads:
        corners = [
            (omin_c - pad, omin_r - pad),
            (omax_c + pad, omin_r - pad),
            (omin_c - pad, omax_r + pad),
            (omax_c + pad, omax_r + pad),
            ((omin_c + omax_c) // 2, omin_r - pad),
            ((omin_c + omax_c) // 2, omax_r + pad),
            (omin_c - pad, (omin_r + omax_r) // 2),
            (omax_c + pad, (omin_r + omax_r) // 2),
        ]
        for cc, rr in corners:
            if 0 <= cc < W and 0 <= rr < H and (cc, rr) not in out:
                out.append((cc, rr))
    return out


def _repair_lumps(
    run: List[Tuple[int, int, int]],
    cost_layer,
    soft_layer,
    blocked_threshold: int,
    corridor_radius: int,
    blocked_layer=None,
    hard_radius: Optional[int] = None,
    angle_step_rad: float = math.pi / 18,
) -> List[Tuple[int, int, int]]:
    """Replace residual stairstep lumps with outward 2–3 leg detours.

    Operates on one greedy-smoothed run: any window [i, j] with >=8 nodes
    whose direct chord is unwalkable is a lump the subset pass cannot fix.
    Tries each bbox-corner Steiner candidate P (A→P→B), scoring
    length + small 45°-alignment bonus so ties prefer KiCad-style angles;
    falls back to the best single candidate pair (A→P→Q→B). Every inserted
    leg is fully walkability-checked with the same conservative corridor,
    so clearance is preserved — vertices only move outward, never into
    foreign copper. New waypoints are grid cells, so output stays straight
    ``(segment)`` tracks.
    """
    nodes = list(run)
    if len(nodes) < 9:
        return nodes
    H, W = cost_layer.shape
    changed = True
    sweeps = 0
    while changed and sweeps < 3:
        changed = False
        sweeps += 1
        i = 0
        while i < len(nodes) - 8:
            # Extend j while the window looks like an un-smoothable lump:
            # many nodes, short straight-line span.
            j = i + 8
            while j + 1 < len(nodes) and (j + 1 - i) <= 64:
                straight = math.hypot(nodes[j + 1][0] - nodes[i][0], nodes[j + 1][1] - nodes[i][1])
                if straight > (j + 1 - i) * 1.5:
                    break  # already sparse — not a lump
                j += 1
            a_c, a_r = nodes[i][0], nodes[i][1]
            b_c, b_r = nodes[j][0], nodes[j][1]
            if _line_walkable(a_c, a_r, b_c, b_r, cost_layer, soft_layer,
                              blocked_threshold, corridor_radius, blocked_layer, hard_radius):
                i += 1
                continue  # greedy should have taken this; move on
            cands = _steiner_candidates(a_c, a_r, b_c, b_r, cost_layer, soft_layer,
                                        blocked_layer, corridor_radius, H, W, hard_radius)
            best = None  # (score, [waypoints])
            for pc, pr in cands:
                if not _point_clear(pc, pr, cost_layer, soft_layer, blocked_threshold,
                                    corridor_radius, blocked_layer, hard_radius):
                    continue
                if not _line_walkable(a_c, a_r, pc, pr, cost_layer, soft_layer,
                                      blocked_threshold, corridor_radius, blocked_layer, hard_radius):
                    continue
                if not _line_walkable(pc, pr, b_c, b_r, cost_layer, soft_layer,
                                      blocked_threshold, corridor_radius, blocked_layer, hard_radius):
                    continue
                length = (math.hypot(pc - a_c, pr - a_r) + math.hypot(b_c - pc, b_r - pr))
                score = length + 0.5 * (_angle_penalty(a_c, a_r, pc, pr, angle_step_rad)
                                        + _angle_penalty(pc, pr, b_c, b_r, angle_step_rad))
                if best is None or score < best[0]:
                    best = (score, [(pc, pr)])
            if best is None and len(cands) >= 2:
                # Two-corner fallback (cap: 6 most central candidates).
                short = cands[:6]
                for ai in range(len(short)):
                    pc, pr = short[ai]
                    if not _point_clear(pc, pr, cost_layer, soft_layer, blocked_threshold,
                                        corridor_radius, blocked_layer, hard_radius):
                        continue
                    if not _line_walkable(a_c, a_r, pc, pr, cost_layer, soft_layer,
                                          blocked_threshold, corridor_radius, blocked_layer, hard_radius):
                        continue
                    for bi in range(len(short)):
                        if bi == ai:
                            continue
                        qc, qr = short[bi]
                        if not _point_clear(qc, qr, cost_layer, soft_layer, blocked_threshold,
                                            corridor_radius, blocked_layer, hard_radius):
                            continue
                        if not _line_walkable(pc, pr, qc, qr, cost_layer, soft_layer,
                                              blocked_threshold, corridor_radius, blocked_layer, hard_radius):
                            continue
                        if not _line_walkable(qc, qr, b_c, b_r, cost_layer, soft_layer,
                                              blocked_threshold, corridor_radius, blocked_layer, hard_radius):
                            continue
                        length = (math.hypot(pc - a_c, pr - a_r)
                                  + math.hypot(qc - pc, qr - pr)
                                  + math.hypot(b_c - qc, b_r - qr))
                        score = length + 0.5 * (_angle_penalty(a_c, a_r, pc, pr, angle_step_rad)
                                                + _angle_penalty(pc, pr, qc, qr, angle_step_rad)
                                                + _angle_penalty(qc, qr, b_c, b_r, angle_step_rad))
                        if best is None or score < best[0]:
                            best = (score, [(pc, pr), (qc, qr)])
            if best is not None:
                layer = nodes[i][2]
                waypoints = [(pc, pr, layer) for pc, pr in best[1]]
                nodes = nodes[:i + 1] + waypoints + nodes[j:]
                changed = True
                # Re-collapse around the insertion; restart scan just after.
                nodes = _greedy_pass(nodes, cost_layer, soft_layer, blocked_threshold,
                                     corridor_radius, blocked_layer, hard_radius, angle_step_rad)
                i += 1
            else:
                i += 1
    return nodes


def _smooth_run(
    run: List[Tuple[int, int, int]],
    cost_layer,
    soft_layer,
    blocked_threshold: int,
    corridor_radius: int,
    blocked_layer=None,
    hard_radius: Optional[int] = None,
    angle_step_rad: float = math.pi / 18,
) -> List[Tuple[int, int, int]]:
    """Iterative bidirectional greedy + outward lump repair.

    Forward-only greedy commits to the furthest-visible node from one end,
    which strands mid-region lumps (the reported stairstep behavior). Running
    greedy forward, then backward on the result, until stable removes the
    start-end bias with the same walkability guarantees. Residual dense
    windows that no subset shortcut can clear go through outward Steiner
    repair (new vertices, same clearance checks).
    """
    n = len(run)
    if n < 3:
        return list(run)
    cur = _greedy_pass(run, cost_layer, soft_layer, blocked_threshold,
                       corridor_radius, blocked_layer, hard_radius, angle_step_rad)
    for _ in range(3):
        rev = list(reversed(cur))
        back = _greedy_pass(rev, cost_layer, soft_layer, blocked_threshold,
                            corridor_radius, blocked_layer, hard_radius, angle_step_rad)
        nxt = list(reversed(back))
        if len(nxt) >= len(cur):
            cur = nxt if len(nxt) < len(cur) else cur
            break
        cur = nxt
    cur = _repair_lumps(cur, cost_layer, soft_layer, blocked_threshold,
                        corridor_radius, blocked_layer, hard_radius, angle_step_rad)
    return cur


def smooth_grid_path(
    path: List[Tuple[int, int, int]],
    cost_grid,
    blocked_threshold: int = 100,
    corridor_radius: int = 0,
    pinned: Optional[Set[Tuple[int, int]]] = None,
    soft_mask=None,
    blocked_mask=None,
    hard_radius: Optional[int] = None,
    angle_step_rad: float = math.pi / 18,
) -> List[Tuple[int, int, int]]:
    """Shortcut a routed grid path within each same-layer run.

    Args:
        path: ``[(col, row, layer_idx)]`` from :class:`SingleNetRouter`.
        cost_grid: ``(n_layers, H, W)`` occupancy grid (pre-route state:
            must NOT yet contain this path's own corridor).
        blocked_threshold: cells >= this are impassable.
        corridor_radius: footprint radius in cells around the shortcut line
            (same convention as ``_apply_route_to_grid``: ``half_width-1``).
        pinned: ``{(col, row)}`` terminals that must survive smoothing.
            MST-joined multi-pad paths pass *through* middle terminals; a
            shortcut skipping one would disconnect that pad. Pinned
            interior nodes force run boundaries.
        soft_mask: optional ``(n_layers, H, W)`` boolean array (see
            :func:`own_net_soft_mask`). Penalized cells are crossable only
            where it is True. When None, every penalized cell blocks.
        blocked_mask: optional ``(n_layers, H, W)`` boolean array of
            per-net hard exclusion (foreign-THT rings, i.e. ``secondary >=
            SECONDARY_BLOCKED``). Shortcuts may never cross True cells;
            the routed net's own pads read False by construction.

    Returns:
        New path with identical endpoints, identical via nodes/layers, all
        pinned nodes, and fewer intermediate nodes. Never returns an empty
        path for non-empty input.
    """
    if not path:
        return []
    if len(path) < 3:
        return list(path)
    n_layers = cost_grid.shape[0]
    pinned = pinned or set()

    # Split into maximal same-layer runs. A layer transition ends the
    # current run and starts a new one, so smoothing can never shortcut
    # across a via (which would move the via position). Concatenating the
    # smoothed runs preserves every via edge exactly: each run keeps its
    # endpoints, and adjacent runs join at the original via nodes.
    # Pinned middle terminals likewise force boundaries (shared node in
    # both runs — same layer, so sharing is safe here).
    runs: List[List[Tuple[int, int, int]]] = [[path[0]]]
    for idx, node in enumerate(path[1:], start=1):
        last_node = runs[-1][-1]
        if node[2] != last_node[2]:
            runs.append([node])
        else:
            runs[-1].append(node)
            is_interior = idx < len(path) - 1
            if is_interior and (node[0], node[1]) in pinned:
                runs.append([node])

    smoothed: List[Tuple[int, int, int]] = []
    for run in runs:
        layer = run[0][2]
        if len(run) < 3 or not (0 <= layer < n_layers):
            new_run = list(run)
        else:
            cost_layer = cost_grid[layer]
            soft_layer = soft_mask[layer] if soft_mask is not None else None
            blocked_layer = blocked_mask[layer] if blocked_mask is not None else None
            new_run = _smooth_run(run, cost_layer, soft_layer, blocked_threshold, max(0, corridor_radius), blocked_layer,
                                   max(0, corridor_radius) if hard_radius is None else max(0, hard_radius),
                                   angle_step_rad)
        if smoothed and new_run and new_run[0] == smoothed[-1]:
            # Shared pinned node: dedupe. (Via boundaries are disjoint
            # node pairs, so they are never merged here.)
            new_run = new_run[1:]
        smoothed.extend(new_run)
    return smoothed


def smoothed_wire_mm(path: List[Tuple[int, int, int]], resolution_mm: float) -> float:
    """Euclidean wire length of a (possibly shortcut) grid path in mm."""
    total = 0.0
    for k in range(len(path) - 1):
        c1, r1, _ = path[k]
        c2, r2, _ = path[k + 1]
        total += math.hypot(c2 - c1, r2 - r1)
    return total * resolution_mm
