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
from typing import Dict, List, Optional, Tuple

import numpy as np
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import nearest_points, polygonize, unary_union

from ..board_model import Footprint, Segment
from .rigid import _rigid_pose_vertices


def _overlap_depth_along(
    pa_coords, pb_coords, ux: float, uy: float
) -> float:
    """Penetration depth (mm) of two rings along unit (ux, uy).

    Vertex-projection interval overlap; exact for convex outlines,
    a conservative under-read for concave ones (never invents depth).
    """
    try:
        pa = [x * ux + y * uy for x, y in pa_coords]
        pb = [x * ux + y * uy for x, y in pb_coords]
    except Exception:
        return 0.0
    return max(0.0, min(max(pa), max(pb)) - max(min(pa), min(pb)))


def label_contact_force(
    label_poly: Polygon,
    body_polys: Dict[str, Polygon],
    halo_mm: float,
    wall_k_N: float,
    wall_exponent: float = 2.0,
    wall_cap_depth_mm: float = 0.0,
    solidity: float = 1.0,
    skip_uuids: Tuple[str, ...] = (),
) -> Tuple[float, float]:
    """Wall-law push (newtons, board frame) on one orbiting label rect.

    Sums one wall-law contact per nearby body (own parent included — a
    filed anchor inside the courtyard equilibrates against its leash,
    which is exactly the float). Direction is the nearest-points axis,
    out of the body; overlap depth comes from vertex-projection along
    that axis. ``solidity`` scales the summed force so the slider stays
    meaningful in orbit mode (0 skips the loop entirely — caller-gated,
    but double-guarded here). Winner-takes-all is deliberately NOT used:
    a label wedged between two bodies should feel both.
    """
    if label_poly is None or label_poly.is_empty:
        return (0.0, 0.0)
    if halo_mm <= 0.0 or wall_k_N <= 0.0 or solidity <= 0.0:
        return (0.0, 0.0)
    try:
        label_coords = list(label_poly.exterior.coords)
    except Exception:
        return (0.0, 0.0)
    fx = fy = 0.0
    for uuid, body in body_polys.items():
        if uuid in skip_uuids:
            continue
        try:
            if body is None or body.is_empty:
                continue
            if label_poly.disjoint(body):
                try:
                    gap = label_poly.distance(body)
                except Exception:
                    continue
                if gap >= halo_mm:
                    continue
                try:
                    qi, qj = nearest_points(label_poly, body)
                    dx, dy = float(qi.x - qj.x), float(qi.y - qj.y)
                except Exception:
                    continue
                dist = math.hypot(dx, dy)
                if dist <= 1e-9:
                    continue
                ux, uy = dx / dist, dy / dist
                s_mm = gap
            else:
                try:
                    qi, qj = nearest_points(label_poly, body)
                    dx, dy = float(qi.x - qj.x), float(qi.y - qj.y)
                except Exception:
                    continue
                dist = math.hypot(dx, dy)
                if dist <= 1e-9:
                    try:
                        dx = label_poly.centroid.x - body.centroid.x
                        dy = label_poly.centroid.y - body.centroid.y
                    except Exception:
                        continue
                    dist = math.hypot(dx, dy)
                    if dist <= 1e-9:
                        # Concentric (e.g. a filed anchor dead-center in
                        # its own courtyard): any axis pushes out, so take
                        # +x instead of reporting zero and sticking forever.
                        dx, dy, dist = 1.0, 0.0, 1.0
                ux, uy = dx / dist, dy / dist
                try:
                    body_coords = list(body.exterior.coords)
                except Exception:
                    continue
                s_mm = -_overlap_depth_along(label_coords, body_coords, ux, uy)
            mag = _wall_mag(
                wall_k_N, s_mm, halo_mm, wall_exponent, wall_cap_depth_mm
            )
            fx += mag * ux
            fy += mag * uy
        except Exception:
            continue
    return (solidity * fx, solidity * fy)


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
    if np.any(edge_lengths <= 0):
        return None  # degenerate corner (repeated vertex): not a rectangle

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


