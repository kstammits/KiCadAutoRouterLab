"""SI placement dynamics: parameters, proposals, and the runner interface.

The runner implements a vectorized numpy dynamics simulation at the PAD level
in SI units (meters, newtons, kilograms, seconds): semi-implicit Euler with
viscous damping, per-footprint masses from ``mass.py``, an adaptive-dt energy
guard, and position-based rigid projection. After each step, every movable
footprint's pads are Kabsch-snapped onto their exact rigid shape, and pad
velocities are re-derived from the projected displacement so the projection
can't inject phantom energy. This allows through-hole components to rotate
organically when forces on their F.Cu and B.Cu pads differ.

Geometry arrays stay millimeter-native (shapely/Kabsch are unit-agnostic
linear algebra); each force law converts to newtons at evaluation.
Normative formulas: ``docs/si_force_laws.md``.

Deltas are keyed by footprint UUID (dx_mm, dy_mm, dangle_deg) because refs may
be duplicated or missing; locked footprints never appear in ``deltas`` (they are
fixed anchors).

Supports multi-board region simulation: footprints are grouped by board outline
region (from Edge.Cuts) and each region is simulated independently with its own
bounding box and timestep state.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field, fields
from typing import Collection, Dict, List, Optional, Set, Tuple

import numpy as np
from shapely.geometry import Polygon, Point
from shapely.ops import nearest_points, polygonize, unary_union

from .board_model import (
    BoardModel, BoardRegion, Footprint, Pad, Segment,
    is_ground_net, is_power_net_effective,
)
from .mass import courtyard_bbox_area_m2, footprint_mass_kg


# Power rails are NOT excluded from placement: on a two-layer board their
# wirelength matters, and excluding them strands decoupling caps (both pads
# on power/GND → zero attraction edges → repulsion-only drift). Instead,
# power edges are fanout-scaled (ka/(fanout-1) per edge, so a rail's total
# pull per pad stays ~constant however many parts share it) instead of
# dragging every powered part together at full ka. GND stays excluded as a
# general attractor (copper pour provides connectivity anywhere) — except
# for decoupling-cap pads, which need a local anchor on both sides.


@dataclass(frozen=True)
class PlacementParams:
    """SI placement-dynamics parameters (v2 set; newtons, meters, seconds).

    Geometry inside the solver stays millimeter-native (shapely/Kabsch are
    unit-agnostic linear algebra); every force law converts to SI at
    evaluation and the integrator runs fully in m/kg/s. Normative formulas:
    ``docs/si_force_laws.md``. Legacy v1 fields are gone on purpose —
    calibration (``scripts/calibrate_si.py``) learns these ranges empirically.
    """

    max_iterations: int = 1000
    # Pair repulsion strength (Coulomb-like k/d^2, d in meters).
    # Calibrated 2026-10-15 (scripts/calibrate_si.py, magnet synthetics):
    # 2e-9 puts the isolated-pair equilibrium sqrt(kr/ka) at sub-mm for
    # ka ~ 1e-2, i.e. repulsion only matters inside contact range and the
    # wall owns contact response. Larger kr fights clustering.
    repulsion_k_N_m2: float = 2e-9
    # Net attraction spring pull (constant magnitude, newtons).
    # Legalize regime default (placed-board polish): weak enough that wall
    # contact (wall_k_N below) wins at touching. Scattered-start clustering
    # wants ~0.04-0.06 (see two-stage recipe in calibration notes); the
    # engine default stays on the safe/legalize side.
    attraction_k_N: float = 8e-3
    # Courtyard contact strength (newtons; elastic push-out on collision).
    # One penalty per footprint pair (summed sub-areas, strongest
    # direction); calibrated so wall beats ~2 attraction edges at contact.
    wall_k_N: float = 8e-2
    # Overlap-area normalization (m^2): F = k_wall * (1 + area / area_ref).
    wall_area_ref_m2: float = 4e-6
    # Near-miss halo reach (meters).
    wall_halo_m: float = 0.006
    # Boundary repulsion strength (newtons).
    boundary_k_N: float = 1e-2
    # Ghost attraction scale (newtons); file "TARGET,ka" values weight it.
    ghost_k_N: float = 5e-5
    # Viscous damping coefficient (N*s/m). Calibrated 0.10-0.15: lighter
    # churns on dense boards, heavier crawls (magnet tightening stalls).
    damping_Ns_m: float = 0.12
    # Dry-friction deadband (N of total footprint force): below this the
    # whole footprint freezes for the step.
    friction_N: float = 1e-6
    # Timestep (s); adaptive guard halves on energy rise, regrows on fall.
    dt_s: float = 1e-3
    # Timestep floor (s) and ceiling (s) for the adaptive guard. Floor 2e-4
    # (not 1e-5): the guard's halve/regrow asymmetry ratchets dt to the
    # floor under oscillation, where tiny steps fake convergence (runs
    # "settle" while forces are still large). Ceiling 2e-3 bounds growth
    # so dense-board steps stay accurate.
    dt_min_s: float = 2e-4
    dt_max_s: float = 2e-3
    # Speed ceiling (m/s). Enforced on velocities AND, via the rigid-step
    # clamp, on post-projection body motion (translation <= vmax*dt,
    # rotation <= 5 deg/step), so tunneling is impossible by construction.
    vmax_m_s: float = 1.0
    # Settled threshold: max pad displacement per step (meters).
    convergence_eps_m: float = 2e-5
    # Footprint mass fallback slope (kg/m^2 of courtyard area).
    mass_density_kg_m2: float = 3.0
    # Minimum part mass (kg).
    mass_floor_kg: float = 1e-9
    # Power-rail attraction trim: multiplies the fanout-scaled power term
    # (dimensionless, unchanged from v1).
    power_attraction_scale: float = 1.0
    # Decoupling-cap edge multiplier (dimensionless, unchanged from v1).
    # Also gates the decap GND-side exception: 0 removes decap GND edges.
    decap_boost: float = 4.0
    # Kill-switch for power participation. False drops all power edges.
    include_power_nets: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PlacementParams":
        """Build params from a JSON-style dict; missing keys take defaults and
        unknown keys are ignored (forward compatibility with newer param sets,
        and silent drop of legacy v1 keys)."""
        known = {f.name for f in fields(cls)}
        params = cls(**{name: data[name] for name in known if name in data})
        _validate(params)
        return params


def _validate(params: PlacementParams) -> None:
    numeric = (
        "repulsion_k_N_m2",
        "attraction_k_N",
        "wall_k_N",
        "wall_area_ref_m2",
        "wall_halo_m",
        "boundary_k_N",
        "ghost_k_N",
        "damping_Ns_m",
        "friction_N",
        "dt_s",
        "dt_min_s",
        "dt_max_s",
        "vmax_m_s",
        "convergence_eps_m",
        "mass_density_kg_m2",
        "mass_floor_kg",
        "power_attraction_scale",
        "decap_boost",
    )
    for name in numeric:
        value = getattr(params, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a number")
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
        if value < 0:
            raise ValueError(f"{name} must be >= 0")
    for name in ("dt_s", "dt_min_s", "dt_max_s", "vmax_m_s", "wall_area_ref_m2"):
        if getattr(params, name) <= 0:
            raise ValueError(f"{name} must be > 0")
    if params.dt_min_s > params.dt_s:
        raise ValueError("dt_min_s must be <= dt_s")
    if params.dt_s > params.dt_max_s:
        raise ValueError("dt_s must be <= dt_max_s")
    if params.power_attraction_scale < 0:
        raise ValueError("power_attraction_scale must be >= 0")
    if params.decap_boost < 0:
        raise ValueError("decap_boost must be >= 0")
    if not isinstance(params.include_power_nets, bool):
        raise ValueError("include_power_nets must be a boolean")
    if isinstance(params.max_iterations, bool) or not isinstance(
        params.max_iterations, int
    ):
        raise ValueError("max_iterations must be an integer")
    if params.max_iterations < 1:
        raise ValueError("max_iterations must be >= 1")


# Trajectory-frame viz precision: 1 micron / 0.01 deg is far below the
# convergence epsilon (~0.01mm) and invisible on SVG, but cuts JSON payload
# ~30% vs full float repr.
FRAME_XY_DECIMALS = 3
FRAME_ANGLE_DECIMALS = 2
FRAME_DEFAULT_MAX = 100


def _round_frame_deltas(
    frame: Dict[str, Tuple[float, float, float]],
    xy_decimals: int = FRAME_XY_DECIMALS,
    angle_decimals: int = FRAME_ANGLE_DECIMALS,
) -> Dict[str, Tuple[float, float, float]]:
    """Round one frame's (dx, dy, da) deltas for compact JSON output."""
    return {
        uuid: (
            round(float(dx), xy_decimals),
            round(float(dy), xy_decimals),
            round(float(da), angle_decimals),
        )
        for uuid, (dx, dy, da) in frame.items()
    }


def _round_sig3(x: float) -> float:
    """Round to 3 significant figures."""
    if x == 0.0 or not math.isfinite(x):
        return 0.0
    return round(x, 2 - int(math.floor(math.log10(abs(x)))))


def _round_force_components(
    comps: Dict[str, Tuple[float, float]],
) -> Dict[str, Tuple[float, float]]:
    """Round force component values to 3 significant figures."""
    return {
        cause: (_round_sig3(fx), _round_sig3(fy))
        for cause, (fx, fy) in comps.items()
    }


def _thin_frames(
    frames: List[Dict[str, Tuple[float, float, float]]], max_frames: int
) -> List[Dict[str, Tuple[float, float, float]]]:
    """Uniformly downsample frames to at most max_frames, always keeping last.

    Deterministic (round-half-away via int(i * (n-1) / (m-1))); no-ops when
    already within budget.
    """
    n = len(frames)
    if max_frames < 1:
        return []
    if n <= max_frames:
        return frames
    if max_frames == 1:
        return [frames[-1]]
    return [frames[int(i * (n - 1) / (max_frames - 1))] for i in range(max_frames - 1)] + [
        frames[-1]
    ]


@dataclass(frozen=True)
class PlacementProposal:
    """Result of a placement run: per-footprint deltas keyed by UUID + diagnostics.

    Deltas are (dx_mm, dy_mm, dangle_deg) for each movable footprint.
    Forces are (Fx, Fy) in arbitrary units for each movable footprint.
    Frames (opt-in via run_placement(record_frames=True)) hold per-sampled-
    iteration (dx, dy, da) deltas vs the committed start positions for the
    SVG motion player; already rounded + downsampled to max_frames.
    frames[0] is always the committed starting pose (all-zero deltas =
    completed iteration 0); frames[k] is the pose after k*stride completed
    iterations, except the last frame which is always the final pose after
    ``iterations`` completed iterations.

    force_frames (parallel to frames): per-sampled-iteration force breakdown
    for each movable footprint. Each frame is {uuid: {cause: (fx, fy)}} with
    causes in {"repulsion", "attraction", "courtyard", "boundary", "ghost"}.
    Values rounded to 3 significant figures for compact JSON.
    """

    deltas: Dict[str, Tuple[float, float, float]]
    iterations: int
    final_max_disp_mm: float
    elapsed_s: float
    params: dict
    forces: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    force_components: Dict[str, Dict[str, Tuple[float, float]]] = field(
        default_factory=dict
    )
    frames: List[Dict[str, Tuple[float, float, float]]] = field(default_factory=list)
    force_frames: List[Dict[str, Dict[str, Tuple[float, float]]]] = field(default_factory=list)
    frame_stride: int = 1


def _collect_pad_nodes(model: BoardModel) -> Tuple[List[Pad], List[int], Dict[str, List[int]], Dict[int, int], List[bool]]:
    """Collect all pads with net connections from both real and ghost footprints.

    Returns:
        - pads: list of Pad objects with net_name
        - pad_to_fp_idx: list mapping pad index -> footprint index (negative for ghost footprints)
        - fp_uuid_to_pad_indices: dict mapping footprint UUID -> list of global pad indices
        - global_pad_idx_to_local: dict mapping global pad index -> local pad index within footprint
        - pad_is_ghost: list of bool indicating if pad belongs to a ghost footprint
    """
    pads = []
    pad_to_fp_idx = []
    fp_uuid_to_pad_indices = {}
    global_pad_idx_to_local = {}
    pad_is_ghost = []

    # Real footprints (positive indices)
    for fp_idx, fp in enumerate(model.footprints):
        if not fp.uuid:
            continue
        fp_pad_indices = []
        for local_pad_idx, pad in enumerate(fp.pads):
            if pad.net_name is None:
                continue
            global_pad_idx = len(pads)
            fp_pad_indices.append(global_pad_idx)
            global_pad_idx_to_local[global_pad_idx] = local_pad_idx
            pads.append(pad)
            pad_to_fp_idx.append(fp_idx)
            pad_is_ghost.append(False)
        if fp_pad_indices:
            fp_uuid_to_pad_indices[fp.uuid] = fp_pad_indices

    # Ghost footprints (negative indices: -(ghost_idx + 1))
    for ghost_idx, fp in enumerate(model.ghost_footprints):
        if not fp.uuid:
            continue
        fp_pad_indices = []
        for local_pad_idx, pad in enumerate(fp.pads):
            if pad.net_name is None:
                continue
            global_pad_idx = len(pads)
            fp_pad_indices.append(global_pad_idx)
            global_pad_idx_to_local[global_pad_idx] = local_pad_idx
            pads.append(pad)
            pad_to_fp_idx.append(-(ghost_idx + 1))  # negative = ghost
            pad_is_ghost.append(True)
        if fp_pad_indices:
            fp_uuid_to_pad_indices[fp.uuid] = fp_pad_indices

    return pads, pad_to_fp_idx, fp_uuid_to_pad_indices, global_pad_idx_to_local, pad_is_ghost


