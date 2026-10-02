"""Force-spring placement domain: parameters, proposals, and the runner interface.

The runner implements a vectorized numpy force-spring simulation at the PAD level
(Fruchterman–Reingold style) with rigid constraints between pads of the same
footprint. This allows through-hole components to rotate organically when forces
on their F.Cu and B.Cu pads differ.

Deltas are keyed by footprint UUID (dx_mm, dy_mm, dangle_deg) because refs may
be duplicated or missing; locked footprints never appear in ``deltas`` (they are
fixed anchors).
"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import asdict, dataclass, fields
from typing import Dict, List, Optional, Set, Tuple

import numpy as np

from .board_model import BoardModel, Footprint, Pad


@dataclass(frozen=True)
class PlacementParams:
    """Tunable force-spring parameters (v1 set; grows with the simulation)."""

    repulsion_kr: float = 100.0
    attraction_ka: float = 0.05
    ideal_length_mm: float = 25.0
    max_iterations: int = 1000
    convergence_eps_mm: float = 0.01
    # Rigid constraint stiffness (very high to keep footprint pads together)
    rigid_stiffness: float = 1e6
    # Preview-only knob for the v0 stub: deterministic per-UUID jitter so the
    # proposal overlay is visibly testable before real physics exists.
    demo_jitter_mm: float = 0.0
    # Backward-compatibility: when True (default), use v0 stub behavior (identity
    # deltas unless demo_jitter_mm > 0). When False, run the vectorized numpy
    # force-spring simulation.
    stub: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PlacementParams":
        """Build params from a JSON-style dict; missing keys take defaults and
        unknown keys are ignored (forward compatibility with newer param sets)."""
        known = {f.name for f in fields(cls)}
        params = cls(**{name: data[name] for name in known if name in data})
        _validate(params)
        return params


def _validate(params: PlacementParams) -> None:
    numeric = (
        "repulsion_kr",
        "attraction_ka",
        "ideal_length_mm",
        "convergence_eps_mm",
        "demo_jitter_mm",
        "rigid_stiffness",
    )
    for name in numeric:
        value = getattr(params, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a number")
    for name in (
        "repulsion_kr",
        "attraction_ka",
        "ideal_length_mm",
        "convergence_eps_mm",
        "rigid_stiffness",
    ):
        if getattr(params, name) <= 0:
            raise ValueError(f"{name} must be > 0")
    if params.demo_jitter_mm < 0:
        raise ValueError("demo_jitter_mm must be >= 0")
    if isinstance(params.max_iterations, bool) or not isinstance(
        params.max_iterations, int
    ):
        raise ValueError("max_iterations must be an integer")
    if params.max_iterations < 1:
        raise ValueError("max_iterations must be >= 1")
    if not isinstance(params.stub, bool):
        raise ValueError("stub must be a boolean")


@dataclass(frozen=True)
class PlacementProposal:
    """Result of a placement run: per-footprint deltas keyed by UUID + diagnostics.

    Deltas are (dx_mm, dy_mm, dangle_deg) for each movable footprint.
    """

    deltas: Dict[str, Tuple[float, float, float]]
    iterations: int
    final_max_disp_mm: float
    elapsed_s: float
    params: dict


def _jitter_for(uuid: str, max_mm: float) -> Tuple[float, float]:
    """Deterministic (angle, magnitude) jitter derived from the footprint UUID.

    Returns 2-tuple (dx, dy) for backward compatibility with stub/demo mode.
    """
    digest = hashlib.sha256(uuid.encode("utf-8")).digest()
    h = int.from_bytes(digest[:4], "big")
    angle = (h % 360) * math.pi / 180.0
    magnitude = max_mm * ((h >> 16) % 1000) / 1000.0
    return (magnitude * math.cos(angle), magnitude * math.sin(angle))


def _collect_pad_nodes(model: BoardModel) -> Tuple[List[Pad], List[int], Dict[str, List[int]], Dict[int, int]]:
    """Collect all pads with net connections.

    Returns:
        - pads: list of Pad objects with net_name
        - pad_to_fp_idx: list mapping pad index -> footprint index in model.footprints
        - fp_uuid_to_pad_indices: dict mapping footprint UUID -> list of global pad indices
        - global_pad_idx_to_local: dict mapping global pad index -> local pad index within footprint
    """
    pads = []
    pad_to_fp_idx = []
    fp_uuid_to_pad_indices = {}
    global_pad_idx_to_local = {}

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
        if fp_pad_indices:
            fp_uuid_to_pad_indices[fp.uuid] = fp_pad_indices

    return pads, pad_to_fp_idx, fp_uuid_to_pad_indices, global_pad_idx_to_local


def _build_net_edges_pad_level(
    pads: List[Pad], pad_to_fp_idx: List[int]
) -> Tuple[np.ndarray, np.ndarray]:
    """Build edge index arrays for pad pairs sharing the same net.

    Returns (src_indices, dst_indices) where each is a 1D array of pad indices.
    Each pair appears once (i < j). Only connects pads on different footprints.
    """
    net_to_pads: Dict[str, List[int]] = {}
    for i, pad in enumerate(pads):
        if pad.net_name:
            net_to_pads.setdefault(pad.net_name, []).append(i)

    edges_set = set()
    for net_name, pad_indices in net_to_pads.items():
        if len(pad_indices) < 2:
            continue
        for i in pad_indices:
            for j in pad_indices:
                if i >= j:
                    continue
                # Only connect pads on different footprints
                if pad_to_fp_idx[i] != pad_to_fp_idx[j]:
                    edges_set.add((i, j))

    if not edges_set:
        return np.array([], dtype=int), np.array([], dtype=int)

    edges = sorted(edges_set)
    src = np.array([e[0] for e in edges], dtype=int)
    dst = np.array([e[1] for e in edges], dtype=int)
    return src, dst


def _build_rigid_constraints(
    fp_uuid_to_pad_indices: Dict[str, List[int]], n_pads: int
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Build rigid constraint edges between pads of the same footprint.

    For each footprint with >1 pad, build a Minimum Spanning Tree (MST) of its
    pads using Prim's algorithm. This distributes constraint forces naturally
    across the footprint geometry rather than concentrating them at one pad.
    Returns (src, dst, rest_lengths).
    """
    src_list = []
    dst_list = []
    rest_lengths = []

    for pad_indices in fp_uuid_to_pad_indices.values():
        n = len(pad_indices)
        if n < 2:
            continue

        # Use global pad indices directly - we need their positions for MST
        # We'll compute MST based on the pad_positions array later
        # For now, just build the MST topology; rest lengths filled in after positions known

        if n == 2:
            # Two pads: just connect them
            src_list.append(pad_indices[0])
            dst_list.append(pad_indices[1])
            rest_lengths.append(0.0)
        else:
            # Three or more pads: build MST using Prim's algorithm
            # We need initial positions - will be passed in separately
            # For now, create placeholder edges; actual MST built in run_placement
            # after pad_positions is available
            pass  # Will be handled in run_placement

    if not src_list:
        return np.array([], dtype=int), np.array([], dtype=int), np.array([], dtype=float)

    return (
        np.array(src_list, dtype=int),
        np.array(dst_list, dtype=int),
        np.array(rest_lengths, dtype=float),
    )


