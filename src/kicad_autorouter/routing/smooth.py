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

from ._numba import njit

# Shared dummy for absent optional masks (Numba has no None-arrays: the
# accompanying ``has_*`` flag short-circuits before any indexing).
_EMPTY_BOOL_2D = np.zeros((0, 0), dtype=bool)

# Cost bands (mirrors routing/obstacles.py + router.CostMap).
SOFT_THRESHOLD = 50  # HIGH_COST pad rings / EDGE_KEEPOUT: no shortcut through
                          # COURTYARD (25) is below this — shortcuts allowed

# Smoother engagement anchors for ``RoutingParams.smooth_effort`` (float,
# 0..3, default 2.0). 0 skips smoothing, 1 is shortcut-only without arcs,
# 2 is the full default (arcs + keep radius), (2, 3] adds a bounded
# deterministic re-attempt ladder, 3 is the high-effort retry rung.
# Fractional values interpolate the keep radius between anchors so
# downstream tasks (polish, future post-passes) get proportional
# engagement from the same float.
EFFORT_ARCS_ON = 1.5  # effort at which fillets/sweeps engage
EFFORT_KEEP_FULL_MM = 1.0  # keep radius the ladder converges to at effort 3


def effort_rungs(effort: float) -> List[float]:
    """Deterministic sub-effort ladder for one smooth call.

    Effort <= 2 is a single rung (the effort itself). Above 2, rungs run
    from 2.0 (today's behavior as the safe baseline) up to the effort in
    half-effort steps — at most 3 rungs ([2.0, 2.5, 3.0]). Callers keep
    the best gate-clean rung by wire length, so fractional effort buys
    proportionally more re-attempts.
    """
    e = float(effort)
    if e <= 2.0:
        return [e]
    n = 1 + int(math.floor((e - 2.0) * 2.0 + 1e-9))
    n = min(max(n, 1), 3)
    if n == 1:
        return [e]
    return [2.0 + (e - 2.0) * i / (n - 1) for i in range(n)]


def resolve_config(effort: float, params) -> dict:
    """Map float effort to ``_smooth_result`` kwargs (one rung).

    ``resolve_config(2.0)`` reproduces the current defaults exactly
    (``fit_arcs``, ``arc_max_radius_mm``, ``arc_min_leg_mm``,
    ``arc_sample_mm``, ``arc_keep_min_radius_mm``), so the default path
    is byte-identical to prior behavior.
    """
    e = float(effort)
    keep_default = float(getattr(params, "arc_keep_min_radius_mm", 0.0) or 0.0)
    sample_default = float(getattr(params, "arc_sample_mm", 0.05) or 0.05)
    if e < EFFORT_ARCS_ON:
        keep_mm = 0.0
    elif e <= 2.0:
        keep_mm = keep_default * (e - EFFORT_ARCS_ON) / (2.0 - EFFORT_ARCS_ON)
    else:
        keep_mm = keep_default + (EFFORT_KEEP_FULL_MM - keep_default) * (e - 2.0)
    if e <= 2.0:
        sample_mm = sample_default
    else:
        sample_mm = sample_default * (1.0 - 0.5 * (e - 2.0))
    return {
        "fit_arcs": bool(getattr(params, "arc_fillets", False)) and e >= EFFORT_ARCS_ON,
        "max_radius_mm": float(getattr(params, "arc_max_radius_mm", 10.0)),
        "min_leg_mm": float(getattr(params, "arc_min_leg_mm", 0.3)),
        "sample_mm": sample_mm,
        "keep_min_radius_mm": keep_mm,
    }