def _find_decap_pad_indices(
    pads: List[Pad],
    pad_to_fp_idx: List[int],
    fp_ref_by_idx: Dict[int, str],
    extra_power: Collection[str] = (),
) -> Set[int]:
    """Pad indices belonging to decoupling caps in this pad set.

    A decap is a footprint whose ref starts with "C", contributing exactly
    2 netted pads here, with one side on a power rail and the other on
    ground (effective-power membership, so user-marked rails count).
    Ghost footprints are never decaps. Used to (a) boost decap-incident edges
    and (b) admit the decap's GND-side edges that are otherwise excluded
    (GND pours connect anywhere, so general GND attraction would only
    clump parts).
    """
    fp_to_rows: Dict[int, List[int]] = {}
    for i, fp_idx in enumerate(pad_to_fp_idx):
        if fp_idx < 0:
            continue  # ghost footprint
        fp_to_rows.setdefault(fp_idx, []).append(i)
    decap: Set[int] = set()
    for fp_idx, rows in fp_to_rows.items():
        if len(rows) != 2:
            continue
        ref = fp_ref_by_idx.get(fp_idx, "")
        if not ref.upper().startswith("C"):
            continue
        nets = [pads[i].net_name for i in rows]
        sides = sorted(
            "power" if is_power_net_effective(n, extra_power) else ("ground" if is_ground_net(n) else "signal")
            for n in nets
        )
        if sides == ["ground", "power"]:
            decap.update(rows)
    return decap


