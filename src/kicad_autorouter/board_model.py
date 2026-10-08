"""Board-space model extracted from a parsed `.kicad_pcb` S-expression tree.

The autorouter moves components around the board, so it needs each footprint's
position and orientation, its courtyard outline in board coordinates, any keepout
zones, and which pads share nets. This module derives all of that from the pure-
Python :class:`~kicad_autorouter.sexpr.SExpr` tree (no ``pcbnew`` import), so it
runs in the project .venv for testing; ``pcbnew_adapter.py`` remains the
routing-accurate semantic source per the 2026-09-29 decision.

Local footprint coordinates are converted to board space with KiCad's transform
order: a B.Cu footprint is mirrored across its local X axis first, then rotated
by its angle (counter-clockwise positive), then translated by its position —
matching ``pcbnew::TRANSFORM`` for back-side footprints.
"""

from __future__ import annotations

import math
import uuid as uuid_module
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .sexpr import SExpr, get_generator_version


@dataclass(frozen=True)
class Point:
    """A position in board millimeters."""

    x_mm: float
    y_mm: float


Segment = Tuple[Point, Point]


@dataclass(frozen=True)
class Pad:
    """One pad of a footprint; ``position`` is already in board coordinates.

    ``net_name`` is None for unconnected pads (e.g. mounting holes).
    ``size_mm`` is the pad's (width, height); ``shape`` is the KiCad shape
    symbol (circle/rect/...); ``angle_deg`` is the pad's local rotation.
    ``pad_type`` is the KiCad pad type (thru_hole, np_thru_hole, smd).
    ``drill_mm`` is the drill diameter (0 for SMD).
    ``layers`` is the tuple of layer names this pad exists on.
    """

    number: str
    net_name: Optional[str]
    position: Point
    size_mm: Tuple[float, float] = (1.0, 1.0)
    shape: str = "circle"
    angle_deg: float = 0.0
    pad_type: str = "smd"
    drill_mm: float = 0.0
    layers: Tuple[str, ...] = ()

    @property
    def is_through_hole(self) -> bool:
        """True for plated or non-plated through-hole pads (thru_hole / np_thru_hole)."""
        return self.pad_type in ("thru_hole", "np_thru_hole") and self.drill_mm > 0.0

    @property
    def is_plated_through_hole(self) -> bool:
        """True for plated through-hole pads (thru_hole)."""
        return self.pad_type == "thru_hole" and self.drill_mm > 0.0


@dataclass(frozen=True)
class Track:
    """A copper track segment in board millimeters."""

    start: Point
    end: Point
    width_mm: float
    layer: str
    net_name: Optional[str] = None


@dataclass(frozen=True)
class Via:
    """A via in board millimeters; ``size_mm`` is the outer diameter."""

    position: Point
    size_mm: float
    drill_mm: float
    layers: Tuple[str, ...] = ()
    net_name: Optional[str] = None


@dataclass(frozen=True)
class Zone:
    """A copper zone with its outline polygon in board millimeters."""

    uuid: str
    layers: Tuple[str, ...]
    polygon: Tuple[Point, ...]
    net_name: Optional[str] = None


@dataclass(frozen=True)
class EdgeCutArc:
    """An Edge.Cuts arc given by its start, mid and end points."""

    start: Point
    mid: Point
    end: Point


@dataclass(frozen=True)
class Footprint:
    """A placed footprint with its courtyard outline in board coordinates.

    ``locked`` mirrors the native KiCad footprint lock (``fp.IsLocked()``);
    locked footprints are fixed anchors that placement must not move.
    ``uuid`` is the footprint's stable identity — refs may be duplicated or
    missing, but every placed footprint carries its own ``(uuid ...)`` token.
    ``ghost`` marks pseudo-components used for placement forces only; they
    participate in the simulation but are never written back to the PCB.
    ``ghost_attractions`` defines explicit attraction springs from this ghost
    to target footprints by Reference: (target_ref, ideal_length_mm, ka).
    """

    ref: str
    footprint_id: str
    layer: str
    x_mm: float
    y_mm: float
    angle_deg: float
    pads: Tuple[Pad, ...] = ()
    courtyard: Tuple[Segment, ...] = ()
    locked: bool = False
    uuid: str = ""
    ghost: bool = False
    ghost_attractions: Tuple[Tuple[str, float, float], ...] = ()