def _build_rigid_constraints_mst(
    pad_positions: np.ndarray, fp_uuid_to_pad_indices: Dict[str, List[int]]
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Build MST rigid constraints for all footprints using Prim's algorithm.

    Args:
        pad_positions: (n_pads, 2) array of current pad positions
        fp_uuid_to_pad_indices: mapping from footprint UUID to list of pad indices

    Returns:
        (src_indices, dst_indices, rest_lengths) for all MST edges across all footprints
    """
    src_list = []
    dst_list = []
    rest_lengths = []

    for pad_indices in fp_uuid_to_pad_indices.values():
        n = len(pad_indices)
        if n < 2:
            continue

        if n == 2:
            src_list.append(pad_indices[0])
            dst_list.append(pad_indices[1])
            diff = pad_positions[pad_indices[1]] - pad_positions[pad_indices[0]]
            rest_lengths.append(np.linalg.norm(diff))
            continue

        # Prim's algorithm for MST on this footprint's pads
        # Start from pad closest to centroid (reduces max edge length)
        positions_subset = pad_positions[pad_indices]  # (n, 2)
        centroid = np.mean(positions_subset, axis=0)
        dists_to_centroid = np.linalg.norm(positions_subset - centroid, axis=1)
        start_local = int(np.argmin(dists_to_centroid))
        start_global = pad_indices[start_local]

        in_mst = np.zeros(n, dtype=bool)
        in_mst[start_local] = True
        # min_edge[i] = (distance, local_idx_in_subset, parent_global_idx)
        # For nodes not in MST, track best edge to MST
        best_dist = np.full(n, np.inf)
        best_parent = np.full(n, -1, dtype=int)

        # Initialize with edges from start node
        for j in range(n):
            if j == start_local:
                continue
            diff = positions_subset[j] - positions_subset[start_local]
            dist = np.linalg.norm(diff)
            best_dist[j] = dist
            best_parent[j] = start_local

        # Grow MST
        for _ in range(n - 1):
            # Find closest node not in MST
            candidates = np.where(~in_mst)[0]
            if len(candidates) == 0:
                break
            next_local = candidates[np.argmin(best_dist[candidates])]
            next_global = pad_indices[next_local]
            parent_local = best_parent[next_local]
            parent_global = pad_indices[parent_local]

            # Add edge
            src_list.append(parent_global)
            dst_list.append(next_global)
            rest_lengths.append(best_dist[next_local])

            in_mst[next_local] = True

            # Update best edges from new node
            for j in range(n):
                if in_mst[j]:
                    continue
                diff = positions_subset[j] - positions_subset[next_local]
                dist = np.linalg.norm(diff)
                if dist < best_dist[j]:
                    best_dist[j] = dist
                    best_parent[j] = next_local

    if not src_list:
        return np.array([], dtype=int), np.array([], dtype=int), np.array([], dtype=float)

    return (
        np.array(src_list, dtype=int),
        np.array(dst_list, dtype=int),
        np.array(rest_lengths, dtype=float),
    )


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
        # Transform from board to local (inverse of _to_board)
        rad = math.radians(fp.angle_deg)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        bx = pad.position.x_mm - fp.x_mm
        by = pad.position.y_mm - fp.y_mm
        if fp.layer == "B.Cu":
            # B.Cu was mirrored across X before rotation
            lx = (bx * cos_a + by * sin_a)  # reverse rotation
            ly = (-bx * sin_a + by * cos_a)
            lx = -lx  # reverse mirror
        else:
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


def run_placement(
    model: BoardModel,
    params: PlacementParams,
    movable_uuids: Optional[Set[str]] = None,
) -> PlacementProposal:
    """Run per-pad force-spring placement for ``model`` under ``params``.

    Args:
        model: Board model with footprints, nets, and board outline.
        params: Force-spring parameters.
        movable_uuids: Optional set of footprint UUIDs that are allowed to move.
            If None (default), all unlocked footprints can move. If provided,
            only footprints in this set AND not locked can move.

    Returns:
        PlacementProposal with per-footprint deltas (dx, dy, dangle) keyed by UUID.
    """
    start = time.perf_counter()

    # Collect pad nodes
    pads, pad_to_fp_idx, fp_uuid_to_pad_indices, global_pad_idx_to_local = _collect_pad_nodes(model)
    n_pads = len(pads)

    # Backward compatibility: stub mode (identity deltas)
    if params.stub and params.demo_jitter_mm == 0.0:
        return PlacementProposal(
            deltas={},
            iterations=0,
            final_max_disp_mm=0.0,
            elapsed_s=time.perf_counter() - start,
            params=params.to_dict(),
        )

    # Backward compatibility: demo jitter (works even with no nets)
    if params.demo_jitter_mm > 0:
        deltas: Dict[str, Tuple[float, float, float]] = {}
        for fp in model.footprints:
            if fp.locked or not fp.uuid:
                continue
            if movable_uuids is not None and fp.uuid not in movable_uuids:
                continue
            dx, dy = _jitter_for(fp.uuid, params.demo_jitter_mm)
            deltas[fp.uuid] = (dx, dy, 0.0)
        max_disp = max((math.hypot(dx, dy) for dx, dy, _ in deltas.values()), default=0.0)
        return PlacementProposal(
            deltas=deltas,
            iterations=0,
            final_max_disp_mm=max_disp,
            elapsed_s=time.perf_counter() - start,
            params=params.to_dict(),
        )

    # Physics mode requires pads with nets
    if n_pads == 0:
        return PlacementProposal(
            deltas={},
            iterations=0,
            final_max_disp_mm=0.0,
            elapsed_s=time.perf_counter() - start,
            params=params.to_dict(),
        )

    # Build initial pad positions array
    pad_positions = np.zeros((n_pads, 2), dtype=np.float64)
    for i, pad in enumerate(pads):
        pad_positions[i, 0] = pad.position.x_mm
        pad_positions[i, 1] = pad.position.y_mm

    # Determine which pads are movable (belong to movable footprints)
    fp_movable = {}
    for fp in model.footprints:
        if not fp.uuid or fp.locked:
            fp_movable[fp.uuid] = False
            continue
        if movable_uuids is not None and fp.uuid not in movable_uuids:
            fp_movable[fp.uuid] = False
        else:
            fp_movable[fp.uuid] = True

    pad_movable = np.array([fp_movable.get(model.footprints[pad_to_fp_idx[i]].uuid, False) for i in range(n_pads)], dtype=bool)

    if not pad_movable.any():
        return PlacementProposal(
            deltas={},
            iterations=0,
            final_max_disp_mm=0.0,
            elapsed_s=time.perf_counter() - start,
            params=params.to_dict(),
        )

    # Board outline bounds
    min_x, max_x, min_y, max_y = _board_bbox(model)

    # Net edges for attraction (pad-level)
    src_idx, dst_idx = _build_net_edges_pad_level(pads, pad_to_fp_idx)
    has_net_edges = len(src_idx) > 0

    # Rigid constraints (MST of pads within each footprint)
    rigid_src, rigid_dst, rigid_rest = _build_rigid_constraints_mst(
        pad_positions, fp_uuid_to_pad_indices
    )
    has_rigid = len(rigid_src) > 0

    # Ideal length for net springs
    board_area = (max_x - min_x) * (max_y - min_y)
    n_movable_pads = int(pad_movable.sum())
    ideal_len = math.sqrt(max(board_area, 1.0) / max(n_movable_pads, 1))

    # Temperature schedule (Fruchterman–Reingold)
    temp = max(max_x - min_x, max_y - min_y) / 10.0
    temp_min = params.convergence_eps_mm

    positions = pad_positions.copy()
    prev_positions = pad_positions.copy()

    for iteration in range(params.max_iterations):
        # Repulsion: all-pairs Coulomb k_r / d^2 (with softening epsilon)
        diff = positions[:, None, :] - positions[None, :, :]  # (n, n, 2)
        dist_sq = np.sum(diff * diff, axis=2)
        # Softening epsilon to avoid divide-by-zero when pads overlap
        eps = 1e-6
        np.fill_diagonal(dist_sq, eps)
        dist_sq = np.maximum(dist_sq, eps)
        dist = np.sqrt(dist_sq)
        force_mag = params.repulsion_kr / dist_sq
        np.fill_diagonal(force_mag, 0.0)
        force_vec = diff * (force_mag[:, :, None] / dist[:, :, None])
        repulsion = np.sum(force_vec, axis=1)  # (n, 2)

        # Attraction: Hooke's law along net edges
        attraction = np.zeros((n_pads, 2), dtype=np.float64)
        if has_net_edges:
            diff_edge = positions[dst_idx] - positions[src_idx]
            dist_edge = np.linalg.norm(diff_edge, axis=1, keepdims=True)
            dist_edge = np.maximum(dist_edge, 1e-6)
            unit = diff_edge / dist_edge
            force_mag_edge = params.attraction_ka * (dist_edge - ideal_len)
            force_vec_edge = unit * force_mag_edge
            np.add.at(attraction, src_idx, -force_vec_edge)
            np.add.at(attraction, dst_idx, force_vec_edge)

        # Rigid constraints: stiff springs to maintain footprint shape (damped)
        rigid_force = np.zeros((n_pads, 2), dtype=np.float64)
        if has_rigid:
            diff_rigid = positions[rigid_dst] - positions[rigid_src]
            dist_rigid = np.linalg.norm(diff_rigid, axis=1, keepdims=True)
            dist_rigid = np.maximum(dist_rigid, 1e-6)
            unit_rigid = diff_rigid / dist_rigid
            # Force magnitude: k * (d - L0) with damping factor
            force_mag_rigid = params.rigid_stiffness * 0.1 * (dist_rigid - rigid_rest[:, None])
            force_vec_rigid = unit_rigid * force_mag_rigid
            np.add.at(rigid_force, rigid_src, -force_vec_rigid)
            np.add.at(rigid_force, rigid_dst, force_vec_rigid)

        # Total force
        force = repulsion + attraction + rigid_force

        # Replace any NaN forces with zero
        force = np.nan_to_num(force, nan=0.0, posinf=0.0, neginf=0.0)

        # Zero force on non-movable pads
        force[~pad_movable] = 0.0

        # Displacement = force * temp (capped by temp)
        disp = force * temp
        disp_norm = np.linalg.norm(disp, axis=1, keepdims=True)
        scale = np.minimum(1.0, temp / np.maximum(disp_norm, 1e-6))
        disp *= scale

        # Update positions
        positions += disp

        # Replace any NaN positions with previous positions
        positions = np.nan_to_num(positions, nan=0.0, posinf=0.0, neginf=0.0)

        # Clamp to board outline
        positions[:, 0] = np.clip(positions[:, 0], min_x, max_x)
        positions[:, 1] = np.clip(positions[:, 1], min_y, max_y)

        # Convergence check
        max_disp = float(np.max(np.linalg.norm(positions - prev_positions, axis=1)))
        if max_disp < params.convergence_eps_mm:
            break

        # Cool temperature
        temp = max(temp * 0.95, temp_min)
        prev_positions = positions.copy()

    # Compute footprint deltas from final pad positions
    deltas = {}
    final_max_disp = 0.0
    for fp in model.footprints:
        if not fp.uuid or fp.locked:
            continue
        if movable_uuids is not None and fp.uuid not in movable_uuids:
            continue
        if fp.uuid not in fp_uuid_to_pad_indices:
            continue

        pad_indices = fp_uuid_to_pad_indices[fp.uuid]
        new_x, new_y, new_angle = _compute_footprint_pose_from_pads(
            fp, positions, pad_indices, global_pad_idx_to_local
        )

        dx = new_x - fp.x_mm
        dy = new_y - fp.y_mm
        # Normalize angle difference to [-180, 180]
        da = new_angle - fp.angle_deg
        da = (da + 180) % 360 - 180

        if dx != 0.0 or dy != 0.0 or da != 0.0:
            deltas[fp.uuid] = (dx, dy, da)
            final_max_disp = max(final_max_disp, math.hypot(dx, dy))

    return PlacementProposal(
        deltas=deltas,
        iterations=iteration + 1,
        final_max_disp_mm=final_max_disp,
        elapsed_s=time.perf_counter() - start,
        params=params.to_dict(),
    )