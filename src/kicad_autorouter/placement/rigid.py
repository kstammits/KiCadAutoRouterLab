"""Rigid-body projection for placement dynamics.

Footprints are snapped onto their exact rigid shape after each integrator
step via Kabsch SVD (position-based dynamics). Replaces the old Hooke-spring
rigid force, whose stiffness (~1e5x past the fixed step cap's stability
limit) rang as a sustained period-2 slam.
"""

from __future__ import annotations

import math
from typing import List, Tuple

import numpy as np

from ..board_model import Footprint

from .params import MIN_ROT_STEP_RAD


def _project_rigid_poses(
    positions: np.ndarray,
    bodies: List[Tuple[np.ndarray, np.ndarray]],
) -> None:
    """Snap proposed pad positions onto their exact rigid bodies (PBD projection).

    Each body is ``(rows, ref)``: region-local pad rows plus a snapshot of
    those pads' reference shape (taken before the run, i.e. the footprint's
    true geometry). Kabsch-fit of proposed rows vs reference gives the
    rotation; pads are rewritten as ``R @ (ref - ref_center) + prop_center``,
    so shape is exact to float precision by construction while translation
    and rotation stay fully emergent from the soft forces.

    Single-pad bodies never appear (a point is trivially rigid); locked and
    ghost footprints are excluded by the caller.
    """
    for rows, ref in bodies:
        prop = positions[rows]
        if np.any(np.isnan(prop)):
            continue
        ref_center = ref.mean(axis=0)
        prop_center = prop.mean(axis=0)
        h = (ref - ref_center).T @ (prop - prop_center)
        try:
            u, _, vt = np.linalg.svd(h)
            r = vt.T @ u.T
            if np.linalg.det(r) < 0:
                vt[-1, :] *= -1
                r = vt.T @ u.T
        except np.linalg.LinAlgError:
            continue
        positions[rows] = (ref - ref_center) @ r.T + prop_center


def _clamp_rigid_step(
    positions: np.ndarray,
    old_positions: np.ndarray,
    bodies: List[Tuple[np.ndarray, np.ndarray]],
    pad_movable: np.ndarray,
    cap_mm: float,
    max_rot_rad: float,
    min_rot_rad: float = MIN_ROT_STEP_RAD,
) -> None:
    """Bound post-projection rigid motion per body per step (SI safety net).

    The velocity ceiling (vmax) bounds pre-projection pad velocities, but the
    Kabsch snap above can rotate a body whose pads saw differential forces,
    swinging pads well past ``vmax*dt``. Unbounded rotation steps tunnel
    parts into locked neighbors and feed sustained jam churn on dense boards.

    Per movable body: clamp centroid shift to ``cap_mm`` and rotation change
    (vs the pre-step pose) to ``max_rot_rad``, re-posing pads rigidly from
    the old pose. Only half the measured rotation is applied (damped snap:
    rotation stays free over many steps but settles geometrically instead
    of ringing).     ``min_rot_rad`` deadband is applied after halving (currently 0.0 =
    disabled — see ``MIN_ROT_STEP_RAD``: parking micro-rotations was
    measured to wedge pairs that otherwise work apart). Single-pad
    footprints never appear here (trivially rigid; already bounded by the
    velocity cap alone).
    """
    if cap_mm <= 0 or max_rot_rad <= 0:
        return
    for rows, _ref in bodies:
        if len(rows) < 2:
            continue
        if pad_movable is not None and not np.any(pad_movable[rows]):
            continue
        old = old_positions[rows]
        new = positions[rows]
        if np.any(np.isnan(old)) or np.any(np.isnan(new)):
            continue
        old_c = old.mean(axis=0)
        new_c = new.mean(axis=0)
        t = new_c - old_c
        tn = float(np.linalg.norm(t))
        if tn > cap_mm and tn > 0:
            t = t * (cap_mm / tn)
        # Body angle from the first two pads with distinct positions.
        base = old[1:] - old[:1]
        blen = np.linalg.norm(base, axis=1)
        k = int(np.argmax(blen)) + 1 if len(blen) else -1
        if k < 1 or blen[k - 1] <= 1e-9:
            if tn > 0:
                positions[rows] = old + t
            continue
        va = old[k] - old[0]
        vb = new[k] - new[0]
        na = float(np.linalg.norm(va))
        nb = float(np.linalg.norm(vb))
        if na <= 1e-9 or nb <= 1e-9:
            if tn > 0:
                positions[rows] = old + t
            continue
        da = math.atan2(float(va[1]), float(va[0]))
        db = math.atan2(float(vb[1]), float(vb[0]))
        dth = db - da
        while dth > math.pi:
            dth -= 2.0 * math.pi
        while dth < -math.pi:
            dth += 2.0 * math.pi
        # Damped snap: apply only half the measured rotation step. Full
        # Kabsch snaps let per-step torque ring as sustained spin/jitter;
        # halving keeps rotation fully free over many steps while each step
        # settles geometrically toward rest.
        dth *= 0.5
        if abs(dth) < min_rot_rad:
            dth = 0.0
        if dth > max_rot_rad:
            dth = max_rot_rad
        elif dth < -max_rot_rad:
            dth = -max_rot_rad
        ca, sa = math.cos(dth), math.sin(dth)
        rel = old - old_c
        rot = np.empty_like(rel)
        rot[:, 0] = ca * rel[:, 0] - sa * rel[:, 1]
        rot[:, 1] = sa * rel[:, 0] + ca * rel[:, 1]
        positions[rows] = old_c + t + rot


