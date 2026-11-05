"""In-dynamics orientation consensus for the Tidy regime.

Replaces the old geometric tidy post-pass: instead of rotating parts after
the dynamics (and fighting the next run), orientation consensus runs *inside*
the simulation as pad-force couples, so the wall law vets every rotation the
same iteration it happens. Legality is enforced by construction, not by a
post-hoc gate.

- Consensus is local (k-nearest same-layer neighbors): diagonals hold,
  nothing snaps to axes or rows.
- Only outliers rotate (deadband); parts near consensus feel nothing.
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
    """
    n = len(fp_local_to_rows)
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
        angle_target: Optional[float] = None
        if (
            neighbor_angles
            and li in ref_axes
            and li in cur_angles
        ):
            med = _circular_median_180(neighbor_angles)
            err = _wrap180(med - cur_angles[li])
            release = deadband_deg * 0.5 if (
                prev_targets is not None
                and prev_targets.get(li, (None, 0.0, 0.0))[0] is not None
            ) else deadband_deg
            if abs(err) > release + 1e-9:
                angle_target = med
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
                err = _wrap180(float(target_angle) - cur)
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