@dataclass(frozen=True)
class KeepoutZone:
    """A copper zone carrying a ``(keepout ...)`` block.

    The ``*_allowed`` flags mirror the zone's keepout policy; ``polygon`` is the
    zone outline in board millimeters (zone geometry is already absolute).
    """

    uuid: str
    layers: Tuple[str, ...]
    polygon: Tuple[Point, ...]
    tracks_allowed: bool = False
    vias_allowed: bool = False
    pads_allowed: bool = False
    copper_pour_allowed: bool = False
    footprints_allowed: bool = False


@dataclass(frozen=True, order=True)
class NetConnection:
    """One pad participating in a net (ordered by ref, then pad number)."""

    ref: str
    pad_number: str


@dataclass(frozen=True)
class BoardRegion:
    """A single board outline region with its boundary polygon and bounding box."""

    uuid: str
    polygon: Tuple[Point, ...]  # Closed polygon vertices (CCW)
    bbox: Tuple[float, float, float, float]  # (min_x, max_x, min_y, max_y)


@dataclass(frozen=True)
class BoardModel:
    """Aggregate board-space view used by the autorouter core.

    ``by_ref`` maps reference -> footprint with first-occurrence-wins semantics;
    refs are not guaranteed unique in masked fixtures, and empty refs are omitted.
    ``ghost_footprints`` are pseudo-components that participate in placement forces
    but are never written back to the PCB.
    """

    footprints: Tuple[Footprint, ...]
    keepout_zones: Tuple[KeepoutZone, ...]
    nets: Dict[str, Tuple[NetConnection, ...]]
    by_ref: Dict[str, Footprint]
    ghost_footprints: Tuple[Footprint, ...] = ()
    tracks: Tuple[Track, ...] = ()
    vias: Tuple[Via, ...] = ()
    zones: Tuple[Zone, ...] = ()
    edge_cuts: Tuple[Segment, ...] = ()
    edge_arcs: Tuple[EdgeCutArc, ...] = ()
    board_regions: Tuple[BoardRegion, ...] = ()
    footprint_region: Dict[str, int] = field(default_factory=dict)  # uuid -> region_idx
    version: str = ""  # KiCad generator_version (e.g., "10.0", "9.0", "11.0")
    version_warning: Optional[str] = None  # Warning for v11+ boards


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
    CW (negative) rotation in Y-down local coords. Apply B.Cu mirror on X,
    then rotate by -angle_deg.
    """
    x = -local_x_mm if layer == "B.Cu" else local_x_mm
    y = local_y_mm
    rad = math.radians(-angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    return Point(x_mm + x * cos_a - y * sin_a, y_mm + x * sin_a + y * cos_a)


def transform_local(fp: Footprint, local_x_mm: float, local_y_mm: float) -> Point:
    """Map a point in ``fp``'s local coordinate system to board coordinates."""
    return _to_board(local_x_mm, local_y_mm, fp.x_mm, fp.y_mm, fp.angle_deg, fp.layer)


def _point(node: SExpr) -> Point:
    return Point(float(node.args[0]), float(node.args[1]))


def _first_number(node: Optional[SExpr]) -> Optional[float]:
    """Return the first numeric argument of a node, else None."""
    if node is None:
        return None
    for arg in node.args:
        if isinstance(arg, (int, float)):
            return float(arg)
    return None


def _net_name(net_node: Optional[SExpr]) -> Optional[str]:
    """Net name from a ``(net ...)`` node; empty/numeric refs map to None.

    KiCad stores net as (net <index> <name>) where index is int and name is string.
    """
    if net_node is None or not net_node.args:
        return None
    # KiCad format: (net <index> <name>) - use second arg if first is numeric
    if len(net_node.args) >= 2 and isinstance(net_node.args[1], str):
        name = net_node.args[1]
        return name if name else None
    # Fallback: first arg if it's a string
    value = net_node.args[0]
    name = str(value) if isinstance(value, str) else None
    return name if name else None


