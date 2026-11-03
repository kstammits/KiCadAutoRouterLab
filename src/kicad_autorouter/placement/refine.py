"""Geometric tidy pass: neighbor-median angle alignment + median-gap equalization.

Runs after the force-spring dynamics (or standalone on a committed board).
Pure geometry in board millimeters — no forces, no SI, no prints.

- Neighbors: same board region AND same layer, k-nearest (k=6) by
  courtyard-centroid distance within ``radius_mm``. Locked and ghost bodies
  count as anchors (alignment/spacing targets) but never move.
- Angle: each movable multi-pad part blends toward the 180-degree-wrapped
  circular median of its neighbors' posed angles. 180-degree wrap keeps the
  common 179/-179-degree split together and suits 2-pin symmetry; asymmetric
  ICs may read 180-degree flips as aligned (documented compromise).
- Spacing: three Jacobi sweeps push each part's edge-to-edge neighbor gaps
  toward the local median gap, split evenly when both ends are movable.
  Cross-layer pairs are excluded (bodies stack by design).
- Safety: the candidate is checked with ``check_legality`` and offending
  parts are rolled back greedily (largest overlap first) so refine never
  adds an overlap or wall violation. ``strength == 0`` returns ``{}``.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Set, Tuple

from ..board_model import BoardModel

_REFINE_NEIGHBOR_K = 6
_REFINE_SWEEPS = 3


def _wrap180(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


def _circular_median_180(angles_deg: List[float]) -> float:
    """Median of angles under 180-degree symmetry.

    Doubles the angles (so a and a+180 coincide), takes the plain median of
    the doubled values after unwrapping, then halves back. Deterministic.
    """
    if not angles_deg:
        return 0.0
    doubled = sorted((2.0 * float(a)) % 360.0 for a in angles_deg)
    n = len(doubled)
    if n % 2 == 1:
        med = doubled[n // 2]
    else:
        lo, hi = doubled[n // 2 - 1], doubled[n // 2]
        if hi - lo > 180.0:
            med = (hi + lo + 360.0) / 2.0 % 360.0
        else:
            med = (lo + hi) / 2.0
    half = (med / 2.0) % 180.0
    return half if half < 90.0 else half - 180.0


def _median(values: List[float]) -> float:
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0.0
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def _empty_report(reason: str) -> Dict[str, object]:
    return {
        "reason": reason,
        "considered": 0,
        "no_neighbors": [],
        "rolled_back": [],
        "kept": [],
        "base_illegal": False,
        "base_overlaps": 0,
    }


def refine_placement(
    model: BoardModel,
    base_deltas: Dict[str, Tuple[float, float, float]],
    *,
    strength: float,
    radius_mm: float,
    movable_uuids: Optional[Set[str]] = None,
    overlap_tolerance_mm2: float = 0.1,
    wall_tolerance_mm: float = 0.1,
) -> Dict[str, Tuple[float, float, float]]:
    """Compute tidy deltas on top of ``base_deltas``.

    Returns {uuid: (dx_mm, dy_mm, dangle_deg)} deltas *relative to the model
    as passed in* (i.e. composable: apply on top of ``base_deltas``). Parts
    with no meaningful change are omitted. Never raises for empty input.
    See :func:`refine_placement_with_report` for the why-nothing-moved
    breakdown.
    """
    deltas, _ = refine_placement_with_report(
        model, base_deltas,
        strength=strength,
        radius_mm=radius_mm,
        movable_uuids=movable_uuids,
        overlap_tolerance_mm2=overlap_tolerance_mm2,
        wall_tolerance_mm=wall_tolerance_mm,
    )
    return deltas


def refine_placement_with_report(
    model: BoardModel,
    base_deltas: Dict[str, Tuple[float, float, float]],
    *,
    strength: float,
    radius_mm: float,
    movable_uuids: Optional[Set[str]] = None,
    overlap_tolerance_mm2: float = 0.1,
    wall_tolerance_mm: float = 0.1,
) -> Tuple[Dict[str, Tuple[float, float, float]], Dict[str, object]]:
    """Same as :func:`refine_placement` plus a report dict.

    Report keys: ``reason`` ("ok"/"off"/"empty"), ``considered`` (movable
    parts posed), ``no_neighbors`` (movable uuids with nobody to align
    to), ``rolled_back`` (uuids dropped by the legality gate),
    ``kept`` (uuids in the returned deltas), ``base_illegal`` (the base
    deltas were already illegal, so nothing could be kept).
    """
    if not math.isfinite(strength) or strength <= 0.0:
        return {}, _empty_report("off")
    strength = min(1.0, float(strength))
    if not math.isfinite(radius_mm) or radius_mm <= 0.0:
        return {}, _empty_report("empty")

    from ..board_model import apply_deltas
    from .legality import _collect_posed_polygons, check_legality

    # Movable set: unlocked, unpinned handled by caller via movable_uuids.
    # Only these receive deltas; every real (non-ghost) part counts as
    # alignment/spacing context, including unselected and pinned parts.
    # Ghosts are fixed walls, never alignment references.
    movable: Set[str] = set()
    locked: Set[str] = set()
    for fp in model.footprints:
        if not fp.uuid:
            continue
        if fp.locked:
            locked.add(fp.uuid)
            continue
        if movable_uuids is not None and fp.uuid not in movable_uuids:
            continue
        movable.add(fp.uuid)
    if not movable:
        return {}, _empty_report("empty")

    posed = apply_deltas(model, dict(base_deltas))
    polys, _, _, moved = _collect_posed_polygons(posed, {})

    ghost_uuids: Set[str] = {
        fp.uuid for fp in moved.ghost_footprints if fp.uuid
    }

    # Posed lookup: uuid -> (x, y, angle, layer, region).
    info: Dict[str, Tuple[float, float, float, str, int]] = {}
    for fp in moved.footprints:
        if not fp.uuid:
            continue
        info[fp.uuid] = (
            fp.x_mm, fp.y_mm, fp.angle_deg, fp.layer or "",
            model.footprint_region.get(fp.uuid, -1),
        )
    for fp in moved.ghost_footprints:
        if not fp.uuid:
            continue
        info[fp.uuid] = (
            fp.x_mm, fp.y_mm, fp.angle_deg, getattr(fp, "layer", "") or "",
            model.footprint_region.get(fp.uuid, -1),
        )

    # Centroids from courtyard polygons (fall back to footprint center).
    cents: Dict[str, Tuple[float, float]] = {}
    for uuid, poly in polys.items():
        try:
            if poly is not None and not poly.is_empty:
                c = poly.centroid
                cents[uuid] = (float(c.x), float(c.y))
                continue
        except Exception:
            pass
        if uuid in info:
            cents[uuid] = (info[uuid][0], info[uuid][1])

    # Neighbor graph: every real part is alignment/spacing context
    # (movable, unselected, pinned, locked); ghosts are fixed walls and
    # never alignment references. Only `movable` receives deltas.
    # Same region + same layer, as before.
    context = sorted(u for u in info if u not in ghost_uuids)
    uuids_sorted = context
    neighbors: Dict[str, List[str]] = {}
    for ua in sorted(movable):
        if ua not in info or ua not in cents:
            neighbors[ua] = []
            continue
        ax, ay = cents[ua]
        _, _, _, alayer, aregion = info[ua]
        scored: List[Tuple[float, str]] = []
        for ub in uuids_sorted:
            if ub == ua or ub not in info or ub not in cents:
                continue
            _, _, _, blayer, bregion = info[ub]
            if blayer != alayer or aregion != bregion:
                continue
            bx, by = cents[ub]
            d = math.hypot(bx - ax, by - ay)
            if d <= radius_mm:
                scored.append((d, ub))
        scored.sort(key=lambda t: (t[0], t[1]))
        neighbors[ua] = [u for _, u in scored[:_REFINE_NEIGHBOR_K]]

    no_neighbors: List[str] = sorted(
        ua for ua in movable
        if ua in info and not neighbors.get(ua)
    )

    # Pad counts to skip single-pad rotation.
    pad_counts: Dict[str, int] = {}
    for fp in moved.footprints:
        if fp.uuid:
            pad_counts[fp.uuid] = len(fp.pads)

    # Angle targets: 180-wrapped median of neighbor posed angles.
    angle_target: Dict[str, float] = {}
    for ua in sorted(movable):
        nbs = neighbors.get(ua, [])
        angs = [info[u][2] for u in nbs if u in info]
        if not angs or pad_counts.get(ua, 0) < 2:
            continue
        med = _circular_median_180(angs)
        cur = info[ua][2]
        # Blend in 180-space: delta wrapped to [-90, 90].
        delta = _wrap180(med - cur)
        if delta > 90.0:
            delta -= 180.0
        elif delta < -90.0:
            delta += 180.0
        if abs(delta) > 1e-9:
            angle_target[ua] = cur + strength * delta

    # Spacing: Jacobi sweeps on positions toward local median gap.
    pos: Dict[str, List[float]] = {
        u: [info[u][0], info[u][1]] for u in uuids_sorted if u in info
    }

    def _radius_of(uuid: str) -> float:
        poly = polys.get(uuid)
        try:
            if poly is not None and not poly.is_empty:
                c = poly.centroid
                coords = list(poly.exterior.coords)
                if coords:
                    return max(
                        math.hypot(x - c.x, y - c.y) for x, y in coords
                    )
        except Exception:
            pass
        return 1.0

    radii = {u: _radius_of(u) for u in pos}

    for _ in range(_REFINE_SWEEPS):
        step: Dict[str, List[float]] = {u: [0.0, 0.0] for u in movable if u in pos}
        for ua in sorted(movable):
            if ua not in pos:
                continue
            nbs = [u for u in neighbors.get(ua, []) if u in pos]
            if not nbs:
                continue
            ax, ay = pos[ua]
            gaps: List[float] = []
            dirs: Dict[str, Tuple[float, float, float]] = {}
            for ub in nbs:
                bx, by = pos[ub]
                dx, dy = ax - bx, ay - by
                dist = math.hypot(dx, dy)
                if dist <= 1e-9:
                    continue
                gap = dist - radii.get(ua, 1.0) - radii.get(ub, 1.0)
                gaps.append(gap)
                dirs[ub] = (dx / dist, dy / dist, gap)
            if not gaps:
                continue
            med_gap = _median(gaps)
            for ub, (ux, uy, gap) in dirs.items():
                err = gap - med_gap
                push = 0.5 * strength * err
                if ub in movable and ub in step:
                    step[ua][0] -= push * ux
                    step[ua][1] -= push * uy
                    step[ub][0] += push * ux
                    step[ub][1] += push * uy
                else:
                    step[ua][0] -= push * ux
                    step[ua][1] -= push * uy
        for u, (sx, sy) in step.items():
            if math.isfinite(sx) and math.isfinite(sy):
                pos[u][0] += sx / _REFINE_SWEEPS
                pos[u][1] += sy / _REFINE_SWEEPS

    # Compose candidate deltas relative to the input model pose.
    candidate: Dict[str, Tuple[float, float, float]] = {}
    for ua in sorted(movable):
        if ua not in info or ua not in pos:
            continue
        ox, oy = info[ua][0], info[ua][1]
        # base pose already includes base_deltas (posed == model + base);
        # candidate is expressed vs the ORIGINAL model: subtract base first.
        bx, by, ba = base_deltas.get(ua, (0.0, 0.0, 0.0))
        nx, ny = pos[ua][0], pos[ua][1]
        dx = (nx - ox) + bx
        dy = (ny - oy) + by
        da = 0.0
        if ua in angle_target:
            da = _wrap180(angle_target[ua] - info[ua][2]) + ba
            da = _wrap180(da)
        else:
            da = ba
        if abs(dx) < 1e-9 and abs(dy) < 1e-9 and abs(da) < 1e-9:
            continue
        if abs(dx - bx) < 1e-9 and abs(dy - by) < 1e-9 and abs(da - ba) < 1e-9:
            continue
        candidate[ua] = (dx, dy, da)

    def _report(kept: Dict[str, Tuple[float, float, float]],
                rolled: List[str],
                base_illegal: bool,
                base_overlaps: int = 0) -> Dict[str, object]:
        return {
            "reason": "ok",
            "considered": sum(1 for u in movable if u in info),
            "no_neighbors": list(no_neighbors),
            "rolled_back": list(rolled),
            "kept": sorted(kept),
            "base_illegal": base_illegal,
            "base_overlaps": base_overlaps,
        }

    if not candidate:
        return {}, _report({}, [], False)

    # Baseline: touches already on the board are not Tidy's business.
    # The gate below only blocks moves that add NEW damage, so Tidy can
    # line parts up on a messy board and simply leaves old touches alone.
    base_leg = check_legality(
        model, dict(base_deltas),
        overlap_tolerance_mm2=overlap_tolerance_mm2,
        wall_tolerance_mm=wall_tolerance_mm,
    )
    base_pairs = {
        tuple(sorted((a, b))) for a, b, _ in base_leg.overlapping_pairs
    }
    base_walls = {u for u, _ in base_leg.wall_violations}
    base_illegal = not base_leg.is_legal
    base_n = len(base_leg.overlapping_pairs) + len(base_leg.wall_violations)

    def _new_damage(rep) -> Tuple[list, list]:
        new_p = [
            t for t in rep.overlapping_pairs
            if tuple(sorted((t[0], t[1]))) not in base_pairs
        ]
        new_w = [t for t in rep.wall_violations if t[0] not in base_walls]
        return new_p, new_w

    # Legality gate: roll back only moves that add fresh damage.
    merged = dict(base_deltas)
    merged.update(candidate)
    merged_leg = check_legality(
        model, merged,
        overlap_tolerance_mm2=overlap_tolerance_mm2,
        wall_tolerance_mm=wall_tolerance_mm,
    )
    new_p, new_w = _new_damage(merged_leg)
    if not new_p and not new_w:
        return candidate, _report(candidate, [], base_illegal, base_n)

    # Greedy rollback: drop the candidate part involved in the worst
    # NEW damage first, re-check, repeat. Pre-existing touches never put
    # a part on this list.
    bad_order: List[str] = []
    seen: Set[str] = set()
    for a, b, _area in sorted(new_p, key=lambda t: -t[2]):
        for u in (a, b):
            if u in candidate and u not in seen:
                seen.add(u)
                bad_order.append(u)
    for u, _d in sorted(new_w, key=lambda t: -t[1]):
        if u in candidate and u not in seen:
            seen.add(u)
            bad_order.append(u)
    for u in sorted(candidate):
        if u not in seen:
            bad_order.append(u)

    current = dict(candidate)
    dropped: List[str] = []
    for u in bad_order:
        if u not in current:
            continue
        trial = {k: v for k, v in current.items() if k != u}
        merged_trial = dict(base_deltas)
        merged_trial.update(trial)
        rep_t = check_legality(
            model, merged_trial,
            overlap_tolerance_mm2=overlap_tolerance_mm2,
            wall_tolerance_mm=wall_tolerance_mm,
        )
        np_t, nw_t = _new_damage(rep_t)
        if not np_t and not nw_t:
            dropped.append(u)
            return trial, _report(trial, dropped, base_illegal, base_n)
        current = trial
        dropped.append(u)
    return {}, _report({}, dropped, base_illegal, base_n)
