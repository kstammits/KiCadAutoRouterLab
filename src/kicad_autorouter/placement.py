"""Force-spring placement domain: parameters, proposals, and the runner interface.

The runner implements a vectorized numpy force-spring simulation at the PAD level
(Fruchterman–Reingold style) with rigid constraints between pads of the same
footprint. This allows through-hole components to rotate organically when forces
on their F.Cu and B.Cu pads differ.

Deltas are keyed by footprint UUID (dx_mm, dy_mm, dangle_deg) because refs may
be duplicated or missing; locked footprints never appear in ``deltas`` (they are
fixed anchors).

Supports multi-board region simulation: footprints are grouped by board outline
region (from Edge.Cuts) and each region is simulated independently with its own
bounding box and temperature schedule.
"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Dict, List, Optional, Set, Tuple

import numpy as np
from shapely.geometry import Polygon

from .board_model import BoardModel, BoardRegion, Footprint, Pad


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
    # Courtyard collision repulsion force constant
    courtyard_repulsion_kc: float = 2000.0
    # Boundary repulsion force constant (pushes footprints away from region edges)
    boundary_repulsion_kb: float = 100000.0
    # Preview-only knob for the v0 stub: deterministic per-UUID jitter so the
    # proposal overlay is visibly testable before real physics exists.
    demo_jitter_mm: float = 0.0
    # Backward-compatibility: when True, use v0 stub behavior (identity
    # deltas unless demo_jitter_mm > 0). When False (default), run the
    # vectorized numpy force-spring simulation.
    stub: bool = False

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
        "courtyard_repulsion_kc",
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
        "courtyard_repulsion_kc",
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
    Forces are (Fx, Fy) in arbitrary units for each movable footprint.
    """

    deltas: Dict[str, Tuple[float, float, float]]
    iterations: int
    final_max_disp_mm: float
    elapsed_s: float
    params: dict
    forces: Dict[str, Tuple[float, float]] = field(default_factory=dict)


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
    pads: List[Pad], pad_to_fp_idx: List[int],
    power_net_patterns: Optional[Set[str]] = None
) -> Tuple[np.ndarray, np.ndarray]:
    """Build edge index arrays for pad pairs sharing the same net.

    Returns (src_indices, dst_indices) where each is a 1D array of pad indices.
    Each pair appears once (i < j). Only connects pads on different footprints.
    """
    if power_net_patterns is None:
        power_net_patterns = {"GND", "VCC", "VDD", "VSS", "GND", "GROUND", "+12V", "+5V", "+3.3V", "+1.8V", "-12V", "-5V"}
    
    net_to_pads: Dict[str, List[int]] = {}
    for i, pad in enumerate(pads):
        if pad.net_name and pad.net_name not in power_net_patterns:
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