def _layer_of(node: SExpr) -> str:
    lay = node.find("layer")
    return str(lay.args[0]) if lay is not None and lay.args else ""


def _is_locked(node: SExpr) -> bool:
    """Footprint lock flag from a top-level ``(locked ...)`` token.

    KiCad writes the token only for locked footprints, so its presence alone
    means locked; an explicit argument (e.g. ``yes``/``no``) is honored when
    present. Property-level ``(unlocked ...)`` blocks are nested and ignored.
    """
    locked = node.find("locked")
    if locked is None:
        return False
    for arg in locked.args:
        if isinstance(arg, str):
            return arg == "yes" or arg == "true"
    return True


def _pad(
    node: SExpr, x_mm: float, y_mm: float, angle_deg: float, layer: str
) -> Pad:
    number = str(node.args[0]) if node.args else ""
    pad_type = str(node.args[1]) if len(node.args) > 1 else "smd"
    shape = str(node.args[2]) if len(node.args) > 2 else "circle"
    at = node.find("at")
    px = py = pangle = 0.0
    if at is not None and len(at.args) >= 2:
        px, py = float(at.args[0]), float(at.args[1])
        pangle = float(at.args[2]) if len(at.args) > 2 else 0.0
    size = (1.0, 1.0)
    size_node = node.find("size")
    if size_node is not None and len(size_node.args) >= 2:
        size = (float(size_node.args[0]), float(size_node.args[1]))
    drill = 0.0
    drill_node = node.find("drill")
    if drill_node is not None and drill_node.args:
        # drill can be simple (drill 0.8) or oval (drill oval 4 3.2)
        first_arg = drill_node.args[0]
        if isinstance(first_arg, (int, float)):
            drill = float(first_arg)
        elif isinstance(first_arg, str) and first_arg not in ("oval", "circle"):
            # handle case where first arg is the diameter
            try:
                drill = float(first_arg)
            except ValueError:
                drill = 0.0
        elif isinstance(first_arg, str) and first_arg in ("oval", "circle"):
            # oval/circle drill: args[1] is the major diameter
            if len(drill_node.args) > 1 and isinstance(drill_node.args[1], (int, float)):
                drill = float(drill_node.args[1])
    layers_node = node.find("layers")
    layers = ()
    if layers_node is not None and layers_node.args:
        layers = tuple(
            str(a) for a in layers_node.args if not isinstance(a, SExpr)
        )
    return Pad(
        number=number,
        net_name=_net_name(node.find("net")),
        position=_to_board(px, py, x_mm, y_mm, angle_deg, layer),
        size_mm=size,
        shape=shape,
        angle_deg=pangle,
        pad_type=pad_type,
        drill_mm=drill,
        layers=layers,
    )


def _courtyard_segments(
    fp_node: SExpr, x_mm: float, y_mm: float, angle_deg: float, layer: str
) -> Tuple[Segment, ...]:
    """Collect F.CrtYd/B.CrtYd outline segments in board coordinates."""

    def to_board(lx: float, ly: float) -> Point:
        return _to_board(lx, ly, x_mm, y_mm, angle_deg, layer)

    segments = []
    for line in fp_node.children("fp_line"):
        lay = line.find("layer")
        if lay is None or not lay.args or str(lay.args[0]) not in ("F.CrtYd", "B.CrtYd"):
            continue
        start = _point(line.find("start"))
        end = _point(line.find("end"))
        segments.append((to_board(start.x_mm, start.y_mm), to_board(end.x_mm, end.y_mm)))
    for poly in fp_node.children("fp_poly"):
        lay = poly.find("layer")
        if lay is None or not lay.args or str(lay.args[0]) not in ("F.CrtYd", "B.CrtYd"):
            continue
        pts_node = poly.find("pts")
        pts = [
            to_board(float(a.args[0]), float(a.args[1]))
            for a in pts_node.args
            if isinstance(a, SExpr) and a.head == "xy"
        ]
        for a, b in zip(pts, pts[1:] + pts[:1]):
            segments.append((a, b))
    for circle in fp_node.children("fp_circle"):
        lay = circle.find("layer")
        if lay is None or not lay.args or str(lay.args[0]) not in ("F.CrtYd", "B.CrtYd"):
            continue
        center = circle.find("center")
        end = circle.find("end")
        if center is None or end is None:
            continue
        cx, cy = float(center.args[0]), float(center.args[1])
        ex, ey = float(end.args[0]), float(end.args[1])
        # Convert circle to line segments (approximate with 16 segments)
        import math
        radius = math.hypot(ex - cx, ey - cy)
        for i in range(16):
            a1 = 2 * math.pi * i / 16
            a2 = 2 * math.pi * (i + 1) / 16
            p1 = to_board(cx + radius * math.cos(a1), cy + radius * math.sin(a1))
            p2 = to_board(cx + radius * math.cos(a2), cy + radius * math.sin(a2))
            segments.append((p1, p2))
    return tuple(segments)