def _soft_stamp(mask, grid, n_layers, res_mm, fp, pad, clearance_mm, track_half_mm) -> None:
    """Stamp one pad's escape halo into a soft mask (shared own/sibling path)."""
    from .grid import board_to_grid, mm_to_cells

    try:
        w, h = float(pad.size_mm[0]), float(pad.size_mm[1])
    except (TypeError, ValueError, IndexError):
        w, h = 0.0, 0.0
    shape = (getattr(pad, "shape", "") or "").lower()
    if shape in ("rect", "roundrect", "chamfered", "trapezoid", "custom"):
        pad_half = math.hypot(w, h) / 2.0
    else:
        pad_half = max(w, h) / 2.0
    radius = mm_to_cells(pad_half + clearance_mm + track_half_mm, res_mm)
    col, row = board_to_grid(grid, pad.position.x_mm, pad.position.y_mm)
    if pad.is_through_hole:
        layers = tuple(range(n_layers))
    else:
        idx = grid.layer_to_idx.get(fp.layer)
        layers = (idx,) if idx is not None else ()
    for li in layers:
        if li >= n_layers:
            continue
        r0, r1 = max(0, row - radius), min(grid.height_cells - 1, row + radius)
        c0, c1 = max(0, col - radius), min(grid.width_cells - 1, col + radius)
        mask[li, r0:r1 + 1, c0:c1 + 1] = True


def own_net_soft_mask(model, grid, net_name: str, clearance_mm: float, track_half_mm: float):
    """Boolean ``(n_layers, H, W)`` mask of the net's own pad areas.

    Inside the mask, penalized cells may be crossed by shortcuts (the
    track must escape its own pads). Everywhere else shortcuts require
    FREE cells. BLOCKED cells are impassable regardless of the mask.

    Same-footprint sibling pads (other nets on a footprint hosting this
    net) are included when the pad-to-pad edge gap admits a legal track
    (``>= 2 * clearance + track width``). A track escaping its own SMD
    pad must cross its siblings' halos; without the exemption the
    smoother vetoes the direct chord (HIGH_COST >= SOFT_THRESHOLD) and
    keeps the router's Manhattan detour even though the gap is
    DRC-legal. Sibling *copper* stays protected: the blocked mask
    (secondary THT_EXCLUSION cores) has no exemption.
    """
    from .obstacles import _pad_half_extent_mm

    n_layers = len(grid.layers)
    mask = np.zeros((n_layers, grid.height_cells, grid.width_cells), dtype=bool)
    res_mm = grid.resolution_mm
    if res_mm <= 0:
        return mask

    need_mm = 2.0 * clearance_mm + 2.0 * track_half_mm
    for fp in model.footprints:
        own_pads = [p for p in fp.pads if p.net_name and p.net_name == net_name]
        if not own_pads:
            continue
        for pad in own_pads:
            _soft_stamp(mask, grid, n_layers, res_mm, fp, pad, clearance_mm, track_half_mm)
        for sib in fp.pads:
            if not sib.net_name or sib.net_name == net_name:
                continue
            sib_half = _pad_half_extent_mm(sib)
            gap_ok = False
            for own in own_pads:
                dist = math.hypot(
                    sib.position.x_mm - own.position.x_mm,
                    sib.position.y_mm - own.position.y_mm,
                )
                if dist - _pad_half_extent_mm(own) - sib_half >= need_mm:
                    gap_ok = True
                    break
            if gap_ok:
                _soft_stamp(mask, grid, n_layers, res_mm, fp, sib, clearance_mm, track_half_mm)
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


# Chords within a cell count as distance ties for fillet-aware ranking.
_RANK_EPS_CELLS = 1.0