def _build_net_edges_pad_level(
    pads: List[Pad], pad_to_fp_idx: List[int],
    decap_pad_indices: Optional[Set[int]] = None,
    power_scale: float = 1.0,
    decap_boost: float = 4.0,
    include_power: bool = True,
    extra_power: Collection[str] = (),
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Build edge index arrays for pad pairs sharing the same net.

    Returns (src_indices, dst_indices, weights) where each pair appears
    once (i < j). Only connects pads on different footprints. Weights are
    per-edge ka multipliers applied by the attraction kernel:

    - signal nets: 1.0 (unchanged behavior);
    - power nets (keyword match or user-marked ``extra_power``):
      power_scale / (fanout - 1) — a rail shared by many
      parts pulls each pad about as hard in total as one signal edge,
      so global rails minimize wirelength without collapsing the board;
    - ground nets: excluded (0 edges) unless at least one endpoint is a
      decap pad (see _find_decap_pad_indices), in which case the edge is
      admitted at the decap-boosted power rate. Pours connect GND
      anywhere, so general GND attraction has no placement value.
    - any edge incident to a decap pad additionally gets x decap_boost.

    With include_power=False, power edges are dropped (decap power-side
    edges go with them); the decap GND-side exception is gated by
    decap_boost > 0 instead.
    """
    decap_set = decap_pad_indices or set()

    net_to_pads: Dict[str, List[int]] = {}
    for i, pad in enumerate(pads):
        if pad.net_name:
            net_to_pads.setdefault(pad.net_name, []).append(i)

    src_list: List[int] = []
    dst_list: List[int] = []
    weight_list: List[float] = []
    for net_name, pad_indices in net_to_pads.items():
        fanout = len(pad_indices)
        if fanout < 2:
            continue
        net_is_power = is_power_net_effective(net_name, extra_power)
        net_is_ground = is_ground_net(net_name)
        if net_is_power and not include_power:
            continue
        if net_is_power:
            base = power_scale / (fanout - 1)
        elif net_is_ground:
            # Admitted only for decap-incident pairs below; base is the
            # fanout-scaled rate and the decap multiplier applies once
            # via the shared incident check (not squared here).
            base = power_scale / (fanout - 1)
        else:
            base = 1.0
        if base == 0.0:
            continue
        for a in range(len(pad_indices)):
            for b in range(a + 1, len(pad_indices)):
                i, j = pad_indices[a], pad_indices[b]
                # Only connect pads on different footprints
                if pad_to_fp_idx[i] == pad_to_fp_idx[j]:
                    continue
                if net_is_ground and i not in decap_set and j not in decap_set:
                    continue  # GND pour: no general attraction
                w = base
                if i in decap_set or j in decap_set:
                    w *= decap_boost
                if w == 0.0:
                    continue
                src_list.append(i)
                dst_list.append(j)
                weight_list.append(w)

    if not src_list:
        return np.array([], dtype=int), np.array([], dtype=int), np.array([], dtype=float)

    order = sorted(range(len(src_list)), key=lambda k: (src_list[k], dst_list[k]))
    return (
        np.array([src_list[k] for k in order], dtype=int),
        np.array([dst_list[k] for k in order], dtype=int),
        np.array([weight_list[k] for k in order], dtype=float),
    )


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
    ghost footprints are excluded by the caller. Replaces the old
    Hooke-spring rigid force, whose stiffness (~1e5x past the fixed step
    cap's stability limit) rang as a sustained period-2 slam (2026-10-11).
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


# Angular deadband (rad/step): per-step rotations below this are numerical
# / gradient creep, not torque response — drop them so orientation doesn't
# random-walk over long runs. Deliberately NOT a PlacementParams slider:
# it is a noise floor, not a tuning knob. Genuine torque (degrees/step)
# passes through untouched.
MIN_ROT_STEP_RAD = math.radians(0.1)


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
    swinging pads well past ``vmax*dt`` (observed: 7mm leap against a 3mm
    cap; -40 deg spins on 2-pad parts). Unbounded rotation steps tunnel
    parts into locked neighbors and feed sustained jam churn on dense
    boards (DCCF C14 vs RV3, 2026-10-15: 2.8 -> 21mm^2 in one step).

    Per movable body: clamp centroid shift to ``cap_mm`` and rotation change
    (vs the pre-step pose) to ``max_rot_rad``, re-posing pads rigidly from
    the old pose. Rotations below ``min_rot_rad`` are dropped outright
    (angular deadband against creep spin). Deterministic function of the
    trajectory, so the no-cooling invariant holds. Single-pad footprints
    never appear here (trivially rigid; already bounded by the velocity
    cap alone).
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
    global_pad_idx_to_local: Dict[int, int],
) -> Tuple[float, float, float]:
    """Compute footprint position and angle from its pads' positions.

    Uses Kabsch algorithm (SVD) to find best-fit rigid transform from
    original pad positions to new pad positions.
    """
    if len(pad_indices) < 2:
        # Single pad: just use its position, no rotation
        new_pos = pad_positions[pad_indices[0]]
        return float(new_pos[0]), float(new_pos[1]), fp.angle_deg

    # Original pad positions in footprint-local coordinates
    orig_local = []
    for global_idx in pad_indices:
        local_idx = global_pad_idx_to_local[global_idx]
        pad = fp.pads[local_idx]
        # Transform from board to local (inverse of _to_board; no mirroring
        # on either layer — verified against pcbnew, see board_model docstring)
        rad = math.radians(fp.angle_deg)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        bx = pad.position.x_mm - fp.x_mm
        by = pad.position.y_mm - fp.y_mm
        lx = bx * cos_a + by * sin_a
        ly = -bx * sin_a + by * cos_a
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

        # Extract angle from rotation matrix
        angle_rad = math.atan2(R[1, 0], R[0, 0])
        angle_deg = math.degrees(angle_rad)

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
            angle_deg = math.degrees(new_angle - orig_angle + math.radians(fp.angle_deg))
        else:
            angle_deg = fp.angle_deg
        return new_x, new_y, angle_deg


def _contact_weights(proj: np.ndarray) -> np.ndarray:
    """Per-pad share of one pair-push, from projection onto push direction.

    ``proj`` is ``((pad - centroid) . u) / radius`` per pad (~[-1, 1];
    negative on the contact side, which takes more: ``w = 1 - proj``).
    Weights sum exactly to 1, so the footprint total is conserved and no
    force is created or destroyed -- only its moment arm changes. A single
    pad gets exactly 1.
    """
    w = 1.0 - np.asarray(proj, dtype=np.float64)
    s = float(w.sum()) if len(w) else 1.0
    if math.isfinite(s) and abs(s) > 1e-9:
        return w / s
    n = len(w)
    if n > 0:
        return np.ones_like(w) / n
    return w



# ============================================================
# Analytical contact detection (Phase 3: SAT-based collision)
# ============================================================

class ShapeType:
    """Shape classification for analytical collision."""
    EMPTY = "empty"
    RECTANGLE = "rectangle"
    CIRCLE = "circle"
    POLYGON = "polygon"


def _classify_rectangle(verts: np.ndarray) -> Optional[dict]:
    """Classify vertices as rectangle, return params or None.
    
    Args:
        verts: (n, 2) array of vertices, may or may not be closed (last == first)
    
    Returns:
        dict with center, half_extents, angle, or None if not rectangle
    """
    if len(verts) < 4:
        return None
    
    # Handle closed ring (last == first)
    if np.allclose(verts[0], verts[-1]):
        corners = verts[:-1]
    else:
        corners = verts
    
    if len(corners) != 4:
        return None
    
    center = corners.mean(axis=0)
    
    # Edge vectors
    edges = np.roll(corners, -1, axis=0) - corners
    edge_lengths = np.linalg.norm(edges, axis=1)
    
    # For rectangle, opposite edges have equal length (2 unique lengths).
    # Use relative tolerance (0.1%) to handle floating-point drift after
    # transformation and hand-drawn courtyards with slight edge mismatches.
    def group_lengths(lengths, rel_tol=1e-3):
        groups = []
        for l in lengths:
            for g in groups:
                if abs(l - g) / g <= rel_tol:
                    break
            else:
                groups.append(l)
        return np.array(groups)
    
    unique_lengths = group_lengths(edge_lengths)
    if len(unique_lengths) != 2:
        return None
    
    # Find angle from first edge of first unique length
    idx = np.where(np.isclose(edge_lengths, unique_lengths[0], rtol=1e-3))[0][0]
    edge0 = edges[idx]
    angle = math.atan2(edge0[1], edge0[0])
    
    # Normalize angle to [-pi/2, pi/2] (180 degrees symmetry)
    angle = (angle + math.pi/2) % math.pi - math.pi/2
    
    # Half extents in local frame
    ca, sa = math.cos(angle), math.sin(angle)
    v = corners - center
    local_x = v[:, 0] * ca + v[:, 1] * sa
    local_y = -v[:, 0] * sa + v[:, 1] * ca
    hx = float(abs(local_x).max())
    hy = float(abs(local_y).max())
    
    # Verify right angles
    angles = np.arctan2(edges[:, 1], edges[:, 0])
    angle_diffs = np.diff(angles)
    angle_diffs = (angle_diffs + np.pi) % (2 * np.pi) - np.pi
    if not np.allclose(np.abs(angle_diffs), np.pi / 2, atol=0.05):
        return None
    
    return {
        "type": ShapeType.RECTANGLE,
        "center": (float(center[0]), float(center[1])),
        "half_extents": (hx, hy),
        "angle": angle,
    }


def _classify_circle(poly: Polygon) -> Optional[dict]:
    """Classify polygon as circle, return params or None."""
    if poly.is_empty:
        return None
    
    coords = list(poly.exterior.coords)
    n_verts = len(coords) - 1  # exclude closure
    
    if n_verts < 8:
        return None
    
    centroid = np.array([poly.centroid.x, poly.centroid.y])
    verts = np.array(coords[:-1])
    radii = np.linalg.norm(verts - centroid, axis=1)
    mean_r = float(radii.mean())
    std_r = float(radii.std())
    
    # Circle: low variation in radius (< 5%)
    if mean_r > 0 and std_r / mean_r < 0.05:
        return {
            "type": ShapeType.CIRCLE,
            "center": (float(centroid[0]), float(centroid[1])),
            "radius": mean_r,
        }
    return None


def _classify_shape(poly: Polygon) -> tuple[str, dict]:
    """Classify polygon shape for analytical collision.
    
    Returns: (shape_type, params_dict)
    shape_type: "rectangle", "circle", "polygon", or "empty"
    params_dict: parameters for analytical collision
    """
    if poly.is_empty:
        return ShapeType.EMPTY, {}
    
    # Try rectangle first
    coords = list(poly.exterior.coords)
    verts = np.array(coords, dtype=np.float64)
    rect = _classify_rectangle(verts)
    if rect:
        return ShapeType.RECTANGLE, rect
    
    # Try circle
    circle = _classify_circle(poly)
    if circle:
        return ShapeType.CIRCLE, circle
    
    return ShapeType.POLYGON, {"polygon": poly}


# SAT collision for two rectangles
def _sat_rect_rect(rect1: dict, rect2: dict) -> tuple[bool, np.ndarray, float]:
    """SAT collision for two rectangles.
    
    Returns: (overlaps, normal, penetration_depth)
    normal points from rect1 to rect2 (pushes rect1 away from rect2).
    Returns True for touching (penetration = 0) as well as overlapping.
    For non-overlapping, returns the separation axis normal (min overlap axis).
    """
    c1 = np.array(rect1["center"])
    c2 = np.array(rect2["center"])
    hx1, hy1 = rect1["half_extents"]
    hx2, hy2 = rect2["half_extents"]
    a1 = rect1["angle"]
    a2 = rect2["angle"]
    
    ca1, sa1 = math.cos(a1), math.sin(a1)
    ca2, sa2 = math.cos(a2), math.sin(a2)
    R1 = np.array([[ca1, -sa1], [sa1, ca1]])
    R2 = np.array([[ca2, -sa2], [sa2, ca2]])
    
    # Local axes for both rectangles (4 axes total)
    axes = np.array([
        [ca1, sa1],    # rect1 x-axis
        [-sa1, ca1],   # rect1 y-axis
        [ca2, sa2],    # rect2 x-axis
        [-sa2, ca2],   # rect2 y-axis
    ])
    
    min_overlap = float('inf')
    best_axis = None
    
    for axis in axes:
        # Project rect1 onto axis
        r1_proj = abs(hx1 * (R1[0] @ axis)) + abs(hy1 * (R1[1] @ axis))
        # Project rect2 onto axis
        r2_proj = abs(hx2 * (R2[0] @ axis)) + abs(hy2 * (R2[1] @ axis))
        # Distance between centers along axis
        center_dist = abs((c2 - c1) @ axis)
        
        overlap = r1_proj + r2_proj - center_dist
        
        if overlap < min_overlap:
            min_overlap = overlap
            best_axis = axis
    
    # No valid axis found (shouldn't happen)
    if best_axis is None:
        return False, np.zeros(2), 0.0
    
    # Normal points from rect1 to rect2
    if ((c2 - c1) @ best_axis) < 0:
        best_axis = -best_axis
    
    overlaps = min_overlap >= -1e-10
    return overlaps, best_axis, max(0.0, min_overlap)


def _circle_circle_contact(c1: dict, c2: dict) -> tuple[bool, np.ndarray, float]:
    """Circle-circle collision."""
    center1 = np.array(c1["center"])
    center2 = np.array(c2["center"])
    r1 = c1["radius"]
    r2 = c2["radius"]
    
    diff = center2 - center1
    dist = np.linalg.norm(diff)
    
    if dist < 1e-10:
        return True, np.array([1.0, 0.0]), r1 + r2  # Arbitrary direction
    
    normal = diff / dist
    penetration = r1 + r2 - dist
    overlaps = penetration >= -1e-10
    return overlaps, normal, max(0.0, penetration)


def _rect_circle_contact(rect: dict, circle: dict) -> tuple[bool, np.ndarray, float]:
    """Rectangle-circle collision using SAT.
    
    Normal points from rect to circle.
    For non-overlapping, returns the separation axis normal (direction to closest point).
    """
    c_rect = np.array(rect["center"])
    c_circle = np.array(circle["center"])
    hx, hy = rect["half_extents"]
    angle = rect["angle"]
    r = circle["radius"]
    
    # Rotate circle center into rect local space
    ca, sa = math.cos(angle), math.sin(angle)
    R = np.array([[ca, -sa], [sa, ca]])
    local_circle = R.T @ (c_circle - c_rect)
    
    # Clamp to rectangle bounds
    clamped = np.array([
        np.clip(local_circle[0], -hx, hx),
        np.clip(local_circle[1], -hy, hy),
    ])
    
    # Distance from circle center to closest point on rect
    diff_local = local_circle - clamped
    dist = np.linalg.norm(diff_local)
    
    if dist < 1e-10:
        # Circle center inside rect - use axis of minimum penetration
        px = hx - abs(local_circle[0])
        py = hy - abs(local_circle[1])
        if px < py:
            normal_local = np.array([1.0 if local_circle[0] >= 0 else -1.0, 0.0])
            penetration = px
        else:
            normal_local = np.array([0.0, 1.0 if local_circle[1] >= 0 else -1.0])
            penetration = py
    else:
        normal_local = diff_local / dist
        penetration = r - dist
    
    # Transform normal back to world space
    normal_world = R @ normal_local
    
    # Ensure normal points from rect to circle
    if (normal_world @ (c_circle - c_rect)) < 0:
        normal_world = -normal_world
    
    overlaps = penetration >= -1e-10
    return overlaps, normal_world, max(0.0, penetration)



def _board_bbox(model: BoardModel) -> Tuple[float, float, float, float]:
    """Compute board bounding box from Edge.Cuts."""
    if not model.edge_cuts and not model.edge_arcs:
        return -1e6, 1e6, -1e6, 1e6
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")
    for seg in model.edge_cuts:
        for p in seg:
            min_x = min(min_x, p.x_mm)
            max_x = max(max_x, p.x_mm)
            min_y = min(min_y, p.y_mm)
            max_y = max(max_y, p.y_mm)
    for arc in model.edge_arcs:
        for p in (arc.start, arc.mid, arc.end):
            min_x = min(min_x, p.x_mm)
            max_x = max(max_x, p.x_mm)
            min_y = min(min_y, p.y_mm)
            max_y = max(max_y, p.y_mm)
    return min_x, max_x, min_y, max_y


def _boundary_edge_push(dist_m: float, threshold_m: float, k_b_N: float) -> float:
    """Inward push from one region edge in newtons: bounded linear falloff.

    Returns 0 past the threshold, rising linearly to 2*k_b exactly at the
    edge; already-outside parts get an ~k_b saturating pull back in.
    Deliberately singularity-free: the old ``kb*2*(t-d)/(d+eps)`` form
    blew up to ~3.5e7 on a part sitting ~0.03mm inside the region edge
    (2026-10-10, DCCF R11) and only the per-iteration step cap masked it.
    """
    if dist_m < 0:
        d = abs(dist_m)
        return k_b_N * d / (d + 1e-9)
    if dist_m < threshold_m:
        return k_b_N * 2.0 * (threshold_m - dist_m) / threshold_m
    return 0.0


def _courtyard_outline_polygon(segments: Tuple[Segment, ...]) -> Optional[Polygon]:
    """Build the courtyard outline polygon from raw segments, any order.

    ``shapely.ops.polygonize`` assembles closed rings regardless of segment
    order/orientation. The old approach (concatenating segment endpoints in
    file order) only forms a valid ring for head-to-tail-ordered files; on
    DCCF 66 of 87 footprints with courtyards came out self-intersecting
    (e.g. every RV fader has a reversed top-edge segment, putting a
    diagonal spike through the ring) and silently degraded to the
    netted-pad bbox. Returns the largest closed ring by area, or None when
    nothing closes (open polylines, degenerate input) — callers keep the
    pad-bbox fallback for that case.

    Segment endpoints may have sub-micron floating-point gaps (e.g. -1.55
    vs -1.550001) that prevent polygonize from closing rings. We snap
    coordinates to a 1μm grid via ``set_precision(grid_size=0.001)`` before
    polygonizing to close these without creating false rings at
    near-junctions (buffer-based approaches do).
    """
    from shapely import set_precision
    from shapely.geometry import LineString

    linestrings: list[LineString] = []
    for a, b in segments or ():
        p = (a.x_mm, a.y_mm)
        q = (b.x_mm, b.y_mm)
        if math.hypot(q[0] - p[0], q[1] - p[1]) > 1e-9:
            linestrings.append(LineString([p, q]))
    if not linestrings:
        return None

    # Snap coordinates to 1μm grid to close floating-point gaps.
    # 0.001mm << manufacturing tolerance, << routing grid (0.1mm).
    linestrings = [set_precision(ls, grid_size=0.001) for ls in linestrings]

    try:
        rings = [r for r in polygonize(linestrings) if r.is_valid and r.area > 0]
    except Exception:
        return None
    if not rings:
        return None
    if len(rings) == 1:
        return rings[0]
    # Disjoint loops (body + separate island outlines): the sim poses a
    # single rigid outline per footprint, so keep the largest. Nested loops
    # merge first so a drawn hole doesn't split the outline.
    try:
        merged = unary_union(rings)
    except Exception:
        return max(rings, key=lambda p: p.area)
    if isinstance(merged, Polygon):
        return merged if merged.is_valid and merged.area > 0 else None
    parts = [
        g for g in getattr(merged, "geoms", [])
        if isinstance(g, Polygon) and g.is_valid and g.area > 0
    ]
    if not parts:
        return None
    return max(parts, key=lambda p: p.area)


def _rigid_pose_vertices(
    verts: np.ndarray, orig_pos: np.ndarray, curr_pos: np.ndarray
) -> np.ndarray:
    """Pose local courtyard vertices by the rigid motion orig->curr pads.

    Translation plus rotation from the first two pads (same rule the old
    per-vertex Python loop used; numpy form is bit-identical). Returns
    verts unchanged when the pad mapping degenerates (empty or length
    mismatch) — in that case there is no observable motion to pose by.
    Shared by the sim's per-iteration posing and _transform_vertices so
    the two entry points cannot drift apart.
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


def _footprint_courtyard_polygon(fp: Footprint, pad_positions: Optional[np.ndarray] = None, pad_indices: Optional[List[int]] = None) -> Polygon:
    """Build a shapely Polygon from footprint's courtyard segments.

    If no courtyard, fall back to pad bounding box.

    Args:
        fp: The footprint
        pad_positions: Optional (n_pads, 2) array of current pad positions for this region
        pad_indices: Optional list of local pad indices for this footprint in the region's pad array
    """
    def _get_courtyard_vertices() -> list[tuple[float, float]]:
        outline = _courtyard_outline_polygon(fp.courtyard)
        if outline is None:
            return []
        return list(outline.exterior.coords)

    def _get_original_pad_positions() -> np.ndarray:
        """Get original pad positions in board coordinates from footprint."""
        if not fp.pads:
            return np.empty((0, 2), dtype=np.float64)
        return np.array([(p.position.x_mm, p.position.y_mm) for p in fp.pads], dtype=np.float64)

    def _transform_vertices(vertices: list[tuple[float, float]], orig_pos: np.ndarray, curr_pos: np.ndarray) -> list[tuple[float, float]]:
        """Apply rigid transformation from original to current pad positions."""
        posed = _rigid_pose_vertices(
            np.asarray(vertices, dtype=np.float64), orig_pos, curr_pos
        )
        return [tuple(map(float, row)) for row in posed.tolist()]

    # Try courtyard first
    courtyard_vertices = _get_courtyard_vertices()
    if courtyard_vertices:
        if pad_positions is not None and pad_indices and len(pad_indices) > 0:
            # Transform courtyard to current position
            orig_pad_pos = _get_original_pad_positions()
            curr_pad_pos = pad_positions[pad_indices]
            if len(orig_pad_pos) == len(curr_pad_pos):
                courtyard_vertices = _transform_vertices(courtyard_vertices, orig_pad_pos, curr_pad_pos)
        if len(courtyard_vertices) >= 3:
            try:
                poly = Polygon(courtyard_vertices)
                if poly.is_valid and poly.area > 0:
                    return poly
            except Exception:
                pass
    
    # Fallback: pad bounding box at current position
    if pad_positions is not None and pad_indices:
        xs = pad_positions[pad_indices, 0]
        ys = pad_positions[pad_indices, 1]
        min_x, max_x = float(xs.min()), float(xs.max())
        min_y, max_y = float(ys.min()), float(ys.max())
    elif fp.pads:
        xs = [p.position.x_mm for p in fp.pads]
        ys = [p.position.y_mm for p in fp.pads]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
    else:
        # Last resort: point at footprint position
        return Polygon([
            (fp.x_mm - 0.5, fp.y_mm - 0.5),
            (fp.x_mm + 0.5, fp.y_mm - 0.5),
            (fp.x_mm + 0.5, fp.y_mm + 0.5),
            (fp.x_mm - 0.5, fp.y_mm + 0.5),
        ])
    # Add small margin
    margin = 0.1
    return Polygon([
        (min_x - margin, min_y - margin),
        (max_x + margin, min_y - margin),
        (max_x + margin, max_y + margin),
        (min_x - margin, max_y + margin),
    ])


# This is the new run_placement function implementation
# It will be inserted into placement.py

# Helper function for per-region simulation
def _run_region_simulation(
    region_idx: int,
    region: "BoardRegion",
    region_pad_indices: List[int],
    region_pads: List[Pad],
    region_pad_to_fp_idx: List[int],
    region_fp_uuid_to_pad_indices: Dict[str, List[int]],
    region_global_pad_idx_to_local: Dict[int, int],
    region_pad_is_ghost: List[bool],
    model: "BoardModel",
    params: "PlacementParams",
    movable_uuids: Optional[Set[str]],
    record_frames: bool = False,
    frame_stride: int = 1,
    extra_power_nets: Collection[str] = (),
) -> Tuple[
    Dict[str, Tuple[float, float, float]],
    Dict[str, Tuple[float, float]],
    int,
    Dict[str, Dict[str, Tuple[float, float]]],
    List[Dict[str, Tuple[float, float, float]]],
    List[Dict[str, Dict[str, Tuple[float, float]]]],
]:
    """Run force-spring simulation for a single board region.

    Returns:
        - deltas: {uuid: (dx, dy, da)} for footprints in this region
        - forces: {uuid: (Fx, Fy)} for footprints in this region
        - iterations: number of iterations run
        - force_components: {uuid: {cause: (Fx, Fy)}}
        - frames: per-sampled-iteration (dx, dy, da) deltas vs start
          (empty unless record_frames; rounded, stride-sampled, final
          frame always appended). frames[0] is the starting pose
          (iteration 0, all-zero deltas); frames[k] follows k*stride
          completed iterations; the last frame is the final pose.
        - force_frames: parallel to frames; per-footprint force breakdown
          {uuid: {cause: (fx, fy)}} with causes in {"repulsion", "attraction",
          "courtyard", "boundary", "ghost"}, values rounded to 3 sig figs.
    """
    from .board_model import BoardRegion

    # Extract region bounds
    min_x, max_x, min_y, max_y = region.bbox

    # Filter pads for this region
    n_pads = len(region_pads)
    if n_pads == 0:
        return {}, {}, 0, {}, [], []

    # Build pad positions array
    pad_positions = np.zeros((n_pads, 2), dtype=np.float64)
    for i, pad in enumerate(region_pads):
        pad_positions[i, 0] = pad.position.x_mm
        pad_positions[i, 1] = pad.position.y_mm

    # Determine movable pads (real footprints only; ghosts are always fixed)
    fp_movable = {}
    for fp in model.footprints:
        if not fp.uuid or fp.locked:
            fp_movable[fp.uuid] = False
            continue
        # Check if footprint belongs to this region
        if model.footprint_region.get(fp.uuid) != region_idx:
            fp_movable[fp.uuid] = False
            continue
        if movable_uuids is not None and fp.uuid not in movable_uuids:
            fp_movable[fp.uuid] = False
        else:
            fp_movable[fp.uuid] = True

    # Ghost footprints are never movable
    for fp in model.ghost_footprints:
        if fp.uuid and model.footprint_region.get(fp.uuid) == region_idx:
            fp_movable[fp.uuid] = False

    pad_movable = np.array([
        (not region_pad_is_ghost[i] and fp_movable.get(
            model.footprints[region_pad_to_fp_idx[i]].uuid, False
        )) if region_pad_to_fp_idx[i] >= 0 else False
        for i in range(n_pads)
    ], dtype=bool)

    # SI masses: per-footprint kg broadcast to pads (static for the run).
    # Ghost pads get the table/fallback mass too (harmless: immovable pads
    # never integrate; it only keeps F/m finite).
    _uuid_to_mass_kg: Dict[str, float] = {}
    for _fp in list(model.footprints) + list(model.ghost_footprints):
        if _fp.uuid and _fp.uuid not in _uuid_to_mass_kg:
            _uuid_to_mass_kg[_fp.uuid] = footprint_mass_kg(
                _fp.footprint_id,
                courtyard_bbox_area_m2(_fp),
                params.mass_density_kg_m2,
                params.mass_floor_kg,
            )
    _fallback_mass_kg = max(params.mass_floor_kg, 1e-12)

    def _pad_footprint(_i: int):
        _idx = region_pad_to_fp_idx[_i]
        try:
            if _idx >= 0:
                return model.footprints[_idx]
            return model.ghost_footprints[-_idx - 1]
        except IndexError:
            return None

    pad_mass_kg = np.array([
        _uuid_to_mass_kg.get(
            _pad_footprint(i).uuid if _pad_footprint(i) is not None else "", _fallback_mass_kg)
        for i in range(n_pads)
    ], dtype=np.float64)
    pad_mass_kg = np.maximum(pad_mass_kg, _fallback_mass_kg)

    if not pad_movable.any():
        return {}, {}, 0, {}, [], []

    # Net attraction edges for this region. Same builder as the tests pin
    # (layer-blind by design: cross-layer electrical nearness keeps
    # working through springs; courtyard collision is what honors layers).
    # Power rails participate fanout-scaled; GND only via decap pads.
    _ref_by_idx: Dict[int, str] = {}
    for _i, _fp_idx in enumerate(region_pad_to_fp_idx):
        if _fp_idx >= 0 and _fp_idx not in _ref_by_idx:
            _ref_by_idx[_fp_idx] = model.footprints[_fp_idx].ref
    _decap_pads = _find_decap_pad_indices(region_pads, region_pad_to_fp_idx, _ref_by_idx, extra_power_nets)
    src_idx, dst_idx, edge_weights = _build_net_edges_pad_level(
        region_pads, region_pad_to_fp_idx,
        decap_pad_indices=_decap_pads,
        power_scale=params.power_attraction_scale,
        decap_boost=params.decap_boost,
        include_power=params.include_power_nets,
        extra_power=extra_power_nets,
    )
    has_net_edges = len(src_idx) > 0

    # Build mapping from footprint UUID to local pad indices for boundary checks
    region_fp_to_pad_indices = {}
    for fp_uuid, pad_indices in region_fp_uuid_to_pad_indices.items():
        region_fp_to_pad_indices[fp_uuid] = pad_indices

    # SI integrator state is initialized just before the loop (velocities,
    # timestep). No cooling schedule: the adaptive-dt guard reacts to the
    # trajectory's energy, not the iteration budget.
    iteration = 0

    # Build list of footprints in this region with courtyards (real + ghost)
    fp_uuids = []
    fp_movable_list = []
    fp_pad_indices = {}  # uuid -> list of local pad indices in region
    pad_to_fp_local = []  # pad_idx -> local fp index in fp_uuids

    # Real footprints (padless graphical parts carry no pads, so they can
    # neither move nor anchor forces — exclude them from collision).
    for fp in model.footprints:
        if not fp.uuid or model.footprint_region.get(fp.uuid) != region_idx:
            continue
        if not fp.pads:
            continue
        fp_uuids.append(fp.uuid)
        fp_movable_list.append(fp_movable.get(fp.uuid, False))
        if fp.uuid in region_fp_uuid_to_pad_indices:
            fp_pad_indices[fp.uuid] = region_fp_uuid_to_pad_indices[fp.uuid]

    # Ghost footprints (always non-movable)
    for fp in model.ghost_footprints:
        if not fp.uuid or model.footprint_region.get(fp.uuid) != region_idx:
            continue
        fp_uuids.append(fp.uuid)
        fp_movable_list.append(False)  # Ghosts never movable
        if fp.uuid in region_fp_uuid_to_pad_indices:
            fp_pad_indices[fp.uuid] = region_fp_uuid_to_pad_indices[fp.uuid]

    # O(1) lookup: uuid -> local index in fp_uuids
    fp_uuid_to_local_idx = {uuid: i for i, uuid in enumerate(fp_uuids)}

    for i in range(len(region_pads)):
        fp_idx = region_pad_to_fp_idx[i]
        if fp_idx >= 0:
            fp_uuid = model.footprints[fp_idx].uuid
        else:
            # Ghost footprint: negative index = -(ghost_idx + 1)
            ghost_idx = -fp_idx - 1
            if ghost_idx < len(model.ghost_footprints):
                fp_uuid = model.ghost_footprints[ghost_idx].uuid
            else:
                fp_uuid = ""
        pad_to_fp_local.append(fp_uuid_to_local_idx.get(fp_uuid, -1))

    fp_movable_arr = np.array(fp_movable_list, dtype=bool)

    # Static pad->footprint index array + per-footprint pad rows. The pad
    # layout never changes during a run; the loop below only moves values.
    _pad_fp_idx = np.array(pad_to_fp_local, dtype=int)
    fp_local_to_rows: List[np.ndarray] = [
        np.where(_pad_fp_idx == _li)[0] for _li in range(len(fp_uuids))
    ]

    # ---- Per-run courtyard shape cache ----
    # A courtyard's LOCAL geometry never changes — each iteration only applies
    # a rigid pose (translate + one rotation). The old code rebuilt every
    # footprint's shapely Polygon from scratch every iteration (~70% of force
    # time on tube111) and linear-scanned for each footprint object. Cache the
    # static local data once here; per iteration only poses update (numpy),
    # and a vectorized bounding-circle broadphase culls far pairs before any
    # shapely call. Force math for surviving pairs is unchanged.
    fp_by_uuid: Dict[str, Footprint] = {}
    for _fp in model.footprints:
        if _fp.uuid:
            fp_by_uuid[_fp.uuid] = _fp
    for _fp in model.ghost_footprints:
        if _fp.uuid:
            fp_by_uuid[_fp.uuid] = _fp

    def _local_shape(_fp: Footprint) -> dict:
        """Static local courtyard data mirroring _footprint_courtyard_polygon.

        Returns dict with:
        - "body": body courtyard (kind, verts, layer)
        - "pins": list of pin courtyards for THT/MP pads (center, radius, layer="*.Cu")
        - "pad_idx": live pad indices
        - "orig_pos": original positions of ALL pads (for rigid pose)
        """
        if _fp is None:
            return {"body": {"kind": "empty", "empty": True}, "pins": [], "pad_idx": np.array([], dtype=int), "orig_pos": np.array([], dtype=np.float64)}
        
        # Body courtyard from F.CrtYd/B.CrtYd
        outline = _courtyard_outline_polygon(_fp.courtyard)
        if outline is not None:
            body = {
                "kind": "courtyard",
                "empty": False,
                "verts": np.array(outline.exterior.coords, dtype=np.float64),
                "layer": _fp.layer,
            }
        elif _fp.pads:
            body = {"kind": "bbox", "empty": False, "layer": _fp.layer}
        else:
            _cx, _cy = _fp.x_mm, _fp.y_mm
            body = {
                "kind": "point",
                "empty": False,
                "verts": np.array([
                    [_cx - 0.5, _cy - 0.5], [_cx + 0.5, _cy - 0.5],
                    [_cx + 0.5, _cy + 0.5], [_cx - 0.5, _cy + 0.5],
                ], dtype=np.float64),
                "layer": _fp.layer,
            }
        
        # Pin courtyards for THT/MP pads (exist on both layers)
        pins = []
        for pad in _fp.pads:
            if pad.is_through_hole or pad.pad_type == "np_thru_hole":
                # Pad radius = half the diagonal + 0.5mm clearance
                pad_r = 0.5 * math.hypot(pad.size_mm[0], pad.size_mm[1]) + 0.5
                pins.append({
                    "center": (pad.position.x_mm, pad.position.y_mm),
                    "radius": pad_r,
                    "layer": "*.Cu",
                })
        
        _pad_idx = np.array(fp_pad_indices.get(_fp.uuid, []), dtype=int)
        _orig_pos = np.array(
            [(p.position.x_mm, p.position.y_mm) for p in _fp.pads],
            dtype=np.float64,
        ) if _fp.pads else np.array([], dtype=np.float64)
        
        return {
            "body": body,
            "pins": pins,
            "pad_idx": _pad_idx,
            "orig_pos": _orig_pos,
        }

    cy_cache: Dict[str, dict] = {}
    for _uuid in fp_uuids:
        _fp = fp_by_uuid.get(_uuid)
        cy_cache[_uuid] = _local_shape(_fp)

    # Same-footprint mask: no repulsion between pads of one footprint.
    # Vectorized form of the old O(n^2) Python loop (identical result:
    # same local fp index, non-negative so ghosts never match each other).
    _fp_col = np.array(pad_to_fp_local, dtype=int)
    same_fp_mask = (
        (_fp_col[:, None] == _fp_col[None, :])
        & (_fp_col[:, None] >= 0)
        & ~np.eye(len(region_pads), dtype=bool)
    )

    # Build ghost attraction edges: (ghost_anchor, target_pad_indices, ka)
    ghost_attractions = []
    for fp in model.ghost_footprints:
        if not fp.uuid or model.footprint_region.get(fp.uuid) != region_idx:
            continue
        if not fp.ghost_attractions:
            continue
        # Ghost anchor is the footprint's position
        ghost_anchor = np.array([fp.x_mm, fp.y_mm], dtype=np.float64)
        for target_ref, ka_attr in fp.ghost_attractions:
            # Resolve target Ref to UUID and pad indices
            target_fp = model.by_ref.get(target_ref)
            if target_fp is None:
                continue
            if not target_fp.uuid:
                continue
            # Check target is in same region
            if model.footprint_region.get(target_fp.uuid) != region_idx:
                continue
            # Get target pad indices in this region
            target_pad_indices = fp_pad_indices.get(target_fp.uuid, [])
            if not target_pad_indices:
                continue
            ghost_attractions.append((ghost_anchor, target_pad_indices, ka_attr))

    positions = pad_positions.copy()
    prev_positions = pad_positions.copy()

    def _compute_total_force(
        pos: np.ndarray,
        n_pads: int,
        pad_movable: np.ndarray,
        params: PlacementParams,
        has_net_edges: bool,
        src_idx: np.ndarray,
        dst_idx: np.ndarray,
        edge_weights: np.ndarray,
        ghost_attractions: list,
        same_fp_mask: np.ndarray = None,
        return_components: bool = False,
        poly_cache: Optional[Dict[str, dict]] = None,
        cache_rebuild_threshold_mm: float = 0.01,
        cache_rebuild_threshold_rad: float = 0.0,
    ):
        """Compute total force on each pad at given positions.

        With ``return_components`` also returns the per-cause breakdown
        ``{cause: (n_pads, 2)}`` for ``repulsion``/``attraction``/
        ``ghost``/``courtyard``/``boundary``. The sim loop leaves it off
        (it only needs the total); the end-of-run reporting recompute turns
        it on for the forces overlay. Rigidity needs no force term:
        footprints are snapped onto their exact shape by
        ``_project_rigid_poses`` after each step.
        """
        # Repulsion: all-pairs Coulomb k/d^2 in SI (d in meters).
        # Geometry arrays stay millimeter-native; only the law converts.
        diff = pos[:, None, :] - pos[None, :, :]  # (n, n, 2), mm
        dist_sq = np.sum(diff * diff, axis=2)
        eps = 1e-6
        np.fill_diagonal(dist_sq, eps)
        dist_sq = np.maximum(dist_sq, eps)
        dist = np.sqrt(dist_sq)
        dist_sq_m = dist_sq * 1e-6  # mm^2 -> m^2
        force_mag = params.repulsion_k_N_m2 / np.maximum(dist_sq_m, 1e-12)
        np.fill_diagonal(force_mag, 0.0)
        if same_fp_mask is not None:
            force_mag[same_fp_mask] = 0.0  # No repulsion between pads of same footprint
        force_vec = diff * (force_mag[:, :, None] / dist[:, :, None])
        repulsion = np.sum(force_vec, axis=1)

        # Attraction: constant-magnitude pull along net edges (newtons).
        # src is pulled toward dst (+unit) and dst toward src (-unit), so
        # the pair settles where this balances Coulomb repulsion
        # (d = sqrt(k_rep/k_attr) for an isolated pad pair). Per-edge weights
        # scale k_attr: 1.0 for signal, fanout-scaled for power, decap-boosted
        # for decap-incident edges (see _build_net_edges_pad_level).
        attraction = np.zeros((n_pads, 2), dtype=np.float64)
        if has_net_edges:
            diff_edge = pos[dst_idx] - pos[src_idx]
            dist_edge = np.linalg.norm(diff_edge, axis=1, keepdims=True)
            dist_edge = np.maximum(dist_edge, 1e-6)
            unit = diff_edge / dist_edge
            force_mag_edge = (params.attraction_k_N * edge_weights)[:, None]
            force_vec_edge = unit * force_mag_edge
            np.add.at(attraction, src_idx, force_vec_edge)
            np.add.at(attraction, dst_idx, -force_vec_edge)

        # Ghost attraction: centroid-to-centroid springs from ghost to target.
        # File "TARGET,ka" values are dimensionless weights scaling ghost_k_N.
        ghost_attr_force = np.zeros((n_pads, 2), dtype=np.float64)
        if ghost_attractions:
            for ghost_anchor, target_pad_indices, ka_attr in ghost_attractions:
                if not target_pad_indices:
                    continue
                # Compute target centroid from current pad positions
                target_centroid = np.mean(pos[target_pad_indices], axis=0)
                # Vector from ghost anchor to target centroid
                diff_attr = target_centroid - ghost_anchor
                dist_attr = np.linalg.norm(diff_attr)
                if dist_attr > 1e-6:
                    unit_attr = diff_attr / dist_attr
                    force_mag_attr = params.ghost_k_N * ka_attr
                    # Force on target points TOWARD ghost (opposite to unit_attr which points ghost->target)
                    force_vec_attr = -unit_attr * force_mag_attr
                    # Apply equally to all target pads (distribute)
                    force_per_pad = force_vec_attr / len(target_pad_indices)
                    for pad_idx in target_pad_indices:
                        ghost_attr_force[pad_idx] += force_per_pad
                    # Ghost is fixed - no reaction force applied to ghost pads

        force = repulsion + attraction + ghost_attr_force

        # Initialized here so the components breakdown below is well-defined
        # even when there are no footprints with courtyards in this region.
        n_pads_local = pos.shape[0]
        poly_force = np.zeros((n_pads_local, 2), dtype=np.float64)
        boundary_force = np.zeros((n_pads_local, 2), dtype=np.float64)

# Polygon-based courtyard collision from cached shapes.
        # Per iteration: build body polygons (posed) + pin polygons (circles at transformed positions)
        n_fp = len(fp_uuids)
        if n_fp:
            # Build body polygons + compute body centroids/radii for broadphase
            body_polys: dict[str, Polygon] = {}
            body_centroids: list[list[float]] = []
            body_radii: list[float] = []
            body_layers: list[str] = []
            
            # Build pin polygons + compute pin centers/radii for broadphase
            # pins_data[fp_uuid] = list of (Polygon, center_xy, radius)
            pins_data: dict[str, list[tuple[Polygon, tuple[float, float], float]]] = {}
            
            for fp_uuid in fp_uuids:
                e = cy_cache[fp_uuid]
                body = e["body"]
                pins = e["pins"]
                pad_idx = e["pad_idx"]
                orig_pos = e["orig_pos"]
                
                # Get current pad positions for this footprint
                cp = pos[pad_idx] if len(pad_idx) == len(orig_pos) and len(pad_idx) > 0 else None
                
                # Compute current centroid and angle for cache invalidation
                if cp is not None and len(cp) >= 2:
                    curr_centroid = cp.mean(axis=0)
                    # Compute angle from first two pads
                    v = cp[1] - cp[0]
                    curr_angle = math.atan2(v[1], v[0])
                elif cp is not None and len(cp) == 1:
                    curr_centroid = cp[0]
                    curr_angle = 0.0
                else:
                    curr_centroid = np.array([0.0, 0.0])
                    curr_angle = 0.0
                
                # Check cache
                cached = poly_cache.get(fp_uuid)
                rebuild = True
                if cached is not None:
                    last_centroid = cached.get("last_centroid")
                    last_angle = cached.get("last_angle")
                    if last_centroid is not None and last_angle is not None:
                        dist = math.hypot(curr_centroid[0] - last_centroid[0], curr_centroid[1] - last_centroid[1])
                        angle_diff = abs(curr_angle - last_angle)
                        # Normalize angle difference
                        angle_diff = min(angle_diff, 2 * math.pi - angle_diff)
                        if dist <= cache_rebuild_threshold_mm and angle_diff <= cache_rebuild_threshold_rad:
                            rebuild = False
                
                if rebuild:
                    # Build body polygon (posed)
                    kind = body["kind"]
                    if kind == "courtyard" and cp is not None:
                        body_poly = Polygon(_rigid_pose_vertices(body["verts"], orig_pos, cp))
                    elif kind == "courtyard":
                        body_poly = Polygon(body["verts"])
                    elif kind == "bbox":
                        if cp is not None and len(cp) > 0:
                            xs = cp[:, 0]
                            ys = cp[:, 1]
                            x0, x1 = float(xs.min()), float(xs.max())
                            y0, y1 = float(ys.min()), float(ys.max())
                        else:
                            _fp0 = fp_by_uuid.get(fp_uuid)
                            xs = [p.position.x_mm for p in _fp0.pads] if _fp0 and _fp0.pads else [0.0]
                            ys = [p.position.y_mm for p in _fp0.pads] if _fp0 and _fp0.pads else [0.0]
                            x0, x1 = min(xs), max(xs)
                            y0, y1 = min(ys), max(ys)
                        m = 0.1
                        body_poly = Polygon([
                            (x0 - m, y0 - m), (x1 + m, y0 - m),
                            (x1 + m, y1 + m), (x0 - m, y1 + m),
                        ])
                    elif kind == "point":
                        body_poly = Polygon(body["verts"])
                    else:
                        body_poly = Polygon()
                    body_polys[fp_uuid] = body_poly
                    
                    # Build pin polygons (circles at transformed pad positions)
                    fp_pins = []
                    if pins and cp is not None:
                        for pin in pins:
                            local_cx, local_cy = pin["center"]
                            pin_vert = np.array([[local_cx, local_cy]], dtype=np.float64)
                            posed_center = _rigid_pose_vertices(pin_vert, orig_pos, cp)[0]
                            pin_poly = Point(posed_center[0], posed_center[1]).buffer(pin["radius"])
                            fp_pins.append((pin_poly, (posed_center[0], posed_center[1]), pin["radius"]))
                    elif pins:
                        for pin in pins:
                            local_cx, local_cy = pin["center"]
                            pin_poly = Point(local_cx, local_cy).buffer(pin["radius"])
                            fp_pins.append((pin_poly, (local_cx, local_cy), pin["radius"]))
                    pins_data[fp_uuid] = fp_pins
                    
                    # Classify shape for analytical collision
                    shape_type, shape_params = _classify_shape(body_poly)
                    
                    # Update cache
                    centroid = body_poly.centroid
                    body_centroids.append([centroid.x, centroid.y])
                    body_layers.append(body.get("layer", ""))
                    max_extent = 0.5
                    if body_poly.area > 0:
                        for x, y in body_poly.exterior.coords:
                            d = math.hypot(x - centroid.x, y - centroid.y)
                            if d > max_extent:
                                max_extent = d
                    body_radii.append(max_extent)
                    
                    poly_cache[fp_uuid] = {
                        "body_poly": body_poly,
                        "pins": fp_pins,
                        "centroid": [centroid.x, centroid.y],
                        "radius": max_extent,
                        "layer": body.get("layer", ""),
                        "last_centroid": [curr_centroid[0], curr_centroid[1]],
                        "last_angle": curr_angle,
                        "shape_type": shape_type,
                        "shape_params": shape_params,
                    }
                else:
                    # Use cached polygons
                    body_poly = cached["body_poly"]
                    body_polys[fp_uuid] = body_poly
                    pins_data[fp_uuid] = cached["pins"]
                    body_centroids.append(cached["centroid"])
                    body_radii.append(cached["radius"])
                    body_layers.append(cached["layer"])
            
            body_centroids_arr = np.array(body_centroids, dtype=np.float64)
            body_radii_arr = np.array(body_radii, dtype=np.float64)
            body_layers_arr = np.array(body_layers, dtype=object)
            
            # Build uniform grid spatial index for broadphase
            # Use fixed cell size based on typical footprint interaction distance
            # (typical footprint diameter ~10mm + halo 3mm = ~13mm, round to 15mm)
            # Large footprints will occupy multiple cells, which is fine.
            cell_size = 15.0  # mm - covers typical interaction distance
            
            # Grid: cell_key -> list of footprint local indices
            grid: Dict[Tuple[int, int], List[int]] = defaultdict(list)
            for fp_idx in range(n_fp):
                cx, cy = body_centroids_arr[fp_idx]
                r = body_radii_arr[fp_idx] + (params.wall_halo_m * 1000.0)
                min_gx = int((cx - r) / cell_size)
                max_gx = int((cx + r) / cell_size)
                min_gy = int((cy - r) / cell_size)
                max_gy = int((cy + r) / cell_size)
                for gx in range(min_gx, max_gx + 1):
                    for gy in range(min_gy, max_gy + 1):
                        grid[(gx, gy)].append(fp_idx)
            
            # Generate candidate pairs from grid (same + adjacent cells)
            # Use a set to deduplicate pairs that appear in multiple cells
            candidate_pairs = set()
            for cell, indices in grid.items():
                # Pairs within the same cell
                for ii, i in enumerate(indices):
                    for j in indices[ii + 1:]:
                        candidate_pairs.add((i, j) if i < j else (j, i))
                # Pairs with adjacent cells
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        if dx == 0 and dy == 0:
                            continue
                        adj_cell = (cell[0] + dx, cell[1] + dy)
                        adj_indices = grid.get(adj_cell, [])
                        for i in indices:
                            for j in adj_indices:
                                if i < j:
                                    candidate_pairs.add((i, j))
                                elif j < i:
                                    candidate_pairs.add((j, i))
            
            # Filter candidate pairs by layer (body-body only) and compute halo matrix for them
            _close = np.zeros((n_fp, n_fp), dtype=bool)
            _halo_matrix = np.zeros((n_fp, n_fp), dtype=np.float64)
            for i, j in candidate_pairs:
                if body_layers_arr[i] == body_layers_arr[j]:
                    ri = body_radii_arr[i]
                    rj = body_radii_arr[j]
                    halo = min(1.5 * (ri + rj), params.wall_halo_m * 1000.0)
                    dx = body_centroids_arr[i, 0] - body_centroids_arr[j, 0]
                    dy = body_centroids_arr[i, 1] - body_centroids_arr[j, 1]
                    dist_sq = dx * dx + dy * dy
                    reach_sq = (ri + rj + halo) ** 2
                    if dist_sq < reach_sq + 1e-8:
                        _close[i, j] = _close[j, i] = True
                        _halo_matrix[i, j] = _halo_matrix[j, i] = halo
            
            # Store for collision loop
            fp_centroids_arr = body_centroids_arr
            fp_radii_arr = body_radii_arr

            # Spread one pair-push over a footprint's pads
            def _spread(rows, cx, cy, radius, ux, uy, fx, fy):
                if rows is None or len(rows) == 0:
                    return
                r = float(radius)
                if not math.isfinite(r) or r < 1e-9:
                    n = len(rows)
                    if n > 0:
                        poly_force[rows, 0] += fx / n
                        poly_force[rows, 1] += fy / n
                    return
                proj = ((pos[rows, 0] - cx) * ux + (pos[rows, 1] - cy) * uy) / r
                w = _contact_weights(proj)
                poly_force[rows, 0] += w * fx
                poly_force[rows, 1] += w * fy

            def _apply_halo_push(
                li: int, lj: int,
                pi: Polygon, pj: Polygon,
                margin: float,
            ) -> None:
                """Halo push for one pair at a given inside-halo margin (newtons).

                Shared by the near-miss branch (margin = reach - gap) and
                the boundary-touch case (margin = full reach at zero gap),
                so contact is continuous from both sides. Direction along
                the contact normal (edge-orthogonal): the nearest-point
                pair on the two outlines. Geometry stays millimeter-native;
                margin/halo_reach enter only as a dimensionless ratio."""
                if margin <= 0:
                    return
                halo_reach = float(_halo_matrix[li, lj])
                try:
                    _qi, _qj = nearest_points(pi, pj)
                    dx = float(_qi.x - _qj.x)
                    dy = float(_qi.y - _qj.y)
                except Exception:
                    dx = dy = 0.0
                _gap = math.hypot(dx, dy)
                if _gap <= 1e-8:
                    # Coincident/degenerate outlines: fall back to
                    # the centroid direction (always defined).
                    dx = fp_centroids_arr[li, 0] - fp_centroids_arr[lj, 0]
                    dy = fp_centroids_arr[li, 1] - fp_centroids_arr[lj, 1]
                    _gap = math.hypot(dx, dy)
                if _gap > 1e-6:
                    strength = (
                        params.wall_k_N
                        * (margin / halo_reach)
                    )
                    ux, uy = dx / _gap, dy / _gap
                    fx, fy = strength * ux, strength * uy
                    _spread(fp_local_to_rows[li], fp_centroids_arr[li, 0], fp_centroids_arr[li, 1],
                            fp_radii_arr[li], ux, uy, fx, fy)
                    if fp_movable_arr[lj]:
                        _spread(fp_local_to_rows[lj], fp_centroids_arr[lj, 0], fp_centroids_arr[lj, 1],
                                fp_radii_arr[lj], -ux, -uy, -fx, -fy)

            def _apply_contact_push(
                li: int, lj: int,
                pi: Polygon, pj: Polygon,
                intersection_area: float,  # >= 0: overlap area mm^2; < 0: gap mm (for halo)
                sink: Optional[list] = None,
            ) -> None:
                """Unified contact push using nearest-point normal (newtons).

                This is the elastic collision response: penalty-based push-out.
                For overlap (intersection_area >= 0): F = k_wall * (1 + area_m2/area_ref)
                For halo (intersection_area < 0, gap = -intersection_area): F = k_wall * (1 - gap/halo)
                Geometry stays millimeter-native; SI conversions happen inline.

                Direction always from nearest-point normal (edge-orthogonal).
                Continuity at boundary: area=0 -> F=k_wall, gap=0 -> F=k_wall.

                With ``sink`` given, appends a candidate
                ``('ov', area_mm2, 0.0, ux, uy)`` or
                ``('halo', gap_mm, reach_mm, ux, uy)`` instead of spreading:
                the pair loop aggregates sub-path candidates (body, pins)
                into ONE penalty per footprint pair (summed overlap areas,
                strongest direction), so THT pin circles can't stack the
                same contact 3-9x.
                """
                if pi.is_empty or pj.is_empty:
                    return
                try:
                    qi, qj = nearest_points(pi, pj)
                    dx = float(qi.x - qj.x)
                    dy = float(qi.y - qj.y)
                except Exception:
                    dx = dy = 0.0
                dist = math.hypot(dx, dy)

                # nearest_points on overlapping polygons returns boundary points with dist=0
                # Fall back to centroid-to-centroid if degenerate
                if dist <= 1e-8:
                    dx = fp_centroids_arr[li, 0] - fp_centroids_arr[lj, 0]
                    dy = fp_centroids_arr[li, 1] - fp_centroids_arr[lj, 1]
                    dist = math.hypot(dx, dy)
                if dist <= 1e-6:
                    dx, dy, dist = 1.0, 0.0, 1.0  # deterministic tiebreak

                ux, uy = dx / dist, dy / dist

                if intersection_area >= 0:
                    if sink is not None:
                        sink.append(('ov', float(intersection_area), 0.0, ux, uy))
                        return
                    # Overlap: F = k_wall * (1 + area_m2 / area_ref) — the
                    # dimensionless ratio keeps the constant sane across sizes.
                    area_m2 = intersection_area * 1e-6
                    force_mag = params.wall_k_N * (1.0 + area_m2 / params.wall_area_ref_m2)
                else:
                    # Halo: gap = -intersection_area, linear falloff to 0 at halo_reach
                    gap = -intersection_area
                    halo_reach = float(_halo_matrix[li, lj])
                    if gap >= halo_reach:
                        return
                    if sink is not None:
                        sink.append(('halo', gap, halo_reach, ux, uy))
                        return
                    gap_m = gap * 1e-3
                    halo_m = halo_reach * 1e-3
                    force_mag = params.wall_k_N * (1.0 - gap_m / halo_m)

                fx, fy = force_mag * ux, force_mag * uy
                _spread(fp_local_to_rows[li], fp_centroids_arr[li, 0], fp_centroids_arr[li, 1],
                        fp_radii_arr[li], ux, uy, fx, fy)
                if fp_movable_arr[lj]:
                    _spread(fp_local_to_rows[lj], fp_centroids_arr[lj, 0], fp_centroids_arr[lj, 1],
                            fp_radii_arr[lj], -ux, -uy, -fx, -fy)

            def _apply_contact_push_analytical(
                li: int, lj: int,
                shape_i_type: str, shape_i_params: dict,
                shape_j_type: str, shape_j_params: dict,
                intersection_area: float,  # >= 0: overlap area; < 0: gap (for halo)
                sink: Optional[list] = None,
            ) -> bool:
                """Analytical contact push using SAT for rectangles/circles.

                Returns True if analytical collision was used, False if fallback needed.

                Normal convention: points from j to i (same as shapely nearest_points).
                Force on i (li) is in normal direction; force on j (lj) is opposite.

                With ``sink`` given, appends a candidate tuple (see
                ``_apply_contact_push``) instead of spreading, for per-pair
                aggregation by the caller.
                """
                # Determine collision type and call appropriate SAT function
                overlap = False
                normal = None
                penetration = 0.0
                
                if shape_i_type == ShapeType.RECTANGLE and shape_j_type == ShapeType.RECTANGLE:
                    overlap, normal, penetration = _sat_rect_rect(shape_i_params, shape_j_params)
                    # SAT normal points from i to j; flip to match shapely (j to i)
                    normal = -normal
                elif shape_i_type == ShapeType.CIRCLE and shape_j_type == ShapeType.CIRCLE:
                    overlap, normal, penetration = _circle_circle_contact(shape_i_params, shape_j_params)
                    # circle_circle normal points from c1 to c2 (i to j); flip
                    normal = -normal
                elif shape_i_type == ShapeType.RECTANGLE and shape_j_type == ShapeType.CIRCLE:
                    overlap, normal, penetration = _rect_circle_contact(shape_i_params, shape_j_params)
                    # rect_circle normal points from rect to circle (i to j); flip
                    normal = -normal
                elif shape_i_type == ShapeType.CIRCLE and shape_j_type == ShapeType.RECTANGLE:
                    overlap, normal, penetration = _rect_circle_contact(shape_j_params, shape_i_params)
                    # rect_circle normal points from rect to circle (j to i); already correct
                    # (rect=j, circle=i -> normal from j to i)
                else:
                    # Fall back to shapely for polygon-polygon, polygon-rectangle, etc.
                    return False
                
                # Compute force magnitude (newtons; gap/halo as SI ratio)
                if intersection_area >= 0:
                    # Overlap: use the SAT minimum-translation (MTV) direction.
                    # The shapely centroid-to-centroid fallback is wrong for
                    # containment / small-in-large overlaps (e.g. a cap tucked
                    # under a pot's wide courtyard span): centroids can point
                    # along the long axis, shoving the small part DEEPER
                    # instead of out the nearest edge (DCCF C14 vs RV3,
                    # 2026-10-15: overlap 2.8 -> 21mm^2 in one step).
                    # Magnitude keeps the wall penalty law from
                    # docs/si_force_laws.md; only the direction changes.
                    if normal is None:
                        return False
                    _n = np.asarray(normal, dtype=np.float64).reshape(-1)
                    _nn = float(np.linalg.norm(_n))
                    if not math.isfinite(_nn) or _nn <= 1e-12:
                        return False
                    ux, uy = float(_n[0] / _nn), float(_n[1] / _nn)
                    if sink is not None:
                        sink.append(('ov', float(intersection_area), 0.0, ux, uy))
                        return True
                    area_m2 = intersection_area * 1e-6
                    force_mag = params.wall_k_N * (1.0 + area_m2 / params.wall_area_ref_m2)
                    fx, fy = force_mag * ux, force_mag * uy
                    _spread(fp_local_to_rows[li], fp_centroids_arr[li, 0], fp_centroids_arr[li, 1],
                            fp_radii_arr[li], ux, uy, fx, fy)
                    if fp_movable_arr[lj]:
                        _spread(fp_local_to_rows[lj], fp_centroids_arr[lj, 0], fp_centroids_arr[lj, 1],
                                fp_radii_arr[lj], -ux, -uy, -fx, -fy)
                    return True
                else:
                    # Halo: gap = -intersection_area, linear falloff to 0 at halo_reach
                    # Use SAT normal (separation axis) for halo force direction
                    gap = -intersection_area
                    halo_reach = float(_halo_matrix[li, lj])
                    if gap >= halo_reach:
                        return True  # Outside halo, no force
                    _n = np.asarray(normal, dtype=np.float64).reshape(-1)
                    _nn = float(np.linalg.norm(_n))
                    if not math.isfinite(_nn) or _nn <= 1e-12:
                        return False
                    if sink is not None:
                        sink.append(('halo', gap, halo_reach,
                                     float(_n[0] / _nn), float(_n[1] / _nn)))
                        return True
                    force_mag = params.wall_k_N * (1.0 - (gap * 1e-3) / (halo_reach * 1e-3))

                ux, uy = normal[0], normal[1]
                fx, fy = force_mag * ux, force_mag * uy
                _spread(fp_local_to_rows[li], fp_centroids_arr[li, 0], fp_centroids_arr[li, 1],
                        fp_radii_arr[li], ux, uy, fx, fy)
                if fp_movable_arr[lj]:
                    _spread(fp_local_to_rows[lj], fp_centroids_arr[lj, 0], fp_centroids_arr[lj, 1],
                            fp_radii_arr[lj], -ux, -uy, -fx, -fy)
                return True

            # Compute collision forces per footprint pair, then distribute to pads.
            # Check: body-body (same layer), pins-body (all layers), body-pins (all layers), pins-pins (all layers)
            # Iterate only over candidate pairs from grid broadphase
            for i, j in candidate_pairs:
                if not fp_movable_arr[i] and not fp_movable_arr[j]:
                    continue  # Skip if neither is movable
                fp_i_uuid = fp_uuids[i]
                fp_j_uuid = fp_uuids[j]
                body_i = body_polys[fp_i_uuid]
                body_j = body_polys[fp_j_uuid]
                pins_i = pins_data[fp_i_uuid]
                pins_j = pins_data[fp_j_uuid]
                # Per-pair contact candidates: body + pin sub-paths below
                # append ('ov'|'halo', scalar, reach, ux, uy) tuples; the
                # consumer after section 4 applies exactly ONE penalty per
                # footprint pair (summed overlap areas along the strongest
                # direction, else strongest halo). Without aggregation the
                # THT pin circles stack the same contact 3-9x, sandwiching
                # small parts into locked neighbors (DCCF C14 vs RV3/U3 and
                # C8/R13 vs RV1, 2026-10-15).
                cand: list = []

                # 1. Body-body collision (same layer only) - already filtered by layer in broadphase
                if _close[i, j]:
                    if not body_i.is_empty and not body_j.is_empty:
                        # Try analytical collision first
                        cached_i = poly_cache.get(fp_i_uuid, {})
                        cached_j = poly_cache.get(fp_j_uuid, {})
                        shape_i_type = cached_i.get("shape_type", ShapeType.POLYGON)
                        shape_j_type = cached_j.get("shape_type", ShapeType.POLYGON)
                        shape_i_params = cached_i.get("shape_params", {})
                        shape_j_params = cached_j.get("shape_params", {})

                        used_analytical = False
                        if body_i.intersects(body_j):
                            intersection = body_i.intersection(body_j)
                            if shape_i_type in (ShapeType.RECTANGLE, ShapeType.CIRCLE) and \
                               shape_j_type in (ShapeType.RECTANGLE, ShapeType.CIRCLE):
                                used_analytical = _apply_contact_push_analytical(
                                    i, j, shape_i_type, shape_i_params,
                                    shape_j_type, shape_j_params,
                                    intersection.area, cand)
                            if not used_analytical:
                                _apply_contact_push(i, j, body_i, body_j, intersection.area, cand)
                        else:
                            body_dist = body_i.distance(body_j)
                            halo_reach = float(_halo_matrix[i, j])
                            if body_dist < halo_reach:
                                if shape_i_type in (ShapeType.RECTANGLE, ShapeType.CIRCLE) and \
                                   shape_j_type in (ShapeType.RECTANGLE, ShapeType.CIRCLE):
                                    used_analytical = _apply_contact_push_analytical(
                                        i, j, shape_i_type, shape_i_params,
                                        shape_j_type, shape_j_params,
                                        -body_dist, cand)
                                if not used_analytical:
                                    _apply_contact_push(i, j, body_i, body_j, -body_dist, cand)

                # 2. Pins_i vs body_j (all layers) - pins are circles
                if pins_i and not body_j.is_empty:
                    for pin_poly, pin_center, pin_radius in pins_i:
                        if pin_poly.is_empty:
                            continue
                        # Pin is a circle
                        pin_shape_params = {"center": pin_center, "radius": pin_radius}
                        cached_j = poly_cache.get(fp_j_uuid, {})
                        shape_j_type = cached_j.get("shape_type", ShapeType.POLYGON)
                        shape_j_params = cached_j.get("shape_params", {})
                        
                        used_analytical = False
                        if pin_poly.intersects(body_j):
                            intersection = pin_poly.intersection(body_j)
                            if shape_j_type in (ShapeType.RECTANGLE, ShapeType.CIRCLE):
                                used_analytical = _apply_contact_push_analytical(
                                    i, j, ShapeType.CIRCLE, pin_shape_params,
                                    shape_j_type, shape_j_params,
                                    intersection.area, cand)
                            if not used_analytical:
                                _apply_contact_push(i, j, pin_poly, body_j, intersection.area, cand)
                        else:
                            pin_dist = pin_poly.distance(body_j)
                            pin_halo = min(1.5 * (pin_radius + body_radii_arr[j]), (params.wall_halo_m * 1000.0))
                            if pin_dist < pin_halo:
                                if shape_j_type in (ShapeType.RECTANGLE, ShapeType.CIRCLE):
                                    used_analytical = _apply_contact_push_analytical(
                                        i, j, ShapeType.CIRCLE, pin_shape_params,
                                        shape_j_type, shape_j_params,
                                        -pin_dist, cand)
                                if not used_analytical:
                                    _apply_contact_push(i, j, pin_poly, body_j, -pin_dist, cand)

                # 3. Body_i vs pins_j (all layers)
                if pins_j and not body_i.is_empty:
                    for pin_poly, pin_center, pin_radius in pins_j:
                        if pin_poly.is_empty:
                            continue
                        # Pin is a circle
                        pin_shape_params = {"center": pin_center, "radius": pin_radius}
                        cached_i = poly_cache.get(fp_i_uuid, {})
                        shape_i_type = cached_i.get("shape_type", ShapeType.POLYGON)
                        shape_i_params = cached_i.get("shape_params", {})
                        
                        used_analytical = False
                        if body_i.intersects(pin_poly):
                            intersection = body_i.intersection(pin_poly)
                            if shape_i_type in (ShapeType.RECTANGLE, ShapeType.CIRCLE):
                                used_analytical = _apply_contact_push_analytical(
                                    i, j, shape_i_type, shape_i_params,
                                    ShapeType.CIRCLE, pin_shape_params,
                                    intersection.area, cand)
                            if not used_analytical:
                                _apply_contact_push(i, j, body_i, pin_poly, intersection.area, cand)
                        else:
                            pin_dist = body_i.distance(pin_poly)
                            pin_halo = min(1.5 * (body_radii_arr[i] + pin_radius), (params.wall_halo_m * 1000.0))
                            if pin_dist < pin_halo:
                                if shape_i_type in (ShapeType.RECTANGLE, ShapeType.CIRCLE):
                                    used_analytical = _apply_contact_push_analytical(
                                        i, j, shape_i_type, shape_i_params,
                                        ShapeType.CIRCLE, pin_shape_params,
                                        -pin_dist, cand)
                                if not used_analytical:
                                    _apply_contact_push(i, j, body_i, pin_poly, -pin_dist, cand)

                # 4. Pins_i vs pins_j (all layers) - both circles
                if pins_i and pins_j:
                    for pin_i_poly, pin_i_center, pin_i_radius in pins_i:
                        for pin_j_poly, pin_j_center, pin_j_radius in pins_j:
                            if pin_i_poly.is_empty or pin_j_poly.is_empty:
                                continue
                            # Both are circles - use analytical
                            pin_i_params = {"center": pin_i_center, "radius": pin_i_radius}
                            pin_j_params = {"center": pin_j_center, "radius": pin_j_radius}
                            
                            used_analytical = False
                            if pin_i_poly.intersects(pin_j_poly):
                                intersection = pin_i_poly.intersection(pin_j_poly)
                                used_analytical = _apply_contact_push_analytical(
                                    i, j, ShapeType.CIRCLE, pin_i_params,
                                    ShapeType.CIRCLE, pin_j_params,
                                    intersection.area, cand)
                            if not used_analytical:
                                # Need to recompute intersection for fallback
                                if pin_i_poly.intersects(pin_j_poly):
                                    intersection = pin_i_poly.intersection(pin_j_poly)
                                    _apply_contact_push(i, j, pin_i_poly, pin_j_poly, intersection.area, cand)
                            else:
                                pin_dist = pin_i_poly.distance(pin_j_poly)
                                pin_halo = min(1.5 * (pin_i_radius + pin_j_radius), (params.wall_halo_m * 1000.0))
                                if pin_dist < pin_halo:
                                    used_analytical = _apply_contact_push_analytical(
                                        i, j, ShapeType.CIRCLE, pin_i_params,
                                        ShapeType.CIRCLE, pin_j_params,
                                        -pin_dist, cand)
                                    if not used_analytical:
                                        _apply_contact_push(i, j, pin_i_poly, pin_j_poly, -pin_dist, cand)

                # Aggregate per-pair candidates into ONE penalty: summed
                # overlap areas along the strongest (max-area) direction,
                # else the strongest (min gap/reach) halo. Magnitudes keep
                # the wall laws from docs/si_force_laws.md.
                if cand:
                    _ovs = [c for c in cand if c[0] == 'ov']
                    if _ovs:
                        _total_area = sum(c[1] for c in _ovs)
                        _win = max(_ovs, key=lambda c: c[1])
                        _ux, _uy = _win[3], _win[4]
                        _mag = params.wall_k_N * (
                            1.0 + (_total_area * 1e-6) / params.wall_area_ref_m2)
                    else:
                        _halos = [c for c in cand if c[0] == 'halo']
                        _win = min(_halos, key=lambda c: c[1] / c[2])
                        _gap_mm, _reach_mm = _win[1], _win[2]
                        _ux, _uy = _win[3], _win[4]
                        _mag = params.wall_k_N * (
                            1.0 - (_gap_mm * 1e-3) / (_reach_mm * 1e-3))
                    _fx, _fy = _mag * _ux, _mag * _uy
                    _spread(fp_local_to_rows[i],
                            fp_centroids_arr[i, 0], fp_centroids_arr[i, 1],
                            fp_radii_arr[i], _ux, _uy, _fx, _fy)
                    if fp_movable_arr[j]:
                        _spread(fp_local_to_rows[j],
                                fp_centroids_arr[j, 0], fp_centroids_arr[j, 1],
                                fp_radii_arr[j], -_ux, -_uy, -_fx, -_fy)

            # Courtyard pushes were spread directly onto pads above
            # (overlap-weighted, halo uniform; totals conserved); nothing
            # left to broadcast.
            force += poly_force

            # Boundary repulsion in newtons (mm geometry converted at the call).
            boundary_k = params.boundary_k_N

            # Compute current footprint bounds from pad positions, expanded by courtyard radius
            fp_bounds = {}
            for fp_uuid, pad_indices in fp_pad_indices.items():
                if pad_indices:
                    fp_pad_pos = pos[pad_indices]
                    # Get courtyard radius for this footprint (exact per-iter
                    # values from the pose update above).
                    fp_local_idx = fp_uuid_to_local_idx.get(fp_uuid)
                    if fp_local_idx is not None:
                        radius = float(fp_radii_arr[fp_local_idx])
                    else:
                        radius = 1.0  # fallback
                    fp_bounds[fp_uuid] = (
                        float(fp_pad_pos[:, 0].min()) - radius,
                        float(fp_pad_pos[:, 1].min()) - radius,
                        float(fp_pad_pos[:, 0].max()) + radius,
                        float(fp_pad_pos[:, 1].max()) + radius,
                    )

            for pad_idx in range(n_pads_local):
                fp_i_local = pad_to_fp_local[pad_idx]
                if fp_i_local < 0 or not fp_movable_arr[fp_i_local]:
                    continue

                fp_i_uuid = fp_uuids[fp_i_local]

                # Get current footprint bounds from pad positions
                if fp_i_uuid not in fp_bounds:
                    continue
                minx, miny, maxx, maxy = fp_bounds[fp_i_uuid]
                fp_width = maxx - minx
                fp_height = maxy - miny
                # Dynamic threshold: 10% of footprint size, minimum 1mm, maximum 10mm
                threshold = max(1.0, min(10.0, 0.1 * max(fp_width, fp_height)))
                threshold_m = threshold * 1e-3

                # Distance to region boundaries in meters (bounded pushes, see
                # _boundary_edge_push; left/bottom push +, right/top push -)
                dist_left = (minx - min_x) * 1e-3
                boundary_force[pad_idx, 0] += _boundary_edge_push(dist_left, threshold_m, boundary_k)

                dist_right = (max_x - maxx) * 1e-3
                boundary_force[pad_idx, 0] -= _boundary_edge_push(dist_right, threshold_m, boundary_k)

                dist_bottom = (miny - min_y) * 1e-3
                boundary_force[pad_idx, 1] += _boundary_edge_push(dist_bottom, threshold_m, boundary_k)

                dist_top = (max_y - maxy) * 1e-3
                boundary_force[pad_idx, 1] -= _boundary_edge_push(dist_top, threshold_m, boundary_k)

            force += boundary_force

        # Replace NaN
        force = np.nan_to_num(force, nan=0.0, posinf=0.0, neginf=0.0)
        force[~pad_movable] = 0.0
        if not return_components:
            return force
        components = {
            "repulsion": np.nan_to_num(repulsion, nan=0.0, posinf=0.0, neginf=0.0),
            "attraction": np.nan_to_num(attraction, nan=0.0, posinf=0.0, neginf=0.0),
            "ghost": np.nan_to_num(ghost_attr_force, nan=0.0, posinf=0.0, neginf=0.0),
            "courtyard": np.nan_to_num(poly_force, nan=0.0, posinf=0.0, neginf=0.0),
            "boundary": np.nan_to_num(boundary_force, nan=0.0, posinf=0.0, neginf=0.0),
        }
        for comp in components.values():
            comp[~pad_movable] = 0.0
        return force, components
    # Polygon cache for courtyard collision - persists across iterations
    # Key: fp_uuid -> {body_poly, body_centroid, body_radius, body_layer,
    #                  pins: list of (pin_poly, pin_center, pin_radius),
    #                  last_centroid, last_angle}
    poly_cache: Dict[str, dict] = {}
    cache_rebuild_threshold_mm = 0.01  # Rebuild if centroid moves > 10 microns
    cache_rebuild_threshold_rad = math.radians(0.01)  # or angle > 0.01 deg

    # Initial forces predict the NEXT step from the committed board positions.
    # They are captured before the loop so the overlay explains the move it
    # is about to make (F0 -> D0), not the residual at the preview end
    # (F1 at pos1, which previously pointed back after overshoot and looked
    # like the integrator moved against the displayed force).
    initial_force, initial_components = _compute_total_force(
        positions, len(region_pads), pad_movable,
        params, has_net_edges, src_idx, dst_idx, edge_weights,
        ghost_attractions,
        same_fp_mask,
        return_components=True,
        poly_cache=poly_cache,
        cache_rebuild_threshold_mm=cache_rebuild_threshold_mm,
        cache_rebuild_threshold_rad=cache_rebuild_threshold_rad,
    )
    # ---- Trajectory frames (opt-in motion viz) ----
    # Dense per-sampled-iteration (dx, dy, da) vs committed start for every
    # movable footprint with pads in this region. Kabsch per frame is cheap
    # (2x2 SVD per part) and only runs when requested. frames[0] (captured
    # below before the loop) is the starting pose; loop samples follow every
    # stride completed iterations; the final pose is always appended.
    _frame_stride = max(1, int(frame_stride or 1))
    _frame_fps: List[Tuple[str, list]] = []
    if record_frames:
        for _fp in model.footprints:
            if not _fp.uuid or _fp.locked:
                continue
            if model.footprint_region.get(_fp.uuid) != region_idx:
                continue
            if movable_uuids is not None and _fp.uuid not in movable_uuids:
                continue
            _pi = region_fp_uuid_to_pad_indices.get(_fp.uuid)
            if not _pi:
                continue
            _frame_fps.append((_fp, list(_pi)))

    def _capture_frame(pos: np.ndarray) -> Dict[str, Tuple[float, float, float]]:
        frame: Dict[str, Tuple[float, float, float]] = {}
        for _fp, _pi in _frame_fps:
            try:
                _nx, _ny, _na = _compute_footprint_pose_from_pads(
                    _fp, pos, _pi, region_global_pad_idx_to_local
                )
            except Exception:
                continue
            _dx = _nx - _fp.x_mm
            _dy = _ny - _fp.y_mm
            _da = (_na - _fp.angle_deg + 180) % 360 - 180
            frame[_fp.uuid] = (
                round(float(_dx), FRAME_XY_DECIMALS),
                round(float(_dy), FRAME_XY_DECIMALS),
                round(float(_da), FRAME_ANGLE_DECIMALS),
            )
        return frame

    def _capture_force_frame(pos: np.ndarray) -> Dict[str, Dict[str, Tuple[float, float]]]:
        """Capture per-footprint force breakdown at current positions.
        
        Recomputes forces with return_components=True at the current positions.
        Returns {uuid: {cause: (fx, fy)}} with values rounded to 3 sig figs.
        """
        force, components = _compute_total_force(
            pos, len(region_pads), pad_movable,
            params, has_net_edges, src_idx, dst_idx, edge_weights,
            ghost_attractions,
            same_fp_mask,
            return_components=True,
            poly_cache=poly_cache,
            cache_rebuild_threshold_mm=cache_rebuild_threshold_mm,
            cache_rebuild_threshold_rad=cache_rebuild_threshold_rad,
        )
        force_frame: Dict[str, Dict[str, Tuple[float, float]]] = {}
        for _fp, _pi in _frame_fps:
            if _fp.uuid not in region_fp_uuid_to_pad_indices:
                continue
            pad_indices = region_fp_uuid_to_pad_indices[_fp.uuid]
            if not pad_indices:
                continue
            comp: Dict[str, Tuple[float, float]] = {}
            for cause, arr in components.items():
                s = arr[pad_indices].sum(axis=0)
                if bool(np.all(np.isfinite(s))):
                    comp[cause] = (float(s[0]), float(s[1]))
            if comp:
                force_frame[_fp.uuid] = _round_force_components(comp)
        return force_frame

    _region_frames: List[Dict[str, Tuple[float, float, float]]] = []
    _region_force_frames: List[Dict[str, Dict[str, Tuple[float, float]]]] = []
    # Frame 0: the committed starting pose (all-zero deltas) so the motion
    # player can scrub from the true start. Previously the first capture
    # happened only after iteration 1 completed, so playback never showed
    # step 0 and short Step budgets looked like they "jumped" on load.
    if record_frames and _frame_fps:
        _region_frames.append(_capture_frame(positions))
        _region_force_frames.append(_capture_force_frame(positions))
    # SI integrator state: per-pad velocities (m/s) and adaptive timestep (s).
    # Deterministic function of the trajectory (not the iteration budget),
    # so the no-cooling invariant holds: same (model, params) prefix →
    # same path.
    _vel = np.zeros((len(region_pads), 2), dtype=np.float64)
    _dt = float(params.dt_s)
    _prev_ke: Optional[float] = None

    # Rigid-body projection data (PBD): per movable multi-pad footprint,
    # (rows, reference-shape snapshot). The reference is the committed board
    # geometry, i.e. each footprint's true shape; per iteration the proposed
    # pads are Kabsch-snapped onto its exact rigid pose (see
    # _project_rigid_poses). The pad layout never changes during a run, so
    # one snapshot up front covers the whole trajectory.
    _proj_bodies: List[Tuple[np.ndarray, np.ndarray]] = []
    for _uuid, _movable in zip(fp_uuids, fp_movable_list):
        if not _movable:
            continue
        _brows = fp_pad_indices.get(_uuid)
        if _brows is None or len(_brows) < 2:
            continue
        _barr = np.asarray(_brows, dtype=int)
        _proj_bodies.append((_barr, positions[_barr].copy()))

    # Polygon cache for courtyard collision - persists across iterations
    # Key: fp_uuid -> {body_poly, body_centroid, body_radius, body_layer,
    #                  pins: list of (pin_poly, pin_center, pin_radius),
    #                  last_centroid, last_angle}
    poly_cache: Dict[str, dict] = {}
    cache_rebuild_threshold_mm = 0.01  # Rebuild if centroid moves > 10 microns
    cache_rebuild_threshold_rad = math.radians(0.01)  # or angle > 0.01 deg

    for iteration in range(params.max_iterations):
        force = _compute_total_force(
            positions, len(region_pads), pad_movable,
            params, has_net_edges, src_idx, dst_idx, edge_weights,
            ghost_attractions,
            same_fp_mask,
            poly_cache=poly_cache,
            cache_rebuild_threshold_mm=cache_rebuild_threshold_mm,
            cache_rebuild_threshold_rad=cache_rebuild_threshold_rad,
        )

        # SI dynamics step (forces are newtons from _compute_total_force).
        # Dry friction: freeze a whole footprint whose largest pad-force
        # norm is below friction_N (all-or-nothing keeps the rigid body
        # consistent for the Kabsch fit). Frozen bodies also lose velocity.
        _frozen = np.zeros(len(region_pads), dtype=bool)
        if params.friction_N > 0:
            _fp_force_norm = np.linalg.norm(force, axis=1)
            for _rows in fp_local_to_rows:
                if len(_rows) and np.max(_fp_force_norm[_rows]) < params.friction_N:
                    force[_rows] = 0.0
                    _frozen[_rows] = True
        force[~pad_movable] = 0.0
        _frozen[~pad_movable] = True

        # Semi-implicit Euler: v += (F/m) dt, with implicit-Euler viscous
        # decay v *= 1/(1 + c*dt/m) (stable for any dt), then a speed
        # ceiling (replaces the old displacement step cap).
        _accel = force / pad_mass_kg[:, None]
        _vel[pad_movable] += _accel[pad_movable] * _dt
        _decay = 1.0 / (1.0 + params.damping_Ns_m * _dt / pad_mass_kg)
        _vel *= _decay[:, None]
        _vel[_frozen] = 0.0
        _speed = np.linalg.norm(_vel, axis=1, keepdims=True)
        _vcap = params.vmax_m_s / np.maximum(_speed, 1e-12)
        np.minimum(_vcap, 1.0, out=_vcap)
        _vel *= _vcap
        _vel[~pad_movable] = 0.0

        # Drift in meters, applied to the millimeter-native positions.
        _old_positions = positions.copy()
        positions[pad_movable] += _vel[pad_movable] * _dt * 1000.0
        positions = np.nan_to_num(positions, nan=0.0, posinf=0.0, neginf=0.0)

        # Rigid projection: snap each movable body onto its exact shape.
        # Unconditionally stable (no stiffness parameter); translation and
        # rotation stay emergent from the force differentials.
        _project_rigid_poses(positions, _proj_bodies)
        # Rigid-step clamp: bound what the snap above may have added. The
        # velocity ceiling alone cannot bound rotation-induced pad swing.
        _clamp_rigid_step(
            positions, _old_positions, _proj_bodies, pad_movable,
            cap_mm=params.vmax_m_s * _dt * 1000.0,
            max_rot_rad=math.radians(5.0),
        )
        # Re-derive velocity from the actual projected displacement so the
        # projection can't inject phantom energy into the guard below.
        if _dt > 0:
            _vel[pad_movable] = (
                (positions[pad_movable] - _old_positions[pad_movable]) * 1e-3 / _dt)
        _vel[_frozen] = 0.0
        _vel[~pad_movable] = 0.0

        # Adaptive-dt energy guard: bleed and slow down on significant
        # kinetic-energy jumps (overshoot/collision ring), regrow otherwise.
        # This is FIRE's blowup-proofing without FIRE's extra state. The
        # rise needs a deadband: accelerating from rest raises KE every
        # step, and halving on any rise ratchets dt to the floor during
        # innocent spin-up (observed: dt pinned at dt_min, ~5x slowdown).
        _ke = 0.5 * float(np.sum(
            pad_mass_kg[pad_movable, None]
            * _vel[pad_movable] * _vel[pad_movable]))
        if _prev_ke is not None and _ke > 1.25 * _prev_ke + 1e-15:
            _dt = max(float(params.dt_min_s), _dt * 0.5)
            _vel *= 0.5
        else:
            _dt = min(float(params.dt_max_s), _dt * 1.1)
        _prev_ke = _ke

        if record_frames and _frame_fps and ((iteration + 1) % _frame_stride == 0):
            _frame = _capture_frame(positions)
            _region_frames.append(_frame)
            _region_force_frames.append(_capture_force_frame(positions))

        # Convergence check: max pad displacement this step, in meters.
        max_disp = float(np.max(np.linalg.norm(positions - prev_positions, axis=1))) * 1e-3
        if max_disp < params.convergence_eps_m:
            break

        prev_positions = positions.copy()

    # Compute footprint deltas from final pad positions
    deltas = {}
    final_max_disp = 0.0
    footprint_forces = {}
    footprint_components: Dict[str, Dict[str, Tuple[float, float]]] = {}

    # Reported forces are the initial (next-step) forces captured above,
    # evaluated at the committed board positions the overlay anchors on.
    # (Previously this recomputed at the final preview positions, whose
    # residual points back after overshoot / convergence and mismatched the
    # displacement just taken.)
    final_force, final_components = initial_force, initial_components

    for fp in model.footprints:
        if model.footprint_region.get(fp.uuid) != region_idx:
            continue
        if not fp.uuid or fp.locked:
            continue
        if movable_uuids is not None and fp.uuid not in movable_uuids:
            continue
        if fp.uuid not in region_fp_uuid_to_pad_indices:
            continue

        pad_indices = region_fp_uuid_to_pad_indices[fp.uuid]
        new_x, new_y, new_angle = _compute_footprint_pose_from_pads(
            fp, positions, pad_indices, region_global_pad_idx_to_local
        )

        dx = new_x - fp.x_mm
        dy = new_y - fp.y_mm
        da = new_angle - fp.angle_deg
        da = (da + 180) % 360 - 180

        # Epsilon filter: Kabsch/SVD on (near-)unmoved pads leaves ~1e-12
        # float noise; only report physically meaningful moves so that
        # converged boards yield empty proposals (nothing left to accept).
        if math.hypot(dx, dy) > 1e-9 or abs(da) > 1e-9:
            deltas[fp.uuid] = (dx, dy, da)
            final_max_disp = max(final_max_disp, math.hypot(dx, dy))

        # Force for this footprint (+ per-cause breakdown for tuning)
        if pad_indices:
            fp_force = final_force[pad_indices].sum(axis=0)
            footprint_forces[fp.uuid] = (float(fp_force[0]), float(fp_force[1]))
            comp: Dict[str, Tuple[float, float]] = {}
            for cause, arr in final_components.items():
                s = arr[pad_indices].sum(axis=0)
                if bool(np.all(np.isfinite(s))):
                    comp[cause] = (float(s[0]), float(s[1]))
            footprint_components[fp.uuid] = comp

    if record_frames and _frame_fps:
        _final_frame = _capture_frame(positions)
        _final_force_frame = _capture_force_frame(positions)
        # Avoid doubling the last sampled frame when the run ended exactly
        # on a stride boundary (same rounded values).
        if not _region_frames or _region_frames[-1] != _final_frame:
            _region_frames.append(_final_frame)
            _region_force_frames.append(_final_force_frame)

    return deltas, footprint_forces, iteration + 1, footprint_components, _region_frames, _region_force_frames


def _merge_region_frames(
    per_region: List[List[Dict[str, Tuple[float, float, float]]]],
) -> List[Dict[str, Tuple[float, float, float]]]:
    """Merge per-region frame lists into global frames.

    Regions converge independently, so lists differ in length; shorter
    regions hold their last pose (parts at rest) while longer ones keep
    moving. Union of uuids per index.
    """
    if not per_region:
        return []
    n = max(len(f) for f in per_region)
    merged: List[Dict[str, Tuple[float, float, float]]] = []
    for k in range(n):
        frame: Dict[str, Tuple[float, float, float]] = {}
        for frames in per_region:
            if not frames:
                continue
            frame.update(frames[min(k, len(frames) - 1)])
        merged.append(frame)
    return merged


def _merge_region_force_frames(
    per_region: List[List[Dict[str, Dict[str, Tuple[float, float]]]]],
) -> List[Dict[str, Dict[str, Tuple[float, float]]]]:
    """Merge per-region force frame lists into global force frames.

    Regions converge independently, so lists differ in length; shorter
    regions hold their last pose (parts at rest) while longer ones keep
    moving. Union of uuids per index.
    """
    if not per_region:
        return []
    n = max(len(f) for f in per_region)
    merged: List[Dict[str, Dict[str, Tuple[float, float]]]] = []
    for k in range(n):
        frame: Dict[str, Dict[str, Tuple[float, float]]] = {}
        for frames in per_region:
            if not frames:
                continue
            frame.update(frames[min(k, len(frames) - 1)])
        merged.append(frame)
    return merged


def run_placement(
    model: BoardModel,
    params: PlacementParams,
    movable_uuids: Optional[Set[str]] = None,
    record_frames: bool = False,
    frame_stride: int = 1,
    max_frames: int = FRAME_DEFAULT_MAX,
    extra_power_nets: Collection[str] = (),
) -> PlacementProposal:
    """Run per-pad force-spring placement for ``model`` under ``params``.

    Supports multi-board region simulation: footprints are grouped by board
    outline region (from Edge.Cuts) and each region is simulated independently
    with its own bounding box and fixed per-iteration step budget.

    Args:
        model: Board model with footprints, nets, and board outline.
        params: Force-spring parameters.
        movable_uuids: Optional set of footprint UUIDs that are allowed to move.
            If None (default), all unlocked footprints can move. If provided,
            only footprints in this set AND not locked can move.
        record_frames: capture per-iteration (dx, dy, da) frames for the SVG
            motion player (default False — zero overhead when off).
            frames[0] is the starting pose (iteration 0); subsequent frames
            follow every stride completed iterations; the last frame is
            always the final pose.
        frame_stride: record every Nth iteration (effective stride is raised
            automatically so capture stays within max_frames).
        max_frames: cap on frames in the proposal; longer runs are uniformly
            thinned (first = starting pose and last = final pose always kept).
        extra_power_nets: user-marked power membership (app nets table,
            seeded from power-named KiCad netclasses on load). Marked nets
            get fanout-scaled rail attraction and count for decap detection.

    Returns:
        PlacementProposal with per-footprint deltas (dx, dy, dangle) keyed by UUID.
    """
    start = time.perf_counter()
    # Bound capture cost upfront: effective stride guarantees at most
    # max_frames samples even for 19k-iteration convergence runs.
    _eff_stride = max(1, int(frame_stride or 1))
    if record_frames and max_frames >= 1 and params.max_iterations > max_frames:
        import math as _math

        _eff_stride = max(
            _eff_stride, _math.ceil(params.max_iterations / max_frames)
        )

    # Collect pad nodes
    pads, pad_to_fp_idx, fp_uuid_to_pad_indices, global_pad_idx_to_local, pad_is_ghost = _collect_pad_nodes(model)
    n_pads = len(pads)

    # Physics requires pads with nets; boards without nets (e.g. the minimal
    # mounting-hole fixture) correctly produce an empty proposal.
    if n_pads == 0:
        return PlacementProposal(
            deltas={},
            iterations=0,
            final_max_disp_mm=0.0,
            elapsed_s=time.perf_counter() - start,
            params=params.to_dict(),
            forces={},
        )

    # Group pads by board region
    if model.board_regions and model.footprint_region:
        # Multi-region mode
        all_deltas = {}
        all_forces = {}
        all_components: Dict[str, Dict[str, Tuple[float, float]]] = {}
        total_iterations = 0
        max_final_disp = 0.0
        all_region_frames: List[List[Dict[str, Tuple[float, float, float]]]] = []
        all_region_force_frames: List[List[Dict[str, Dict[str, Tuple[float, float]]]]] = []

        for region_idx, region in enumerate(model.board_regions):
            # Collect pads for this region
            region_pad_indices = []
            region_pads = []
            region_pad_to_fp_idx = []
            region_fp_uuid_to_pad_indices = {}
            region_global_pad_idx_to_local = {}
            region_pad_is_ghost = []

            for i, pad in enumerate(pads):
                fp_idx = pad_to_fp_idx[i]
                if fp_idx >= 0:
                    fp = model.footprints[fp_idx]
                else:
                    # Ghost footprint
                    ghost_idx = -fp_idx - 1
                    if ghost_idx >= len(model.ghost_footprints):
                        continue
                    fp = model.ghost_footprints[ghost_idx]
                if model.footprint_region.get(fp.uuid) == region_idx:
                    local_idx = len(region_pads)
                    region_pad_indices.append(i)
                    region_pads.append(pad)
                    region_pad_to_fp_idx.append(fp_idx)
                    region_pad_is_ghost.append(pad_is_ghost[i])
                    if fp.uuid not in region_fp_uuid_to_pad_indices:
                        region_fp_uuid_to_pad_indices[fp.uuid] = []
                    region_fp_uuid_to_pad_indices[fp.uuid].append(local_idx)
                    region_global_pad_idx_to_local[local_idx] = global_pad_idx_to_local[i]

            if region_pads:
                region_deltas, region_forces, region_iters, region_comps, region_frames, region_force_frames = _run_region_simulation(
                    region_idx, model.board_regions[region_idx],
                    region_pad_indices, region_pads, region_pad_to_fp_idx,
                    region_fp_uuid_to_pad_indices, region_global_pad_idx_to_local,
                    region_pad_is_ghost, model, params, movable_uuids,
                    record_frames, _eff_stride, extra_power_nets,
                )
                all_deltas.update(region_deltas)
                all_forces.update(region_forces)
                all_components.update(region_comps)
                total_iterations = max(total_iterations, region_iters)
                max_final_disp = max(max_final_disp, max((math.hypot(dx, dy) for dx, dy, _ in region_deltas.values()), default=0.0))
                if record_frames and region_frames:
                    all_region_frames.append(region_frames)
                    all_region_force_frames.append(region_force_frames)

        merged_frames = _thin_frames(
            _merge_region_frames(all_region_frames), max_frames
        ) if record_frames else []
        merged_force_frames = _thin_frames(
            _merge_region_force_frames(all_region_force_frames), max_frames
        ) if record_frames else []
        return PlacementProposal(
            deltas=all_deltas,
            iterations=total_iterations,
            final_max_disp_mm=max_final_disp,
            elapsed_s=time.perf_counter() - start,
            params=params.to_dict(),
            forces=all_forces,
            force_components=all_components,
            frames=merged_frames,
            force_frames=merged_force_frames,
            frame_stride=_eff_stride if record_frames else 1,
        )

    # Single region mode: create a virtual region covering the whole board
    min_x, max_x, min_y, max_y = _board_bbox(model)
    virtual_region = BoardRegion(
        uuid="virtual",
        polygon=(),
        bbox=(min_x, max_x, min_y, max_y),
    )

    # All pads belong to this single region
    region_pad_indices = list(range(n_pads))
    region_pads = pads
    region_pad_to_fp_idx = pad_to_fp_idx
    region_fp_uuid_to_pad_indices = fp_uuid_to_pad_indices
    region_global_pad_idx_to_local = global_pad_idx_to_local
    region_pad_is_ghost = pad_is_ghost

    region_deltas, region_forces, region_iters, region_comps, region_frames, region_force_frames = _run_region_simulation(
        0, virtual_region,
        region_pad_indices, region_pads, region_pad_to_fp_idx,
        region_fp_uuid_to_pad_indices, region_global_pad_idx_to_local,
        region_pad_is_ghost, model, params, movable_uuids,
        record_frames, _eff_stride, extra_power_nets,
    )

    thin_frames = _thin_frames(region_frames, max_frames) if record_frames else []
    thin_force_frames = _thin_frames(region_force_frames, max_frames) if record_frames else []
    return PlacementProposal(
        deltas=region_deltas,
        iterations=region_iters,
        final_max_disp_mm=max((math.hypot(dx, dy) for dx, dy, _ in region_deltas.values()), default=0.0),
        elapsed_s=time.perf_counter() - start,
        params=params.to_dict(),
        forces=region_forces,
        force_components=region_comps,
        frames=thin_frames,
        force_frames=thin_force_frames,
        frame_stride=_eff_stride if record_frames else 1,
    )
