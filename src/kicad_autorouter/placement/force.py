"""Total force computation for placement dynamics.

Extracted from _run_region_simulation as a module-level function so the
simulation loop stays readable. Force components:

- repulsion: all-pairs Coulomb k/d^2 (SI, d in meters)
- attraction: constant-magnitude pull along net edges
- ghost: fixed-anchor springs from ghost footprints to target pads
- courtyard: penalty wall forces from SAT/shapely collision (deepest contact wins per pair)
  plus the board wall — the region polygon treated as an inverted courtyard
  with the same wall law, so parts stay inside the outline / out of cutouts.
"""

from __future__ import annotations

import logging
import math
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

import numpy as np
from shapely.geometry import Point, Polygon
from shapely.ops import nearest_points

from .align import apply_align_forces
from .contact import (
    ShapeType,
    _analytical_collision,
    _classify_shape,
    _contact_weights,
    _pose_shape_params,
    _wall_mag,
    board_wall_force,
)
from .params import PlacementParams
from .rigid import _apply_rigid_motion, _rigid_motion_params, _rigid_pose_vertices


def _overlap_depth_mm(pi: Polygon, pj: Polygon, ux: float, uy: float) -> float:
    """Penetration depth (mm) of overlapping pi/pj along unit (ux, uy).

    Vertex-projection interval overlap; exact for convex outlines,
    a conservative under-read for concave ones (never invents depth).
    """
    try:
        pa = [x * ux + y * uy for x, y in pi.exterior.coords]
        pb = [x * ux + y * uy for x, y in pj.exterior.coords]
    except Exception:
        logger.debug("overlap depth shapely failed")
        return 0.0
    return max(0.0, min(max(pa), max(pb)) - max(min(pa), min(pb)))


class ForceContext:
    """Precomputed per-region state used by _compute_total_force every iteration.

    Holds the static cache (courtyard shapes, per-footprint centroids/radii,
    halo matrix) plus the pad->footprint mapping needed to spread forces.
    """

    def __init__(
        self,
        fp_uuids: List[str],
        fp_local_to_rows: List[np.ndarray],
        fp_movable_arr: np.ndarray,
        pad_to_fp_local: List[int],
        poly_cache: Optional[Dict[str, dict]],
        cache_rebuild_threshold_mm: float,
        cache_rebuild_threshold_rad: float,
        pad_movable: np.ndarray,
        fp_centroids_arr: Optional[np.ndarray] = None,
        fp_radii_arr: Optional[np.ndarray] = None,
        halo_matrix: Optional[np.ndarray] = None,
        fp_pad_indices: Optional[Dict[str, List[int]]] = None,
        fp_uuid_to_local_idx: Optional[Dict[str, int]] = None,
        region_polygons: Optional[List] = None,
        fp_uuid_to_region: Optional[Dict[str, int]] = None,
        cy_cache: Optional[Dict[str, dict]] = None,
        pad_charged: Optional[np.ndarray] = None,
        region_boundaries: Optional[List] = None,
        region_centroids: Optional[List] = None,
    ):
        self.fp_uuids = fp_uuids
        self.fp_local_to_rows = fp_local_to_rows
        self.fp_movable_arr = fp_movable_arr
        self.pad_to_fp_local = pad_to_fp_local
        self.fp_centroids_arr = fp_centroids_arr
        self.fp_radii_arr = fp_radii_arr
        self.halo_matrix = halo_matrix
        self.poly_cache: Dict[str, dict] = poly_cache or {}
        self.cache_rebuild_threshold_mm = cache_rebuild_threshold_mm
        self.cache_rebuild_threshold_rad = cache_rebuild_threshold_rad
        self.pad_movable = pad_movable
        self.fp_pad_indices: Dict[str, List[int]] = fp_pad_indices or {}
        self.fp_uuid_to_local_idx: Dict[str, int] = fp_uuid_to_local_idx or {}
        self.region_polygons: List = region_polygons or []
        # Static per-run derivatives of the region polygons. The board wall
        # is evaluated every iteration for every movable footprint, so the
        # boundary ring and centroid are built once here instead of via a
        # GEOS construction per footprint per iteration (identical values).
        if region_boundaries is not None:
            self.region_boundaries: List = region_boundaries
        else:
            self.region_boundaries = [
                (p.boundary if not p.is_empty else None)
                for p in self.region_polygons
            ]
        if region_centroids is not None:
            self.region_centroids: List = region_centroids
        else:
            self.region_centroids = [
                ((p.centroid.x, p.centroid.y) if not p.is_empty else (math.nan, math.nan))
                for p in self.region_polygons
            ]
        self.fp_uuid_to_region: Dict[str, int] = fp_uuid_to_region or {}
        self.cy_cache: Dict[str, dict] = cy_cache or {}
        # Per-node Coulomb participation (netted pads only). Unconnected
        # copper (mounting pins, holes) joins the node set for geometry —
        # rigid fit, courtyard re-fit, force spreading — but carries no
        # charge: it collides by contact, never by long-range repulsion.
        # None (default) charges every node (backward compatible).
        self.pad_charged: Optional[np.ndarray] = pad_charged
        # Last applied contact normal per footprint pair, keyed by
        # (min, max) local indices. The winner's fresh normal is mixed
        # with the prior step's so near-tie winner switches (body vs pin,
        # SAT axis A vs B) rotate the push instead of flipping it — the
        # wall response stays a continuous surface instead of chattering
        # between discrete candidates. Opposing winners slew in slowly
        # (prior-dominant blend) so a 180° flip can never land at once.
        self.last_contact: Dict[Tuple[int, int], Tuple[float, float]] = {}
        # Latched tangential direction per contact, keyed like last_contact
        # (board-wall uses (fp, -1)). Holds +1/-1 so a part pressed at an
        # angle keeps sliding one way instead of flip-flopping each step
        # when the relative tangential velocity hovers near zero — letting
        # the contact-weighted spread settle it flat. Internal solver
        # memory, not a user param.
        self.last_tangent: Dict[Tuple[int, int], float] = {}
        # Pad velocities (n_pads, 2) in m/s — used for tangential sliding assist
        self.pad_velocities: Optional[np.ndarray] = None


