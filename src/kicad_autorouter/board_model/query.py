"""Board queries: geometry, regions, deltas, placement commit."""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Collection, Dict, List, Optional, Set, Tuple

from ..sexpr import SExpr
from .model import ArcTrack, BoardModel, EdgeCutArc, Footprint, Point, Segment, Track

def _to_board(
    local_x_mm: float,
    local_y_mm: float,
    x_mm: float,
    y_mm: float,
    angle_deg: float,
    layer: str,
) -> Point:
    """Map a footprint-local point to board coordinates.

    Footprint local coordinates have Y pointing DOWN (KiCad editor convention);
    board coordinates have Y pointing UP. The PCB file stores footprint orientation
    as CCW angle in board (Y-up) coordinates. A CCW rotation in Y-up equals a
    CW (negative) rotation in Y-down local coords, so rotate by -angle_deg.
    Both ``F.Cu`` and ``B.Cu`` footprints use the same transform (no mirroring;
    verified against ``pcbnew`` — see module docstring). ``layer`` is accepted
    for API compatibility and ignored.
    """
    x = local_x_mm
    y = local_y_mm
    rad = math.radians(-angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    return Point(x_mm + x * cos_a - y * sin_a, y_mm + x * sin_a + y * cos_a)

def transform_local(fp: Footprint, local_x_mm: float, local_y_mm: float) -> Point:
    """Map a point in ``fp``'s local coordinate system to board coordinates."""
    return _to_board(local_x_mm, local_y_mm, fp.x_mm, fp.y_mm, fp.angle_deg, fp.layer)

def arc_chords(model: BoardModel, n: int = 16) -> List[Track]:
    """Straight-chord approximations of copper arcs for analysis code.

    Grids, keepouts, pour cuts and resistance walks operate on straight
    segments; arcs reach them as sampled chords (shared endpoints, so
    connectivity and ~length are preserved). Emission and the viewer use
    the true arcs — this helper is analysis-only.
    """
    out: List[Track] = []
    for arc in model.arcs:
        pts = list(sample_circular_arc(
            (arc.start.x_mm, arc.start.y_mm),
            (arc.mid.x_mm, arc.mid.y_mm),
            (arc.end.x_mm, arc.end.y_mm),
            max(1, int(n)),
        ))
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            if x1 == x2 and y1 == y2:
                continue
            out.append(Track(
                start=Point(x1, y1), end=Point(x2, y2),
                width_mm=arc.width_mm, layer=arc.layer,
                net_name=arc.net_name,
            ))
    return out


def copper_tracks(model: BoardModel) -> List[Track]:
    """All copper as straight segments: tracks plus sampled arc chords."""
    return list(model.tracks) + arc_chords(model)


def _point_key(p: Point) -> Tuple[float, float]:
    """Rounded point key for graph adjacency (1e-3 mm tolerance).

    1e-3mm (1 micron) tolerance handles KiCad's floating-point discrepancies
    between gr_line and gr_arc endpoints while staying well below PCB
    manufacturing tolerances.
    """
    tolerance = 1e-3
    return (round(p.x_mm / tolerance) * tolerance, round(p.y_mm / tolerance) * tolerance)

def sample_circular_arc(
    p0: Tuple[float, float],
    p1: Tuple[float, float],
    p2: Tuple[float, float],
    n: int,
):
    """Yield ``n+1`` ``(x, y)`` points along the circular arc through 3 points.

    KiCad ``gr_arc``/``fp_arc`` ``(start mid end)`` are three points ON the
    arc (``mid`` is not a Bezier control point). Treating them as Bezier
    controls flattens the curve (shallow corners). Falls back to the
    straight chord when collinear/degenerate.
    """
    n = max(1, int(n))
    (x0, y0), (x1, y1), (x2, y2) = p0, p1, p2
    d = 2.0 * (x0 * (y1 - y2) + x1 * (y2 - y0) + x2 * (y0 - y1))
    if abs(d) < 1e-12:
        for i in range(n + 1):
            t = i / n
            yield (x0 + (x2 - x0) * t, y0 + (y2 - y0) * t)
        return
    s0 = x0 * x0 + y0 * y0
    s1 = x1 * x1 + y1 * y1
    s2 = x2 * x2 + y2 * y2
    cx = (s0 * (y1 - y2) + s1 * (y2 - y0) + s2 * (y0 - y1)) / d
    cy = (s0 * (x2 - x1) + s1 * (x0 - x2) + s2 * (x1 - x0)) / d
    r = math.hypot(x0 - cx, y0 - cy)
    if r <= 0:
        for i in range(n + 1):
            t = i / n
            yield (x0 + (x2 - x0) * t, y0 + (y2 - y0) * t)
        return
    two_pi = 2.0 * math.pi
    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    a2 = math.atan2(y2 - cy, x2 - cx)
    ccw_01 = (a1 - a0) % two_pi
    ccw_02 = (a2 - a0) % two_pi
    if ccw_01 <= ccw_02:
        sweep = ccw_02  # counter-clockwise from start to end through mid
    else:
        sweep = ccw_02 - two_pi  # clockwise
    for i in range(n + 1):
        a = a0 + sweep * (i / n)
        yield (cx + r * math.cos(a), cy + r * math.sin(a))

def sample_quadratic_bezier(
    p0: Tuple[float, float],
    p1: Tuple[float, float],
    p2: Tuple[float, float],
    n: int,
):
    """Yield ``n+1`` ``(x, y)`` points along the quadratic Bezier ``B(t)``.

    Legacy helper (kept for compatibility); arc code now uses
    :func:`sample_circular_arc` since KiCad stores three points ON the arc.
    """
    n = max(1, n)
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        yield (
            u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
            u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1],
        )