def _footprint_courtyard_polygon(fp: Footprint, pad_positions: Optional[np.ndarray] = None, pad_indices: Optional[List[int]] = None) -> Polygon:
    """Build a shapely Polygon from footprint's courtyard segments.

    If no courtyard, fall back to pad bounding box.
    
    Args:
        fp: The footprint
        pad_positions: Optional (n_pads, 2) array of current pad positions for this region
        pad_indices: Optional list of local pad indices for this footprint
    """
    if fp.courtyard:
        # Collect all vertices from courtyard segments
        vertices = []
        for a, b in fp.courtyard:
            vertices.append((a.x_mm, a.y_mm))
            vertices.append((b.x_mm, b.y_mm))
        if len(vertices) >= 3:
            try:
                poly = Polygon(vertices)
                if poly.is_valid and poly.area > 0:
                    return poly
            except Exception:
                pass
    # Fallback: pad bounding box
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
    model: "BoardModel",
    params: "PlacementParams",
    movable_uuids: Optional[Set[str]],
) -> Tuple[Dict[str, Tuple[float, float, float]], Dict[str, Tuple[float, float]], int]:
    """Run force-spring simulation for a single board region.

    Returns:
        - deltas: {uuid: (dx, dy, da)} for footprints in this region
        - forces: {uuid: (Fx, Fy)} for footprints in this region
        - iterations: number of iterations run
    """
    from .board_model import BoardRegion

    # Extract region bounds
    min_x, max_x, min_y, max_y = region.bbox

    # Filter pads for this region
    n_pads = len(region_pads)
    if n_pads == 0:
        return {}, {}, 0

    # Build pad positions array
    pad_positions = np.zeros((n_pads, 2), dtype=np.float64)
    for i, pad in enumerate(region_pads):
        pad_positions[i, 0] = pad.position.x_mm
        pad_positions[i, 1] = pad.position.y_mm

    # Determine movable pads
    fp_movable = {}
    for fp in model.footprints:
        if not fp.uuid or fp.locked:
            fp_movable[fp.uuid] = False
            continue
        # Check if footprint belongs to this region
        if model.footprint_region.get(fp.uuid) != region_idx:
            fp_movable[fp.uuid] = False
            continue
        if fp.uuid is None or fp.locked:
            fp_movable[fp.uuid] = False
        elif movable_uuids is not None and fp.uuid not in movable_uuids:
            fp_movable[fp.uuid] = False
        else:
            fp_movable[fp.uuid] = True

    pad_movable = np.array([
        fp_movable.get(model.footprints[region_pad_to_fp_idx[i]].uuid, False)
        for i in range(n_pads)
    ], dtype=bool)

    if not pad_movable.any():
        return {}, {}, 0

    # Power net patterns to exclude from attraction (connect globally, not for placement)
    power_net_patterns = {"GND", "VCC", "VDD", "VSS", "GND", "GROUND", "+12V", "+5V", "+3.3V", "+1.8V", "-12V", "-5V"}

    # Build net edges for this region
    pads_with_nets = [(i, pad) for i, pad in enumerate(region_pads) if pad.net_name]
    net_to_pads: Dict[str, List[int]] = {}
    for i, pad in pads_with_nets:
        if pad.net_name and pad.net_name not in power_net_patterns:
            net_to_pads.setdefault(pad.net_name, []).append(i)

    edges_set = set()
    for net_name, pad_indices in net_to_pads.items():
        if len(pad_indices) < 2:
            continue
        for i in pad_indices:
            for j in pad_indices:
                if i >= j:
                    continue
                if region_pad_to_fp_idx[i] != region_pad_to_fp_idx[j]:
                    edges_set.add((i, j))

    if edges_set:
        edges = sorted(edges_set)
        src_idx = np.array([e[0] for e in edges], dtype=int)
        dst_idx = np.array([e[1] for e in edges], dtype=int)
        has_net_edges = True
    else:
        src_idx = np.array([], dtype=int)
        dst_idx = np.array([], dtype=int)
        has_net_edges = False

    # Rigid constraints (MST of pads within each footprint in this region)
    rigid_src, rigid_dst, rigid_rest = _build_rigid_constraints_mst(
        pad_positions, region_fp_uuid_to_pad_indices
    )
    has_rigid = len(rigid_src) > 0

    # Build mapping from footprint UUID to local pad indices for boundary checks
    region_fp_to_pad_indices = {}
    for fp_uuid, pad_indices in region_fp_uuid_to_pad_indices.items():
        region_fp_to_pad_indices[fp_uuid] = pad_indices

    # Ideal length for net springs
    if params.ideal_length_mm > 0:
        ideal_len = params.ideal_length_mm
    else:
        region_area = (max_x - min_x) * (max_y - min_y)
        n_movable_pads = int(pad_movable.sum())
        ideal_len = math.sqrt(max(region_area, 1.0) / max(n_movable_pads, 1))

    # Temperature schedule (Fruchterman–Reingold)
    temp = max(max_x - min_x, max_y - min_y) / 10.0
    temp_min = params.convergence_eps_mm
    # Max displacement per iteration to prevent boundary crossing
    max_step = min(2.0, temp)

    iteration = 0

    # Build list of footprints in this region with courtyards
    fp_uuids = []
    fp_movable_list = []
    fp_pad_indices = {}  # uuid -> list of local pad indices in region
    pad_to_fp_local = []  # pad_idx -> local fp index in fp_uuids

    for fp in model.footprints:
        if not fp.uuid or model.footprint_region.get(fp.uuid) != region_idx:
            continue
        local_idx = len(fp_uuids)
        fp_uuids.append(fp.uuid)
        fp_movable_list.append(fp.uuid in fp_movable and fp_movable.get(fp.uuid, False))
        if fp.uuid in region_fp_uuid_to_pad_indices:
            fp_pad_indices[fp.uuid] = region_fp_uuid_to_pad_indices[fp.uuid]

    # O(1) lookup: uuid -> local index in fp_uuids
    fp_uuid_to_local_idx = {uuid: i for i, uuid in enumerate(fp_uuids)}

    for i in range(len(region_pads)):
        fp_uuid = model.footprints[region_pad_to_fp_idx[i]].uuid
        pad_to_fp_local.append(fp_uuid_to_local_idx.get(fp_uuid, -1))

    fp_movable_arr = np.array(fp_movable_list, dtype=bool)

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
        ideal_len: float,
        has_rigid: bool,
        rigid_src: np.ndarray,
        rigid_dst: np.ndarray,
        rigid_rest: np.ndarray,
    ) -> np.ndarray:
        """Compute total force on each pad at given positions."""
        # Repulsion: all-pairs Coulomb k_r / d^2
        diff = pos[:, None, :] - pos[None, :, :]  # (n, n, 2)
        dist_sq = np.sum(diff * diff, axis=2)
        eps = 1e-6
        np.fill_diagonal(dist_sq, eps)
        dist_sq = np.maximum(dist_sq, eps)
        dist = np.sqrt(dist_sq)
        force_mag = params.repulsion_kr / dist_sq
        np.fill_diagonal(force_mag, 0.0)
        force_vec = diff * (force_mag[:, :, None] / dist[:, :, None])
        repulsion = np.sum(force_vec, axis=1)

        # Attraction: Hooke's law along net edges
        attraction = np.zeros((n_pads, 2), dtype=np.float64)
        if has_net_edges:
            diff_edge = pos[dst_idx] - pos[src_idx]
            dist_edge = np.linalg.norm(diff_edge, axis=1, keepdims=True)
            dist_edge = np.maximum(dist_edge, 1e-6)
            unit = diff_edge / dist_edge
            force_mag_edge = params.attraction_ka * (dist_edge - ideal_len)
            force_vec_edge = unit * force_mag_edge
            np.add.at(attraction, src_idx, -force_vec_edge)
            np.add.at(attraction, dst_idx, force_vec_edge)

        # Rigid constraints
        rigid_force = np.zeros((n_pads, 2), dtype=np.float64)
        if has_rigid:
            diff_rigid = pos[rigid_dst] - pos[rigid_src]
            dist_rigid = np.linalg.norm(diff_rigid, axis=1, keepdims=True)
            dist_rigid = np.maximum(dist_rigid, 1e-6)
            unit_rigid = diff_rigid / dist_rigid
            force_mag_rigid = params.rigid_stiffness * 0.1 * (dist_rigid - rigid_rest[:, None])
            force_vec_rigid = unit_rigid * force_mag_rigid
            np.add.at(rigid_force, rigid_src, -force_vec_rigid)
            np.add.at(rigid_force, rigid_dst, force_vec_rigid)

        force = repulsion + attraction + rigid_force

        # Polygon-based courtyard collision (rebuild polygons from current positions)
        if fp_uuids:
            n_pads_local = pos.shape[0]
            poly_force = np.zeros((n_pads_local, 2), dtype=np.float64)

            # Build courtyard polygons at current positions
            courtyard_polys = {}
            fp_centroids = []
            fp_radii = []
            for fp_uuid in fp_uuids:
                local_idx = fp_uuid_to_local_idx[fp_uuid]
                pad_indices = fp_pad_indices.get(fp_uuid, [])
                if pad_indices:
                    poly = _footprint_courtyard_polygon(
                        next(fp for fp in model.footprints if fp.uuid == fp_uuid),
                        pad_positions=pos,
                        pad_indices=pad_indices,
                    )
                else:
                    # Fallback: find the footprint and use its original position
                    fp = next(fp for fp in model.footprints if fp.uuid == fp_uuid)
                    poly = _footprint_courtyard_polygon(fp)
                courtyard_polys[fp_uuid] = poly
                centroid = poly.centroid
                fp_centroids.append([centroid.x, centroid.y])
                max_extent = 0.5
                if poly.area > 0:
                    for x, y in poly.exterior.coords:
                        d = math.hypot(x - centroid.x, y - centroid.y)
                        if d > max_extent:
                            max_extent = d
                fp_radii.append(max_extent)

            fp_centroids_arr = np.array(fp_centroids, dtype=np.float64)
            fp_radii_arr = np.array(fp_radii, dtype=np.float64)

            # Compute collision forces per footprint pair, then distribute to pads
            n_fp = len(fp_uuids)
            fp_forces = np.zeros((n_fp, 2), dtype=np.float64)

            for i in range(n_fp):
                if not fp_movable_arr[i]:
                    continue
                fp_i_uuid = fp_uuids[i]
                poly_i = courtyard_polys[fp_i_uuid]

                for j in range(i + 1, n_fp):
                    if not fp_movable_arr[j]:
                        continue
                    fp_j_uuid = fp_uuids[j]
                    poly_j = courtyard_polys[fp_j_uuid]

                    # Check overlap
                    if poly_i.intersects(poly_j):
                        intersection = poly_i.intersection(poly_j)
                        if intersection.area > 0:
                            # Penetration vector from intersection centroid to poly_i centroid
                            inter_centroid = intersection.centroid
                            poly_i_centroid = poly_i.centroid
                            dx = poly_i_centroid.x - inter_centroid.x
                            dy = poly_i_centroid.y - inter_centroid.y
                            dist = math.hypot(dx, dy)
                            if dist > 1e-6:
                                # Force proportional to overlap area
                                force_mag = params.courtyard_repulsion_kc * intersection.area / (dist + 1e-6)
                                fx = force_mag * dx / dist
                                fy = force_mag * dy / dist
                                fp_forces[i, 0] += fx
                                fp_forces[i, 1] += fy
                                fp_forces[j, 0] -= fx
                                fp_forces[j, 1] -= fy
                    else:
                        # Near miss: distance-based falloff
                        dist = poly_i.distance(poly_j)
                        if dist < 1.5 * (fp_radii_arr[i] + fp_radii_arr[j]):
                            # Gentle repulsion
                            min_dist = fp_radii_arr[i] + fp_radii_arr[j]
                            margin = min_dist * 1.5 - dist
                            if margin > 0:
                                # Direction from j centroid to i centroid
                                dx = fp_centroids_arr[i, 0] - fp_centroids_arr[j, 0]
                                dy = fp_centroids_arr[i, 1] - fp_centroids_arr[j, 1]
                                dist = math.hypot(dx, dy)
                                if dist > 1e-6:
                                    force_mag = params.courtyard_repulsion_kc * 0.1 * margin / (dist * dist + 1e-6)
                                    fx = force_mag * dx / dist
                                    fy = force_mag * dy / dist
                                    fp_forces[i, 0] += fx
                                    fp_forces[i, 1] += fy
                                    fp_forces[j, 0] -= fx
                                    fp_forces[j, 1] -= fy

            # Distribute footprint forces to pads
            for pad_idx in range(n_pads_local):
                fp_i_local = pad_to_fp_local[pad_idx]
                if fp_i_local >= 0 and fp_movable_arr[fp_i_local]:
                    poly_force[pad_idx, 0] = fp_forces[fp_i_local, 0]
                    poly_force[pad_idx, 1] = fp_forces[fp_i_local, 1]

            force += poly_force

            # Boundary repulsion: push footprints away from region edges
            n_pads_local = pos.shape[0]
            boundary_force = np.zeros((n_pads_local, 2), dtype=np.float64)
            boundary_k = params.boundary_repulsion_kb

            # Compute current footprint bounds from pad positions, expanded by courtyard radius
            fp_bounds = {}
            for fp_uuid, pad_indices in fp_pad_indices.items():
                if pad_indices:
                    fp_pad_pos = pos[pad_indices]
                    # Get courtyard radius for this footprint
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

                # Distance to region boundaries
                # Left edge
                dist_left = minx - min_x
                if dist_left < 0:
                    # Already outside - strong push back
                    force_mag = boundary_k * abs(dist_left) / (abs(dist_left) + 1e-6)
                    boundary_force[pad_idx, 0] += force_mag
                elif dist_left < 5.0:
                    # Close to edge - strong repulsion
                    force_mag = boundary_k * 1.5 * (5.0 - dist_left) / (dist_left + 1e-6)
                    boundary_force[pad_idx, 0] += force_mag

                # Right edge
                dist_right = max_x - maxx
                if dist_right < 0:
                    force_mag = boundary_k * abs(dist_right) / (abs(dist_right) + 1e-6)
                    boundary_force[pad_idx, 0] -= force_mag
                elif dist_right < 5.0:
                    force_mag = boundary_k * 1.5 * (5.0 - dist_right) / (dist_right + 1e-6)
                    boundary_force[pad_idx, 0] -= force_mag

                # Bottom edge
                dist_bottom = miny - min_y
                if dist_bottom < 0:
                    force_mag = boundary_k * abs(dist_bottom) / (abs(dist_bottom) + 1e-6)
                    boundary_force[pad_idx, 1] += force_mag
                elif dist_bottom < 5.0:
                    force_mag = boundary_k * 1.5 * (5.0 - dist_bottom) / (dist_bottom + 1e-6)
                    boundary_force[pad_idx, 1] += force_mag

                # Top edge
                dist_top = max_y - maxy
                if dist_top < 0:
                    force_mag = boundary_k * abs(dist_top) / (abs(dist_top) + 1e-6)
                    boundary_force[pad_idx, 1] -= force_mag
                elif dist_top < 5.0:
                    force_mag = boundary_k * 1.5 * (5.0 - dist_top) / (dist_top + 1e-6)
                    boundary_force[pad_idx, 1] -= force_mag

            force += boundary_force

        # Replace NaN
        force = np.nan_to_num(force, nan=0.0, posinf=0.0, neginf=0.0)
        force[~pad_movable] = 0.0
        return force
    for iteration in range(params.max_iterations):
        force = _compute_total_force(
            positions, len(region_pads), pad_movable,
            params, has_net_edges, src_idx, dst_idx, ideal_len,
            has_rigid, rigid_src, rigid_dst, rigid_rest,
        )

        # Displacement = force * temp (capped by temp)
        disp = force * temp
        disp_norm = np.linalg.norm(disp, axis=1, keepdims=True)
        scale = np.minimum(1.0, temp / np.maximum(disp_norm, 1e-6))
        disp *= scale

        # Cap max step size to prevent boundary crossing
        disp_norm = np.linalg.norm(disp, axis=1, keepdims=True)
        scale = np.minimum(1.0, max_step / np.maximum(disp_norm, 1e-6))
        disp *= scale

        # Update positions
        positions += disp
        positions = np.nan_to_num(positions, nan=0.0, posinf=0.0, neginf=0.0)

        # Clamp to region bounds
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
    footprint_forces = {}

    # Recompute final forces for force reporting
    final_force = _compute_total_force(
        positions, len(region_pads), pad_movable,
        params, has_net_edges, src_idx, dst_idx, ideal_len,
        has_rigid, rigid_src, rigid_dst, rigid_rest,
    )

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

        if dx != 0.0 or dy != 0.0 or da != 0.0:
            deltas[fp.uuid] = (dx, dy, da)
            final_max_disp = max(final_max_disp, math.hypot(dx, dy))

        # Force for this footprint
        if pad_indices:
            fp_force = final_force[pad_indices].sum(axis=0)
            footprint_forces[fp.uuid] = (float(fp_force[0]), float(fp_force[1]))

    return deltas, footprint_forces, iteration + 1


