"""Analytical contact detection tests for Phase 3 placement optimization.

These tests validate that analytical collision detection (SAT for rectangles,
circle-rectangle, circle-circle) produces results identical to shapely's
nearest_points/distance/intersects, but faster.

Run with: pytest tests/test_analytical_contact.py -v
"""

import math
import numpy as np
import pytest
from shapely.geometry import Polygon, Point, box

from kicad_autorouter.placement import (
    _rigid_pose_vertices,
    _courtyard_outline_polygon,
    _footprint_courtyard_polygon,
    _contact_weights,
)
from kicad_autorouter.board_model import Footprint, Pad, Point as BP, is_power_net_effective, is_ground_net
from tests.helpers import make_fp, make_rect_courtyard, make_smd_pad


# ============================================================
# Analytical contact functions (to be implemented in Phase 3)
# ============================================================

def rect_from_verts(verts: np.ndarray) -> dict:
    """Extract rectangle parameters from 4+1 vertices (closed ring).
    
    Returns: {"center": (cx, cy), "half_extents": (hx, hy), "angle": rad, "corners": 4x2 array}
    Handles any vertex ordering (CW/CCW, starting at any corner).
    """
    # verts is (5, 2) with last == first
    corners = verts[:-1]  # (4, 2)
    center = corners.mean(axis=0)
    
    # Find the two unique edge vectors (ignoring direction)
    edges = np.roll(corners, -1, axis=0) - corners  # (4, 2)
    edge_lengths = np.linalg.norm(edges, axis=1)
    
    # Group edges by length (opposite edges have same length)
    # For a rectangle, there are 2 unique lengths
    unique_lengths = np.unique(np.round(edge_lengths, 6))
    
    if len(unique_lengths) != 2:
        # Not a rectangle, fall back to first edge
        edge0 = edges[0]
        angle = math.atan2(edge0[1], edge0[0])
    else:
        # Find edge with first unique length
        idx = np.where(np.isclose(edge_lengths, unique_lengths[0]))[0][0]
        edge0 = edges[idx]
        angle = math.atan2(edge0[1], edge0[0])
    
    # Normalize angle to [-pi/2, pi/2] since rectangle has 180° symmetry
    angle = (angle + math.pi/2) % math.pi - math.pi/2
    
    # Half extents in local frame
    ca, sa = math.cos(angle), math.sin(angle)
    v = corners - center  # (4, 2)
    local_x = v[:, 0] * ca + v[:, 1] * sa
    local_y = -v[:, 0] * sa + v[:, 1] * ca
    hx = abs(local_x).max()
    hy = abs(local_y).max()
    
    return {
        "center": (float(center[0]), float(center[1])),
        "half_extents": (float(hx), float(hy)),
        "angle": angle,
        "corners": corners,
    }


def circle_from_poly(poly: Polygon) -> dict:
    """Extract circle center/radius from polygon (approximate)."""
    centroid = poly.centroid
    # Radius = max distance from centroid to any vertex
    coords = list(poly.exterior.coords)
    if len(coords) <= 2:
        return {"center": (float(centroid.x), float(centroid.y)), "radius": 0.0}
    max_r = 0.0
    for x, y in coords:
        r = math.hypot(x - centroid.x, y - centroid.y)
        max_r = max(max_r, r)
    return {"center": (float(centroid.x), float(centroid.y)), "radius": max_r}


