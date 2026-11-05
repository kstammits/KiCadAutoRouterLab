"""In-dynamics Manhattan snap for the Tidy regime.

Replaces the old geometric tidy post-pass: instead of rotating parts after
the dynamics (and fighting the next run), orientation snap runs *inside*
the simulation as pad-force couples, so the wall law vets every rotation
the same iteration it happens. Legality is enforced by construction, not by
a post-hoc gate.

- Snap is to compass modes: pad-pair axes are inherently 180-degree-
  ambiguous (swapping the pair adds 180), so N/E/S/W collapses to {0, 90}
  and every snap is at most a 45-degree rotation — pin-1-safe, nothing
  ever flips to the opposite side.
- Only outliers rotate (deadband); parts already on a mode feel nothing.
- A diagonal thread far from any mode is left alone: engagement needs
  mode agreement from the neighborhood (see the gate below), so a lone
  part never straightens against its thread and pays in wirelength.
- Position hold is a weak leash to the last recompute pose, not a target —
  spacing stays fully force-driven.
- Locked and ghost bodies are context (alignment references) but never
  receive alignment forces. Ghosts are not references either: fixed walls
  carry no orientation opinion.
- Torque is constant-magnitude (like attraction): proportional torque
  stalls under viscous damping — terminal drift F*dt/c falls below the
  convergence epsilon while the error is still large. The deadband is the
  off-switch, so motion runs at a steady rate until the error is inside it.

Conventions: positions are millimeter-native like the rest of the solver.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Set, Tuple

import numpy as np

# Consensus refresh cadence (iterations): moving targets every step inject
# noise; every-N keeps the torque quasi-static while tracking drift.
ALIGN_RECOMPUTE_ITERS = 25
# Neighbor count cap, matching the old geometric pass.
_ALIGN_NEIGHBOR_K = 6


def _wrap180(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


def _wrap90(deg: float) -> float:
    """Axis error wrapped to [-90, 90].

    Reference axes come from pad pairs, so they are 180-degree-ambiguous
    quantities: 170 deg and -10 deg are the same axis. A plain 180 wrap
    reads that error as -170 and servos the long way around (a near-full
    spin); folding into [-90, 90] always takes the short path.
    """
    d = _wrap180(deg)
    if d > 90.0:
        return d - 180.0
    if d < -90.0:
        return d + 180.0
    return d


def _mode_dist_deg(axis_deg: float, mode_deg: float) -> float:
    """180-wrapped distance from an axis to a compass mode."""
    d = abs(_wrap180(axis_deg - mode_deg))
    return d if d <= 90.0 else 180.0 - d


def _nearest_mode(axis_deg: float) -> float:
    """Nearest compass mode (0 or 90) under 180-degree symmetry."""
    if _mode_dist_deg(axis_deg, 0.0) <= _mode_dist_deg(axis_deg, 90.0):
        return 0.0
    return 90.0


# Mode-agreement fraction: engage only when at least half the in-radius
# neighbors already sit near the target mode, else a lone part straightens
# against its thread and pays in wirelength.
_MODE_AGREE_FRAC = 0.5


def build_align_ref_axes(
    fp_local_to_rows: List[np.ndarray],
    ref_positions: np.ndarray,
) -> Dict[int, Tuple[int, int, float]]:
    """Reference orientation axis per footprint: longest pad pair + its angle.

    Returns {local_idx: (pad_a, pad_b, ref_angle_deg)} with region-level pad
    indices, for footprints with >= 2 pads. The pair is rigid by construction
    (projection keeps bodies exact), so any pair reads the same angle; the
    longest one maximizes the torque lever and minimizes angular noise.
    """
    out: Dict[int, Tuple[int, int, float]] = {}
    for li, rows in enumerate(fp_local_to_rows):
        if rows is None or len(rows) < 2:
            continue
        best = None
        best_d2 = -1.0
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                dx = float(ref_positions[rows[j], 0] - ref_positions[rows[i], 0])
                dy = float(ref_positions[rows[j], 1] - ref_positions[rows[i], 1])
                d2 = dx * dx + dy * dy
                if d2 > best_d2:
                    best_d2 = d2
                    best = (int(rows[i]), int(rows[j]), math.degrees(math.atan2(dy, dx)))
        if best is not None and best_d2 > 1e-12:
            out[li] = best
    return out


def _axis_angle_deg(
    positions: np.ndarray, ia: int, ib: int
) -> Optional[float]:
    dx = float(positions[ib, 0] - positions[ia, 0])
    dy = float(positions[ib, 1] - positions[ia, 1])
    if dx * dx + dy * dy <= 1e-12:
        return None
    return math.degrees(math.atan2(dy, dx))


def _part_poses(
    positions: np.ndarray,
    fp_local_to_rows: List[np.ndarray],
    ghost_local: Set[int],
    ref_axes: Dict[int, Tuple[int, int, float]],
    committed_angles: Dict[int, float],
) -> Tuple[Dict[int, Tuple[float, float]], Dict[int, float]]:
    """Centroids + current axis angles for every non-ghost footprint.

    Shared by the angle snap and the row hold so both read the same pose.
    Angles prefer the live pad-pair axis and fall back to the committed
    footprint angle where no axis exists (single-pad parts).
    """
    centroids: Dict[int, Tuple[float, float]] = {}
    cur_angles: Dict[int, float] = {}
    for li, rows in enumerate(fp_local_to_rows):
        if rows is None or len(rows) == 0 or li in ghost_local:
            continue
        cx = float(np.mean(positions[rows, 0]))
        cy = float(np.mean(positions[rows, 1]))
        centroids[li] = (cx, cy)
        if li in ref_axes:
            ang = _axis_angle_deg(positions, ref_axes[li][0], ref_axes[li][1])
            if ang is not None:
                cur_angles[li] = ang
        elif li in committed_angles:
            cur_angles[li] = committed_angles[li]
    return centroids, cur_angles


def compute_align_targets(
    positions: np.ndarray,
    fp_local_to_rows: List[np.ndarray],
    fp_movable_arr: np.ndarray,
    fp_layers: List[str],
    ghost_local: Set[int],
    committed_angles: Dict[int, float],
    ref_axes: Dict[int, Tuple[int, int, float]],
    *,
    radius_mm: float,
    deadband_deg: float,
    prev_targets: Optional[Dict[int, Tuple[Optional[float], float, float]]] = None,
) -> Dict[int, Tuple[Optional[float], float, float]]:
    """Consensus targets for movable footprints.

    Returns {local_idx: (target_angle_deg | None, anchor_x_mm, anchor_y_mm)}.
    Angle targets go to movable multi-pad outliers; the anchor (current
    centroid) goes to every movable footprint for the position hold.
    Context is every non-ghost footprint (movable, locked, single-pad);
    ghosts never vote.

    Hysteresis: a part engages above the deadband and releases below half
    of it, so an engaged outlier finishes inside the band instead of
    parking on its edge (and boundary chatter can't flip-flop the target).

    Angle targets freeze at first engagement: the mode is read from the
    board as presented, then held. Re-reading the target every recompute
    lets a churning neighborhood whip it around, and parts chase it into
    full spins. Anchors still refresh (position hold is a leash, not a
    lock).

    Mode-agreement gate: snapping a lone part while its thread sits
    elsewhere breaks nets for no board-level gain. Engage only when at
    least half the in-radius neighbors already sit near the target mode
    (within the deadband). No agreement, no torque — the anchor holds.
    Threads far from any mode therefore keep their angle: every member
    sees zero agreement and nobody moves.
    """
    centroids, cur_angles = _part_poses(
        positions, fp_local_to_rows, ghost_local, ref_axes, committed_angles)

    targets: Dict[int, Tuple[Optional[float], float, float]] = {}
    for li, rows in enumerate(fp_local_to_rows):
        if rows is None or len(rows) == 0:
            continue
        if li in ghost_local or not bool(fp_movable_arr[li]):
            continue
        if li not in centroids:
            continue
        ax, ay = centroids[li]
        layer = fp_layers[li] if li < len(fp_layers) else ""
        scored: List[Tuple[float, int]] = []
        for lj, (bx, by) in centroids.items():
            if lj == li:
                continue
            lj_layer = fp_layers[lj] if lj < len(fp_layers) else ""
            if lj_layer != layer:
                continue
            d = math.hypot(bx - ax, by - ay)
            if d <= radius_mm:
                scored.append((d, lj))
        scored.sort(key=lambda t: (t[0], t[1]))
        neighbor_angles = [
            cur_angles[lj] for _, lj in scored[:_ALIGN_NEIGHBOR_K]
            if lj in cur_angles
        ]
        prev_angle: Optional[float] = None
        if prev_targets is not None:
            prev_angle = prev_targets.get(li, (None, 0.0, 0.0))[0]
        angle_target: Optional[float] = None
        if li in ref_axes and li in cur_angles:
            if prev_angle is not None:
                # Engaged: hold the original mode; release only.
                err = _wrap90(prev_angle - cur_angles[li])
                if abs(err) > deadband_deg * 0.5 + 1e-9:
                    angle_target = prev_angle
            elif neighbor_angles:
                mode = _nearest_mode(cur_angles[li])
                err = _wrap90(mode - cur_angles[li])
                if abs(err) > deadband_deg + 1e-9:
                    agree = sum(
                        1 for a in neighbor_angles
                        if _mode_dist_deg(a, mode) <= deadband_deg + 1e-9
                    )
                    if agree >= _MODE_AGREE_FRAC * len(neighbor_angles) - 1e-9:
                        angle_target = mode
        targets[li] = (angle_target, ax, ay)
    return targets


def apply_align_forces(
    positions: np.ndarray,
    fp_local_to_rows: List[np.ndarray],
    fp_movable_arr: np.ndarray,
    ref_axes: Dict[int, Tuple[int, int, float]],
    targets: Dict[int, Tuple[Optional[float], float, float]],
    *,
    angle_k_N: float,
    pos_k_N_m: float,
) -> np.ndarray:
    """Alignment pad-force couples plus weak position leash.

    Orientation error becomes an equal-and-opposite constant-magnitude
    tangential couple on the reference axis pair (zero net force — pure
    torque, which the rigid projection turns into rotation). With Fa = -F*t
    on pa and Fb = +F*t on pb (t = CCW normal of the current axis), the
    axis angle drifts with the sign of F = k*sign(err), i.e. toward the
    target. Engagement lives entirely in target presence (with hysteresis
    at recompute time); here an engaged part simply servos by sign, so no
    release-edge straddle can stall it. Position hold is a net spring
    toward the recompute anchor, spread evenly over the footprint's pads.
    Row-engaged parts carry a row baseline/pitch target in place of the
    leash anchor (merged by the caller), so the same spring does the
    pulling — no separate row force term.
    """
    out = np.zeros_like(positions)
    if angle_k_N <= 0.0 and pos_k_N_m <= 0.0:
        return out
    for li, (target_angle, anchor_x, anchor_y) in targets.items():
        if li < 0 or li >= len(fp_local_to_rows):
            continue
        if not bool(fp_movable_arr[li]):
            continue
        rows = fp_local_to_rows[li]
        if rows is None or len(rows) == 0:
            continue
        if angle_k_N > 0.0 and target_angle is not None and li in ref_axes:
            ia, ib, _ref = ref_axes[li]
            cur = _axis_angle_deg(positions, ia, ib)
            if cur is not None:
                err = _wrap90(float(target_angle) - cur)
                if abs(err) > 1e-9:
                    f_mag = float(angle_k_N) * (1.0 if err > 0.0 else -1.0)
                    dx = float(positions[ib, 0] - positions[ia, 0])
                    dy = float(positions[ib, 1] - positions[ia, 1])
                    dist = math.hypot(dx, dy)
                    if dist > 1e-9:
                        ux, uy = dx / dist, dy / dist
                        tx, ty = -uy, ux
                        out[ia, 0] -= f_mag * tx
                        out[ia, 1] -= f_mag * ty
                        out[ib, 0] += f_mag * tx
                        out[ib, 1] += f_mag * ty
        if pos_k_N_m > 0.0:
            cx = float(np.mean(positions[rows, 0]))
            cy = float(np.mean(positions[rows, 1]))
            fx = -float(pos_k_N_m) * (cx - anchor_x) * 1e-3
            fy = -float(pos_k_N_m) * (cy - anchor_y) * 1e-3
            if math.isfinite(fx) and math.isfinite(fy):
                per = 1.0 / len(rows)
                out[rows, 0] += fx * per
                out[rows, 1] += fy * per
    return out


# Row hold (Tidy phase 2): shared baselines + even pitch, in-dynamics.
#
# Detection runs per recompute over movable non-ghost footprints:
# - angled parts must sit within the deadband of a compass mode, and all
#   angled members of one row must share that mode (a row reads one way);
#   diagonal threads are excluded, never straightened by position forces
# - y-band clustering (tolerance _ROW_BAND_MM), x-chained by consecutive
#   centroid gaps within the align radius, minimum _ROW_MIN_PARTS members,
#   single layer only (opposite-side parts stack by design)
# - baseline = median of the tighter body-edge set (top/bottom from the
#   committed courtyard half-extents, swapped when the part sits near
#   mode 90); pitch = median centroid gap with cumulative targets
#   recentered on the row mean
#
# Engagement is deadband-gated (baseline / pitch), frozen at engagement,
# released inside half deadbands or when the part leaves the neighborhood
# (target farther than the align radius — ejected parts are not dragged
# back). Engaged row targets override the leash anchor, so the existing
# position-hold spring does the pulling at align_pos_k_N_m: no separate
# force term, and the wall law still vets every move the same iteration.
_ROW_MIN_PARTS = 3
_ROW_BAND_MM = 3.0
_ROW_BASE_DEADBAND_MM = 1.0
_ROW_PITCH_DEADBAND_MM = 1.5


def courtyard_half_extents(fp) -> Tuple[float, float]:
    """Body half extents (hw, hh) in mm from the courtyard outline.

    Falls back to the pad-copper hull when no outline exists (pad-hull
    fallback, same rule as legality). Zero-area outlines yield 0.0, which
    the row hold reads as centroid (no edge information).
    """
    xs: List[float] = []
    ys: List[float] = []
    try:
        segments = fp.courtyard or ()
    except AttributeError:
        segments = ()
    for seg in segments:
        try:
            a, b = seg[0], seg[1]
            xs.append(float(a.x_mm))
            xs.append(float(b.x_mm))
            ys.append(float(a.y_mm))
            ys.append(float(b.y_mm))
        except (AttributeError, TypeError, IndexError):
            continue
    if not xs:
        try:
            pads = fp.pads or ()
        except AttributeError:
            pads = ()
        for p in pads:
            try:
                xs.append(float(p.position.x_mm))
                ys.append(float(p.position.y_mm))
            except AttributeError:
                continue
    if not xs:
        return (0.0, 0.0)
    return (max(0.0, (max(xs) - min(xs)) / 2.0),
            max(0.0, (max(ys) - min(ys)) / 2.0))


def _median(values: List[float]) -> float:
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def _detect_rows(
    order: List[int],
    centroids: Dict[int, Tuple[float, float]],
    cur_angles: Dict[int, float],
    ref_axes: Dict[int, Tuple[int, int, float]],
    fp_layers: List[str],
    *,
    radius_mm: float,
    deadband_deg: float,
) -> List[List[int]]:
    """Group movable candidates into rows (x-ordered member lists)."""
    by_layer: Dict[str, List[int]] = {}
    for li in order:
        if li not in centroids:
            continue
        if li in ref_axes:
            if li not in cur_angles:
                continue
            if _mode_dist_deg(cur_angles[li], _nearest_mode(cur_angles[li])) > deadband_deg + 1e-9:
                continue
        layer = fp_layers[li] if li < len(fp_layers) else ""
        by_layer.setdefault(layer, []).append(li)
    rows: List[List[int]] = []
    for members in by_layer.values():
        members.sort(key=lambda li: centroids[li][1])
        band: List[int] = []
        band_y = 0.0
        bands: List[List[int]] = []
        for li in members:
            cy = centroids[li][1]
            if band and cy - band_y > _ROW_BAND_MM + 1e-9:
                bands.append(band)
                band = []
            if not band:
                band_y = cy
            band.append(li)
        if band:
            bands.append(band)
        for group in bands:
            group.sort(key=lambda li: centroids[li][0])
            chain: List[int] = []
            for li in group:
                if chain and centroids[li][0] - centroids[chain[-1]][0] > radius_mm + 1e-9:
                    if len(chain) >= _ROW_MIN_PARTS:
                        rows.append(chain)
                    chain = []
                chain.append(li)
            if len(chain) >= _ROW_MIN_PARTS:
                rows.append(chain)
    # One mode per row: a mixed-orientation band is not a row.
    gated: List[List[int]] = []
    for chain in rows:
        modes = {
            _nearest_mode(cur_angles[li])
            for li in chain
            if li in ref_axes and li in cur_angles
        }
        if len(modes) <= 1:
            gated.append(chain)
    return gated


def compute_row_targets(
    positions: np.ndarray,
    fp_local_to_rows: List[np.ndarray],
    fp_movable_arr: np.ndarray,
    fp_layers: List[str],
    ghost_local: Set[int],
    committed_angles: Dict[int, float],
    ref_axes: Dict[int, Tuple[int, int, float]],
    extents: Dict[int, Tuple[float, float]],
    *,
    radius_mm: float,
    deadband_deg: float,
    prev_targets: Optional[Dict[int, Tuple[float, float]]] = None,
) -> Dict[int, Tuple[float, float]]:
    """Row-hold targets for movable footprints: {local_idx: (tx, ty)} mm.

    Frozen (baseline, pitch) targets per engaged part; hysteresis and
    freeze semantics mirror the angle snap. Parts outside any row keep no
    target (their leash anchor is untouched by the caller).
    """
    centroids, cur_angles = _part_poses(
        positions, fp_local_to_rows, ghost_local, ref_axes, committed_angles)
    order = [
        li for li, rows in enumerate(fp_local_to_rows)
        if rows is not None and len(rows) > 0
        and li not in ghost_local and bool(fp_movable_arr[li])
    ]
    chains = _detect_rows(
        order, centroids, cur_angles, ref_axes, fp_layers,
        radius_mm=radius_mm, deadband_deg=deadband_deg)
    member_of: Dict[int, List[int]] = {}
    for chain in chains:
        for li in chain:
            member_of[li] = chain

    targets: Dict[int, Tuple[float, float]] = {}
    for li in order:
        if li not in centroids:
            continue
        cx, cy = centroids[li]
        _, hh = _eff_hw_hh(li, extents, cur_angles, ref_axes)
        prev: Optional[Tuple[float, float]] = None
        if prev_targets is not None:
            prev = prev_targets.get(li)
        if prev is not None:
            # Engaged: hold the frozen frame; release inside half
            # deadbands, or drop when ejected clear of the neighborhood.
            tx, ty = prev
            if math.hypot(cx - tx, cy - ty) > radius_mm + 1e-9:
                continue
            # Body half-height cancels: edge error == centroid error for a
            # frozen frame, whichever side the baseline was read from.
            if (abs(cx - tx) <= _ROW_PITCH_DEADBAND_MM * 0.5 + 1e-9
                    and abs(cy - ty) <= _ROW_BASE_DEADBAND_MM * 0.5 + 1e-9):
                continue
            targets[li] = (tx, ty)
        elif li in member_of:
            chain = member_of[li]
            bottoms = [centroids[m][1] - _eff_hw_hh(
                m, extents, cur_angles, ref_axes)[1] for m in chain]
            tops = [centroids[m][1] + _eff_hw_hh(
                m, extents, cur_angles, ref_axes)[1] for m in chain]
            spread_b = max(bottoms) - min(bottoms)
            spread_t = max(tops) - min(tops)
            if spread_b <= spread_t:
                baseline = _median(bottoms)
                ty = baseline + hh
                edge = cy - hh
            else:
                baseline = _median(tops)
                ty = baseline - hh
                edge = cy + hh
            xs = sorted(centroids[m][0] for m in chain)
            gaps = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
            pitch = _median(gaps)
            raw = [xs[0] + i * pitch for i in range(len(xs))]
            shift = sum(xs) / len(xs) - sum(raw) / len(raw)
            rank = xs.index(cx) if cx in xs else 0
            # Duplicate x positions are degenerate for ranking; hold x.
            tx = raw[rank] + shift if xs.count(cx) == 1 else cx
            if (abs(edge - baseline) > _ROW_BASE_DEADBAND_MM + 1e-9
                    or abs(cx - tx) > _ROW_PITCH_DEADBAND_MM + 1e-9):
                targets[li] = (tx, ty)
    return targets


def _eff_hw_hh(
    li: int,
    extents: Dict[int, Tuple[float, float]],
    cur_angles: Dict[int, float],
    ref_axes: Dict[int, Tuple[int, int, float]],
) -> Tuple[float, float]:
    """Effective half extents, swapping axes when the part sits near mode 90."""
    hw, hh = extents.get(li, (0.0, 0.0))
    if li in ref_axes and li in cur_angles:
        if abs(_wrap90(cur_angles[li] - 90.0)) < 45.0:
            return hh, hw
    return hw, hh