def _is_ghost(node: SExpr) -> bool:
    """Check if footprint has ghost=yes property."""
    for prop in node.children("property"):
        if len(prop.args) >= 2 and str(prop.args[0]).lower() == "ghost":
            val = str(prop.args[1]).lower()
            if val in ("yes", "true", "1", "on"):
                return True
    return False


def _parse_ghost_attractions(node: SExpr) -> Tuple[Tuple[str, float, float], ...]:
    """Parse ghost_attract_N properties from a footprint node.
    
    Format: (property "ghost_attract_1" "TARGET_REF,ideal_length_mm,ka")
    Returns tuple of (target_ref, ideal_length_mm, ka).
    """
    attractions = []
    for prop in node.children("property"):
        if len(prop.args) < 2:
            continue
        key = str(prop.args[0]).lower()
        if key.startswith("ghost_attract_"):
            val = str(prop.args[1]).strip()
            if not val:
                continue
            parts = [p.strip() for p in val.split(",")]
            if len(parts) != 3:
                continue
            target_ref, ideal_len_str, ka_str = parts
            try:
                ideal_len = float(ideal_len_str)
                ka = float(ka_str)
                if ideal_len >= 0 and ka >= 0:
                    attractions.append((target_ref, ideal_len, ka))
            except ValueError:
                continue
    return tuple(attractions)


def footprints(tree: SExpr) -> list[Footprint]:
    """Extract every top-level footprint with board-space pads and courtyard."""
    result = []
    for node in tree.children("footprint"):
        at = node.find("at")
        if at is None or len(at.args) < 2:
            continue
        x, y = float(at.args[0]), float(at.args[1])
        angle = float(at.args[2]) if len(at.args) > 2 else 0.0
        layer_node = node.find("layer")
        layer = (
            str(layer_node.args[0])
            if layer_node is not None and layer_node.args
            else "F.Cu"
        )
        ref = ""
        for prop in node.children("property"):
            if len(prop.args) >= 2 and prop.args[0] == "Reference":
                ref = str(prop.args[1])
                break
        fp_id = str(node.args[0]) if node.args else ""
        uuid_node = node.find("uuid")
        uuid = (
            str(uuid_node.args[0])
            if uuid_node is not None and uuid_node.args
            else ""
        )
        pads = tuple(_pad(p, x, y, angle, layer) for p in node.children("pad"))
        result.append(
            Footprint(
                ref=ref,
                footprint_id=fp_id,
                layer=layer,
                x_mm=x,
                y_mm=y,
                angle_deg=angle,
                pads=pads,
                courtyard=_courtyard_segments(node, x, y, angle, layer),
                locked=_is_locked(node),
                uuid=uuid,
                ghost=_is_ghost(node),
                ghost_attractions=_parse_ghost_attractions(node),
            )
        )
    return result


