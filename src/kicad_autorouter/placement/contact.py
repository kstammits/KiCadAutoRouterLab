"""Analytical collision detection, wall laws, and courtyard geometry for placement.

Contact uses penalty forces (soft constraints), not hard constraints:
signed separation `s` in mm (gap +, penetration -); wall law
F = k_wall * (1 - s/halo)^p. Deepest contact wins per pair (winner-takes-all)
so stacked sub-contacts (e.g. THT pin circles) can't inflate one pair's penalty.
Direction is the SAT minimum-translation-vector axis (out the nearest edge),
never centroid-to-centroid.
"""

from __future__ import annotations

import math
import warnings
from typing import Optional, Tuple

import numpy as np
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import nearest_points, polygonize, unary_union

from ..board_model import Footprint, Segment
from .rigid import _rigid_pose_vertices


class ShapeType:
    """Shape classification for analytical collision."""
    EMPTY = "empty"
    RECTANGLE = "rectangle"
    CIRCLE = "circle"
    POLYGON = "polygon"


# ---------------------------------------------------------------------------
# Shape classification
# ---------------------------------------------------------------------------

def _classify_rectangle(verts: np.ndarray) -> Optional[dict]:
    """Classify vertices as rectangle, return params or None."""
    if len(verts) < 4:
        return None
    if np.allclose(verts[0], verts[-1]):
        corners = verts[:-1]
    else:
        corners = verts
    if len(corners) != 4:
        return None
    center = corners.mean(axis=0)
    edges = np.roll(corners, -1, axis=0) - corners
    edge_lengths = np.linalg.norm(edges, axis=1)

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
    idx = np.where(np.isclose(edge_lengths, unique_lengths[0], rtol=1e-3))[0][0]
    edge0 = edges[idx]
    angle = math.atan2(edge0[1], edge0[0])
    angle = (angle + math.pi / 2) % math.pi - math.pi / 2
    ca, sa = math.cos(angle), math.sin(angle)
    v = corners - center
    local_x = v[:, 0] * ca + v[:, 1] * sa
    local_y = -v[:, 0] * sa + v[:, 1] * ca
    hx = float(abs(local_x).max())
    hy = float(abs(local_y).max())
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
    n_verts = len(coords) - 1
    if n_verts < 8:
        return None
    centroid = np.array([poly.centroid.x, poly.centroid.y])
    verts = np.array(coords[:-1])
    radii = np.linalg.norm(verts - centroid, axis=1)
    mean_r = float(radii.mean())
    std_r = float(radii.std())
    if mean_r > 0 and std_r / mean_r < 0.05:
        return {
            "type": ShapeType.CIRCLE,
            "center": (float(centroid[0]), float(centroid[1])),
            "radius": mean_r,
        }
    return None


def _classify_shape(poly: Polygon) -> Tuple[str, dict]:
    """Classify polygon shape for analytical collision.

    Returns: (shape_type, params_dict)
    """
    if poly.is_empty:
        return ShapeType.EMPTY, {}
    coords = list(poly.exterior.coords)
    verts = np.array(coords, dtype=np.float64)
    rect = _classify_rectangle(verts)
    if rect:
        return ShapeType.RECTANGLE, rect
    approx_rect = _classify_approx_rectangle(poly)
    if approx_rect:
        return ShapeType.RECTANGLE, approx_rect
    circle = _classify_circle(poly)
    if circle:
        return ShapeType.CIRCLE, circle
    return ShapeType.POLYGON, {"polygon": poly}