def _greedy_pass(
    run: List[Tuple[int, int, int]],
    cost_layer,
    soft_layer,
    blocked_threshold: int,
    corridor_radius: int,
    blocked_layer=None,
    hard_radius: Optional[int] = None,
    rank_radius_cells: float = 0.0,
    keep_min_radius_cells: float = 0.0,
    keep_max_radius_cells: float = 0.0,
    keep_min_leg_cells: float = 0.0,
    keep_sample_spacing_cells: float = 0.5,
) -> List[Tuple[int, int, int]]:
    """One greedy furthest-visible pass over a same-layer run.

    Furthest reachable node wins (fewest, longest segments). With
    ``rank_radius_cells > 0``, distance ties (within a cell) break by
    admitted fillet radius at the current corner (the joint the fillet
    would replace — bigger means more sweep per fillet). Ranking is pure
    geometry (no grid access); acceptance still needs the clearance gate
    downstream. ``0`` keeps legacy distance-only comparison exactly.

    With ``keep_min_radius_cells > 0``, a winning chord that would erase
    a keep-worthy corner is cut short at the first such corner along its
    span: same geometry the fitter will use plus the identical clearance
    gate, so every kept corner becomes an arc downstream, never a
    leftover sharp joint. Staircases (cell-scale legs) admit only nub
    radii and keep collapsing as before; only open corners with roomy
    legs survive. ``0`` keeps legacy shortcut-everything behavior
    exactly.
    """
    n = len(run)
    if n < 3:
        return list(run)
    # Lazy: biarc gates via this module's footprint checks, so a top-level
    # import would cycle (biarc -> smooth -> biarc).
    corner_radius = None
    if rank_radius_cells and rank_radius_cells > 0:
        from .biarc import corner_radius as _cr
        corner_radius = _cr
    out = [run[0]]
    i = 0
    while i < n - 1:
        j = n - 1
        best_j = None
        best_radius = -1.0
        while j > i + 1:
            if _line_walkable(
                run[i][0], run[i][1], run[j][0], run[j][1],
                cost_layer, soft_layer, blocked_threshold, corridor_radius,
                blocked_layer, hard_radius,
            ):
                dist = math.hypot(run[j][0] - run[i][0], run[j][1] - run[i][1])
                if corner_radius is not None:
                    # Corner at run[i]: incoming leg is the committed
                    # out[-2] -> run[i] (earlier nodes may be skipped;
                    # out[-1] IS run[i], so it would be degenerate).
                    # Candidate leg is run[i] -> run[j]. Radius is the
                    # tie-breaker (distance, then radius): tangent length
                    # ties whenever distances tie, so only radius — which
                    # grows with the turn the fillet would replace —
                    # discriminates here. Prefer the bigger fillet: that
                    # is where rounding buys the most sweep. Strict >
                    # keeps full ties deterministic (first-found wins).
                    prev = out[-2] if len(out) >= 2 else run[i]
                    radius = corner_radius(
                        (float(prev[0]), float(prev[1])),
                        (float(run[i][0]), float(run[i][1])),
                        (float(run[j][0]), float(run[j][1])),
                        float(rank_radius_cells),
                    )
                    take = (
                        best_j is None
                        or dist > best_dist + _RANK_EPS_CELLS
                        or (
                            abs(dist - best_dist) <= _RANK_EPS_CELLS
                            and radius > best_radius
                        )
                    )
                else:
                    radius = -1.0
                    take = best_j is None or dist > best_dist
                if take:
                    best_j = j
                    best_dist = dist
                    best_radius = radius
            j -= 1
        if best_j is None:
            best_j = i + 1
        if (
            keep_min_radius_cells > 0.0
            and keep_max_radius_cells > 0.0
            and best_j > i + 1
        ):
            # The winning chord erases every node it skips. Cut it short
            # at the first keep-worthy corner along its span — later
            # iterations handle the rest. Legs are measured as the chords
            # that would actually result (run[i] -> run[k] -> run[best_j]),
            # not the raw 1-cell steps: a real sweep corner only exists
            # once the straight approaches collapse. Geometry checks
            # inside _keep_sweep_corner fail fast (angle, then radius,
            # then legs) so only genuine candidates pay for the
            # clearance gate.
            #
            # The cut replaces a walkable chord with two kink legs, so
            # both legs must be walkable as straight segments too: the
            # keep gate only verifies the fillet arc, and the fitter may
            # decline the arc and emit the straight chords — an unchecked
            # kink manufactures an emit-gate trip out of a legal chord
            # (tube114 +12V: 15mm kink through 40 exclusion cells the
            # line check had refused). A corner whose kink is illegal is
            # simply not keep-worthy; the scan moves on.
            for k in range(i + 1, best_j):
                if _keep_sweep_corner(
                    run[i], run[k], run[best_j],
                    cost_layer, soft_layer, blocked_threshold,
                    corridor_radius, blocked_layer, hard_radius,
                    keep_min_radius_cells, keep_max_radius_cells,
                    keep_min_leg_cells, keep_sample_spacing_cells,
                ) and _line_walkable(
                    run[i][0], run[i][1], run[k][0], run[k][1],
                    cost_layer, soft_layer, blocked_threshold, corridor_radius,
                    blocked_layer, hard_radius,
                ) and _line_walkable(
                    run[k][0], run[k][1], run[best_j][0], run[best_j][1],
                    cost_layer, soft_layer, blocked_threshold, corridor_radius,
                    blocked_layer, hard_radius,
                ):
                    best_j = k
                    break
        out.append(run[best_j])
        i = best_j
    return out