def _zone_polygon(zone_node: SExpr) -> Tuple[Point, ...]:
    """The ``(polygon (pts ...))`` outline of a zone in board millimeters."""
    poly_node = zone_node.find("polygon")
    if poly_node is None:
        return ()
    pts_node = poly_node.find("pts")
    if pts_node is None:
        return ()
    return tuple(
        Point(float(a.args[0]), float(a.args[1]))
        for a in pts_node.args
        if isinstance(a, SExpr) and a.head == "xy"
    )


def zones(tree: SExpr) -> list[Zone]:
    """Extract every copper zone with its outline polygon."""
    result = []
    for zone in tree.children("zone"):
        uuid_node = zone.find("uuid")
        layers_node = zone.find("layers")
        result.append(
            Zone(
                uuid=str(uuid_node.args[0]) if uuid_node is not None and uuid_node.args else "",
                layers=(
                    tuple(str(a) for a in layers_node.args if not isinstance(a, SExpr))
                    if layers_node is not None
                    else ()
                ),
                polygon=_zone_polygon(zone),
                net_name=_net_name(zone.find("net")),
            )
        )
    return result


def keepout_zones(tree: SExpr) -> list[KeepoutZone]:
    """Extract zones carrying a ``(keepout ...)`` block."""
    zones = []
    for zone in tree.children("zone"):
        ko = zone.find("keepout")
        if ko is None:
            continue

        def allowed(name: str) -> bool:
            child = ko.find(name)
            return (
                child is not None
                and len(child.args) >= 1
                and str(child.args[0]) == "allowed"
            )

        layers_node = zone.find("layers")
        layers = (
            tuple(str(a) for a in layers_node.args if not isinstance(a, SExpr))
            if layers_node is not None
            else ()
        )
        uuid_node = zone.find("uuid")
        zones.append(
            KeepoutZone(
                uuid=str(uuid_node.args[0])
                if uuid_node is not None and uuid_node.args
                else "",
                layers=layers,
                polygon=_zone_polygon(zone),
                tracks_allowed=allowed("tracks"),
                vias_allowed=allowed("vias"),
                pads_allowed=allowed("pads"),
                copper_pour_allowed=allowed("copperpour"),
                footprints_allowed=allowed("footprints"),
            )
        )
    return zones


def tracks(tree: SExpr) -> list[Track]:
    """Extract copper track segments (``(segment ...)``) in board coordinates."""
    result = []
    for seg in tree.children("segment"):
        start = seg.find("start")
        end = seg.find("end")
        if start is None or end is None:
            continue
        width = _first_number(seg.find("width"))
        result.append(
            Track(
                start=Point(float(start.args[0]), float(start.args[1])),
                end=Point(float(end.args[0]), float(end.args[1])),
                width_mm=width if width is not None else 0.25,
                layer=_layer_of(seg),
                net_name=_net_name(seg.find("net")),
            )
        )
    return result


def vias(tree: SExpr) -> list[Via]:
    """Extract vias in board coordinates."""
    result = []
    for via in tree.children("via"):
        at = via.find("at")
        if at is None or len(at.args) < 2:
            continue
        layers_node = via.find("layers")
        size = _first_number(via.find("size"))
        drill = _first_number(via.find("drill"))
        result.append(
            Via(
                position=Point(float(at.args[0]), float(at.args[1])),
                size_mm=size if size is not None else 0.8,
                drill_mm=drill if drill is not None else 0.4,
                layers=(
                    tuple(str(a) for a in layers_node.args if not isinstance(a, SExpr))
                    if layers_node is not None
                    else ()
                ),
                net_name=_net_name(via.find("net")),
            )
        )
    return result