def _classify_approx_rectangle(poly: Polygon) -> Optional[dict]:
    """Classify polygon as approximate rectangle using minimum rotated rectangle.

    Returns rectangle params if polygon area >= 95% of minimum bounding
    rectangle area.
    """
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=RuntimeWarning)
            min_rect = poly.minimum_rotated_rectangle
        if min_rect.is_empty or min_rect.area <= 0:
            return None
        area_ratio = poly.area / min_rect.area
        if area_ratio < 0.95:
            return None
        coords = list(min_rect.exterior.coords)
        rect_verts = np.array(coords[:-1], dtype=np.float64)
        if len(rect_verts) != 4:
            return None
        center = rect_verts.mean(axis=0)
        edges = np.roll(rect_verts, -1, axis=0) - rect_verts
        edge_lengths = np.linalg.norm(edges, axis=1)
        idx = int(np.argmax(edge_lengths))
        edge0 = edges[idx]
        angle = math.atan2(edge0[1], edge0[0])
        angle = (angle + math.pi / 2) % math.pi - math.pi / 2
        ca, sa = math.cos(angle), math.sin(angle)
        v = rect_verts - center
        local_x = v[:, 0] * ca + v[:, 1] * sa
        local_y = -v[:, 0] * sa + v[:, 1] * ca
        hx = float(abs(local_x).max())
        hy = float(abs(local_y).max())
        return {
            "type": ShapeType.RECTANGLE,
            "center": (float(center[0]), float(center[1])),
            "half_extents": (hx, hy),
            "angle": angle,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# SAT collision
# ---------------------------------------------------------------------------

def _sat_rect_rect(rect1: dict, rect2: dict) -> Tuple[bool, Tuple[float, float], float]:
    """SAT collision for two rectangles (pure Python).

    Returns: (overlaps, normal, penetration_depth)
    normal points from rect1 to rect2 (pushes rect1 away from rect2).
    """
    c1x, c1y = rect1["center"]
    c2x, c2y = rect2["center"]
    hx1, hy1 = rect1["half_extents"]
    hx2, hy2 = rect2["half_extents"]
    a1 = rect1["angle"]
    a2 = rect2["angle"]

    ca1, sa1 = math.cos(a1), math.sin(a1)
    ca2, sa2 = math.cos(a2), math.sin(a2)

    axes = [
        (ca1, sa1),
        (-sa1, ca1),
        (ca2, sa2),
        (-sa2, ca2),
    ]

    min_overlap = float("inf")
    best_axis = None
    dx_centers = c2x - c1x
    dy_centers = c2y - c1y

    for ax, ay in axes:
        r1_proj = abs(hx1 * (ca1 * ax + sa1 * ay)) + abs(hy1 * (-sa1 * ax + ca1 * ay))
        r2_proj = abs(hx2 * (ca2 * ax + sa2 * ay)) + abs(hy2 * (-sa2 * ax + ca2 * ay))
        center_dist = abs(dx_centers * ax + dy_centers * ay)
        overlap = r1_proj + r2_proj - center_dist
        if overlap < min_overlap:
            min_overlap = overlap
            best_axis = (ax, ay)

    if best_axis is None:
        return False, (0.0, 0.0), 0.0

    if (dx_centers * best_axis[0] + dy_centers * best_axis[1]) < 0:
        best_axis = (-best_axis[0], -best_axis[1])

    overlaps = min_overlap >= -1e-10
    return overlaps, best_axis, min_overlap


def _circle_circle_contact(c1: dict, c2: dict) -> Tuple[bool, Tuple[float, float], float]:
    """Circle-circle collision (pure Python)."""
    c1x, c1y = c1["center"]
    c2x, c2y = c2["center"]
    r1 = c1["radius"]
    r2 = c2["radius"]

    dx = c2x - c1x
    dy = c2y - c1y
    dist_sq = dx * dx + dy * dy

    if dist_sq < 1e-20:
        return True, (1.0, 0.0), r1 + r2

    dist = math.sqrt(dist_sq)
    nx = dx / dist
    ny = dy / dist
    penetration = r1 + r2 - dist
    overlaps = penetration >= -1e-10
    return overlaps, (nx, ny), penetration


def _rect_circle_contact(rect: dict, circle: dict) -> Tuple[bool, Tuple[float, float], float]:
    """Rectangle-circle collision using SAT (pure Python).

    Normal points from rect to circle.
    """
    rx, ry = rect["center"]
    cx, cy = circle["center"]
    hx, hy = rect["half_extents"]
    angle = rect["angle"]
    r = circle["radius"]

    ca, sa = math.cos(angle), math.sin(angle)
    dx = cx - rx
    dy = cy - ry
    local_x = dx * ca + dy * sa
    local_y = -dx * sa + dy * ca

    clamped_x = max(-hx, min(hx, local_x))
    clamped_y = max(-hy, min(hy, local_y))

    diff_x = local_x - clamped_x
    diff_y = local_y - clamped_y
    dist_sq = diff_x * diff_x + diff_y * diff_y

    if dist_sq < 1e-20:
        px = hx - abs(local_x)
        py = hy - abs(local_y)
        if px < py:
            nx_local = 1.0 if local_x >= 0 else -1.0
            ny_local = 0.0
            penetration = px
        else:
            nx_local = 0.0
            ny_local = 1.0 if local_y >= 0 else -1.0
            penetration = py
    else:
        dist = math.sqrt(dist_sq)
        nx_local = diff_x / dist
        ny_local = diff_y / dist
        penetration = r - dist

    nx_world = nx_local * ca - ny_local * sa
    ny_world = nx_local * sa + ny_local * ca

    if (nx_world * dx + ny_world * dy) < 0:
        nx_world = -nx_world
        ny_world = -ny_world

    overlaps = penetration >= -1e-10
    return overlaps, (nx_world, ny_world), penetration


def _analytical_collision(
    shape_i_type: str, shape_i_params: dict,
    shape_j_type: str, shape_j_params: dict
) -> Optional[Tuple[float, Tuple[float, float], bool]]:
    """Perform analytical collision detection for supported shape pairs.

    Returns: (signed_penetration, normal, is_overlap) or None if not supported.
    signed_penetration > 0: overlap depth (mm)
    signed_penetration < 0: gap distance (mm)
    normal: unit vector from j to i (push i away from j)
    """
    if shape_i_type == ShapeType.RECTANGLE and shape_j_type == ShapeType.RECTANGLE:
        overlaps, normal, penetration = _sat_rect_rect(shape_i_params, shape_j_params)
        return penetration, (-normal[0], -normal[1]), overlaps
    if shape_i_type == ShapeType.CIRCLE and shape_j_type == ShapeType.CIRCLE:
        overlaps, normal, penetration = _circle_circle_contact(shape_i_params, shape_j_params)
        return penetration, (-normal[0], -normal[1]), overlaps
    if shape_i_type == ShapeType.RECTANGLE and shape_j_type == ShapeType.CIRCLE:
        overlaps, normal, penetration = _rect_circle_contact(shape_i_params, shape_j_params)
        return penetration, (-normal[0], -normal[1]), overlaps
    if shape_i_type == ShapeType.CIRCLE and shape_j_type == ShapeType.RECTANGLE:
        overlaps, normal, penetration = _rect_circle_contact(shape_j_params, shape_i_params)
        return penetration, normal, overlaps
    return None


# ---------------------------------------------------------------------------
# Contact force helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Wall laws
# ---------------------------------------------------------------------------

def _wall_mag(
    k_wall_N: float, s_mm: float, halo_mm: float, exponent: float = 1.0,
    cap_depth_mm: float = 0.0,
) -> float:
    """Unified wall-law magnitude in newtons: F = k*(1-s/h)^p.

    s is the signed separation in mm (gap +, penetration -); zero past the
    halo reach. p = 1 is the linear default; p > 1 softens the halo approach
    and stiffens deep penetration. cap_depth_mm > 0 clamps the evaluated
    depth to s >= -cap (force saturates); 0 disables capping.
    """
    if s_mm >= halo_mm:
        return 0.0
    if cap_depth_mm > 0.0:
        s_mm = max(s_mm, -cap_depth_mm)
    return k_wall_N * (1.0 - s_mm / halo_mm) ** exponent


def _boundary_edge_push(dist_m: float, threshold_m: float, k_b_N: float) -> float:
    """Inward push from one region edge in newtons: bounded linear falloff.

    Returns 0 past the threshold, rising linearly to 2*k_b exactly at the
    edge; already-outside parts get an ~k_b saturating pull back in.
    """
    if dist_m < 0:
        d = abs(dist_m)
        return k_b_N * d / (d + 1e-9)
    if dist_m < threshold_m:
        return k_b_N * 2.0 * (threshold_m - dist_m) / threshold_m
    return 0.0


# ---------------------------------------------------------------------------
# Courtyard geometry
# ---------------------------------------------------------------------------

def _courtyard_outline_polygon(segments: Tuple[Segment, ...]) -> Optional[Polygon]:
    """Build the courtyard outline polygon from raw segments, any order.

    ``shapely.ops.polygonize`` assembles closed rings regardless of segment
    order/orientation. Returns the largest closed ring by area, or None when
    nothing closes.
    """
    from shapely import set_precision

    linestrings: list = []
    for a, b in segments or ():
        p = (a.x_mm, a.y_mm)
        q = (b.x_mm, b.y_mm)
        if math.hypot(q[0] - p[0], q[1] - p[1]) > 1e-9:
            linestrings.append(LineString([p, q]))
    if not linestrings:
        return None

    linestrings = [set_precision(ls, grid_size=0.001) for ls in linestrings]

    try:
        rings = [r for r in polygonize(linestrings) if r.is_valid and r.area > 0]
    except Exception:
        return None
    if not rings:
        return None
    if len(rings) == 1:
        return rings[0]
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


def _footprint_courtyard_polygon(
    fp: Footprint,
    pad_positions: Optional[np.ndarray] = None,
    pad_indices: Optional[List[int]] = None,
) -> Polygon:
    """Build a shapely Polygon from footprint's courtyard segments.

    If no courtyard, falls back to pad bounding box.
    """

    def _get_courtyard_vertices() -> list:
        outline = _courtyard_outline_polygon(fp.courtyard)
        if outline is None:
            return []
        return list(outline.exterior.coords)

    def _get_original_pad_positions() -> np.ndarray:
        if not fp.pads:
            return np.empty((0, 2), dtype=np.float64)
        return np.array(
            [(p.position.x_mm, p.position.y_mm) for p in fp.pads], dtype=np.float64
        )

    def _transform_vertices(vertices, orig_pos, curr_pos):
        posed = _rigid_pose_vertices(
            np.asarray(vertices, dtype=np.float64), orig_pos, curr_pos
        )
        return [tuple(map(float, row)) for row in posed.tolist()]

    courtyard_vertices = _get_courtyard_vertices()
    if courtyard_vertices:
        if pad_positions is not None and pad_indices and len(pad_indices) > 0:
            orig_pad_pos = _get_original_pad_positions()
            curr_pad_pos = pad_positions[pad_indices]
            if len(orig_pad_pos) == len(curr_pad_pos):
                courtyard_vertices = _transform_vertices(
                    courtyard_vertices, orig_pad_pos, curr_pad_pos
                )
        if len(courtyard_vertices) >= 3:
            try:
                poly = Polygon(courtyard_vertices)
                if poly.is_valid and poly.area > 0:
                    return poly
            except Exception:
                pass

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
        return Polygon([
            (fp.x_mm - 0.5, fp.y_mm - 0.5),
            (fp.x_mm + 0.5, fp.y_mm - 0.5),
            (fp.x_mm + 0.5, fp.y_mm + 0.5),
            (fp.x_mm - 0.5, fp.y_mm + 0.5),
        ])
    margin = 0.1
    return Polygon([
        (min_x - margin, min_y - margin),
        (max_x + margin, min_y - margin),
        (max_x + margin, max_y + margin),
        (min_x - margin, max_y + margin),
    ])


# ---------------------------------------------------------------------------
# Board bounding box
# ---------------------------------------------------------------------------

def _board_bbox(model) -> Tuple[float, float, float, float]:
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