def _repair_lumps(
    run: List[Tuple[int, int, int]],
    cost_layer,
    soft_layer,
    blocked_threshold: int,
    corridor_radius: int,
    blocked_layer=None,
    hard_radius: Optional[int] = None,
    keep_min_radius_cells: float = 0.0,
    keep_max_radius_cells: float = 0.0,
    keep_min_leg_cells: float = 0.0,
    keep_sample_spacing_cells: float = 0.5,
) -> List[Tuple[int, int, int]]:
    """Replace residual stairstep lumps with outward 2–3 leg detours.

    Operates on one greedy-smoothed run: any window [i, j] with >=8 nodes
    whose direct chord is unwalkable is a lump the subset pass cannot fix.
    Tries each bbox-corner Steiner candidate P (A→P→B), scoring pure
    length (shortest wins); falls back to the best single candidate pair
    (A→P→Q→B). Every inserted leg is fully walkability-checked with the
    same conservative corridor, so clearance is preserved — vertices only
    move outward, never into foreign copper. New waypoints are grid cells,
    so output stays straight ``(segment)`` tracks.
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
                score = length
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
                        score = length
                        if best is None or score < best[0]:
                            best = (score, [(pc, pr), (qc, qr)])
            if best is not None:
                layer = nodes[i][2]
                waypoints = [(pc, pr, layer) for pc, pr in best[1]]
                nodes = nodes[:i + 1] + waypoints + nodes[j:]
                changed = True
                # Re-collapse around the insertion; restart scan just after.
                # Keep-rule params ride along so repair never erases a corner
                # the main passes preserved.
                nodes = _greedy_pass(nodes, cost_layer, soft_layer, blocked_threshold,
                                     corridor_radius, blocked_layer, hard_radius,
                                     0.0, keep_min_radius_cells, keep_max_radius_cells,
                                     keep_min_leg_cells, keep_sample_spacing_cells)
                i += 1
            else:
                i += 1
    return nodes


# Minimum direction change a kept corner must carry. Shallower joints keep
# legacy shortcutting: rounding them buys a near-invisible arc at the cost
# of the tightest chord, so there is no sweep worth preserving.
_KEEP_MIN_TURN_RAD = math.pi / 4


def _keep_sweep_corner(
    a, b, c,
    cost_layer,
    soft_layer,
    blocked_threshold: int,
    corridor_radius: int,
    blocked_layer,
    hard_radius,
    keep_min_radius_cells: float,
    keep_max_radius_cells: float,
    keep_min_leg_cells: float,
    keep_sample_spacing_cells: float,
) -> bool:
    """True when the corner ``a -> b -> c`` must survive shortcutting.

    Callers pass the chords that would actually result from cutting the
    winning shortcut at ``b`` (incoming ``a -> b``, outgoing ``b -> c``),
    not the raw path steps — a sweep corner only exists once the straight
    approaches collapse. Preserve-only-if-fillet-fires: the admitted
    radius uses the same ``_corner_params`` geometry the fitter will use
    (same tangent cap), the minimum leg mirrors the fitter's
    ``min_leg_cells`` skip, and the clearance gate is the identical
    ``_arc_walkable`` call with the same corridor/hard/spacing arguments.
    A kept corner therefore becomes an arc downstream, never a leftover
    sharp joint.
    """
    # Lazy: biarc gates via this module's footprint checks, so a top-level
    # import would cycle (biarc -> smooth -> biarc).
    from .biarc import _arc_walkable, _corner_arc, _corner_params
    p0 = (float(a[0]), float(a[1]))
    p1 = (float(b[0]), float(b[1]))
    p2 = (float(c[0]), float(c[1]))
    params = _corner_params(p0, p1, p2, keep_max_radius_cells)
    if params is None:
        return False
    _t, radius, _ax, _ay, _bx, _by, half = params
    if 2.0 * half < _KEEP_MIN_TURN_RAD:
        return False
    if radius < keep_min_radius_cells:
        return False
    d1 = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
    d2 = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
    if min(d1, d2) < keep_min_leg_cells:
        return False
    arc = _corner_arc(p0, p1, p2, keep_max_radius_cells)
    if arc is None:
        return False
    t1, mid, t2 = arc
    return bool(_arc_walkable(
        t1, mid, t2, cost_layer, soft_layer, blocked_threshold,
        corridor_radius, blocked_layer, hard_radius,
        keep_sample_spacing_cells,
    ))


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


    return out


def _smooth_run(
    run: List[Tuple[int, int, int]],
    cost_layer,
    soft_layer,
    blocked_threshold: int,
    corridor_radius: int,
    blocked_layer=None,
    hard_radius: Optional[int] = None,
    rank_radius_cells: float = 0.0,
    keep_min_radius_cells: float = 0.0,
    keep_max_radius_cells: float = 0.0,
    keep_min_leg_cells: float = 0.0,
    keep_sample_spacing_cells: float = 0.5,
) -> List[Tuple[int, int, int]]:
    """Iterative bidirectional greedy + outward lump repair.

    Forward-only greedy commits to the furthest-visible node from one end,
    which strands mid-region lumps (the reported stairstep behavior).
    Running greedy forward, then backward on the result, until stable
    removes the start-end bias with the same walkability guarantees.
    Residual dense windows that no subset shortcut can clear go through
    outward Steiner repair (new vertices, same clearance checks).

    Keep-rule params ride into every greedy call (both directions plus
    the repair re-collapse) so a preserved sweep corner survives the
    whole pass, not just one direction. All default to legacy
    shortcut-everything.
    """
    n = len(run)
    if n < 3:
        return list(run)
    keep = (keep_min_radius_cells, keep_max_radius_cells,
            keep_min_leg_cells, keep_sample_spacing_cells)
    cur = _greedy_pass(run, cost_layer, soft_layer, blocked_threshold,
                       corridor_radius, blocked_layer, hard_radius,
                       rank_radius_cells, *keep)
    for _ in range(3):
        rev = list(reversed(cur))
        back = _greedy_pass(rev, cost_layer, soft_layer, blocked_threshold,
                            corridor_radius, blocked_layer, hard_radius,
                            rank_radius_cells, *keep)
        nxt = list(reversed(back))
        if len(nxt) >= len(cur):
            cur = nxt if len(nxt) < len(cur) else cur
            break
        cur = nxt
    cur = _repair_lumps(cur, cost_layer, soft_layer, blocked_threshold,
                        corridor_radius, blocked_layer, hard_radius,
                        *keep)
    return cur


def _split_runs(
    path: List[Tuple[int, int, int]],
    pinned: Set[Tuple[int, int]],
) -> List[List[Tuple[int, int, int]]]:
    """Split a branch into maximal same-layer runs.

    A layer transition ends the current run and starts a new one, so
    smoothing can never shortcut across a via (which would move the via
    position). Concatenating the smoothed runs preserves every via edge
    exactly: each run keeps its endpoints, and adjacent runs join at the
    original via nodes. Pinned middle terminals likewise force boundaries
    (shared node in both runs — same layer, so sharing is safe here).
    """
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
    return runs


def smooth_grid_path(
    path: List[Tuple[int, int, int]],
    cost_grid,
    blocked_threshold: int = 100,
    corridor_radius: int = 0,
    pinned: Optional[Set[Tuple[int, int]]] = None,
    soft_mask=None,
    blocked_mask=None,
    hard_radius: Optional[int] = None,
    rank_radius_cells: float = 0.0,
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
    runs = _split_runs(path, pinned)

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
                                   rank_radius_cells)
        if smoothed and new_run and new_run[0] == smoothed[-1]:
            # Shared pinned node: dedupe. (Via boundaries are disjoint
            # node pairs, so they are never merged here.)
            new_run = new_run[1:]
        smoothed.extend(new_run)
    return smoothed


def smooth_branch_pieces(
    path: List[Tuple[int, int, int]],
    cost_grid,
    blocked_threshold: int = 100,
    corridor_radius: int = 0,
    pinned: Optional[Set[Tuple[int, int]]] = None,
    soft_mask=None,
    blocked_mask=None,
    hard_radius: Optional[int] = None,
    rank_radius_cells: float = 0.0,
    max_radius_cells: float = 10.0,
    min_leg_cells: float = 3.0,
    sample_spacing_cells: float = 0.5,
    keep_min_radius_cells: float = 0.0,
) -> Tuple[List[Tuple[int, int, int]], List[tuple]]:
    """Smooth one branch and fillet its runs: the single gate, where runs live.

    Same topology pass as :func:`smooth_grid_path` (same-layer runs, via
    and pinned boundaries preserved), but each smoothed run is additionally
    fitted in place: dense noisy windows collapse into broad circumcircle
    sweeps first, then remaining interior unpinned corners get tangent arcs
    — both through the same biarc clearance gate, rejected candidates keep
    sharp joints. Returns ``(smoothed_path, pieces)`` where pieces is the
    flat arc-chain for the whole branch (``("seg"/"arc", ..., layer)`` plus
    ``("via", ...)`` markers at layer transitions) for arc-aware
    reservation/emission.

    ``keep_min_radius_cells > 0`` spares open corners admitting at least
    that fillet radius from the pull-string (same clearance gate the
    fitter uses, so kept corners always become arcs). ``0`` keeps legacy
    shortcut-everything smoothing exactly. The broad floor reuses the same
    value, so sweeps stay visibly broad; ``0`` still gates on walkability
    and non-straight geometry only.
    """
    from .biarc import fit_branch_arcs_with_sweeps  # lazy: biarc gates via this module
    if not path:
        return [], []
    if len(path) < 3:
        return list(path), []
    n_layers = cost_grid.shape[0]
    pinned = set(pinned or set())
    pinned.add((path[0][0], path[0][1]))
    pinned.add((path[-1][0], path[-1][1]))
    runs = _split_runs(path, pinned)

    smoothed: List[Tuple[int, int, int]] = []
    pieces: List[tuple] = []
    for ri, run in enumerate(runs):
        layer = run[0][2]
        if ri > 0:
            prev_end, cur_start = runs[ri - 1][-1], run[0]
            pieces.append(("via", float(cur_start[0]), float(cur_start[1]),
                           prev_end[2], cur_start[2]))
        if len(run) < 3 or not (0 <= layer < n_layers):
            new_run = list(run)
            for a, b in zip(new_run, new_run[1:]):
                pieces.append(("seg", float(a[0]), float(a[1]),
                               float(b[0]), float(b[1]), layer))
        else:
            cost_layer = cost_grid[layer]
            soft_layer = soft_mask[layer] if soft_mask is not None else None
            blocked_layer = blocked_mask[layer] if blocked_mask is not None else None
            new_run = _smooth_run(run, cost_layer, soft_layer, blocked_threshold, max(0, corridor_radius), blocked_layer,
                                   max(0, corridor_radius) if hard_radius is None else max(0, hard_radius),
                                   rank_radius_cells,
                                   keep_min_radius_cells, max_radius_cells,
                                   min_leg_cells, sample_spacing_cells)
            run_pieces = fit_branch_arcs_with_sweeps(
                new_run, cost_layer, soft_layer, blocked_threshold,
                max(0, corridor_radius), blocked_layer,
                max(0, corridor_radius) if hard_radius is None else max(0, hard_radius),
                pinned, max_radius_cells, min_leg_cells, sample_spacing_cells,
                keep_min_radius_cells,
            )
            pieces.extend(run_pieces)
        if smoothed and new_run and new_run[0] == smoothed[-1]:
            new_run = new_run[1:]
        smoothed.extend(new_run)
    return smoothed, pieces


def smoothed_wire_mm(path: List[Tuple[int, int, int]], resolution_mm: float) -> float:
    """Euclidean wire length of a (possibly shortcut) grid path in mm."""
    total = 0.0
    for k in range(len(path) - 1):
        c1, r1, _ = path[k]
        c2, r2, _ = path[k + 1]
        total += math.hypot(c2 - c1, r2 - r1)
    return total * resolution_mm