def edge_cuts(tree: SExpr) -> Tuple[Segment, ...]:
    """Board outline segments from Edge.Cuts ``gr_line``/``gr_rect`` graphics."""
    segments = []
    for line in tree.children("gr_line"):
        if _layer_of(line) != "Edge.Cuts":
            continue
        start = line.find("start")
        end = line.find("end")
        if start is None or end is None:
            continue
        segments.append(
            (Point(float(start.args[0]), float(start.args[1])),
             Point(float(end.args[0]), float(end.args[1])))
        )
    for rect in tree.children("gr_rect"):
        if _layer_of(rect) != "Edge.Cuts":
            continue
        start = rect.find("start")
        end = rect.find("end")
        if start is None or end is None:
            continue
        sx, sy = float(start.args[0]), float(start.args[1])
        ex, ey = float(end.args[0]), float(end.args[1])
        corners = (Point(sx, sy), Point(ex, sy), Point(ex, ey), Point(sx, ey))
        for a, b in zip(corners, corners[1:] + corners[:1]):
            segments.append((a, b))
    return tuple(segments)


def edge_arcs(tree: SExpr) -> Tuple[EdgeCutArc, ...]:
    """Board outline arcs from Edge.Cuts ``gr_arc`` graphics."""
    arcs = []
    for arc in tree.children("gr_arc"):
        if _layer_of(arc) != "Edge.Cuts":
            continue
        start = arc.find("start")
        mid = arc.find("mid")
        end = arc.find("end")
        if start is None or mid is None or end is None:
            continue
        arcs.append(
            EdgeCutArc(
                start=Point(float(start.args[0]), float(start.args[1])),
                mid=Point(float(mid.args[0]), float(mid.args[1])),
                end=Point(float(end.args[0]), float(end.args[1])),
            )
        )
    return tuple(arcs)


def _point_key(p: Point) -> Tuple[float, float]:
    """Rounded point key for graph adjacency (1e-6 mm tolerance)."""
    return (round(p.x_mm, 6), round(p.y_mm, 6))


def _approximate_arc_as_segments(arc: EdgeCutArc, num_segments: int = 8) -> List[Segment]:
    """Approximate an EdgeCutArc as line segments using quadratic Bezier.

    Uses the arc's start, mid, end as control points for a quadratic Bezier curve.
    """
    import math
    start = arc.start
    mid = arc.mid
    end = arc.end

    segments = []
    prev = start
    for i in range(1, num_segments + 1):
        t = i / num_segments
        # Quadratic Bezier: B(t) = (1-t)²*P0 + 2(1-t)t*P1 + t²*P2
        u = 1 - t
        x = u * u * start.x_mm + 2 * u * t * mid.x_mm + t * t * end.x_mm
        y = u * u * start.y_mm + 2 * u * t * mid.y_mm + t * t * end.y_mm
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
        assignment[fp.uuid] = region_idx
    return assignment


def netlist(tree: SExpr) -> Dict[str, Tuple[NetConnection, ...]]:
    """Map net name -> its pad connections, from pad-level ``(net ...)`` refs.

    KiCad 10 files carry no top-level net declarations (nets exist only on pads);
    older files may declare nets without any pad reference — those are ignored here.
    """
    nets: Dict[str, list[NetConnection]] = {}
    for fp in footprints(tree):
        for pad in fp.pads:
            if pad.net_name is None:
                continue
            nets.setdefault(pad.net_name, []).append(NetConnection(fp.ref, pad.number))
    return {name: tuple(sorted(conns)) for name, conns in sorted(nets.items())}


def _to_local(
    board_x_mm: float,
    board_y_mm: float,
    x_mm: float,
    y_mm: float,
    angle_deg: float,
    layer: str,
) -> Point:
    """Map a board-space point to footprint-local coordinates.
    
    Inverse of _to_board().
    """
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    bx = board_x_mm - x_mm
    by = board_y_mm - y_mm
    if layer == "B.Cu":
        # B.Cu was mirrored across X before rotation
        lx = bx * cos_a + by * sin_a
        ly = -bx * sin_a + by * cos_a
        lx = -lx
    else:
        lx = bx * cos_a + by * sin_a
        ly = -bx * sin_a + by * cos_a
    return Point(lx, ly)