def _approximate_arc_as_segments(arc: EdgeCutArc, num_segments: int = 8) -> List[Segment]:
    """Approximate an EdgeCutArc as line segments along its circular arc.

    The arc's start, mid, end are three points ON the arc (KiCad semantics).
    """
    start = arc.start
    mid = arc.mid
    end = arc.end

    segments = []
    prev = start
    for x, y in sample_circular_arc(
        (start.x_mm, start.y_mm), (mid.x_mm, mid.y_mm), (end.x_mm, end.y_mm),
        num_segments,
    ):
        if (x, y) == (prev.x_mm, prev.y_mm):
            continue  # skip t=0 (== start)
        curr = Point(x, y)
        segments.append((prev, curr))
        prev = curr
    return segments

def _build_edge_graph(segments: List[Segment]) -> Dict[Tuple[float, float], List[Tuple[float, float]]]:
    """Build adjacency graph from segments (endpoints within 1e-6 mm connect)."""
    graph: Dict[Tuple[float, float], List[Tuple[float, float]]] = {}
    for a, b in segments:
        ka, kb = _point_key(a), _point_key(b)
        if ka not in graph:
            graph[ka] = []
        if kb not in graph:
            graph[kb] = []
        if kb not in graph[ka]:
            graph[ka].append(kb)
        if ka not in graph[kb]:
            graph[kb].append(ka)
    return graph

def _find_polygons_from_graph(
    graph: Dict[Tuple[float, float], List[Tuple[float, float]]]
) -> List[List[Point]]:
    """Find closed polygons (cycles) from an edge graph using DFS."""
    polygons = []
    visited_edges = set()

    def dfs(current: Tuple[float, float], start: Tuple[float, float], path: List[Tuple[float, float]]) -> None:
        if len(path) > 2 and current == start:
            # Found a cycle - convert to Points
            poly = [Point(x, y) for x, y in path[:-1]]  # exclude duplicate start
            if len(poly) >= 3:
                polygons.append(poly)
            return

        for neighbor in graph.get(current, []):
            edge = tuple(sorted((current, neighbor)))
            if edge in visited_edges:
                continue
            visited_edges.add(edge)
            dfs(neighbor, start, path + [neighbor])

    for node in graph:
        if any(tuple(sorted((node, n))) not in visited_edges for n in graph[node]):
            dfs(node, node, [node])

    return polygons

def _polygon_area(poly: List[Point]) -> float:
    """Compute signed area of polygon (positive = CCW)."""
    area = 0.0
    n = len(poly)
    for i in range(n):
        j = (i + 1) % n
        area += poly[i].x_mm * poly[j].y_mm - poly[j].x_mm * poly[i].y_mm
    return area / 2.0

def _ensure_ccw(poly: List[Point]) -> List[Point]:
    """Ensure polygon vertices are in CCW order."""
    if _polygon_area(poly) < 0:
        return list(reversed(poly))
    return poly