def _pose_shape_params(
    shape_type: str,
    shape_params: dict,
    last_centroid: tuple,
    last_angle: float,
    curr_centroid: np.ndarray,
    curr_angle: float,
    body_poly: Polygon,
) -> Tuple[str, dict]:
    """Re-pose cached classification params by an incremental rigid motion.

    A courtyard body is a rigid transform of fixed file-pose vertices, so
    its shape type and rotation-invariant extents (rectangle half-extents,
    circle radius) never change across rebuilds — only the center and angle
    move. Reposing them from the pad-centroid shift and wrapped angle delta
    is real-arithmetic-identical to re-posing the vertices and running
    :func:`_classify_shape` again, without the numpy/GEOS classify cost
    (``minimum_rotated_rectangle`` for polygon types). POLYGON bodies reuse
    the freshly posed polygon directly.
    """
    dx = float(curr_centroid[0] - last_centroid[0])
    dy = float(curr_centroid[1] - last_centroid[1])
    dang = float(curr_angle - last_angle)
    dang = (dang + math.pi) % (2.0 * math.pi) - math.pi
    ca, sa = math.cos(dang), math.sin(dang)
    lx, ly = last_centroid

    def _move_center(cx: float, cy: float) -> tuple:
        rx, ry = cx - lx, cy - ly
        return (
            rx * ca - ry * sa + lx + dx,
            rx * sa + ry * ca + ly + dy,
        )

    if shape_type == ShapeType.RECTANGLE:
        cx, cy = shape_params["center"]
        ncx, ncy = _move_center(float(cx), float(cy))
        angle = float(shape_params["angle"]) + dang
        angle = (angle + math.pi / 2) % math.pi - math.pi / 2
        hx, hy = shape_params["half_extents"]
        return shape_type, {
            "type": ShapeType.RECTANGLE,
            "center": (ncx, ncy),
            "half_extents": (float(hx), float(hy)),
            "angle": angle,
        }
    if shape_type == ShapeType.CIRCLE:
        cx, cy = shape_params["center"]
        ncx, ncy = _move_center(float(cx), float(cy))
        return shape_type, {
            "type": ShapeType.CIRCLE,
            "center": (ncx, ncy),
            "radius": float(shape_params["radius"]),
        }
    if shape_type == ShapeType.POLYGON:
        return shape_type, {"polygon": body_poly}
    return _classify_shape(body_poly)


