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

import math
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Dict, List, Optional, Set, Tuple

import numpy as np
from shapely.geometry import Polygon
from shapely.ops import polygonize, unary_union

from .board_model import BoardModel, BoardRegion, Footprint, Pad, Segment


# Nets that connect globally (power distribution), not for placement:
# attraction springs on these would drag every powered part together.
_POWER_NET_PATTERNS = frozenset({
    "GND", "VCC", "VDD", "VSS", "GROUND",
    "+12V", "+5V", "+3.3V", "+1.8V", "-12V", "-5V",
})


@dataclass(frozen=True)
class PlacementParams:
    """Tunable force-spring parameters (v1 set; grows with the simulation)."""

    repulsion_kr: float = 100.0
    attraction_ka: float = 0.05
    ideal_length_mm: float = 25.0
    max_iterations: int = 1000
    convergence_eps_mm: float = 0.01
    # Rigid constraint stiffness (high to keep footprint pads together)
    rigid_stiffness: float = 5e5
    # Courtyard collision repulsion force constant. Deliberately modest:
    # the integrator caps every step, so extra magnitude only buys
    # overshoot oscillation, not faster separation (2026-10-10).
    courtyard_repulsion_kc: float = 2000.0
    # Near-miss halo: courtyard interference reach past part outlines (mm).
    # Caps the size-scaled 1.5x(radius_i + radius_j) halo so large parts
    # don't push across the board; strength saturates at kc at contact.
    courtyard_halo_mm: float = 3.0
    # Boundary repulsion force constant (pushes footprints away from region
    # edges). Kept at ~2x kc: decisive at the edge (max 2*kb) without
    # shoving edge-near parts into their neighbors at 100x the force that
    # separates them (2026-10-10: kb was 5e5).
    boundary_repulsion_kb: float = 20000.0
    # Rotation rate limit (degrees per iteration). Translation caps are
    # per-pad, which otherwise synthesizes pure spin (one pad takes the
    # full budget while its mate takes ~0: tens of degrees/iter on 2-4mm
    # parts, read out later by Kabsch). 15deg still allows a full turn
    # in 24 iterations -- THT reorientation into aligned wells needs it
    # (3deg starves magnet clustering) -- while clipping the worst
    # helicoptering 4x. Translation is bit-identical with the cap on or
    # off; only spin is bounded.
    max_dangle_deg: float = 15.0

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
        "rigid_stiffness",
        "courtyard_repulsion_kc",
        "courtyard_halo_mm",
        "boundary_repulsion_kb",
        "max_dangle_deg",
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
        "courtyard_halo_mm",
        "boundary_repulsion_kb",
        "max_dangle_deg",
    ):
        if getattr(params, name) <= 0:
            raise ValueError(f"{name} must be > 0")
    if isinstance(params.max_iterations, bool) or not isinstance(
        params.max_iterations, int
    ):
        raise ValueError("max_iterations must be an integer")
    if params.max_iterations < 1:
        raise ValueError("max_iterations must be >= 1")


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
    force_components: Dict[str, Dict[str, Tuple[float, float]]] = field(
        default_factory=dict
    )


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