def _compute_footprint_pose_from_pads(
    fp: Footprint,
    pad_positions: np.ndarray,
    pad_indices: List[int],
    global_pad_idx_to_local: dict,
) -> Tuple[float, float, float]:
    """Compute footprint position and angle from its pads' positions.

    Uses Kabsch algorithm (SVD) to find best-fit rigid transform from
    original pad positions to new pad positions.
    """
    if len(pad_indices) < 2:
        # Single pad: just use its position, no rotation
        new_pos = pad_positions[pad_indices[0]]
        return float(new_pos[0]), float(new_pos[1]), fp.angle_deg

    # Original pad positions in true footprint-local coordinates (Y down).
    # ``_to_board`` maps local -> board with R(-angle_deg), so the inverse
    # used here is R(+angle_deg). Building this frame with the same R(-angle)
    # as ``_to_board`` (the old behavior) returned a delta whose sign was
    # opposite to the simulated rigid rotation; ``apply_deltas`` then swung
    # the part the wrong way (2B Q1 appeared to rotate into C6).
    orig_local = []
    for global_idx in pad_indices:
        local_idx = global_pad_idx_to_local[global_idx]
        pad = fp.pads[local_idx]
        rad = math.radians(fp.angle_deg)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        bx = pad.position.x_mm - fp.x_mm
        by = pad.position.y_mm - fp.y_mm
        lx = bx * cos_a - by * sin_a
        ly = bx * sin_a + by * cos_a
        orig_local.append([lx, ly])
    orig_local = np.array(orig_local, dtype=np.float64)

    # New pad positions (in board coordinates)
    new_positions = pad_positions[pad_indices]

    # Check for NaN in new positions
    if np.any(np.isnan(new_positions)):
        # Fall back to original position
        return fp.x_mm, fp.y_mm, fp.angle_deg

    # Center both point sets
    orig_center = np.mean(orig_local, axis=0)
    new_center = np.mean(new_positions, axis=0)
    orig_centered = orig_local - orig_center
    new_centered = new_positions - new_center

    # Kabsch algorithm: find rotation matrix
    H = orig_centered.T @ new_centered
    try:
        U, _, Vt = np.linalg.svd(H)
        R = Vt.T @ U.T

        # Ensure proper rotation (no reflection)
        if np.linalg.det(R) < 0:
            Vt[-1, :] *= -1
            R = Vt.T @ U.T

        # Kabsch returns the row-vector map new = old @ R.T. ``_to_board``
        # applies R(-angle), so the footprint angle is the negative of the
        # matrix angle.
        angle_rad = math.atan2(R[1, 0], R[0, 0])
        angle_deg = math.degrees(-angle_rad)

        # New footprint position = new_center - R @ orig_center
        new_fp_pos = new_center - R @ orig_center

        return float(new_fp_pos[0]), float(new_fp_pos[1]), angle_deg
    except np.linalg.LinAlgError:
        # SVD failed (e.g., collinear points) - fall back to simple centroid + angle from first two pads
        new_x, new_y = float(new_center[0]), float(new_center[1])
        # Estimate angle from first two pads if available
        if len(pad_indices) >= 2:
            i0, i1 = pad_indices[0], pad_indices[1]
            p0_orig = orig_local[0]
            p1_orig = orig_local[1]
            p0_new = new_positions[0]
            p1_new = new_positions[1]
            orig_angle = math.atan2(p1_orig[1] - p0_orig[1], p1_orig[0] - p0_orig[0])
            new_angle = math.atan2(p1_new[1] - p0_new[1], p1_new[0] - p0_new[0])
            # True local -> board is R(-angle): a local vector at orig_angle
            # appears at orig_angle - angle_deg.
            angle_deg = math.degrees(orig_angle - new_angle)
        else:
            angle_deg = fp.angle_deg
        return new_x, new_y, angle_deg


def _rigid_pose_vertices(
    verts: np.ndarray, orig_pos: np.ndarray, curr_pos: np.ndarray
) -> np.ndarray:
    """Pose local courtyard vertices by the rigid motion orig->curr pads.

    Translation plus rotation from the first two pads. Returns verts unchanged
    when the pad mapping degenerates (empty or length mismatch).
    """
    v = np.asarray(verts, dtype=np.float64)
    o = np.asarray(orig_pos, dtype=np.float64)
    c = np.asarray(curr_pos, dtype=np.float64)
    if v.size == 0 or o.shape != c.shape or o.shape[0] == 0:
        return v
    oc = o.mean(axis=0)
    cc = c.mean(axis=0)
    ang = 0.0
    if o.shape[0] >= 2:
        ov = o[1] - o[0]
        cv = c[1] - c[0]
        ang = math.atan2(cv[1], cv[0]) - math.atan2(ov[1], ov[0])
    ca, sa = math.cos(ang), math.sin(ang)
    tx, ty = cc[0] - oc[0], cc[1] - oc[1]
    dx = v[:, 0] - oc[0]
    dy = v[:, 1] - oc[1]
    return np.column_stack((
        dx * ca - dy * sa + oc[0] + tx,
        dx * sa + dy * ca + oc[1] + ty,
    ))