def _classify_approx_rectangle(poly: Polygon) -> Optional[dict]:
    """Classify polygon as approximate rectangle using minimum rotated rectangle.

    Returns rectangle params if polygon area >= 95% of minimum bounding
    rectangle area.
    """
    try:
        if poly.is_empty or poly.area <= 0:
            return None
        if len(list(poly.exterior.coords)) < 4:
            return None
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
        # _rect_circle_contact returns normal from rect to circle (i to j).
        # We need normal from j to i (circle to rect), so negate.
        overlaps, normal, penetration = _rect_circle_contact(shape_i_params, shape_j_params)
        return penetration, (-normal[0], -normal[1]), overlaps
    if shape_i_type == ShapeType.CIRCLE and shape_j_type == ShapeType.RECTANGLE:
        # Args swapped: _rect_circle_contact(rect, circle) returns normal from rect to circle (j to i).
        # This is already the correct direction (j to i), so DON'T negate.
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
    halo reach. p = 1 is linear; p > 1 softens the halo approach and
    stiffens deep penetration. cap_depth_mm > 0 clamps the evaluated
    depth to s >= -cap (force saturates); 0 disables capping.
    halo_mm <= 0 disables the law entirely (returns 0: "no courtyard
    contact") instead of dividing by zero.
    """
    if halo_mm <= 0:
        return 0.0
    if s_mm >= halo_mm:
        return 0.0
    if cap_depth_mm > 0.0:
        s_mm = max(s_mm, -cap_depth_mm)
    return k_wall_N * (1.0 - s_mm / halo_mm) ** exponent





# ---------------------------------------------------------------------------
# Pad copper geometry (per-layer, true shape)
# ---------------------------------------------------------------------------

def pad_copper_polygon(pad) -> Polygon:
    """Copper polygon of one pad in board coordinates (true shape, not a disc).

    ``circle`` (and square ``oval``) is exact; ``rect`` is exact;
    ``roundrect`` uses its bounding rect (corner radius only removes copper,
    so this over-covers); ``oval`` is a capsule along the pad's local x-axis
    (same construction as the router's ``_paint_pad``); ``chamfered`` /
    ``trapezoid`` / ``custom`` / unknown fall back to the bounding rect and
    are documented as conservative. Rotation uses ``pad.angle_deg`` (absolute
    board orientation) with the board-geometry sign convention R(-angle):
    same negation as ``_to_board`` and the SVG pad paint (local Y-DOWN to
    board), verified against pcbnew — R(+angle) mirrors copper against its
    own pad row. Polygons built from posed pads are pose-correct.
    """
    w = float(pad.size_mm[0])
    h = float(pad.size_mm[1])
    cx = float(pad.position.x_mm)
    cy = float(pad.position.y_mm)
    shape = (getattr(pad, "shape", "") or "").lower()
    if shape == "circle" or (shape == "oval" and w == h):
        return Point(cx, cy).buffer(max(w, h) / 2.0)
    ang = math.radians(-float(pad.angle_deg or 0.0))
    ca, sa = math.cos(ang), math.sin(ang)

    def rot(sx: float, sy: float) -> tuple:
        return (cx + sx * ca - sy * sa, cy + sy * ca + sx * sa)

    hw, hh = w / 2.0, h / 2.0
    rect = Polygon([rot(-hw, -hh), rot(hw, -hh), rot(hw, hh), rot(-hw, hh)])
    if shape == "oval":
        # Capsule ends (pour._paint_pad construction, but with the board
        # sign convention): spine offset along local x by (w-h)/2, end
        # discs of radius min/2.
        ex, ey = ca * (w - h) / 2.0, sa * (w - h) / 2.0
        r_end = min(w, h) / 2.0
        ends = [
            Point(cx - ex, cy - ey).buffer(r_end),
            Point(cx + ex, cy + ey).buffer(r_end),
        ]
        return unary_union([rect, *ends])
    return rect


def pad_present_on_layer(pad, fp_layer: str, layer: str, copper_layers) -> bool:
    """Whether a pad has copper on a given copper layer.

    Through-hole pads (plated or bare, ``*.Cu``) span every copper layer;
    SMD pads live only on their footprint's layer (mirrors the router's
    ``_pad_layers``).
    """
    if pad.is_through_hole:
        return layer in tuple(copper_layers or ())
    return layer == fp_layer


# ---------------------------------------------------------------------------
# Courtyard geometry
# ---------------------------------------------------------------------------

# Courtyard edges in real footprints are occasionally a few microns short of
# meeting (KiCad writes segment endpoints independently). Polygonize needs an
# exact node, so a sub-10um drafting gap can otherwise collapse a large body to
# the pad-bbox fallback. Repair only when the normal polygonize pass fails so
# valid, complex courtyards keep their exact vertices.
_COURTYARD_GAP_TOL_MM = 0.02
_COURTYARD_FALLBACK_MARGIN_MM = 0.1


def _add_courtyard_gap_connectors(
    linestrings: list, tol_mm: float
) -> list:
    """Return connector segments for distinct endpoints within ``tol_mm``."""
    endpoints = []
    for idx, line in enumerate(linestrings):
        coords = list(line.coords)
        if len(coords) < 2:
            continue
        endpoints.append((idx, coords[0]))
        endpoints.append((idx, coords[-1]))

    connectors = []
    seen = set()
    for i in range(len(endpoints)):
        idx_a, p = endpoints[i]
        for j in range(i + 1, len(endpoints)):
            idx_b, q = endpoints[j]
            if idx_a == idx_b:
                continue
            dist = math.hypot(q[0] - p[0], q[1] - p[1])
            if not (1e-9 < dist <= tol_mm):
                continue
            key = tuple(sorted((p, q)))
            if key in seen:
                continue
            seen.add(key)
            connectors.append(LineString([p, q]))
    return connectors


def _courtyard_segment_bbox_polygon(
    segments: Tuple[Segment, ...],
    margin_mm: float = _COURTYARD_FALLBACK_MARGIN_MM,
) -> Optional[Polygon]:
    """Bounding box of declared courtyard segments (broken-ring fallback)."""
    xs = []
    ys = []
    for a, b in segments or ():
        xs.extend((a.x_mm, b.x_mm))
        ys.extend((a.y_mm, b.y_mm))
    if not xs:
        return None
    min_x, max_x = min(xs) - margin_mm, max(xs) + margin_mm
    min_y, max_y = min(ys) - margin_mm, max(ys) + margin_mm
    return Polygon([
        (min_x, min_y),
        (max_x, min_y),
        (max_x, max_y),
        (min_x, max_y),
    ])


def _courtyard_outline_polygon(segments: Tuple[Segment, ...]) -> Optional[Polygon]:
    """Build the courtyard outline polygon from raw segments, any order.

    ``shapely.ops.polygonize`` assembles closed rings regardless of segment
    order/orientation. If nothing closes, retry after adding short connectors
    between endpoint pairs within :data:`_COURTYARD_GAP_TOL_MM` (T1/T2 style
    drafting gaps). Existing vertices are never moved. Returns the largest
    closed ring by area, or None when nothing closes.
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

    def _rings(lines):
        try:
            return [r for r in polygonize(lines) if r.is_valid and r.area > 0]
        except Exception:
            return []

    rings = _rings(linestrings)
    if not rings:
        connectors = _add_courtyard_gap_connectors(
            linestrings, _COURTYARD_GAP_TOL_MM
        )
        if connectors:
            rings = _rings(linestrings + connectors)
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


def _pose_ring(vertices, fp, pad_positions, pad_indices) -> list:
    """Rigidly re-fit a vertex ring from file pads onto current pad centers.

    Node set must mirror the footprint pads (unconnected copper is a node
    too) for the fit; on mismatch keep file coordinates rather than
    fitting the wrong cloud. A lone pad gives translation without spin.
    """
    if pad_positions is None or not pad_indices or not fp.pads:
        return vertices
    orig_pad_pos = np.array(
        [(p.position.x_mm, p.position.y_mm) for p in fp.pads], dtype=np.float64
    )
    curr_pad_pos = pad_positions[pad_indices]
    if len(orig_pad_pos) != len(curr_pad_pos) or len(orig_pad_pos) == 0:
        return vertices
    posed = _rigid_pose_vertices(
        np.asarray(vertices, dtype=np.float64), orig_pad_pos, curr_pad_pos
    )
    return [tuple(map(float, row)) for row in posed.tolist()]


def _footprint_courtyard_polygon(
    fp: Footprint,
    pad_positions: Optional[np.ndarray] = None,
    pad_indices: Optional[List[int]] = None,
) -> Polygon:
    """Build a shapely Polygon from footprint's courtyard segments.

    Closed/repaired courtyards use their true outline — unless the outline
    is drafting debris: polygonized area under half the pad-copper hull
    (pin-marker Fab dots with no body outline) is discarded in favor of
    the pad hull. A non-empty courtyard that still cannot close falls back
    to the bounding box of its declared segments (plus the same 0.1mm
    margin as the pad fallback), with the same debris gate. Only
    footprints with no courtyard at all fall back to the pad hull.

    Coverage gaps (a closed outline that leaves pins hanging out, or a
    partial outline that misses pads) do NOT rebuild the body: drawn
    geometry stands for physics, protruding THT pins already collide as
    extenders, and :func:`courtyard_is_degenerate` reports the ref so a
    bad library footprint gets fixed at the source instead of silently
    reshaped.
    """

    def _get_courtyard_vertices() -> list:
        outline = _courtyard_outline_polygon(fp.courtyard)
        if outline is None:
            return []
        return list(outline.exterior.coords)

    def _pose_vertices(vertices):
        return _pose_ring(vertices, fp, pad_positions, pad_indices)

    def _as_polygon(vertices) -> Optional[Polygon]:
        if len(vertices) < 3:
            return None
        try:
            poly = Polygon(vertices)
            if poly.is_valid and poly.area > 0:
                return poly
        except Exception:
            pass
        return None

    pad_body = _pad_body_polygon(fp, pad_positions, pad_indices)
    pad_hull = _pad_copper_hull(fp)
    hull_area = pad_hull.area if not pad_hull.is_empty else 0.0
    courtyard_vertices = _get_courtyard_vertices()
    if courtyard_vertices:
        poly = _as_polygon(_pose_vertices(courtyard_vertices))
        if poly is not None and poly.area >= _DEGENERATE_OUTLINE_RATIO * hull_area:
            return poly
        # Debris outline (e.g. pin-marker Fab dots): fall through to the
        # pad-copper hull, skipping the segment-bbox fallback whose
        # extents are the same drafting debris.

    if fp.courtyard:
        fallback = _courtyard_segment_bbox_polygon(fp.courtyard)
        if fallback is not None:
            poly = _as_polygon(_pose_vertices(list(fallback.exterior.coords)))
            # Gate on the RAW segment extents (margin stripped): the
            # fallback's own +0.1mm band would otherwise inflate debris
            # past the bar.
            raw = _segment_extents_area(fp.courtyard)
            if poly is not None and raw >= _DEGENERATE_OUTLINE_RATIO * hull_area:
                return poly

    return pad_body


# Ratio below which a closed courtyard outline is drafting debris (pin
# markers, not a body): a real courtyard encloses the pads, so anything
# under half the pad-copper hull area cannot be one.
_DEGENERATE_OUTLINE_RATIO = 0.5

# Pad-copper area (mm^2) allowed outside the drawn body before the ref
# is reported as a bad library drawing. Same 1mm^2 bar as the DCCF
# overlap audit: sub-mm slivers are polygonize noise everywhere in this
# codebase. Reporting only — the body still uses the drawing (protruding
# THT pins already collide as extenders).
_CONTAIN_TOL_MM2 = 1.0


def _uncovered_area(container: Polygon, hull: Polygon) -> float:
    """Pad-copper hull area lying outside the drawn body (mm^2)."""
    try:
        if container.is_empty or hull.is_empty:
            return float(hull.area)
        return float(hull.difference(container).area)
    except Exception:
        return float(hull.area)


def _segment_extents_area(segments) -> float:
    """Raw (unmargined) bounding-box area of courtyard segments."""
    try:
        xs = [p.x_mm for seg in (segments or ()) for p in seg]
        ys = [p.y_mm for seg in (segments or ()) for p in seg]
    except (TypeError, AttributeError):
        return 0.0
    if not xs or not ys:
        return 0.0
    return max(max(xs) - min(xs), 0.0) * max(max(ys) - min(ys), 0.0)


def _pad_copper_hull(fp: Footprint) -> Polygon:
    """Convex hull around all pad copper in file coordinates (no margin).

    The unmargined extent used for coverage comparisons: margin is
    clearance, not copper, and must not count as "outside the drawing".
    Padless footprints yield an empty polygon (the point-box fallback
    lives in :func:`_pad_body_polygon`, not here).
    """
    if not fp.pads:
        return Polygon()
    try:
        copper = unary_union([pad_copper_polygon(p) for p in fp.pads])
        hull = copper.convex_hull
        if isinstance(hull, Polygon) and not hull.is_empty:
            return hull
        minx, miny, maxx, maxy = copper.bounds
        return Polygon([
            (minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy),
        ])
    except Exception:
        return Polygon()


# Arc-vertex budget for the pad-copper hull body: round-pin stadiums
# (e.g. a two-pin header) tessellate each semicircular end into dozens of
# segments at buffer resolution 8. Buffering slightly wide
# (margin + tol) and simplifying back by tol halves the vertex count while
# the result provably still covers copper + margin.
_HULL_BUFFER_RESOLUTION = 4
_HULL_SIMPLIFY_TOL_MM = 0.05


def _pad_body_polygon(
    fp: Footprint,
    pad_positions: Optional[np.ndarray] = None,
    pad_indices: Optional[List[int]] = None,
    margin_mm: float = 0.1,
) -> Polygon:
    """Convex hull around all pad copper (+ margin), posed when possible.

    The hull follows pad shapes and rotations (via
    :func:`pad_copper_polygon`), so diagonal parts get a rotated body
    instead of an axis-aligned box. The margined ring is simplified with
    a tolerance funded by a matching extra buffer band, so the final body
    still covers copper + ``margin_mm`` with roughly half the arc
    vertices. In posed mode the file-pose hull ring is re-fit through the
    pad centers like a courtyard outline; a lone pad (no rigid rotation
    observable) translates without spinning.
    """
    if not fp.pads:
        return Polygon([
            (fp.x_mm - 0.5, fp.y_mm - 0.5),
            (fp.x_mm + 0.5, fp.y_mm - 0.5),
            (fp.x_mm + 0.5, fp.y_mm + 0.5),
            (fp.x_mm - 0.5, fp.y_mm + 0.5),
        ])
    copper_hull = _pad_copper_hull(fp)
    body = copper_hull.buffer(
        margin_mm + _HULL_SIMPLIFY_TOL_MM, quad_segs=_HULL_BUFFER_RESOLUTION
    )
    if isinstance(body, Polygon) and not body.is_empty:
        try:
            simple = body.simplify(_HULL_SIMPLIFY_TOL_MM, preserve_topology=True)
            need = copper_hull.buffer(margin_mm, quad_segs=_HULL_BUFFER_RESOLUTION)
            if (
                isinstance(simple, Polygon)
                and simple.is_valid
                and not simple.is_empty
                and simple.area > 0
                and simple.covers(need)
            ):
                body = simple
        except Exception:
            pass
    if not isinstance(body, Polygon) or body.is_empty:
        # Degenerate copper (coincident points): AABB of the union.
        copper = unary_union([pad_copper_polygon(p) for p in fp.pads])
        minx, miny, maxx, maxy = copper.bounds
        body = Polygon([
            (minx - margin_mm, miny - margin_mm),
            (maxx + margin_mm, miny - margin_mm),
            (maxx + margin_mm, maxy + margin_mm),
            (minx - margin_mm, maxy + margin_mm),
        ])
    if pad_positions is not None and pad_indices:
        ring = _pose_ring(list(body.exterior.coords), fp, pad_positions, pad_indices)
        try:
            posed = Polygon(ring)
            if posed.is_valid and posed.area > 0:
                return posed
        except Exception:
            pass
    return body


def courtyard_is_degenerate(fp: Footprint) -> bool:
    """Whether a footprint's declared outline is drafting debris, not a body.

    File-pose comparison of the polygonized courtyard against the pad-copper
    hull of pad copper: True when a closed outline exists but covers less
    than :data:`_DEGENERATE_OUTLINE_RATIO` of the hull area (e.g. Fab
    layers carrying only pin-marker dots) or leaves pad copper hanging
    out (partial outline, courtyard missing a pin row). Comparisons use
    the unmargined hull — margin is clearance, not copper. Empty/missing
    outlines are honest pad-hull fallbacks, not degenerate.
    """
    pad_hull = _pad_copper_hull(fp)
    if pad_hull.is_empty or pad_hull.area <= 0:
        return False
    outline = _courtyard_outline_polygon(fp.courtyard)
    if outline is not None and not outline.is_empty:
        if outline.area < _DEGENERATE_OUTLINE_RATIO * pad_hull.area:
            return True
        return _uncovered_area(outline, pad_hull) > _CONTAIN_TOL_MM2
    if fp.courtyard:
        if _segment_extents_area(fp.courtyard) < _DEGENERATE_OUTLINE_RATIO * pad_hull.area:
            return True
        fallback = _courtyard_segment_bbox_polygon(fp.courtyard)
        if fallback is not None and not fallback.is_empty:
            return _uncovered_area(fallback, pad_hull) > _CONTAIN_TOL_MM2
    return False


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