def _compute_total_force(
    pos: np.ndarray,
    n_pads: int,
    pad_movable: np.ndarray,
    params: PlacementParams,
    has_net_edges: bool,
    src_idx: np.ndarray,
    dst_idx: np.ndarray,
    edge_weights: np.ndarray,
    ghost_attractions: List[Tuple[np.ndarray, List[int], float]],
    same_fp_mask: Optional[np.ndarray] = None,
    edge_sides: Optional[np.ndarray] = None,
    return_components: bool = False,
    ctx: Optional[ForceContext] = None,
    pad_velocities: Optional[np.ndarray] = None,
    align_targets: Optional[Dict[int, Tuple[Optional[float], float, float]]] = None,
    align_ref_axes: Optional[Dict[int, Tuple[int, int, float]]] = None,
) -> Tuple[np.ndarray, Optional[Dict[str, np.ndarray]]]:
    """Compute total force on each pad at given positions.

    With ``return_components`` also returns the per-cause breakdown
    ``{cause: (n_pads, 2)}`` for repulsion/attraction/ghost/courtyard
    (courtyard includes the board wall), plus ``align`` when the
    orientation-consensus regime is active (either stiffness > 0 with
    targets present).

    ``edge_sides`` optionally marks attraction edges as one-sided
    (0 = symmetric, 1 = src only, 2 = dst only). Decap-incident edges are
    one-sided so a decoupling cap is pulled toward its target without
    dragging the target (often a large anchored part) into the cap.
    """
    # ------------------------------------------------------------------
    # Repulsion: all-pairs Coulomb k/d^2 in SI (d in meters).
    # Geometry arrays stay millimeter-native; only the law converts.
    # ------------------------------------------------------------------
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
    if ctx is not None and getattr(ctx, "pad_charged", None) is not None:
        charged = np.asarray(getattr(ctx, "pad_charged"), dtype=bool)
        if charged.shape == (n_pads,):
            force_mag[~charged, :] = 0.0
            force_mag[:, ~charged] = 0.0
    force_vec = diff * (force_mag[:, :, None] / dist[:, :, None])
    repulsion = np.sum(force_vec, axis=1)

    # ------------------------------------------------------------------
    # Attraction: constant-magnitude pull along net edges (newtons).
    # ------------------------------------------------------------------
    attraction = np.zeros((n_pads, 2), dtype=np.float64)
    if has_net_edges:
        diff_edge = pos[dst_idx] - pos[src_idx]
        dist_edge = np.linalg.norm(diff_edge, axis=1, keepdims=True)
        dist_edge = np.maximum(dist_edge, 1e-6)
        unit = diff_edge / dist_edge
        force_mag_edge = (params.attraction_k_N * edge_weights)[:, None]
        force_vec_edge = unit * force_mag_edge
        if edge_sides is None:
            np.add.at(attraction, src_idx, force_vec_edge)
            np.add.at(attraction, dst_idx, -force_vec_edge)
        else:
            sides = np.asarray(edge_sides)
            src_mask = sides != 2
            dst_mask = sides != 1
            np.add.at(attraction, src_idx[src_mask], force_vec_edge[src_mask])
            np.add.at(attraction, dst_idx[dst_mask], -force_vec_edge[dst_mask])

    # ------------------------------------------------------------------
    # Ghost attraction: centroid-to-centroid springs from ghost to target.
    # ------------------------------------------------------------------
    ghost_attr_force = np.zeros((n_pads, 2), dtype=np.float64)
    if ghost_attractions:
        for ghost_anchor, target_pad_indices, ka_attr in ghost_attractions:
            if not target_pad_indices:
                continue
            target_centroid = np.mean(pos[target_pad_indices], axis=0)
            diff_attr = target_centroid - ghost_anchor
            dist_attr = np.linalg.norm(diff_attr)
            if dist_attr > 1e-6:
                unit_attr = diff_attr / dist_attr
                force_mag_attr = params.ghost_k_N * ka_attr
                force_vec_attr = -unit_attr * force_mag_attr
                force_per_pad = force_vec_attr / len(target_pad_indices)
                for pad_idx in target_pad_indices:
                    ghost_attr_force[pad_idx] += force_per_pad

    # ------------------------------------------------------------------
    # Alignment: orientation-consensus couples + weak position hold
    # (Tidy regime). Zero net torque-free force when off; wall law below
    # vets every rotation the same iteration it happens.
    # ------------------------------------------------------------------
    align_active = (
        (params.align_angle_k_N > 0.0 or params.align_pos_k_N_m > 0.0)
        and align_targets is not None
        and align_ref_axes is not None
        and ctx is not None
    )
    if align_active:
        align_force = apply_align_forces(
            pos, ctx.fp_local_to_rows, ctx.fp_movable_arr,
            align_ref_axes, align_targets,
            angle_k_N=params.align_angle_k_N,
            pos_k_N_m=params.align_pos_k_N_m,
        )
    else:
        align_force = np.zeros((n_pads, 2), dtype=np.float64)

    force = repulsion + attraction + ghost_attr_force + align_force

    # Courtyard force accumulator
    n_pads_local = pos.shape[0]
    poly_force = np.zeros((n_pads_local, 2), dtype=np.float64)
    tangential_force = np.zeros((n_pads_local, 2), dtype=np.float64)

    # ------------------------------------------------------------------
    # Polygon-based courtyard collision from cached shapes.
    # ------------------------------------------------------------------
    if ctx is not None:
        fp_uuids = ctx.fp_uuids
        fp_local_to_rows = ctx.fp_local_to_rows
        fp_movable_arr = ctx.fp_movable_arr
        pad_to_fp_local = ctx.pad_to_fp_local
        fp_centroids_arr = ctx.fp_centroids_arr
        fp_radii_arr = ctx.fp_radii_arr
        halo_matrix = ctx.halo_matrix
        poly_cache = ctx.poly_cache
        cache_rebuild_threshold_mm = ctx.cache_rebuild_threshold_mm
        cache_rebuild_threshold_rad = ctx.cache_rebuild_threshold_rad
        # Pad velocities for tangential sliding assist
        pad_vel = pad_velocities if pad_velocities is not None else ctx.pad_velocities
        if pad_vel is not None and len(pad_vel) != n_pads:
            pad_vel = None  # size mismatch, disable tangential assist

        n_fp = len(fp_uuids)
        if n_fp:
            body_polys: Dict[str, Polygon] = {}
            body_centroids: List[List[float]] = []
            body_radii: List[float] = []
            body_layers: List[str] = []
            pins_data: Dict[str, List] = {}
            body_empty: Dict[str, bool] = {}
            pins_empty: Dict[str, List[bool]] = {}

            cy_cache = ctx.cy_cache

            for fp_uuid in fp_uuids:
                e = cy_cache.get(fp_uuid, {})
                body = e.get("body", {})
                pins = e.get("pins", [])
                pad_idx = e.get("pad_idx", np.array([], dtype=int))
                orig_pos = e.get("orig_pos", np.array([], dtype=np.float64))

                cp = pos[pad_idx] if len(pad_idx) == len(orig_pos) and len(pad_idx) > 0 else None

                if cp is not None and len(cp) >= 2:
                    curr_centroid = cp.mean(axis=0)
                    v = cp[1] - cp[0]
                    curr_angle = math.atan2(v[1], v[0])
                elif cp is not None and len(cp) == 1:
                    curr_centroid = cp[0]
                    curr_angle = 0.0
                else:
                    curr_centroid = np.array([0.0, 0.0])
                    curr_angle = 0.0

                cached = poly_cache.get(fp_uuid)
                rebuild = True
                if cached is not None:
                    last_centroid = cached.get("last_centroid")
                    last_angle = cached.get("last_angle")
                    if last_centroid is not None and last_angle is not None:
                        dist = math.hypot(
                            curr_centroid[0] - last_centroid[0],
                            curr_centroid[1] - last_centroid[1],
                        )
                        angle_diff = abs(curr_angle - last_angle)
                        angle_diff = min(angle_diff, 2 * math.pi - angle_diff)
                        if dist <= cache_rebuild_threshold_mm and angle_diff <= cache_rebuild_threshold_rad:
                            rebuild = False

                if rebuild:
                    kind = body.get("kind", "empty")
                    body_verts = body.get("verts", np.array([]))
                    # One rigid fit per footprint per rebuild: the motion is
                    # shared by the body ring, every pin center, and the
                    # shape-parameter re-pose below (identical values to one
                    # _rigid_pose_vertices call per set, one mean/atan2).
                    motion = _rigid_motion_params(orig_pos, cp) if cp is not None else None
                    if kind == "courtyard" and motion is not None:
                        oc, cc, ca, sa, tx, ty, _ang = motion
                        body_poly = Polygon(_apply_rigid_motion(body_verts, oc, cc, ca, sa, tx, ty))
                    elif kind == "courtyard" and cp is not None:
                        body_poly = Polygon(_rigid_pose_vertices(body_verts, orig_pos, cp))
                    elif kind == "courtyard":
                        body_poly = Polygon(body_verts)
                    elif kind == "bbox":
                        if cp is not None and len(cp) > 0:
                            xs = cp[:, 0]
                            ys = cp[:, 1]
                            x0, x1 = float(xs.min()), float(xs.max())
                            y0, y1 = float(ys.min()), float(ys.max())
                        else:
                            xs = [0.0]
                            ys = [0.0]
                            x0, x1 = min(xs), max(xs)
                            y0, y1 = min(ys), max(ys)
                        m = 0.1
                        body_poly = Polygon([
                            (x0 - m, y0 - m), (x1 + m, y0 - m),
                            (x1 + m, y1 + m), (x0 - m, y1 + m),
                        ])
                    elif kind == "point":
                        body_poly = Polygon(body_verts)
                    else:
                        body_poly = Polygon()
                    # Label collision lives in the orbit nodes
                    # (region._orbit_label_forces) — never unioned here,
                    # so the parent body stays the bare courtyard and the
                    # label finds its own home nearby via the orbit clamp.
                    body_polys[fp_uuid] = body_poly
                    body_empty[fp_uuid] = bool(body_poly.is_empty)

                    fp_pins = []
                    if pins and motion is not None:
                        oc, cc, ca, sa, tx, ty, _ang = motion
                        local_centers = np.array(
                            [pin["center"] for pin in pins], dtype=np.float64
                        )
                        posed_centers = _apply_rigid_motion(
                            local_centers, oc, cc, ca, sa, tx, ty
                        )
                        for pin, posed_center in zip(pins, posed_centers):
                            pin_poly = Point(float(posed_center[0]), float(posed_center[1])).buffer(pin["radius"])
                            fp_pins.append((pin_poly, (float(posed_center[0]), float(posed_center[1])), pin["radius"], pin.get("layer", ""), pin.get("extends_beyond_courtyard", False)))
                    elif pins and cp is not None:
                        for pin in pins:
                            local_cx, local_cy = pin["center"]
                            pin_vert = np.array([[local_cx, local_cy]], dtype=np.float64)
                            posed_center = _rigid_pose_vertices(pin_vert, orig_pos, cp)[0]
                            pin_poly = Point(posed_center[0], posed_center[1]).buffer(pin["radius"])
                            fp_pins.append((pin_poly, (posed_center[0], posed_center[1]), pin["radius"], pin.get("layer", ""), pin.get("extends_beyond_courtyard", False)))
                    elif pins:
                        for pin in pins:
                            local_cx, local_cy = pin["center"]
                            pin_poly = Point(local_cx, local_cy).buffer(pin["radius"])
                            fp_pins.append((pin_poly, (local_cx, local_cy), pin["radius"], pin.get("layer", ""), pin.get("extends_beyond_courtyard", False)))
                    pins_data[fp_uuid] = fp_pins
                    pins_empty[fp_uuid] = [bool(p.is_empty) for p, _, _, _, _ in fp_pins]

                    # Courtyard bodies are rigid transforms of fixed file-pose
                    # vertices: a repeat classification can only reproduce the
                    # cached type, so re-pose the cached params from the pad
                    # motion instead of re-running the numpy/GEOS classify.
                    # First builds and non-courtyard kinds classify fully.
                    prev_shape = cached.get("shape_type") if cached is not None else None
                    if (
                        kind == "courtyard"
                        and prev_shape in (ShapeType.RECTANGLE, ShapeType.CIRCLE, ShapeType.POLYGON)
                        and cached is not None
                        and cached.get("last_centroid") is not None
                        and cached.get("last_angle") is not None
                    ):
                        shape_type, shape_params = _pose_shape_params(
                            prev_shape,
                            cached.get("shape_params", {}),
                            cached["last_centroid"],
                            cached["last_angle"],
                            curr_centroid,
                            curr_angle,
                            body_poly,
                        )
                    else:
                        shape_type, shape_params = _classify_shape(body_poly)

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
                        "body_empty": body_empty[fp_uuid],
                        "pins": fp_pins,
                        "pins_empty": pins_empty[fp_uuid],
                        "centroid": [centroid.x, centroid.y],
                        "radius": max_extent,
                        "layer": body.get("layer", ""),
                        "last_centroid": [curr_centroid[0], curr_centroid[1]],
                        "last_angle": curr_angle,
                        "shape_type": shape_type,
                        "shape_params": shape_params,
                    }
                else:
                    body_poly = cached["body_poly"]
                    body_polys[fp_uuid] = body_poly
                    body_empty[fp_uuid] = cached["body_empty"]
                    pins_data[fp_uuid] = cached["pins"]
                    pins_empty[fp_uuid] = cached["pins_empty"]
                    body_centroids.append(cached["centroid"])
                    body_radii.append(cached["radius"])
                    body_layers.append(cached["layer"])

            body_centroids_arr = np.array(body_centroids, dtype=np.float64)
            body_radii_arr = np.array(body_radii, dtype=np.float64)
            body_layers_arr = np.array(body_layers, dtype=object)

            # Posed pin discs are shared with the label orbit (same THT
            # extenders repel labels through the same wall law), so stash
            # the refreshed set on the context; staleness matches the pad
            # kernel exactly (same refresh cadence, same poses).
            ctx.pins_data = pins_data

            # Uniform grid spatial index for broadphase
            cell_size = 15.0  # mm
            grid: Dict[Tuple[int, int], List[int]] = {}
            for fp_idx in range(n_fp):
                cx, cy = body_centroids_arr[fp_idx]
                r = body_radii_arr[fp_idx] + (params.wall_halo_m * 1000.0)
                min_gx = int((cx - r) / cell_size)
                max_gx = int((cx + r) / cell_size)
                min_gy = int((cy - r) / cell_size)
                max_gy = int((cy + r) / cell_size)
                for gx in range(min_gx, max_gx + 1):
                    for gy in range(min_gy, max_gy + 1):
                        grid.setdefault((gx, gy), []).append(fp_idx)

            candidate_pairs = set()
            for cell, indices in grid.items():
                for ii, i in enumerate(indices):
                    for j in indices[ii + 1:]:
                        candidate_pairs.add((i, j) if i < j else (j, i))
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

            _close = np.zeros((n_fp, n_fp), dtype=bool)
            _halo_matrix_local = np.zeros((n_fp, n_fp), dtype=np.float64)
            for i, j in candidate_pairs:
                if body_layers_arr[i] == body_layers_arr[j]:
                    ri = body_radii_arr[i]
                    rj = body_radii_arr[j]
                    halo = params.wall_halo_m * 1000.0
                    dx = body_centroids_arr[i, 0] - body_centroids_arr[j, 0]
                    dy = body_centroids_arr[i, 1] - body_centroids_arr[j, 1]
                    dist_sq = dx * dx + dy * dy
                    reach_sq = (ri + rj + halo) ** 2
                    if dist_sq < reach_sq + 1e-8:
                        _close[i, j] = _close[j, i] = True
                        _halo_matrix_local[i, j] = _halo_matrix_local[j, i] = halo
                else:
                    # Cross-layer: bodies never collide, but THT pins
                    # protruding onto the far side do (pin-vs-body below).
                    # Seed the pair halo with the pin halo so those pushes
                    # use solder-fillet clearance, not the courtyard halo.
                    # _close stays False: no body-body response. 0 disables.
                    pin_halo = params.cross_layer_pin_halo_m * 1000.0
                    _halo_matrix_local[i, j] = _halo_matrix_local[j, i] = pin_halo

            fp_centroids_arr = body_centroids_arr
            fp_radii_arr = body_radii_arr

            # Per-footprint mean pad velocity, hoisted out of the per-push
            # tangential helper: one mean per footprint per iteration
            # instead of one per contact push (same rows, same mean).
            fp_vel_mean: Optional[List[Tuple[float, float]]] = None
            if pad_vel is not None and params.contact_tangent_gamma > 0.0:
                fp_vel_mean = []
                for _li in range(n_fp):
                    _rows = fp_local_to_rows[_li]
                    if _rows is None or len(_rows) == 0:
                        fp_vel_mean.append((0.0, 0.0))
                    else:
                        _m = pad_vel[_rows].mean(axis=0)
                        fp_vel_mean.append((float(_m[0]), float(_m[1])))

            def _spread_force(rows, cx, cy, radius, ux, uy, fx, fy,
                              poly_force, tangential_force,
                              ft_x=0.0, ft_y=0.0):
                """Spread a normal (fx/fy) force onto poly_force and an
                optional tangential (ft_x/ft_y) force onto tangential_force.

                Both use the contact-weight projection along (ux, uy). The
                tangential accumulator is only written when non-zero,
                matching the prior split helpers' call guard.
                """
                if rows is None or len(rows) == 0:
                    return
                r = float(radius)
                if not math.isfinite(r) or r < 1e-9:
                    n = len(rows)
                    if n > 0:
                        poly_force[rows, 0] += fx / n
                        poly_force[rows, 1] += fy / n
                        if ft_x != 0.0 or ft_y != 0.0:
                            tangential_force[rows, 0] += ft_x / n
                            tangential_force[rows, 1] += ft_y / n
                    return
                proj = ((pos[rows, 0] - cx) * ux + (pos[rows, 1] - cy) * uy) / r
                w = _contact_weights(proj)
                poly_force[rows, 0] += w * fx
                poly_force[rows, 1] += w * fy
                if ft_x != 0.0 or ft_y != 0.0:
                    tangential_force[rows, 0] += w * ft_x
                    tangential_force[rows, 1] += w * ft_y

            def _fp_tangential_vel(fp_local_idx, tx, ty):
                """Get average tangential velocity for a footprint.
                Returns (0, 0) if footprint has no pads (e.g., wall ghost)."""
                if fp_vel_mean is not None:
                    _vx, _vy = fp_vel_mean[fp_local_idx]
                    return _vx * tx + _vy * ty
                rows = fp_local_to_rows[fp_local_idx]
                if len(rows) == 0:
                    return 0.0
                v = pad_vel[rows].mean(axis=0)
                return v[0] * tx + v[1] * ty

            def _tangent_sign(pair, v_rel_t):
                """Latched sign of relative tangential motion for a contact.

                Updates only on decisive motion; near-zero noise keeps the
                previous direction so sustained pushes settle instead of
                rubbing left/right each step. Defaults to +1 (matches the
                old v_rel_t >= 0 branch) on first contact."""
                prev = ctx.last_tangent.get(pair) if ctx is not None else None
                if abs(v_rel_t) > 1e-9:
                    sgn = 1.0 if v_rel_t > 0 else -1.0
                    if ctx is not None:
                        ctx.last_tangent[pair] = sgn
                    return sgn
                if prev is None:
                    if ctx is not None:
                        ctx.last_tangent[pair] = 1.0
                    return 1.0
                return prev

            def _apply_push(li, lj, s_mm, ux, uy, halo_reach):
                """Apply one contact push between footprints li and lj.

                Computes the wall-law force from ``s_mm``/``halo_reach``,
                adds tangential assist, and spreads the normal force onto
                ``poly_force`` plus the tangential force onto
                ``tangential_force`` for footprint li (and lj when movable).
                No-op at or beyond the halo reach.
                """
                if s_mm >= halo_reach:
                    return
                force_mag = _wall_mag(
                    params.wall_k_N, s_mm, halo_reach,
                    params.wall_exponent, params.wall_cap_depth_mm,
                )
                fx, fy = force_mag * ux, force_mag * uy
                ft_x = ft_y = 0.0
                if pad_vel is not None and params.contact_tangent_gamma > 0.0:
                    tx, ty = -uy, ux
                    v_i_t = _fp_tangential_vel(li, tx, ty)
                    v_j_t = _fp_tangential_vel(lj, tx, ty)
                    v_rel_t = v_i_t - v_j_t
                    sgn = _tangent_sign((li, lj) if li < lj else (lj, li), v_rel_t)
                    f_t = params.contact_tangent_gamma * force_mag * sgn
                    ft_x, ft_y = f_t * tx, f_t * ty
                rows_i = fp_local_to_rows[li]
                _spread_force(
                    rows_i, fp_centroids_arr[li, 0], fp_centroids_arr[li, 1],
                    fp_radii_arr[li], ux, uy, fx, fy,
                    poly_force, tangential_force, ft_x, ft_y,
                )
                if fp_movable_arr[lj]:
                    rows_j = fp_local_to_rows[lj]
                    _spread_force(
                        rows_j, fp_centroids_arr[lj, 0], fp_centroids_arr[lj, 1],
                        fp_radii_arr[lj], -ux, -uy, -fx, -fy,
                        poly_force, tangential_force, -ft_x, -ft_y,
                    )

            def _apply_contact_push(
                li, lj, pi, pj, intersection_area, sink=None,
            ):
                if pi.is_empty or pj.is_empty:
                    return
                halo_reach = float(_halo_matrix_local[li, lj])
                try:
                    qi, qj = nearest_points(pi, pj)
                    dx = float(qi.x - qj.x)
                    dy = float(qi.y - qj.y)
                except Exception:
                    logger.debug("nearest_points failed li=%s lj=%s", li, lj)
                    dx = dy = 0.0
                dist = math.hypot(dx, dy)
                if dist <= 1e-8:
                    dx = fp_centroids_arr[li, 0] - fp_centroids_arr[lj, 0]
                    dy = fp_centroids_arr[li, 1] - fp_centroids_arr[lj, 1]
                    dist = math.hypot(dx, dy)
                if dist <= 1e-6:
                    dx, dy, dist = 1.0, 0.0, 1.0

                ux, uy = dx / dist, dy / dist

                if intersection_area >= 0:
                    s_mm = -_overlap_depth_mm(pi, pj, ux, uy)
                else:
                    s_mm = -intersection_area
                    if s_mm >= halo_reach:
                        return
                if sink is not None:
                    sink.append(('s', float(s_mm), ux, uy))
                    return
                _apply_push(li, lj, s_mm, ux, uy, halo_reach)

            def _apply_contact_push_analytical(
                li, lj, penetration, normal, is_overlap, sink=None,
            ):
                s_mm = -penetration
                ux, uy = normal
                nn = math.hypot(ux, uy)
                if not math.isfinite(nn) or nn <= 1e-12:
                    return False
                ux, uy = ux / nn, uy / nn
                halo_reach = float(_halo_matrix_local[li, lj])
                if sink is not None:
                    if is_overlap or s_mm < halo_reach:
                        sink.append(('s', float(s_mm), ux, uy))
                    return True
                _apply_push(li, lj, s_mm, ux, uy, halo_reach)
                return True

            # Compute collision forces per footprint pair
            for i, j in candidate_pairs:
                if not fp_movable_arr[i] and not fp_movable_arr[j]:
                    continue
                fp_i_uuid = fp_uuids[i]
                fp_j_uuid = fp_uuids[j]
                body_i = body_polys[fp_i_uuid]
                body_j = body_polys[fp_j_uuid]
                pins_i = pins_data[fp_i_uuid]
                pins_j = pins_data[fp_j_uuid]
                body_i_empty = body_empty[fp_i_uuid]
                body_j_empty = body_empty[fp_j_uuid]
                # Per-pair halo from the broadphase matrix: same-layer pairs
                # carry wall_halo_m, cross-layer pairs carry
                # cross_layer_pin_halo_m (0 when out of reach / disabled).
                pair_halo = float(_halo_matrix_local[i, j])
                pins_i_empty = pins_empty[fp_i_uuid]
                pins_j_empty = pins_empty[fp_j_uuid]

                cand: List = []
                has_body_body = False

                # 1. Body-body collision (same layer only)
                if _close[i, j]:
                    if not body_i_empty and not body_j_empty:
                        cached_i = poly_cache.get(fp_i_uuid, {})
                        cached_j = poly_cache.get(fp_j_uuid, {})
                        shape_i_type = cached_i.get("shape_type", ShapeType.POLYGON)
                        shape_j_type = cached_j.get("shape_type", ShapeType.POLYGON)
                        shape_i_params = cached_i.get("shape_params", {})
                        shape_j_params = cached_j.get("shape_params", {})

                        analytical_result = _analytical_collision(
                            shape_i_type, shape_i_params, shape_j_type, shape_j_params
                        )
                        if analytical_result is not None:
                            penetration, normal, is_overlap = analytical_result
                            _apply_contact_push_analytical(
                                i, j, penetration, normal, is_overlap, cand
                            )
                            has_body_body = True
                        else:
                            if body_i.intersects(body_j):
                                _apply_contact_push(i, j, body_i, body_j, 0.0, cand)
                                has_body_body = True
                            else:
                                body_dist = body_i.distance(body_j)
                                halo_reach = float(_halo_matrix_local[i, j])
                                if body_dist < halo_reach:
                                    _apply_contact_push(
                                        i, j, body_i, body_j, -body_dist, cand
                                    )
                                    has_body_body = True

                # For same-layer courtyard parts, body-body is authoritative.
                # Skip pin-body/pin-pin only for pins fully inside their courtyards
                # (redundant with body-body). Pins extending beyond courtyard
                # (e.g. mounting holes) are kept for cross-layer and mounting-pin collisions.
                same_layer_courtyard = (
                    body_layers_arr[i] == body_layers_arr[j]
                    and not body_i_empty and not body_j_empty
                )

                # 2. Pins_i vs body_j
                if pins_i and not body_j_empty:
                    for pin_k, (pin_poly, pin_center, pin_radius, pin_layer, pin_extends) in enumerate(pins_i):
                        if pins_i_empty[pin_k]:
                            continue
                        # Skip same-layer pin-body if pin is fully inside courtyard (redundant)
                        if pin_layer and pin_layer == body_layers_arr[j] and not pin_extends:
                            continue
                        pin_cx, pin_cy = pin_center
                        body_cx, body_cy = (
                            fp_centroids_arr[j, 0], fp_centroids_arr[j, 1]
                        )
                        dx = pin_cx - body_cx
                        dy = pin_cy - body_cy
                        center_dist = math.hypot(dx, dy)
                        max_reach = (
                            pin_radius + body_radii_arr[j] + pair_halo
                        )
                        if center_dist > max_reach:
                            continue
                        pin_shape_params = {"center": pin_center, "radius": pin_radius}
                        cached_j = poly_cache.get(fp_j_uuid, {})
                        shape_j_type = cached_j.get("shape_type", ShapeType.POLYGON)
                        shape_j_params = cached_j.get("shape_params", {})
                        analytical_result = _analytical_collision(
                            ShapeType.CIRCLE, pin_shape_params, shape_j_type, shape_j_params
                        )
                        if analytical_result is not None:
                            penetration, normal, is_overlap = analytical_result
                            _apply_contact_push_analytical(
                                i, j, penetration, normal, is_overlap, cand
                            )
                        else:
                            if pin_poly.intersects(body_j):
                                _apply_contact_push(i, j, pin_poly, body_j, 0.0, cand)
                            else:
                                pin_dist = pin_poly.distance(body_j)
                                pin_halo = pair_halo
                                if pin_dist < pin_halo:
                                    _apply_contact_push(
                                        i, j, pin_poly, body_j, -pin_dist, cand
                                    )

                # 3. Body_i vs pins_j
                if pins_j and not body_i_empty:
                    for pin_k, (pin_poly, pin_center, pin_radius, pin_layer, pin_extends) in enumerate(pins_j):
                        if pins_j_empty[pin_k]:
                            continue
                        # Skip same-layer pin-body if pin is fully inside courtyard (redundant)
                        if pin_layer and pin_layer == body_layers_arr[i] and not pin_extends:
                            continue
                        body_cx, body_cy = (
                            fp_centroids_arr[i, 0], fp_centroids_arr[i, 1]
                        )
                        pin_cx, pin_cy = pin_center
                        dx = body_cx - pin_cx
                        dy = body_cy - pin_cy
                        center_dist = math.hypot(dx, dy)
                        max_reach = (
                            body_radii_arr[i] + pin_radius + pair_halo
                        )
                        if center_dist > max_reach:
                            continue
                        pin_shape_params = {"center": pin_center, "radius": pin_radius}
                        cached_i = poly_cache.get(fp_i_uuid, {})
                        shape_i_type = cached_i.get("shape_type", ShapeType.POLYGON)
                        shape_i_params = cached_i.get("shape_params", {})
                        analytical_result = _analytical_collision(
                            shape_i_type, shape_i_params, ShapeType.CIRCLE, pin_shape_params
                        )
                        if analytical_result is not None:
                            penetration, normal, is_overlap = analytical_result
                            _apply_contact_push_analytical(
                                i, j, penetration, normal, is_overlap, cand
                            )
                        else:
                            if body_i.intersects(pin_poly):
                                _apply_contact_push(i, j, body_i, pin_poly, 0.0, cand)
                            else:
                                pin_dist = body_i.distance(pin_poly)
                                pin_halo = pair_halo
                                if pin_dist < pin_halo:
                                    _apply_contact_push(
                                        i, j, body_i, pin_poly, -pin_dist, cand
                                    )

                # 4. Pins_i vs pins_j
                if pins_i and pins_j:
                    for pin_ik, (pin_i_poly, pin_i_center, pin_i_radius, pin_i_layer, pin_i_extends) in enumerate(pins_i):
                        if pins_i_empty[pin_ik]:
                            continue
                        for pin_jk, (pin_j_poly, pin_j_center, pin_j_radius, pin_j_layer, pin_j_extends) in enumerate(pins_j):
                            if pins_j_empty[pin_jk]:
                                continue
                            # Skip same-layer pin-pin if both pins are fully inside courtyards (redundant)
                            if pin_i_layer and pin_i_layer == pin_j_layer and not pin_i_extends and not pin_j_extends:
                                continue
                            dx = pin_i_center[0] - pin_j_center[0]
                            dy = pin_i_center[1] - pin_j_center[1]
                            center_dist = math.hypot(dx, dy)
                            max_reach = (
                                pin_i_radius + pin_j_radius + pair_halo
                            )
                            if center_dist > max_reach:
                                continue
                            pin_i_params = {"center": pin_i_center, "radius": pin_i_radius}
                            pin_j_params = {"center": pin_j_center, "radius": pin_j_radius}
                            analytical_result = _analytical_collision(
                                ShapeType.CIRCLE, pin_i_params,
                                ShapeType.CIRCLE, pin_j_params,
                            )
                            if analytical_result is not None:
                                penetration, normal, is_overlap = analytical_result
                                _apply_contact_push_analytical(
                                    i, j, penetration, normal, is_overlap, cand
                                )
                            else:
                                if pin_i_poly.intersects(pin_j_poly):
                                    _apply_contact_push(
                                        i, j, pin_i_poly, pin_j_poly, 0.0, cand
                                    )
                                else:
                                    pin_dist = pin_i_poly.distance(pin_j_poly)
                                    pin_halo = pair_halo
                                    if pin_dist < pin_halo:
                                        _apply_contact_push(
                                            i, j, pin_i_poly, pin_j_poly, -pin_dist, cand
                                        )

                # Aggregate per-pair candidates into ONE penalty: deepest contact wins.
                if cand:
                    _win = min(cand, key=lambda c: c[1])
                    _s_mm, _ux, _uy = _win[1], _win[2], _win[3]
                    # Mix the fresh normal with the prior step's so the applied
                    # push rotates smoothly instead of snapping between
                    # near-tie candidates (jitter source). Aligned winners
                    # blend halfway; an opposing winner slews in slowly
                    # (prior-dominant) so a 180° flip can never land in one
                    # step — its average would cancel to zero and bypass any
                    # blend, which is exactly the chatter pattern.
                    _pair = (i, j) if i < j else (j, i)
                    _prev = ctx.last_contact.get(_pair) if ctx is not None else None
                    if _prev is not None:
                        _dot = _ux * _prev[0] + _uy * _prev[1]
                        if _dot < 0.0:
                            _w_new, _w_old = 0.25, 0.75
                        else:
                            _w_new, _w_old = 0.5, 0.5
                        _bx = _w_new * _ux + _w_old * _prev[0]
                        _by = _w_new * _uy + _w_old * _prev[1]
                        _bn = math.hypot(_bx, _by)
                        if _bn > 1e-12:
                            _ux, _uy = _bx / _bn, _by / _bn
                    if ctx is not None:
                        ctx.last_contact[_pair] = (_ux, _uy)
                    _halo_mm = pair_halo
                    _apply_push(i, j, _s_mm, _ux, _uy, _halo_mm)

            # Drop blended normals for pairs no longer in contact range so a
            # part that leaves and returns doesn't mix with a stale direction.
            if ctx is not None and (ctx.last_contact or ctx.last_tangent):
                _live = set()
                for _a, _b in candidate_pairs:
                    _live.add((_a, _b) if _a < _b else (_b, _a))
                for _k in list(ctx.last_contact):
                    if _k not in _live:
                        del ctx.last_contact[_k]
                for _k in list(ctx.last_tangent):
                    if _k[1] != -1 and _k not in _live:
                        del ctx.last_tangent[_k]

            # Board wall: the region polygon is an inverted courtyard. Same
            # wall law as part-part contact (wall_k_N / halo / exponent /
            # cap), so the board edge pushes parts inside exactly like a
            # neighbour's courtyard pushes them apart. Uses region.boundary
            # (outer ring + holes) so cutouts act as walls for free once
            # regions carry holes. Signed separation follows the same
            # convention: gap +, penetration -.
            if ctx.region_polygons and ctx.fp_uuid_to_region:
                halo_mm = params.wall_halo_m * 1000.0
                for fp_local_idx, fp_uuid in enumerate(ctx.fp_uuids):
                    if not fp_movable_arr[fp_local_idx]:
                        continue
                    region_idx = ctx.fp_uuid_to_region.get(fp_uuid)
                    if region_idx is None or region_idx < 0 or region_idx >= len(ctx.region_polygons):
                        continue
                    region_poly = ctx.region_polygons[region_idx]
                    if region_poly.is_empty:
                        continue
                    body_poly = ctx.poly_cache.get(fp_uuid, {}).get("body_poly")
                    if body_poly is None or body_poly.is_empty:
                        continue
                    clearance_boundary = ctx.region_boundaries[region_idx]
                    if clearance_boundary is None or clearance_boundary.is_empty:
                        continue
                    # Inverted courtyard via the shared helper (same law the
                    # label orbit uses); tangential assist stays local.
                    fx, fy = board_wall_force(
                        body_poly, region_poly, clearance_boundary,
                        ctx.region_centroids[region_idx],
                        params.wall_k_N, halo_mm,
                        params.wall_exponent, params.wall_cap_depth_mm,
                    )
                    force_mag = math.hypot(fx, fy)
                    if force_mag <= 0.0:
                        continue
                    ux, uy = fx / force_mag, fy / force_mag
                    # Tangential assist for board wall (wall velocity = 0)
                    ft_x = ft_y = 0.0
                    if pad_vel is not None and params.contact_tangent_gamma > 0.0:
                        tx, ty = -uy, ux
                        v_i_t = _fp_tangential_vel(fp_local_idx, tx, ty)
                        sgn = _tangent_sign((fp_local_idx, -1), v_i_t)
                        f_t = params.contact_tangent_gamma * force_mag * sgn
                        ft_x, ft_y = f_t * tx, f_t * ty
                    rows = ctx.fp_local_to_rows[fp_local_idx]
                    if rows is not None and len(rows) > 0:
                        _spread_force(
                            rows, fp_centroids_arr[fp_local_idx, 0], fp_centroids_arr[fp_local_idx, 1],
                            fp_radii_arr[fp_local_idx], ux, uy, fx, fy,
                            poly_force, tangential_force, ft_x, ft_y,
                        )

            force += poly_force
    force += tangential_force

    # Replace NaN
    force = np.nan_to_num(force, nan=0.0, posinf=0.0, neginf=0.0)
    force[~pad_movable] = 0.0

    if not return_components:
        return force, None

    components = {
        "repulsion": np.nan_to_num(repulsion, nan=0.0, posinf=0.0, neginf=0.0),
        "attraction": np.nan_to_num(attraction, nan=0.0, posinf=0.0, neginf=0.0),
        "ghost": np.nan_to_num(ghost_attr_force, nan=0.0, posinf=0.0, neginf=0.0),
        "courtyard": np.nan_to_num(poly_force, nan=0.0, posinf=0.0, neginf=0.0),
        "tangential": np.nan_to_num(tangential_force, nan=0.0, posinf=0.0, neginf=0.0),
    }
    if align_active:
        # Present only when the regime is active, so off-regime
        # breakdowns keep their exact cause set.
        components["align"] = np.nan_to_num(align_force, nan=0.0, posinf=0.0, neginf=0.0)
    for comp in components.values():
        comp[~pad_movable] = 0.0
    return force, components