def _build_net_edges_pad_level(
    pads: List[Pad], pad_to_fp_idx: List[int],
    power_net_patterns: Optional[Set[str]] = None
) -> Tuple[np.ndarray, np.ndarray]:
    """Build edge index arrays for pad pairs sharing the same net.

    Returns (src_indices, dst_indices) where each is a 1D array of pad indices.
    Each pair appears once (i < j). Only connects pads on different footprints.
    """
    if power_net_patterns is None:
        power_net_patterns = _POWER_NET_PATTERNS
    
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
    Weights average exactly 1, so the footprint total is conserved and no
    force is created or destroyed -- only its moment arm changes. A single
    pad gets exactly 1.
    """
    w = 1.0 - np.asarray(proj, dtype=np.float64)
    m = float(w.mean()) if len(w) else 1.0
    if math.isfinite(m) and abs(m) > 1e-9:
        return w / m
    return np.ones_like(w)


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


def _boundary_edge_push(dist_mm: float, threshold_mm: float, kb: float) -> float:
    """Inward push from one region edge: bounded linear falloff.

    Returns 0 past the threshold, rising linearly to 2*kb exactly at the
    edge; already-outside parts get an ~kb saturating pull back in.
    Deliberately singularity-free: the old ``kb*2*(t-d)/(d+eps)`` form
    blew up to ~3.5e7 on a part sitting ~0.03mm inside the region edge
    (2026-10-10, DCCF R11) and only the per-iteration step cap masked it.
    """
    if dist_mm < 0:
        d = abs(dist_mm)
        return kb * d / (d + 1e-6)
    if dist_mm < threshold_mm:
        return kb * 2.0 * (threshold_mm - dist_mm) / threshold_mm
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
    """
    lines: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for a, b in segments or ():
        p = (a.x_mm, a.y_mm)
        q = (b.x_mm, b.y_mm)
        if math.hypot(q[0] - p[0], q[1] - p[1]) > 1e-9:
            lines.append((p, q))
    if not lines:
        return None
    try:
        rings = [r for r in polygonize(lines) if r.is_valid and r.area > 0]
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
        return {}, {}, 0, {}

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

    if not pad_movable.any():
        return {}, {}, 0, {}

    # Net attraction edges for this region. Same builder as the tests pin
    # (layer-blind by design: cross-layer electrical nearness keeps
    # working through springs; courtyard collision is what honors layers).
    src_idx, dst_idx = _build_net_edges_pad_level(
        region_pads, region_pad_to_fp_idx
    )
    has_net_edges = len(src_idx) > 0

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
    # Max displacement per iteration: 1mm keeps parts inside the halo
    # gradient instead of vaulting across it in one step (halo reach is
    # 3mm; the old 2mm budget crossed most of it at once, landing in
    # overlap past the restoring slope).
    max_step = min(1.0, temp)

    iteration = 0

    # Build list of footprints in this region with courtyards (real + ghost)
    fp_uuids = []
    fp_movable_list = []
    fp_pad_indices = {}  # uuid -> list of local pad indices in region
    pad_to_fp_local = []  # pad_idx -> local fp index in fp_uuids

    # Real footprints
    for fp in model.footprints:
        if not fp.uuid or model.footprint_region.get(fp.uuid) != region_idx:
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
    # Pads whose displacement the F2 rigid-projection may touch: movable
    # pads of movable footprints (immobile pads carry zero disp anyway).
    if len(fp_uuids):
        _touch_static = pad_movable & (_pad_fp_idx >= 0) & fp_movable_arr[
            np.clip(_pad_fp_idx, 0, len(fp_uuids) - 1)
        ]
    else:
        _touch_static = np.zeros(len(region_pads), dtype=bool)

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

        Only the pose-independent parts are cached here (branch resolution +
        local vertices); per-iteration centroids/radii are derived exactly as
        before from the posed polygons. Both entry points share
        _courtyard_outline_polygon, so the branch order (courtyard-first,
        then pad-bbox, then 1x1 point fallback) stays in sync by
        construction.

        kind 'courtyard': verts posed per iter (or left static when the live
        pad mapping degenerates — same rule as _transform_vertices).
        kind 'bbox': bounds follow CURRENT pads (recomputed per iter).
        kind 'point': static 1x1 rect (padless footprint fallback).
        """
        if _fp is None:
            return {"kind": "empty", "empty": True}
        outline = _courtyard_outline_polygon(_fp.courtyard)
        if outline is not None:
            return {
                "kind": "courtyard",
                "empty": False,
                "verts": np.array(outline.exterior.coords, dtype=np.float64),
            }
        if _fp.pads:
            # Pad-bbox fallback: bounds track current pads, resolved per iter.
            return {"kind": "bbox", "empty": False}
        _cx, _cy = _fp.x_mm, _fp.y_mm
        return {
            "kind": "point",
            "empty": False,
            "verts": np.array([
                [_cx - 0.5, _cy - 0.5], [_cx + 0.5, _cy - 0.5],
                [_cx + 0.5, _cy + 0.5], [_cx - 0.5, _cy + 0.5],
            ], dtype=np.float64),
        }

    cy_cache: Dict[str, dict] = {}
    for _uuid in fp_uuids:
        _fp = fp_by_uuid.get(_uuid)
        _entry = _local_shape(_fp)
        _pad_idx = np.array(fp_pad_indices.get(_uuid, []), dtype=int)
        _entry["pad_idx"] = _pad_idx
        # Layer model for courtyard collision (2026-10-09): bodies collide on
        # their placement side; through-hole leads protrude through the board,
        # so any pair involving THT pads collides conservatively on both
        # sides. (Refinement to per-pad layer checks is future work — the
        # conservative rule is safe for THT-heavy boards like DCCF.) No
        # same-net exemption: soldering/silkscreen spacing is net-agnostic;
        # cross-layer electrical nearness keeps working via layer-blind
        # attraction springs.
        _entry["layer"] = _fp.layer if _fp is not None else ""
        _entry["has_tht"] = bool(
            _fp is not None and any(p.is_through_hole for p in _fp.pads)
        )
        if _entry["kind"] == "courtyard" and _fp is not None:
            # Original positions of ALL footprint pads (not just the netted
            # subset): _transform_vertices only applies when this matches the
            # live pad count, else the courtyard stays untransformed. Pads
            # without nets are skipped by _collect_pad_nodes, so footprints
            # with netless pads (mounting holes etc.) never transform — the
            # length check below replicates that exactly.
            _entry["orig_pos"] = np.array(
                [(p.position.x_mm, p.position.y_mm) for p in _fp.pads],
                dtype=np.float64,
            )
        cy_cache[_uuid] = _entry

    # Same-footprint mask: no repulsion between pads of one footprint.
    # Vectorized form of the old O(n^2) Python loop (identical result:
    # same local fp index, non-negative so ghosts never match each other).
    _fp_col = np.array(pad_to_fp_local, dtype=int)
    same_fp_mask = (
        (_fp_col[:, None] == _fp_col[None, :])
        & (_fp_col[:, None] >= 0)
        & ~np.eye(len(region_pads), dtype=bool)
    )

    # Build ghost attraction edges: (ghost_anchor, target_pad_indices, ideal_len, ka)
    ghost_attractions = []
    for fp in model.ghost_footprints:
        if not fp.uuid or model.footprint_region.get(fp.uuid) != region_idx:
            continue
        if not fp.ghost_attractions:
            continue
        # Ghost anchor is the footprint's position
        ghost_anchor = np.array([fp.x_mm, fp.y_mm], dtype=np.float64)
        for target_ref, ideal_len_attr, ka_attr in fp.ghost_attractions:
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
            ghost_attractions.append((ghost_anchor, target_pad_indices, ideal_len_attr, ka_attr))

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
        ghost_attractions: list,
        same_fp_mask: np.ndarray = None,
        return_components: bool = False,
    ):
        """Compute total force on each pad at given positions.

        With ``return_components`` also returns the per-cause breakdown
        ``{cause: (n_pads, 2)}`` for ``repulsion``/``attraction``/``rigid``/
        ``ghost``/``courtyard``/``boundary``. The sim loop leaves it off
        (it only needs the total); the end-of-run reporting recompute turns
        it on for the forces overlay.
        """
        # Repulsion: all-pairs Coulomb k_r / d^2
        diff = pos[:, None, :] - pos[None, :, :]  # (n, n, 2)
        dist_sq = np.sum(diff * diff, axis=2)
        eps = 1e-6
        np.fill_diagonal(dist_sq, eps)
        dist_sq = np.maximum(dist_sq, eps)
        dist = np.sqrt(dist_sq)
        force_mag = params.repulsion_kr / dist_sq
        np.fill_diagonal(force_mag, 0.0)
        if same_fp_mask is not None:
            force_mag[same_fp_mask] = 0.0  # No repulsion between pads of same footprint
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

        # Ghost attraction: centroid-to-centroid springs from ghost to target
        ghost_attr_force = np.zeros((n_pads, 2), dtype=np.float64)
        if ghost_attractions:
            for ghost_anchor, target_pad_indices, ideal_len_attr, ka_attr in ghost_attractions:
                if not target_pad_indices:
                    continue
                # Compute target centroid from current pad positions
                target_centroid = np.mean(pos[target_pad_indices], axis=0)
                # Vector from ghost anchor to target centroid
                diff_attr = target_centroid - ghost_anchor
                dist_attr = np.linalg.norm(diff_attr)
                if dist_attr > 1e-6:
                    unit_attr = diff_attr / dist_attr
                    force_mag_attr = ka_attr * (dist_attr - ideal_len_attr)
                    # Force on target points TOWARD ghost (opposite to unit_attr which points ghost->target)
                    force_vec_attr = -unit_attr * force_mag_attr
                    # Apply equally to all target pads (distribute)
                    force_per_pad = force_vec_attr / len(target_pad_indices)
                    for pad_idx in target_pad_indices:
                        ghost_attr_force[pad_idx] += force_per_pad
                    # Ghost is fixed - no reaction force applied to ghost pads

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

        force = repulsion + attraction + rigid_force + ghost_attr_force

        # Initialized here so the components breakdown below is well-defined
        # even when there are no footprints with courtyards in this region.
        n_pads_local = pos.shape[0]
        poly_force = np.zeros((n_pads_local, 2), dtype=np.float64)
        boundary_force = np.zeros((n_pads_local, 2), dtype=np.float64)

        # Polygon-based courtyard collision from cached shapes.
        # Per iteration only rigid poses change, posed through the shared
        # _rigid_pose_vertices kernel (same rule the old per-vertex Python
        # loop used, so force math is unchanged) while shapely
        # Polygons/centroids/radii are derived exactly as before. A vectorized bounding-circle
        # broadphase then culls far pairs (+1e-9 margin) so the expensive
        # pairwise exact calls (intersects/intersection/distance) run only
        # for pairs within halo reach — the common far-field case costs one
        # numpy op and zero shapely pair calls. Force math for surviving
        # pairs is byte-for-byte the old logic.
        n_fp = len(fp_uuids)
        if n_fp:
            courtyard_polys: dict[str, Polygon] = {}
            fp_centroids: list[list[float]] = []
            fp_radii: list[float] = []
            for fp_uuid in fp_uuids:
                e = cy_cache[fp_uuid]
                kind = e["kind"]
                if kind == "courtyard" and len(e["pad_idx"]) == len(e.get("orig_pos", [])) \
                        and len(e["pad_idx"]) > 0:
                    cp = pos[e["pad_idx"]]
                    poly = Polygon(_rigid_pose_vertices(
                        e["verts"], e["orig_pos"], cp
                    ))
                elif kind == "courtyard":
                    # No live pads (length mismatch): old code left the
                    # courtyard untransformed — static local polygon.
                    poly = Polygon(e["verts"])
                elif kind == "bbox":
                    idx = e["pad_idx"]
                    if len(idx):
                        xs = pos[idx, 0]
                        ys = pos[idx, 1]
                        x0, x1 = float(xs.min()), float(xs.max())
                        y0, y1 = float(ys.min()), float(ys.max())
                    else:
                        _fp0 = fp_by_uuid.get(fp_uuid)
                        xs = [p.position.x_mm for p in _fp0.pads] if _fp0 and _fp0.pads else [0.0]
                        ys = [p.position.y_mm for p in _fp0.pads] if _fp0 and _fp0.pads else [0.0]
                        x0, x1 = min(xs), max(xs)
                        y0, y1 = min(ys), max(ys)
                    m = 0.1
                    poly = Polygon([
                        (x0 - m, y0 - m), (x1 + m, y0 - m),
                        (x1 + m, y1 + m), (x0 - m, y1 + m),
                    ])
                elif kind == "point":
                    poly = Polygon(e["verts"])
                else:
                    poly = Polygon()
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

            # Vectorized broadphase on the exact values above: a pair can
            # interact only if centroid distance < ri + rj + halo_reach
            # (outline gap >= centroid gap - ri - rj). The +1e-9 margin keeps
            # boundary-straddling pairs on the exact path, so the cull can
            # only skip provably non-interacting pairs.
            # Layer gate (2026-10-09): bodies collide on their placement
            # side; a pair involving through-hole pads additionally collides
            # across sides (protruding leads). Cross-layer SMD-SMD pairs skip
            # exact geometry entirely — this also shrinks pair work on
            # mixed-side boards like tube111.
            _lyr = np.array([cy_cache[u].get("layer", "") for u in fp_uuids])
            _tht = np.array([bool(cy_cache[u].get("has_tht", False)) for u in fp_uuids])
            _d = fp_centroids_arr[:, None, :] - fp_centroids_arr[None, :, :]
            _dist = np.sqrt(np.sum(_d * _d, axis=2))
            _halo = np.minimum(1.5 * (fp_radii_arr[:, None] + fp_radii_arr[None, :]),
                               params.courtyard_halo_mm)
            _near = _dist < fp_radii_arr[:, None] + fp_radii_arr[None, :] + _halo + 1e-9
            _sides = (
                (_lyr[:, None] == _lyr[None, :]) | _tht[:, None] | _tht[None, :]
            )
            _close = _near & _sides

            # Spread one pair-push over a footprint's pads, weighted by
            # projection onto the push direction: contact-side pads take
            # more, far-side pads less. A shove then also twists the part
            # to glance off (corrective moment) instead of translating
            # straight through its neighbor -- torque without any new
            # geometry. Weights average exactly 1, so the footprint total
            # (and the reported components) is conserved.
            def _spread(rows, cx, cy, radius, ux, uy, fx, fy):
                if rows is None or len(rows) == 0:
                    return
                r = float(radius)
                if not math.isfinite(r) or r < 1e-9:
                    poly_force[rows, 0] += fx
                    poly_force[rows, 1] += fy
                    return
                proj = ((pos[rows, 0] - cx) * ux + (pos[rows, 1] - cy) * uy) / r
                w = _contact_weights(proj)
                poly_force[rows, 0] += w * fx
                poly_force[rows, 1] += w * fy

            # Compute collision forces per footprint pair, then distribute to pads.
            # Movable footprints collide with EVERYTHING (movable, locked,
            # unselected, ghosts) regardless of file order: a selected part
            # must feel an immobile part's courtyard even when the immobile
            # part sorts earlier (2026-10-10: R11 slipped into RV2's
            # courtyard because the old j>i loop never evaluated the pair).
            # Movable-movable pairs are still evaluated exactly once (j > i);
            # the j < i arm below only adds pairs with a non-movable j.
            for i in range(n_fp):
                if not fp_movable_arr[i]:
                    continue  # Only movable footprints accumulate forces
                fp_i_uuid = fp_uuids[i]
                poly_i = courtyard_polys[fp_i_uuid]

                for j in range(n_fp):
                    if j == i:
                        continue
                    if j < i and fp_movable_arr[j]:
                        continue  # movable-movable pair already done from the other side
                    if not _close[i, j]:
                        continue  # Far field, or opposite sides (SMD-SMD)
                    fp_j_uuid = fp_uuids[j]
                    poly_j = courtyard_polys[fp_j_uuid]

                    # Check overlap (including ghost footprints). Lenient with
                    # degenerate outlines: empty polygons never collide.
                    if poly_i.is_empty or poly_j.is_empty:
                        continue
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
                                ux, uy = dx / dist, dy / dist
                                fx, fy = force_mag * ux, force_mag * uy
                                _spread(fp_local_to_rows[i], fp_centroids_arr[i, 0], fp_centroids_arr[i, 1],
                                        fp_radii_arr[i], ux, uy, fx, fy)
                                # Equal/opposite on j (only matters if j moves)
                                if fp_movable_arr[j]:
                                    _spread(fp_local_to_rows[j], fp_centroids_arr[j, 0], fp_centroids_arr[j, 1],
                                            fp_radii_arr[j], -ux, -uy, -fx, -fy)
                    else:
                        # Near miss: bounded-halo linear falloff.
                        # Reach is capped at an absolute halo past the part
                        # outlines so giant parts (e.g. DCCF RV sockets with
                        # r~37mm) don't project ~60mm halos: a quarter inch of
                        # clearance then means no interference, like DRC
                        # thinking. Strength saturates at kc (fraction of the
                        # halo consumed) instead of growing with part size as
                        # the old kc*margin/d^2 did. (Emptiness was already
                        # checked above via the shape cache.)
                        poly_dist = poly_i.distance(poly_j)
                        # Same reach the broadphase above culled on (matrix
                        # form); reuse it instead of recomputing per pair.
                        halo_reach = float(_halo[i, j])
                        if poly_dist < halo_reach:
                            # Gentle repulsion - use polygon distance for margin (more accurate for asymmetric shapes)
                            margin = halo_reach - poly_dist
                            if margin > 0:
                                # Direction from j centroid to i centroid
                                dx = fp_centroids_arr[i, 0] - fp_centroids_arr[j, 0]
                                dy = fp_centroids_arr[i, 1] - fp_centroids_arr[j, 1]
                                centroid_dist = math.hypot(dx, dy)
                                if centroid_dist > 1e-6:
                                    strength = (
                                        params.courtyard_repulsion_kc
                                        * (margin / halo_reach)
                                    )
                                    ux, uy = dx / centroid_dist, dy / centroid_dist
                                    fx, fy = strength * ux, strength * uy
                                    # Halo nudges stay uniform broadcasts:
                                    # weighting them would inject a moment
                                    # into every near-miss and churn crowded
                                    # boards instead of settling them; the
                                    # corrective moment applies to true
                                    # overlap only (branch above).
                                    poly_force[fp_local_to_rows[i], 0] += fx
                                    poly_force[fp_local_to_rows[i], 1] += fy
                                    if fp_movable_arr[j]:
                                        poly_force[fp_local_to_rows[j], 0] -= fx
                                        poly_force[fp_local_to_rows[j], 1] -= fy

            # Courtyard pushes were spread directly onto pads above
            # (overlap-weighted, halo uniform; totals conserved); nothing
            # left to broadcast.
            force += poly_force

            # Boundary repulsion: push footprints away from region edges
            boundary_k = params.boundary_repulsion_kb

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

                # Distance to region boundaries (bounded pushes, see
                # _boundary_edge_push; left/bottom push +, right/top push -)
                dist_left = minx - min_x
                boundary_force[pad_idx, 0] += _boundary_edge_push(dist_left, threshold, boundary_k)

                dist_right = max_x - maxx
                boundary_force[pad_idx, 0] -= _boundary_edge_push(dist_right, threshold, boundary_k)

                dist_bottom = miny - min_y
                boundary_force[pad_idx, 1] += _boundary_edge_push(dist_bottom, threshold, boundary_k)

                dist_top = max_y - maxy
                boundary_force[pad_idx, 1] -= _boundary_edge_push(dist_top, threshold, boundary_k)

            force += boundary_force

        # Replace NaN
        force = np.nan_to_num(force, nan=0.0, posinf=0.0, neginf=0.0)
        force[~pad_movable] = 0.0
        if not return_components:
            return force
        components = {
            "repulsion": np.nan_to_num(repulsion, nan=0.0, posinf=0.0, neginf=0.0),
            "attraction": np.nan_to_num(attraction, nan=0.0, posinf=0.0, neginf=0.0),
            "rigid": np.nan_to_num(rigid_force, nan=0.0, posinf=0.0, neginf=0.0),
            "ghost": np.nan_to_num(ghost_attr_force, nan=0.0, posinf=0.0, neginf=0.0),
            "courtyard": np.nan_to_num(poly_force, nan=0.0, posinf=0.0, neginf=0.0),
            "boundary": np.nan_to_num(boundary_force, nan=0.0, posinf=0.0, neginf=0.0),
        }
        for comp in components.values():
            comp[~pad_movable] = 0.0
        return force, components
    # Initial forces predict the NEXT step from the committed board positions.
    # They are captured before the loop so the overlay explains the move it
    # is about to make (F0 -> D0), not the residual at the preview end
    # (F1 at pos1, which previously pointed back after overshoot and looked
    # like the integrator moved against the displayed force).
    initial_force, initial_components = _compute_total_force(
        positions, len(region_pads), pad_movable,
        params, has_net_edges, src_idx, dst_idx, ideal_len,
        has_rigid, rigid_src, rigid_dst, rigid_rest,
        ghost_attractions,
        same_fp_mask,
        return_components=True,
    )
    for iteration in range(params.max_iterations):
        force = _compute_total_force(
            positions, len(region_pads), pad_movable,
            params, has_net_edges, src_idx, dst_idx, ideal_len,
            has_rigid, rigid_src, rigid_dst, rigid_rest,
            ghost_attractions,
            same_fp_mask,
        )

        # Displacement = force * temp (capped by temp)
        disp = force * temp
        disp_norm = np.linalg.norm(disp, axis=1, keepdims=True)
        scale = np.minimum(1.0, temp / np.maximum(disp_norm, 1e-6))
        disp *= scale

        # Cap max step size: 1mm keeps parts inside the halo gradient
        # instead of vaulting across it in one step (halo reach is 3mm;
        # the old 2mm budget crossed most of it at once, landing in
        # overlap past the restoring slope).
        disp_norm = np.linalg.norm(disp, axis=1, keepdims=True)
        scale = np.minimum(1.0, max_step / np.maximum(disp_norm, 1e-6))
        disp *= scale

        # Rotation rate cap (F2): bound each footprint's spin to
        # max_dangle_deg per iteration with translation bit-identical to
        # the uncapped path. Per-pad caps above let one pad take the full
        # budget while its mate takes ~0 -- pure spin that Kabsch only
        # reads out afterwards (tens of degrees/iter on small parts,
        # which then land inside neighbors). The fitted 2D angle comes
        # from a closed-form Kabsch fit (no SVD); over-cap footprints
        # are rotated back to exactly the cap about their centroid and
        # recentered, so translation is untouched. Rotation stays fully
        # free across iterations (a THT part can still turn around over
        # a run; >= 180 disables the cap entirely).
        if _touch_static.any() and params.max_dangle_deg < 180.0:
            _rows = np.where(_touch_static)[0]
            _fp_rows = _pad_fp_idx[_rows]
            _nfp = len(fp_uuids)
            _cnt = np.zeros(_nfp)
            _sx = np.zeros((_nfp, 2))
            _sp = np.zeros((_nfp, 2))
            np.add.at(_cnt, _fp_rows, 1.0)
            np.add.at(_sx, _fp_rows, disp[_rows])
            np.add.at(_sp, _fp_rows, positions[_rows])
            _n = np.maximum(_cnt, 1.0)
            _dc = _sx / _n[:, None]
            _pc = _sp / _n[:, None]
            _lp = positions[_rows] - _pc[_fp_rows]
            _vp = _lp + (disp[_rows] - _dc[_fp_rows])
            _num = np.zeros(_nfp)
            _den = np.zeros(_nfp)
            np.add.at(_num, _fp_rows, _lp[:, 0] * _vp[:, 1] - _lp[:, 1] * _vp[:, 0])
            np.add.at(_den, _fp_rows, _lp[:, 0] * _vp[:, 0] + _lp[:, 1] * _vp[:, 1])
            _cap_a = math.radians(params.max_dangle_deg)
            for _li in np.where(
                (_cnt >= 2.0)
                & (np.abs(np.arctan2(_num, _den)) > _cap_a)
                & ((_num * _num + _den * _den) > 1e-24)
            )[0]:
                _m = fp_local_to_rows[_li]
                _m = _m[_touch_static[_m]]
                _th = math.atan2(float(_num[_li]), float(_den[_li]))
                _phi = math.copysign(_cap_a, _th) - _th
                _cc, _ss = math.cos(_phi), math.sin(_phi)
                # Recompute this footprint's vectors (cheap: few pads).
                _sel = np.where(_fp_rows == _li)[0]
                _lr = _lp[_sel]
                _vr = _vp[_sel]
                _wr = np.empty_like(_vr)
                _wr[:, 0] = _cc * _vr[:, 0] - _ss * _vr[:, 1]
                _wr[:, 1] = _ss * _vr[:, 0] + _cc * _vr[:, 1]
                _nd = _dc[_li] + (_wr - _lr)
                # Recenter: keep baseline translation exactly.
                _nd -= _nd.mean(axis=0, keepdims=True) - _dc[_li]
                disp[_m] = _nd

        # Update positions (only movable pads)
        positions[pad_movable] += disp[pad_movable]
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

    return deltas, footprint_forces, iteration + 1, footprint_components


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
                region_deltas, region_forces, region_iters, region_comps = _run_region_simulation(
                    region_idx, model.board_regions[region_idx],
                    region_pad_indices, region_pads, region_pad_to_fp_idx,
                    region_fp_uuid_to_pad_indices, region_global_pad_idx_to_local,
                    region_pad_is_ghost, model, params, movable_uuids
                )
                all_deltas.update(region_deltas)
                all_forces.update(region_forces)
                all_components.update(region_comps)
                total_iterations = max(total_iterations, region_iters)
                max_final_disp = max(max_final_disp, max((math.hypot(dx, dy) for dx, dy, _ in region_deltas.values()), default=0.0))

        return PlacementProposal(
            deltas=all_deltas,
            iterations=total_iterations,
            final_max_disp_mm=max_final_disp,
            elapsed_s=time.perf_counter() - start,
            params=params.to_dict(),
            forces=all_forces,
            force_components=all_components,
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

    region_deltas, region_forces, region_iters, region_comps = _run_region_simulation(
        0, virtual_region,
        region_pad_indices, region_pads, region_pad_to_fp_idx,
        region_fp_uuid_to_pad_indices, region_global_pad_idx_to_local,
        region_pad_is_ghost, model, params, movable_uuids
    )

    return PlacementProposal(
        deltas=region_deltas,
        iterations=region_iters,
        final_max_disp_mm=max((math.hypot(dx, dy) for dx, dy, _ in region_deltas.values()), default=0.0),
        elapsed_s=time.perf_counter() - start,
        params=params.to_dict(),
        forces=region_forces,
        force_components=region_comps,
    )