def _extract_board_polygons(
    edge_cuts: Tuple[Segment, ...],
    edge_arcs: Tuple[EdgeCutArc, ...],
    min_area_mm2: float = 10.0,
) -> List[List[Point]]:
    """Extract closed board outline polygons from Edge.Cuts segments and arcs.

    Returns list of polygons (each = list of Points in CCW order), one per
    disconnected board outline. Filters out holes (polygons contained within others).
    """
    # Collect all segments (edge_cuts + arc approximations)
    all_segments: List[Segment] = list(edge_cuts)
    for arc in edge_arcs:
        all_segments.extend(_approximate_arc_as_segments(arc, num_segments=16))

    # Build graph and find cycles
    graph = _build_edge_graph(all_segments)
    raw_polygons = _find_polygons_from_graph(graph)

    # Filter and orient polygons
    candidates = []
    for poly in raw_polygons:
        area = abs(_polygon_area(poly))
        if area >= min_area_mm2:
            candidates.append(_ensure_ccw(poly))

    # Filter out holes: a polygon that is contained within another is a hole
    # Keep only polygons that are NOT contained within any other polygon
    outer_polygons = []
    for i, poly_i in enumerate(candidates):
        is_hole = False
        for j, poly_j in enumerate(candidates):
            if i == j:
                continue
            # Check if poly_i is contained in poly_j
            # Test a vertex of poly_i against poly_j
            test_point = poly_i[0]
            if _point_in_polygon(test_point, poly_j):
                is_hole = True
                break
        if not is_hole:
            outer_polygons.append(poly_i)

    return outer_polygons

def _point_in_polygon(point: Point, polygon: List[Point]) -> bool:
    """Ray casting point-in-polygon test. Returns True if point is inside polygon."""
    x, y = point.x_mm, point.y_mm
    inside = False
    n = len(polygon)
    for i in range(n):
        j = (i + 1) % n
        xi, yi = polygon[i].x_mm, polygon[i].y_mm
        xj, yj = polygon[j].x_mm, polygon[j].y_mm
        # Check if edge crosses horizontal ray to the right of point
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
    return inside

def _polygon_bbox(polygon: List[Point]) -> Tuple[float, float, float, float]:
    """Compute bounding box of polygon: (min_x, max_x, min_y, max_y)."""
    if not polygon:
        raise ValueError("empty polygon has no bounding box")
    xs = [p.x_mm for p in polygon]
    ys = [p.y_mm for p in polygon]
    return (min(xs), max(xs), min(ys), max(ys))

def _footprint_centroid(fp) -> Point:
    """Compute centroid of footprint's courtyard or pad bounding box."""
    if fp.courtyard:
        # Collect all courtyard vertices
        vertices = []
        for a, b in fp.courtyard:
            vertices.append((a.x_mm, a.y_mm))
            vertices.append((b.x_mm, b.y_mm))
        if vertices:
            xs = [v[0] for v in vertices]
            ys = [v[1] for v in vertices]
            return Point(sum(xs) / len(xs), sum(ys) / len(ys))
    # Fallback: pad bounding box centroid
    if fp.pads:
        xs = [p.position.x_mm for p in fp.pads]
        ys = [p.position.y_mm for p in fp.pads]
        return Point(sum(xs) / len(xs), sum(ys) / len(ys))
    return Point(fp.x_mm, fp.y_mm)

def _assign_board_regions(
    footprints: Tuple,
    board_polygons: List[List[Point]],
) -> Dict[str, int]:
    """Assign each footprint to a board region via point-in-polygon test.

    Returns {fp.uuid: region_idx} for footprints with UUID.
    Footprints not in any region get -1.

    Centroid decides first (unchanged). Fallback: a footprint whose
    centroid falls outside every region but whose courtyard/pads overlap
    one (e.g. a ghost wall straddling the board edge) joins the first
    overlapped region — otherwise edge-straddling walls silently leave
    every region's simulation and exert no force.
    """
    assignment: Dict[str, int] = {}
    for fp in footprints:
        if not fp.uuid:
            continue
        centroid = _footprint_centroid(fp)
        region_idx = -1
        for idx, poly in enumerate(board_polygons):
            if _point_in_polygon(centroid, poly):
                region_idx = idx
                break
        if region_idx < 0:
            outline = _footprint_outline_points(fp)
            for idx, poly in enumerate(board_polygons):
                if any(_point_in_polygon(pt, poly) for pt in outline):
                    region_idx = idx
                    break
        assignment[fp.uuid] = region_idx
    return assignment

def _footprint_outline_points(fp) -> List[Point]:
    """Board-frame sample points of a footprint's extent (courtyard, else pads)."""
    pts: List[Point] = []
    for a, b in fp.courtyard or ():
        pts.append(a)
        pts.append(b)
    if not pts:
        for pad in fp.pads:
            pts.append(pad.position)
    if not pts:
        pts.append(Point(fp.x_mm, fp.y_mm))
    return pts