def apply_deltas(
    model: BoardModel, deltas: Dict[str, Tuple[float, float, float]]
) -> BoardModel:
    """Return a new BoardModel with footprint positions updated by deltas.

    Deltas are keyed by footprint UUID as (dx_mm, dy_mm, dangle_deg).
    Only unlocked footprints are moved; locked footprints stay in place
    regardless of delta. Footprints without a delta entry are unchanged.
    """
    if not deltas:
        return model

    updated_footprints = []
    for fp in model.footprints:
        if fp.locked or not fp.uuid or fp.uuid not in deltas:
            updated_footprints.append(fp)
            continue
        dx, dy, da = deltas[fp.uuid]
        new_x = fp.x_mm + dx
        new_y = fp.y_mm + dy
        new_angle = fp.angle_deg + da

        # Recompute pad positions from new footprint pose
        new_pads = tuple(
            Pad(
                number=pad.number,
                net_name=pad.net_name,
                position=transform_local(
                    Footprint(
                        ref=fp.ref,
                        footprint_id=fp.footprint_id,
                        layer=fp.layer,
                        x_mm=new_x,
                        y_mm=new_y,
                        angle_deg=new_angle,
                        pads=(),
                        courtyard=(),
                        locked=fp.locked,
                        uuid=fp.uuid,
                    ),
                    # Convert board pad position to local coordinates using old pose
                    _to_local(pad.position.x_mm, pad.position.y_mm, fp.x_mm, fp.y_mm, fp.angle_deg, fp.layer).x_mm,
                    _to_local(pad.position.x_mm, pad.position.y_mm, fp.x_mm, fp.y_mm, fp.angle_deg, fp.layer).y_mm,
                ),
                size_mm=pad.size_mm,
                shape=pad.shape,
                angle_deg=pad.angle_deg,
            )
            for pad in fp.pads
        )

        # Recompute courtyard from new footprint pose
        new_courtyard = tuple(
            (
                transform_local(
                    Footprint(
                        ref=fp.ref,
                        footprint_id=fp.footprint_id,
                        layer=fp.layer,
                        x_mm=new_x,
                        y_mm=new_y,
                        angle_deg=new_angle,
                        pads=(),
                        courtyard=(),
                        locked=fp.locked,
                        uuid=fp.uuid,
                    ),
                    _to_local(a.x_mm, a.y_mm, fp.x_mm, fp.y_mm, fp.angle_deg, fp.layer).x_mm,
                    _to_local(a.x_mm, a.y_mm, fp.x_mm, fp.y_mm, fp.angle_deg, fp.layer).y_mm,
                ),
                transform_local(
                    Footprint(
                        ref=fp.ref,
                        footprint_id=fp.footprint_id,
                        layer=fp.layer,
                        x_mm=new_x,
                        y_mm=new_y,
                        angle_deg=new_angle,
                        pads=(),
                        courtyard=(),
                        locked=fp.locked,
                        uuid=fp.uuid,
                    ),
                    _to_local(b.x_mm, b.y_mm, fp.x_mm, fp.y_mm, fp.angle_deg, fp.layer).x_mm,
                    _to_local(b.x_mm, b.y_mm, fp.x_mm, fp.y_mm, fp.angle_deg, fp.layer).y_mm,
                ),
            )
            for a, b in fp.courtyard
        )

        updated_footprints.append(
            Footprint(
                ref=fp.ref,
                footprint_id=fp.footprint_id,
                layer=fp.layer,
                x_mm=new_x,
                y_mm=new_y,
                angle_deg=new_angle,
                pads=new_pads,
                courtyard=new_courtyard,
                locked=fp.locked,
                uuid=fp.uuid,
            )
        )

    by_ref: Dict[str, Footprint] = {}
    for fp in updated_footprints:
        if fp.ref and fp.ref not in by_ref:
            by_ref[fp.ref] = fp

    return BoardModel(
        footprints=tuple(updated_footprints),
        keepout_zones=model.keepout_zones,
        nets=model.nets,
        by_ref=by_ref,
        tracks=model.tracks,
        vias=model.vias,
        zones=model.zones,
        edge_cuts=model.edge_cuts,
        edge_arcs=model.edge_arcs,
    )