def run_placement(
    model: BoardModel,
    params: PlacementParams,
    movable_uuids: Optional[Set[str]] = None,
) -> PlacementProposal:
    """Run per-pad force-spring placement for ``model`` under ``params``.

    Supports multi-board region simulation: footprints are grouped by board
    outline region (from Edge.Cuts) and each region is simulated independently
    with its own bounding box and temperature schedule.

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
            forces={},
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
            forces={},
        )

    # Physics mode requires pads with nets
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
        total_iterations = 0
        max_final_disp = 0.0

        for region_idx, region in enumerate(model.board_regions):
            # Collect pads for this region
            region_pad_indices = []
            region_pads = []
            region_pad_to_fp_idx = []
            region_fp_uuid_to_pad_indices = {}
            region_global_pad_idx_to_local = {}

            for i, pad in enumerate(pads):
                fp_idx = pad_to_fp_idx[i]
                fp = model.footprints[fp_idx]
                if model.footprint_region.get(fp.uuid) == region_idx:
                    local_idx = len(region_pads)
                    region_pad_indices.append(i)
                    region_pads.append(pad)
                    region_pad_to_fp_idx.append(fp_idx)
                    if fp.uuid not in region_fp_uuid_to_pad_indices:
                        region_fp_uuid_to_pad_indices[fp.uuid] = []
                    region_fp_uuid_to_pad_indices[fp.uuid].append(local_idx)
                    region_global_pad_idx_to_local[local_idx] = global_pad_idx_to_local[i]

            if region_pads:
                region_deltas, region_forces, region_iters = _run_region_simulation(
                    region_idx, model.board_regions[region_idx],
                    region_pad_indices, region_pads, region_pad_to_fp_idx,
                    region_fp_uuid_to_pad_indices, region_global_pad_idx_to_local,
                    model, params, movable_uuids
                )
                all_deltas.update(region_deltas)
                all_forces.update(region_forces)
                total_iterations = max(total_iterations, region_iters)
                max_final_disp = max(max_final_disp, max((math.hypot(dx, dy) for dx, dy, _ in region_deltas.values()), default=0.0))

        return PlacementProposal(
            deltas=all_deltas,
            iterations=total_iterations,
            final_max_disp_mm=max_final_disp,
            elapsed_s=time.perf_counter() - start,
            params=params.to_dict(),
            forces=all_forces,
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

    region_deltas, region_forces, region_iters = _run_region_simulation(
        0, virtual_region,
        region_pad_indices, region_pads, region_pad_to_fp_idx,
        region_fp_uuid_to_pad_indices, region_global_pad_idx_to_local,
        model, params, movable_uuids
    )

    return PlacementProposal(
        deltas=region_deltas,
        iterations=region_iters,
        final_max_disp_mm=max((math.hypot(dx, dy) for dx, dy, _ in region_deltas.values()), default=0.0),
        elapsed_s=time.perf_counter() - start,
        params=params.to_dict(),
        forces=region_forces,
    )