def _to_local(
    board_x_mm: float,
    board_y_mm: float,
    x_mm: float,
    y_mm: float,
    angle_deg: float,
    layer: str,
) -> Point:
    """Map a board-space point to footprint-local coordinates.

    Exact inverse of :func:`_to_board` (transpose of ``R(-angle)``), so
    ``_to_board(_to_local(p)) == p`` round-trips for every angle.
    No mirroring on either layer (see module docstring); ``layer`` is
    accepted for API compatibility and ignored.

    Note: :func:`placement._compute_footprint_pose_from_pads` inverts
    :func:`_to_board` the same way (``R(+angle)``) before running Kabsch.
    Its extracted angle must be negated (``atan2(R)`` is the *negative*
    footprint angle, because ``_to_board`` applies ``R(-angle)``); using
    the raw angle rotates ``apply_deltas`` opposite to the simulated pose.
    """
    rad = math.radians(-angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    bx = board_x_mm - x_mm
    by = board_y_mm - y_mm
    lx = bx * cos_a + by * sin_a
    ly = -bx * sin_a + by * cos_a
    return Point(lx, ly)

def _to_local_point(p: Point, fp: Footprint) -> Tuple[float, float]:
    """Board point -> footprint-local (x, y), single _to_local() call."""
    local = _to_local(p.x_mm, p.y_mm, fp.x_mm, fp.y_mm, fp.angle_deg, fp.layer)
    return (local.x_mm, local.y_mm)


def label_board_pose(fp: Footprint) -> Optional[Tuple[float, float, float]]:
    """Board-frame (cx_mm, cy_mm, angle_deg) of a footprint's label.

    The anchor rides the footprint-local frame via :func:`_to_board`
    (pads/courtyard convention); the angle is local-plus-parent (pad-angle
    convention — verified: FP@180 stores text 0 with pads at absolute 180).
    None when the footprint carries no label.
    """
    from .labels import label_board_angle

    if fp.label is None:
        return None
    c = _to_board(
        fp.label.local_x_mm, fp.label.local_y_mm,
        fp.x_mm, fp.y_mm, fp.angle_deg, fp.layer,
    )
    return (
        c.x_mm,
        c.y_mm,
        label_board_angle(fp.label.local_angle_deg, fp.angle_deg),
    )

def apply_deltas(
    model: BoardModel, deltas: Dict[str, Tuple[float, float, float]],
    label_snap: bool = True,
    label_allow_inverted: bool = False,
    label_deltas: Optional[Dict[str, Tuple[float, float]]] = None,
) -> BoardModel:
    """Return a new BoardModel with footprint positions updated by deltas.

    Deltas are keyed by footprint UUID as (dx_mm, dy_mm, dangle_deg).
    Only unlocked footprints are moved; locked footprints stay in place
    regardless of delta. Footprints without a delta entry are unchanged.

    Labels ride in the footprint-local frame (``fp_text`` angles are
    footprint-relative — a file with the footprint at 180 stores text
    angle 0 while its pads store absolute 180), so translation and
    rotation need no label rewrite; when the footprint rotates
    (``da != 0``), an optional readable snap counter-rotates the label's
    local angle to the nearest upright board orientation (0/90/270, 180
    only with ``label_allow_inverted``). Pure translations never touch
    text (a zero delta stays the exact no-op), matching ``io``.

    Orbit mode passes ``label_deltas`` (uuid to footprint-local dx/dy
    from the filed anchor): the local anchor shifts before the snap, so
    the final board pose reproduces the integrated orbit exactly. A
    footprint with only a label entry (parent unmoved) still updates.
    """
    if not deltas and not label_deltas:
        return model

    updated_footprints = []
    for fp in model.footprints:
        if fp.locked or not fp.uuid or (
            fp.uuid not in deltas
            and not (label_deltas and fp.uuid in label_deltas)
        ):
            updated_footprints.append(fp)
            continue
        dx, dy, da = deltas.get(fp.uuid, (0.0, 0.0, 0.0))
        new_x = fp.x_mm + dx
        new_y = fp.y_mm + dy
        new_angle = fp.angle_deg + da

        new_pose = replace(fp, x_mm=new_x, y_mm=new_y, angle_deg=new_angle)

        # Recompute pad positions from new footprint pose. Pad angles in
        # the file store the effective board orientation (verified via
        # pcbnew: tube111 Q1 fp 90 has pads file-angle 90 = GetOrientation
        # 90, FPRel 0), so they rotate rigidly with the footprint: the
        # footprint-relative orientation is preserved, the absolute angle
        # gains da. Positions use the local-frame round trip; angles just
        # add (normalized to [0, 360) to match KiCad's serialization).
        new_pads = tuple(
            replace(
                pad,
                position=transform_local(
                    new_pose,
                    # Convert board pad position to local coordinates using old pose
                    *_to_local_point(pad.position, fp),
                ),
                angle_deg=(pad.angle_deg + da) % 360.0 if da != 0.0 else pad.angle_deg,
            )
            for pad in fp.pads
        )

        # Recompute courtyard from new footprint pose.
        new_courtyard = tuple(
            (
                transform_local(new_pose, *_to_local_point(a, fp)),
                transform_local(new_pose, *_to_local_point(b, fp)),
            )
            for a, b in fp.courtyard
        )

        # Labels ride the footprint-local frame: the local anchor is
        # invariant under moves (only the parent pose changes), while the
        # local angle gains the readable snap expressed in the new frame.
        # Orbit mode shifts the anchor itself via label_deltas first.
        new_label = None
        if fp.label is not None:
            lx, ly = fp.label.local_x_mm, fp.label.local_y_mm
            if label_deltas and fp.uuid in label_deltas:
                ldx, ldy = label_deltas[fp.uuid]
                lx, ly = lx + ldx, ly + ldy
            if da != 0.0 and label_snap:
                from .labels import snapped_local_angle

                new_local_angle = snapped_local_angle(
                    fp.label.local_angle_deg,
                    fp.angle_deg,
                    new_angle,
                    snap=True,
                    allow_inverted=label_allow_inverted,
                )
            else:
                new_local_angle = fp.label.local_angle_deg
            if (
                new_local_angle != fp.label.local_angle_deg
                or lx != fp.label.local_x_mm
                or ly != fp.label.local_y_mm
            ):
                new_label = replace(
                    fp.label,
                    local_x_mm=lx,
                    local_y_mm=ly,
                    local_angle_deg=new_local_angle,
                )
            else:
                new_label = fp.label

        updated_footprints.append(
            replace(
                fp,
                x_mm=new_x,
                y_mm=new_y,
                angle_deg=new_angle,
                pads=new_pads,
                courtyard=new_courtyard,
                label=new_label,
            )
        )

    by_ref: Dict[str, Footprint] = {}
    for fp in updated_footprints:
        if fp.ref and fp.ref not in by_ref:
            by_ref[fp.ref] = fp

    return replace(model, footprints=tuple(updated_footprints), by_ref=by_ref)

def commit_placement(
    model: BoardModel,
    deltas: Dict[str, Tuple[float, float, float]],
    protected_nets: Optional[Set[str]] = None,
    label_deltas: Optional[Dict[str, Tuple[float, float]]] = None,
) -> BoardModel:
    """Apply deltas AND rip up tracks/vias on nets of moved footprints.

    Args:
        model: Board model
        deltas: {uuid: (dx, dy, dangle)} footprint movements
        protected_nets: Net names to never rip up (e.g., power rails)
        label_deltas: orbit-mode {uuid: (dx, dy)} footprint-local label
            offsets, forwarded to :func:`apply_deltas`

    Returns:
        New BoardModel with moved footprints and pruned tracks/vias
    """
    if not deltas and not label_deltas:
        return model

    # Collect affected nets from moved footprints' pads
    affected_nets: Set[str] = set()
    for fp in model.footprints:
        if fp.uuid in deltas and not fp.locked:
            dx, dy, da = deltas[fp.uuid]
            if dx != 0.0 or dy != 0.0 or da != 0.0:
                for pad in fp.pads:
                    if pad.net_name:
                        affected_nets.add(pad.net_name)

    if not affected_nets:
        return apply_deltas(model, deltas, label_deltas=label_deltas)

    protected = protected_nets or set()

    # Filter tracks and vias: keep if net not affected OR net is protected
    def keep_copper(net_name: Optional[str]) -> bool:
        if net_name is None:
            return True
        if net_name in protected:
            return True
        return net_name not in affected_nets

    kept_tracks = tuple(t for t in model.tracks if keep_copper(t.net_name))
    kept_arcs = tuple(a for a in model.arcs if keep_copper(a.net_name))
    kept_vias = tuple(v for v in model.vias if keep_copper(v.net_name))

    # Apply footprint movement (preserves all board metadata via replace
    # inside apply_deltas), then prune copper.
    moved_model = apply_deltas(model, deltas, label_deltas=label_deltas)

    # Return model with pruned copper, preserving everything else
    # (ghost_footprints, version, regions, ...).
    return replace(moved_model, tracks=kept_tracks, arcs=kept_arcs, vias=kept_vias)