def commit_placement(
    model: BoardModel,
    deltas: Dict[str, Tuple[float, float, float]],
    protected_nets: Optional[Set[str]] = None,
) -> BoardModel:
    """Apply deltas AND rip up tracks/vias on nets of moved footprints.

    Args:
        model: Board model
        deltas: {uuid: (dx, dy, dangle)} footprint movements
        protected_nets: Net names to never rip up (e.g., power rails)

    Returns:
        New BoardModel with moved footprints and pruned tracks/vias
    """
    if not deltas:
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
        return apply_deltas(model, deltas)

    protected = protected_nets or set()

    # Filter tracks and vias: keep if net not affected OR net is protected
    def keep_copper(net_name: Optional[str]) -> bool:
        if net_name is None:
            return True
        if net_name in protected:
            return True
        return net_name not in affected_nets

    kept_tracks = tuple(t for t in model.tracks if keep_copper(t.net_name))
    kept_vias = tuple(v for v in model.vias if keep_copper(v.net_name))

    # Apply footprint movement
    moved_model = apply_deltas(model, deltas)

    # Return model with pruned copper
    return BoardModel(
        footprints=moved_model.footprints,
        keepout_zones=moved_model.keepout_zones,
        nets=moved_model.nets,
        by_ref=moved_model.by_ref,
        tracks=kept_tracks,
        vias=kept_vias,
        zones=moved_model.zones,
        edge_cuts=moved_model.edge_cuts,
        edge_arcs=moved_model.edge_arcs,
        board_regions=moved_model.board_regions,
        footprint_region=moved_model.footprint_region,
    )


def _parse_version(version_str: Optional[str]) -> tuple[str, Optional[str]]:
    """Parse generator_version string and return (version, warning).
    
    Version logic:
    - v8.x, v9.x: OK, no warning
    - v10.x: OK, no warning (current target)
    - v11.x+: Warning - compatibility not verified
    - Unknown/missing: Empty version, no warning
    """
    if not version_str:
        return "", None
    
    try:
        major = int(version_str.split(".")[0])
    except (ValueError, IndexError):
        return version_str, None
    
    if major >= 11:
        return version_str, (
            f"KiCad {major}+ format detected (generator_version={version_str}); "
            "compatibility not verified. Loading may produce unexpected results."
        )
    return version_str, None


def board_model(tree: SExpr) -> BoardModel:
    """Build the aggregate :class:`BoardModel` from a parsed board tree."""
    all_fps = footprints(tree)
    real_fps = [fp for fp in all_fps if not fp.ghost]
    ghost_fps = [fp for fp in all_fps if fp.ghost]

    by_ref: Dict[str, Footprint] = {}
    for fp in real_fps:  # first occurrence wins, matching io.find_footprint
        if fp.ref and fp.ref not in by_ref:
            by_ref[fp.ref] = fp

    # Extract board outline polygons from Edge.Cuts
    edge_cuts_list = edge_cuts(tree)
    edge_arcs_list = edge_arcs(tree)
    board_polygons = _extract_board_polygons(edge_cuts_list, edge_arcs_list)

    # Create BoardRegion objects
    board_regions = tuple(
        BoardRegion(
            uuid=uuid_module.uuid4().hex,
            polygon=tuple(poly),
            bbox=_polygon_bbox(poly),
        )
        for poly in board_polygons
    )

    # Assign footprints to regions (real + ghost)
    all_fps_for_region = real_fps + ghost_fps
    footprint_region = _assign_board_regions(all_fps_for_region, board_polygons)

    # Parse version and warning
    gen_version = get_generator_version(tree)
    version, version_warning = _parse_version(gen_version)

    return BoardModel(
        footprints=tuple(real_fps),
        ghost_footprints=tuple(ghost_fps),
        keepout_zones=tuple(keepout_zones(tree)),
        nets=netlist(tree),
        by_ref=by_ref,
        tracks=tuple(tracks(tree)),
        vias=tuple(vias(tree)),
        zones=tuple(zones(tree)),
        edge_cuts=edge_cuts_list,
        edge_arcs=edge_arcs_list,
        board_regions=board_regions,
        footprint_region=footprint_region,
        version=version,
        version_warning=version_warning,
    )
