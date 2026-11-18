from __future__ import annotations
"""Smoothing core: config, walkability, soft masks (split from smooth.py)."""
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


import math
import time
from typing import Dict, List, Optional, Set, Tuple

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

    Route time takes a single pass at the requested effort; the retry
    ladder below belongs to polish (re-smoothing against the finished
    board, roomiest rung within 10% of the shortest wins).

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