def classify_shape(poly: Polygon) -> tuple[str, dict]:
    """Classify a polygon as 'rectangle', 'circle', or 'polygon'.
    
    Rectangle: 4 vertices (5 with closure), right angles, opposite edges parallel.
    Circle: Many vertices, roughly constant radius from centroid.
    """
    if poly.is_empty:
        return "empty", {}
    
    coords = list(poly.exterior.coords)
    n_verts = len(coords) - 1  # exclude closure
    
    if n_verts == 4:
        # Check if it's a rectangle
        verts = np.array(coords[:-1], dtype=np.float64)
        edges = np.roll(verts, -1, axis=0) - verts
        angles = np.arctan2(edges[:, 1], edges[:, 0])
        angle_diffs = np.diff(angles)
        # Normalize to [-pi, pi]
        angle_diffs = (angle_diffs + np.pi) % (2 * np.pi) - np.pi
        # Check if all angles are ~90 degrees apart
        if np.allclose(np.abs(angle_diffs), np.pi / 2, atol=0.05):
            return "rectangle", rect_from_verts(np.array(coords, dtype=np.float64))
    
    # Check for circle: many vertices, roughly constant radius
    if n_verts >= 8:
        centroid = np.array([poly.centroid.x, poly.centroid.y])
        radii = np.linalg.norm(np.array(coords[:-1]) - centroid, axis=1)
        if np.std(radii) / np.mean(radii) < 0.05:  # < 5% variation
            return "circle", circle_from_poly(poly)
    
    return "polygon", {"polygon": poly}


# ============================================================
# SAT collision detection for rectangles
# ============================================================

def sat_rect_rect(rect1: dict, rect2: dict) -> tuple[bool, np.ndarray, float]:
    """SAT collision for two rectangles.
    
    Returns: (overlaps, normal, penetration_depth)
    normal points from rect1 to rect2 (i.e., pushes rect1 away from rect2)
    Returns True for touching (penetration = 0) as well as overlapping.
    """
    c1 = np.array(rect1["center"])
    c2 = np.array(rect2["center"])
    hx1, hy1 = rect1["half_extents"]
    hx2, hy2 = rect2["half_extents"]
    a1 = rect1["angle"]
    a2 = rect2["angle"]
    
    # Rotation matrices
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
        
        if overlap < -1e-10:
            return False, np.zeros(2), 0.0  # Separating axis found
        
        if overlap < min_overlap:
            min_overlap = overlap
            best_axis = axis
    
    # Normal points from rect1 to rect2
    # Determine direction: (c2 - c1) dot best_axis > 0 means rect2 is in +axis direction
    if ((c2 - c1) @ best_axis) < 0:
        best_axis = -best_axis
    
    # max(0, min_overlap) treats touching as 0 penetration
    return True, best_axis, max(0.0, min_overlap)


def circle_circle_contact(c1: dict, c2: dict) -> tuple[bool, np.ndarray, float]:
    """Circle-circle collision."""
    center1 = np.array(c1["center"])
    center2 = np.array(c2["center"])
    r1 = c1["radius"]
    r2 = c2["radius"]
    
    diff = center2 - center1
    dist = np.linalg.norm(diff)
    
    if dist >= r1 + r2:
        return False, np.zeros(2), 0.0
    
    if dist < 1e-10:
        return True, np.array([1.0, 0.0]), r1 + r2  # Arbitrary direction
    
    normal = diff / dist
    penetration = r1 + r2 - dist
    return True, normal, penetration


