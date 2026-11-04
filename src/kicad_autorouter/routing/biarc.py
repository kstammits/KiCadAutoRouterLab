"""Tangent-arc (biarc-style) copper fitting for routed branches.

Post-pass after grid smoothing: replaces sharp interior corners of a
same-layer branch with tangent circular arcs, keeping every straight stub
on the already clearance-checked chord. Output is KiCad-native copper —
(timestamp-free) ``(segment)`` stubs plus ``(arc start mid end)`` bends —
so nothing downstream (DRC, fabrication, viewer) needs a spline primitive.

Freeform curvature (varying radius, S-bends) is expressed as tangent-
joined arc chains, not single arcs: each corner gets its own fillet sized
to its legs, and consecutive short legs naturally chain into sweeps.

Safety contract (mirrors ``smooth.py``):
- Via (layer-transition) nodes and pinned terminals/Steiner attachments
  are never moved, removed, or filleted across.
- A candidate arc is accepted only if every sample along it (outside a
  1-cell allowance at its tangent-point ends, which sit on routed copper)
  has a clear corridor footprint: FREE cells, own-net pads exempt via
  ``soft_mask``, foreign-THT hard exclusion with no exemption.
- Straight stubs between fillets are sub-segments of already-checked
  smoother chords, so they need no re-check.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Set, Tuple

from ..board_model import sample_circular_arc
from .smooth import _blocked_footprint_hit, _footprint_clear

# Piece kinds in grid-float coordinates, layer-tagged:
# ("seg", c1, r1, c2, r2, layer), ("arc", cs, rs, cm, rm, ce, re, layer),
# or ("via", c, r, l1, l2) markers at layer transitions (pipeline-added).
SegPiece = Tuple[str, float, float, float, float, int]
ArcPiece = Tuple[str, float, float, float, float, float, float, int]

_EPS = 1e-9
# Cosine of the largest direction change still treated as straight.
_STRAIGHT_COS = math.cos(math.radians(1.0))


def _dist(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def simplify_waypoints(
    pts: List[Tuple[float, float]],
    pinned: Set[Tuple[float, float]],
) -> List[Tuple[float, float]]:
    """Strip spikes, drops zero-lengths, merge near-collinear joints.

    Pinned waypoints (terminals, Steiner attachments) are never removed:
    any window touching one is left alone. Operates on float grid coords;
    exact-int waypoints compare exactly, so pinned membership is exact.
    """
    pts = [p for p in pts if p is not None]
    # Drop consecutive duplicates.
    out: List[Tuple[float, float]] = []
    for p in pts:
        if out and _dist(out[-1], p) < _EPS:
            continue
        out.append(p)
    # Strip A->B->A spikes (keep a single A).
    changed = True
    while changed:
        changed = False
        i = 0
        while i + 2 <= len(out) - 1:
            a, b, c = out[i], out[i + 1], out[i + 2]
            if (
                _dist(a, c) < _EPS
                and a not in pinned
                and b not in pinned
                and c not in pinned
            ):
                del out[i + 1 : i + 3]
                changed = True
                continue
            i += 1
    # Merge near-collinear middles.
    i = 0
    merged: List[Tuple[float, float]] = []
    for p in out:
        merged.append(p)
        while (
            len(merged) >= 3
            and merged[-2] not in pinned
        ):
            a, b, c = merged[-3], merged[-2], merged[-1]
            d1 = _dist(a, b)
            d2 = _dist(b, c)
            if d1 < _EPS or d2 < _EPS:
                del merged[-2]
                continue
            ux, uy = (b[0] - a[0]) / d1, (b[1] - a[1]) / d1
            vx, vy = (c[0] - b[0]) / d2, (c[1] - b[1]) / d2
            if ux * vx + uy * vy > _STRAIGHT_COS:
                del merged[-2]
                continue
            break
    return merged


def _corner_params(
    p0: Tuple[float, float],
    p1: Tuple[float, float],
    p2: Tuple[float, float],
    max_radius: float,
) -> Optional[Tuple[float, float, float, float, float, float, float]]:
    """Admitted ``(t, radius, ax, ay, bx, by, half)`` at corner ``p1``.

    Shared core of ranking and gating so both agree exactly: tangent
    length capped at half each leg (adjacent fillets never overlap) and
    at ``max_radius``; radius follows from ``t = R * tan(phi/2)``.
    None for straight/degenerate corners.
    """
    frame = _corner_frame(p0, p1, p2)
    if frame is None:
        return None
    ax, ay, bx, by, d1, d2 = frame
    cos_phi = ax * bx + ay * by
    phi = math.acos(max(-1.0, min(1.0, cos_phi)))
    half = phi / 2.0
    if half < _EPS or abs(math.tan(half)) < _EPS:
        return None
    t = min(max_radius, d1 * 0.5, d2 * 0.5)
    if t < _EPS:
        return None
    return t, t / math.tan(half), ax, ay, bx, by, half


def corner_radius(
    p0: Tuple[float, float],
    p1: Tuple[float, float],
    p2: Tuple[float, float],
    max_radius: float,
) -> float:
    """Admitted tangent-fillet radius at corner ``p1`` (pure geometry).

    No grid access. Returns 0.0 for straight/degenerate corners.
    Gating use — ranking prefers tangent length (see
    :func:`fillet_tangent`), which measures roominess, not sharpness.
    """
    params = _corner_params(p0, p1, p2, max_radius)
    return params[1] if params is not None else 0.0


def fillet_tangent(
    p0: Tuple[float, float],
    p1: Tuple[float, float],
    p2: Tuple[float, float],
    max_radius: float,
) -> float:
    """Admitted tangent length at corner ``p1`` (pure geometry).

    Diagnostic use: how much of each leg a fillet would consume.
    NOT a ranking metric — under a distance tie the candidate legs are
    near-equal, so tangents tie too and ranking would be vacuous (see
    ``corner_radius`` for the discriminator that survives ties).
    Returns 0.0 for straight/degenerate corners.
    """
    params = _corner_params(p0, p1, p2, max_radius)
    return params[0] if params is not None else 0.0


def _corner_frame(
    p0: Tuple[float, float],
    p1: Tuple[float, float],
    p2: Tuple[float, float],
) -> Optional[Tuple[float, float, float, float, float]]:
    """Corner unit vectors and leg lengths: ``(ax, ay, bx, by, d1, d2)``.

    Shared core of :func:`corner_radius` and :func:`_corner_arc` so the
    ranking and gating geometries agree exactly. None when degenerate.
    """
    d1 = _dist(p0, p1)
    d2 = _dist(p1, p2)
    if d1 < _EPS or d2 < _EPS:
        return None
    ax, ay = (p0[0] - p1[0]) / d1, (p0[1] - p1[1]) / d1
    bx, by = (p2[0] - p1[0]) / d2, (p2[1] - p1[1]) / d2
    cos_phi = ax * bx + ay * by
    if cos_phi > _STRAIGHT_COS:
        return None  # straight: nothing to fillet
    if cos_phi < -1.0 + 1e-9:
        return None  # hairpin: simplify pass owns these, not the fitter
    return ax, ay, bx, by, d1, d2


def _corner_arc(
    p0: Tuple[float, float],
    p1: Tuple[float, float],
    p2: Tuple[float, float],
    max_radius: float,
) -> Optional[Tuple[Tuple[float, float], Tuple[float, float], Tuple[float, float]]]:
    """Tangent arc fillet at corner ``p1``: returns ``(t1, mid, t2)``.

    Tangent length is capped at half each leg (adjacent fillets never
    overlap) and at ``max_radius`` worth of tangent length. Returns None
    for straight/degenerate corners.
    """
    params = _corner_params(p0, p1, p2, max_radius)
    if params is None:
        return None
    t, radius, ax, ay, bx, by, half = params
    # Bisector from the corner toward the arc.
    nx, ny = ax + bx, ay + by
    nl = math.hypot(nx, ny)
    if nl < _EPS:
        return None
    nx, ny = nx / nl, ny / nl
    dist_center = radius / math.sin(half)
    cx, cy = p1[0] + nx * dist_center, p1[1] + ny * dist_center
    t1 = (p1[0] + ax * t, p1[1] + ay * t)
    t2 = (p1[0] + bx * t, p1[1] + by * t)
    # Arc mid: circle point on the bisector toward the chord.
    qx, qy = (t1[0] + t2[0]) / 2.0, (t1[1] + t2[1]) / 2.0
    dx, dy = qx - cx, qy - cy
    dl = math.hypot(dx, dy)
    if dl < _EPS:
        return None
    mid = (cx + dx / dl * radius, cy + dy / dl * radius)
    return t1, mid, t2


def _arc_walkable(
    s: Tuple[float, float],
    m: Tuple[float, float],
    e: Tuple[float, float],
    cost_layer,
    soft_layer,
    blocked_threshold: int,
    corridor_radius: int,
    blocked_layer=None,
    hard_radius: Optional[int] = None,
    spacing: float = 0.5,
) -> bool:
    """Clearance gate for one candidate arc (grid-float coords).

    Samples the arc at ``spacing`` cells; samples within 1 cell of either
    tangent end sit on routed copper and are exempt (same allowance the
    smoother grants chord endpoints). Every other sample needs a clear
    corridor footprint, with no soft exemption on hard exclusion.
    """
    chord = _dist(s, e)
    n = max(4, int(math.ceil(chord / max(spacing, _EPS))) + 1)
    pts = list(sample_circular_arc((s[0], s[1]), (m[0], m[1]), (e[0], e[1]), n))
    for (cf, rf) in pts:
        if math.hypot(cf - s[0], rf - s[1]) < 1.0 or math.hypot(cf - e[0], rf - e[1]) < 1.0:
            continue
        hr = corridor_radius if hard_radius is None else hard_radius
        if blocked_layer is not None and _blocked_footprint_hit(
            blocked_layer, float(cf), float(rf), int(hr)
        ):
            return False
        if not _footprint_clear(
            cost_layer, soft_layer, float(cf), float(rf),
            int(corridor_radius), int(blocked_threshold), blocked_layer,
        ):
            return False
    return True


def fit_branch_arcs(
    run: List[Tuple[int, int, int]],
    cost_layer,
    soft_layer,
    blocked_threshold: int,
    corridor_radius: int,
    blocked_layer=None,
    hard_radius: Optional[int] = None,
    pinned: Optional[Set[Tuple[int, int]]] = None,
    max_radius_cells: float = 10.0,
    min_leg_cells: float = 3.0,
    sample_spacing_cells: float = 0.5,
) -> List[tuple]:
    """Fillet the sharp corners of one same-layer smoothed run.

    Returns ordered pieces in grid-float coords (``("seg", ...)`` /
    ``("arc", ...)``). Corners at pinned nodes, with a leg shorter than
    ``min_leg_cells``, or whose arc fails the clearance gate keep their
    sharp joint. Runs shorter than 3 waypoints pass through unchanged.
    """
    pts: List[Tuple[float, float]] = [(float(c), float(r)) for c, r, _ in run]
    layer = run[0][2]
    if len(pts) < 3:
        return [("seg", pts[0][0], pts[0][1], pts[-1][0], pts[-1][1], layer)] if len(pts) == 2 else []
    pin_f = {(float(c), float(r)) for c, r in (pinned or set())}
    pts = simplify_waypoints(pts, pin_f)
    if len(pts) < 3:
        pieces: List[tuple] = []
        for a, b in zip(pts, pts[1:]):
            pieces.append(("seg", a[0], a[1], b[0], b[1], layer))
        return pieces
    # Fillet decisions per interior corner (geometry first).
    fillets: Dict[int, tuple] = {}
    for i in range(1, len(pts) - 1):
        if pts[i] in pin_f:
            continue
        d1 = _dist(pts[i - 1], pts[i])
        d2 = _dist(pts[i], pts[i + 1])
        if min(d1, d2) < min_leg_cells:
            continue
        arc = _corner_arc(pts[i - 1], pts[i], pts[i + 1], max_radius_cells)
        if arc is None:
            continue
        t1, mid, t2 = arc
        if not _arc_walkable(
            t1, mid, t2, cost_layer, soft_layer, blocked_threshold,
            corridor_radius, blocked_layer, hard_radius, sample_spacing_cells,
        ):
            continue
        fillets[i] = (t1, mid, t2)
    # Stitch pieces: stub -> arc -> stub along the waypoint chain.
    pieces = []
    cursor = pts[0]
    for i in range(1, len(pts) - 1):
        if i in fillets:
            t1, mid, t2 = fillets[i]
            if _dist(cursor, t1) > _EPS:
                pieces.append(("seg", cursor[0], cursor[1], t1[0], t1[1], layer))
            pieces.append(("arc", t1[0], t1[1], mid[0], mid[1], t2[0], t2[1], layer))
            cursor = t2
        else:
            if _dist(cursor, pts[i]) > _EPS:
                pieces.append(("seg", cursor[0], cursor[1], pts[i][0], pts[i][1], layer))
            cursor = pts[i]
    if _dist(cursor, pts[-1]) > _EPS:
        pieces.append(("seg", cursor[0], cursor[1], pts[-1][0], pts[-1][1], layer))
    return pieces


def pieces_wire_cells(pieces: List[tuple]) -> float:
    """Wire length of fitted pieces in grid cells (arcs via chord chain)."""
    total = 0.0
    for p in pieces:
        if p[0] == "seg":
            total += math.hypot(p[3] - p[1], p[4] - p[2])
        else:
            pts = list(sample_circular_arc((p[1], p[2]), (p[3], p[4]), (p[5], p[6]), 12))
            total += sum(
                math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:])
            )
    return total