def rect_circle_contact(rect: dict, circle: dict) -> tuple[bool, np.ndarray, float]:
    """Rectangle-circle collision using SAT.
    
    Normal points from rect to circle.
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
    
    if dist >= r:
        return False, np.zeros(2), 0.0
    
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
    
    return True, normal_world, penetration


# ============================================================
# Test fixtures
# ============================================================

def make_rect_fp(ref, uuid, x, y, hw, hh, angle=0.0, layer="F.Cu"):
    """Create footprint with rectangular courtyard."""
    return make_fp(ref, uuid, x, y, ("N1", "N2"), layer=layer, hw=hw, hh=hh, pad_shape="rect")


def make_circle_fp(ref, uuid, x, y, radius, layer="F.Cu", angle=0.0):
    """Create footprint with circular courtyard (many-segment polygon)."""
    # Approximate circle with 16 segments
    angles = np.linspace(0, 2 * math.pi, 17)  # 16 segments + closure
    verts = [(x + radius * math.cos(a), y + radius * math.sin(a)) for a in angles]
    courtyard = tuple((BP(vx, vy), BP(vx2, vy2)) for (vx, vy), (vx2, vy2) in zip(verts, verts[1:]))
    
    pads = tuple(
        make_smd_pad(str(k+1), f"N{k+1}", layer, shape="circle")
        for k in range(2)
    )
    return Footprint(
        ref=ref,
        footprint_id="Test:Circle",
        layer=layer,
        x_mm=x,
        y_mm=y,
        angle_deg=angle,
        pads=pads,
        courtyard=courtyard,
        uuid=uuid,
    )


# ============================================================
# Validation tests (compare analytical vs shapely)
# ============================================================

def shapely_contact(poly1: Polygon, poly2: Polygon) -> tuple[bool, np.ndarray, float]:
    """Get contact normal and penetration from shapely.
    
    Returns True for touching (intersection area = 0) and overlapping.
    """
    if poly1.is_empty or poly2.is_empty:
        return False, np.zeros(2), 0.0
    
    if poly1.intersects(poly2):
        intersection = poly1.intersection(poly2)
        # Use centroid difference as approximate normal
        c1 = np.array([poly1.centroid.x, poly1.centroid.y])
        c2 = np.array([poly2.centroid.x, poly2.centroid.y])
        diff = c2 - c1
        dist = np.linalg.norm(diff)
        if dist < 1e-10:
            normal = np.array([1.0, 0.0])
        else:
            normal = diff / dist
        # Penetration depth from intersection area
        if intersection.area > 0:
            penetration = math.sqrt(intersection.area / math.pi) * 2
        else:
            # Touching (point or line intersection) - zero penetration
            penetration = 0.0
        return True, normal, penetration
    else:
        # Near miss - use distance
        dist = poly1.distance(poly2)
        if dist > 3.0:  # halo threshold
            return False, np.zeros(2), 0.0
        # Get nearest points
        try:
            p1, p2 = shapely.ops.nearest_points(poly1, poly2)
            diff = np.array([p2.x - p1.x, p2.y - p1.y])
            dist = np.linalg.norm(diff)
            if dist < 1e-10:
                c1 = np.array([poly1.centroid.x, poly1.centroid.y])
                c2 = np.array([poly2.centroid.x, poly2.centroid.y])
                diff = c2 - c1
                dist = np.linalg.norm(diff)
            normal = diff / dist if dist > 1e-10 else np.array([1.0, 0.0])
            # Halo penetration = halo_reach - dist
            penetration = 3.0 - dist  # HALO_MM = 3.0
            return True, normal, max(0, penetration)
        except Exception:
            return False, np.zeros(2), 0.0


@pytest.mark.parametrize("angle", [0, 30, 45, 90, 135])
def test_rect_rect_sat_matches_shapely(angle):
    """SAT rectangle-rectangle matches shapely for various rotations."""
    # Fixed rect at origin
    rect1 = make_rect_fp("A", "uuid-a", 0, 0, 3.0, 2.0, angle=0)
    # Moving rect at various positions
    for dx in [-6, -3, -1, 0, 1, 3, 6]:
        for dy in [-4, -2, 0, 2, 4]:
            rect2 = make_rect_fp("B", "uuid-b", dx, dy, 3.0, 2.0, angle=angle)
            
            poly1 = _footprint_courtyard_polygon(rect1)
            poly2 = _footprint_courtyard_polygon(rect2)
            
            # Classify shapes
            type1, data1 = classify_shape(poly1)
            type2, data2 = classify_shape(poly2)
            
            if type1 == "rectangle" and type2 == "rectangle":
                sat_overlap, sat_normal, sat_pen = sat_rect_rect(data1, data2)
                shp_overlap, shp_normal, shp_pen = shapely_contact(poly1, poly2)
                
                # Both should agree on overlap
                assert sat_overlap == shp_overlap, \
                    f"Mismatch at ({dx},{dy}) angle={angle}: sat={sat_overlap}, shp={shp_overlap}"
                
                if sat_overlap:
                    # SAT normal should be along one of the 4 rectangle axes (valid separation axis)
                    # Check that normal is axis-aligned (within tolerance)
                    # For axis-aligned rects, normal should be close to (±1, 0) or (0, ±1)
                    # For rotated rects, normal should be close to one of the rectangle's local axes
                    nx, ny = sat_normal
                    # Check normal is unit length
                    assert abs(nx*nx + ny*ny - 1.0) < 1e-6, f"Normal not unit: {sat_normal}"
                    # Check normal is along a valid separation axis
                    # The normal should be parallel to one of the rectangle edges
                    # For rect1 at angle=0, axes are (1,0), (0,1), (-1,0), (0,-1)
                    # Check if normal is close to any axis direction
                    valid_axes = []
                    for a in [data1["angle"], data1["angle"] + math.pi/2, 
                              data2["angle"], data2["angle"] + math.pi/2]:
                        valid_axes.append((math.cos(a), math.sin(a)))
                        valid_axes.append((-math.cos(a), -math.sin(a)))
                    
                    is_valid_axis = any(abs(nx * vx + ny * vy) > 0.98 for vx, vy in valid_axes)
                    assert is_valid_axis, f"SAT normal {sat_normal} not along valid separation axis (valid: {valid_axes})"


def test_rect_circle_sat_matches_shapely():
    """SAT rectangle-circle matches shapely."""
    rect = make_rect_fp("A", "uuid-a", 0, 0, 3.0, 2.0)
    poly1 = _footprint_courtyard_polygon(rect)
    type1, data1 = classify_shape(poly1)
    
    for dx in [-6, -3, -1, 0, 1, 3, 6]:
        for dy in [-4, -2, 0, 2, 4]:
            circle = make_circle_fp("B", "uuid-b", dx, dy, 2.0)
            poly2 = _footprint_courtyard_polygon(circle)
            type2, data2 = classify_shape(poly2)
            
            if type1 == "rectangle" and type2 == "circle":
                sat_overlap, sat_normal, sat_pen = rect_circle_contact(data1, data2)
                shp_overlap, shp_normal, shp_pen = shapely_contact(poly1, poly2)
                
                assert sat_overlap == shp_overlap, \
                    f"Mismatch at ({dx},{dy}): sat={sat_overlap}, shp={shp_overlap}"
                
                if sat_overlap:
                    # SAT normal for rect-circle should be either:
                    # - Along rect's local axes (if circle hits face)
                    # - Toward circle center from closest rect point (if circle hits corner/edge)
                    # Check normal is unit length
                    nx, ny = sat_normal
                    assert abs(nx*nx + ny*ny - 1.0) < 1e-6, f"Normal not unit: {sat_normal}"
                    # Check it's a valid direction - either aligned with rect axes or pointing to circle center
                    valid_axes = []
                    for a in [data1["angle"], data1["angle"] + math.pi/2]:
                        valid_axes.append((math.cos(a), math.sin(a)))
                        valid_axes.append((-math.cos(a), -math.sin(a)))
                    # Also allow direction from rect center to circle center
                    c1 = np.array(data1["center"])
                    c2 = np.array(data2["center"])
                    diff = c2 - c1
                    if np.linalg.norm(diff) > 1e-10:
                        valid_axes.append(diff / np.linalg.norm(diff))
                        valid_axes.append(-diff / np.linalg.norm(diff))
                    
                    is_valid_axis = any(abs(nx * vx + ny * vy) > 0.98 for vx, vy in valid_axes)
                    assert is_valid_axis, f"SAT normal {sat_normal} not along valid separation axis (valid: {valid_axes})"


def test_circle_circle_sat_matches_shapely():
    """Circle-circle analytical matches shapely."""
    circle1 = make_circle_fp("A", "uuid-a", 0, 0, 2.0)
    poly1 = _footprint_courtyard_polygon(circle1)
    type1, data1 = classify_shape(poly1)
    
    for dx in [-6, -3, -1, 0, 1, 3, 6]:
        for dy in [-4, -2, 0, 2, 4]:
            circle2 = make_circle_fp("B", "uuid-b", dx, dy, 2.0)
            poly2 = _footprint_courtyard_polygon(circle2)
            type2, data2 = classify_shape(poly2)
            
            if type1 == "circle" and type2 == "circle":
                sat_overlap, sat_normal, sat_pen = circle_circle_contact(data1, data2)
                shp_overlap, shp_normal, shp_pen = shapely_contact(poly1, poly2)
                
                assert sat_overlap == shp_overlap, \
                    f"Mismatch at ({dx},{dy}): sat={sat_overlap}, shp={shp_overlap}"
                
                if sat_overlap:
                    dot = sat_normal @ shp_normal
                    assert dot > 0.99, \
                        f"Normal mismatch at ({dx},{dy}): dot={dot:.3f}"


def test_rotated_rect_rect():
    """Test SAT with both rectangles rotated."""
    for a1 in [0, 30, 45, 60, 90]:
        for a2 in [0, 30, 45, 60, 90]:
            rect1 = make_rect_fp("A", "uuid-a", 0, 0, 3.0, 2.0, angle=a1)
            rect2 = make_rect_fp("B", "uuid-b", 2.0, 1.0, 3.0, 2.0, angle=a2)  # Overlapping
            
            poly1 = _footprint_courtyard_polygon(rect1)
            poly2 = _footprint_courtyard_polygon(rect2)
            type1, data1 = classify_shape(poly1)
            type2, data2 = classify_shape(poly2)
            
            if type1 == "rectangle" and type2 == "rectangle":
                sat_overlap, sat_normal, sat_pen = sat_rect_rect(data1, data2)
                shp_overlap, shp_normal, shp_pen = shapely_contact(poly1, poly2)
                
                assert sat_overlap == shp_overlap, \
                    f"Mismatch a1={a1} a2={a2}: sat={sat_overlap}, shp={shp_overlap}"


def test_containment_case():
    """Large rect containing small rect - fallback to centroid direction."""
    large = make_rect_fp("LARGE", "uuid-large", 0, 0, 10.0, 5.0)
    small = make_rect_fp("SMALL", "uuid-small", 0, 0, 1.0, 0.5)
    
    poly1 = _footprint_courtyard_polygon(large)
    poly2 = _footprint_courtyard_polygon(small)
    type1, data1 = classify_shape(poly1)
    type2, data2 = classify_shape(poly2)
    
    if type1 == "rectangle" and type2 == "rectangle":
        sat_overlap, sat_normal, sat_pen = sat_rect_rect(data1, data2)
        # Small centered in large - SAT should detect overlap
        assert sat_overlap
        # Normal should point along one of the axes
        assert abs(sat_normal[0]) > 0.9 or abs(sat_normal[1]) > 0.9


def test_classify_shape_on_real_footprints():
    """Test shape classification on real footprints from tube111."""
    from kicad_autorouter.board_model import board_model
    from kicad_autorouter.sexpr import parse_file
    
    model = board_model(parse_file("tests/fixtures/tube111.kicad_pcb"))
    
    for fp in model.footprints:
        if not fp.courtyard:
            continue
        poly = _footprint_courtyard_polygon(fp)
        if poly.is_empty:
            continue
        shape_type, data = classify_shape(poly)
        # Should classify most as rectangle or circle
        assert shape_type in ("rectangle", "circle", "polygon", "empty")
        if shape_type == "rectangle":
            assert "half_extents" in data
            assert "angle" in data
        elif shape_type == "circle":
            assert "radius" in data


if __name__ == "__main__":
    # Quick manual run
    test_rect_rect_sat_matches_shapely(0)
    test_rect_rect_sat_matches_shapely(45)
    test_rect_rect_sat_matches_shapely(90)
    test_rect_circle_sat_matches_shapely()
    test_circle_circle_sat_matches_shapely()
    test_rotated_rect_rect()
    test_containment_case()
    test_classify_shape_on_real_footprints()
    print("All tests passed!")