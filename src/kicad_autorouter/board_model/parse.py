"""Board parsing: S-expression tree -> BoardModel."""

from __future__ import annotations

import math
import uuid as uuid_module
from typing import Collection, Dict, List, Optional, Set, Tuple

from ..sexpr import SExpr, get_generator_version
from .model import ArcTrack, BoardModel, BoardRegion, EdgeCutArc, FilledPolygon, Footprint, KeepoutZone, NetClass, NetConnection, Pad, Point, Segment, SilkLabel, Track, Via, Zone
from .query import _assign_board_regions, _extract_board_polygons, _polygon_bbox, _to_board, sample_circular_arc

def _point(node: SExpr) -> Point:
    """First two numeric args as Point; caller guarantees well-formed input."""
    return Point(float(node.args[0]), float(node.args[1]))

def _point_or_none(node: Optional[SExpr]) -> Optional[Point]:
    """Two numeric args -> Point, else None (malformed nodes are skipped)."""
    if node is None or len(node.args) < 2:
        return None
    try:
        return Point(float(node.args[0]), float(node.args[1]))
    except (TypeError, ValueError):
        return None

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
    """Child ``(layer …)`` name, or empty string when absent."""
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
    """Pad node -> Pad with board-space position; missing fields fall back to smd/circle/1mm."""
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

def _graphic_segments(node: SExpr, to_board) -> list:
    """Outline segments of one ``fp_line``/``fp_rect``/``fp_poly``/``fp_circle``/``fp_arc`` node.

    Local coordinates → board via ``to_board``.     Circles become 16 chords;
    arcs are sampled as circular arcs through start/mid/end (KiCad stores
    three points ON the arc). Unknown/malformed nodes yield no segments.
    """
    import math

    segs = []
    try:
        if node.head == "fp_line":
            start, end = _point(node.find("start")), _point(node.find("end"))
            segs.append((to_board(start.x_mm, start.y_mm), to_board(end.x_mm, end.y_mm)))
        elif node.head == "fp_rect":
            start, end = _point(node.find("start")), _point(node.find("end"))
            corners = [
                (start.x_mm, start.y_mm), (end.x_mm, start.y_mm),
                (end.x_mm, end.y_mm), (start.x_mm, end.y_mm),
            ]
            pts = [to_board(px, py) for px, py in corners]
            segs.extend(zip(pts, pts[1:] + pts[:1]))
        elif node.head == "fp_poly":
            pts_node = node.find("pts")
            if pts_node is None:
                return segs
            pts = [
                to_board(float(a.args[0]), float(a.args[1]))
                for a in pts_node.args
                if isinstance(a, SExpr) and a.head == "xy"
            ]
            segs.extend(zip(pts, pts[1:] + pts[:1]))
        elif node.head == "fp_circle":
            center, end = node.find("center"), node.find("end")
            if center is None or end is None:
                return segs
            cx, cy = float(center.args[0]), float(center.args[1])
            ex, ey = float(end.args[0]), float(end.args[1])
            radius = math.hypot(ex - cx, ey - cy)
            for i in range(16):
                a1 = 2 * math.pi * i / 16
                a2 = 2 * math.pi * (i + 1) / 16
                p1 = to_board(cx + radius * math.cos(a1), cy + radius * math.sin(a1))
                p2 = to_board(cx + radius * math.cos(a2), cy + radius * math.sin(a2))
                segs.append((p1, p2))
        elif node.head == "fp_arc":
            start, mid, end = node.find("start"), node.find("mid"), node.find("end")
            if start is None or mid is None or end is None:
                return segs
            s = (float(start.args[0]), float(start.args[1]))
            m = (float(mid.args[0]), float(mid.args[1]))
            e = (float(end.args[0]), float(end.args[1]))
            pts = [
                to_board(px, py)
                for px, py in sample_circular_arc(s, m, e, 16)
            ]
            segs.extend(zip(pts, pts[1:]))
    except (TypeError, ValueError, IndexError, AttributeError):
        return []
    return segs

_COURTYARD_LAYERS = ("F.CrtYd", "B.CrtYd")

_FAB_LAYERS = ("F.Fab", "B.Fab")

_SILK_LAYERS = ("F.SilkS", "B.SilkS")

_GRAPHIC_KINDS = ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc")

def _courtyard_segments(
    fp_node: SExpr, x_mm: float, y_mm: float, angle_deg: float, layer: str
) -> Tuple[Segment, ...]:
    """Collect footprint outline segments in board coordinates.

    Preferred source is the ``F.CrtYd``/``B.CrtYd`` courtyard; footprints
    without one (e.g. ``europi:Thonkiconn`` jacks, which carry only
    ``F.SilkS`` + pads) fall back to ``F.Fab``/``B.Fab``, then
    ``F.SilkS``/``B.SilkS``, then a pad-bounding-box rectangle — so every
    multi-pad part gets a drawn body outline for the SVG and a real
    collision polygon for placement instead of an empty courtyard.
    """

    def to_board(lx: float, ly: float) -> Point:
        return _to_board(lx, ly, x_mm, y_mm, angle_deg, layer)

    buckets: dict = {"crt": [], "fab": [], "silk": []}
    for kind in _GRAPHIC_KINDS:
        for node in fp_node.children(kind):
            lay = node.find("layer")
            layer_name = str(lay.args[0]) if lay is not None and lay.args else ""
            if layer_name in _COURTYARD_LAYERS:
                buckets["crt"].extend(_graphic_segments(node, to_board))
            elif layer_name in _FAB_LAYERS:
                buckets["fab"].extend(_graphic_segments(node, to_board))
            elif layer_name in _SILK_LAYERS:
                buckets["silk"].extend(_graphic_segments(node, to_board))
    for key in ("crt", "fab", "silk"):
        if buckets[key]:
            return tuple(buckets[key])

    # Last resort: pad bounding box + 0.25mm courtyard clearance.
    pad_boxes = []
    for pad_node in fp_node.children("pad"):
        at = pad_node.find("at")
        px = float(at.args[0]) if at is not None and len(at.args) >= 1 else 0.0
        py = float(at.args[1]) if at is not None and len(at.args) >= 2 else 0.0
        size_node = pad_node.find("size")
        if size_node is not None and len(size_node.args) >= 2:
            try:
                sx, sy = float(size_node.args[0]), float(size_node.args[1])
            except (TypeError, ValueError):
                sx, sy = 1.0, 1.0
        else:
            sx, sy = 1.0, 1.0
        pad_boxes.append((px - sx / 2, py - sy / 2, px + sx / 2, py + sy / 2))
    if not pad_boxes:
        return ()
    m = 0.25
    x0 = min(b[0] for b in pad_boxes) - m
    y0 = min(b[1] for b in pad_boxes) - m
    x1 = max(b[2] for b in pad_boxes) + m
    y1 = max(b[3] for b in pad_boxes) + m
    corners = [to_board(x0, y0), to_board(x1, y0), to_board(x1, y1), to_board(x0, y1)]
    return tuple(zip(corners, corners[1:] + corners[:1]))

def _is_ghost(node: SExpr) -> bool:
    """Check if footprint has ghost=yes property."""
    for prop in node.children("property"):
        if len(prop.args) >= 2 and str(prop.args[0]).lower() == "ghost":
            val = str(prop.args[1]).lower()
            if val in ("yes", "true", "1", "on"):
                return True
    return False

def _parse_ghost_attractions(node: SExpr) -> Tuple[Tuple[str, float], ...]:
    """Parse ghost_attract_N properties from a footprint node.
    
    Format: (property "ghost_attract_1" "TARGET_REF,ka")
    Returns tuple of (target_ref, ka).
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
            if len(parts) != 2:
                continue
            target_ref, ka_str = parts
            try:
                ka = float(ka_str)
                if ka >= 0:
                    attractions.append((target_ref, ka))
            except ValueError:
                continue
    return tuple(attractions)

def _parse_label(node: SExpr, fp_layer: str, fp_ref: str) -> Optional[SilkLabel]:
    """Pick the single visible label of a footprint on its own side.

    Like an SMD pad, a label belongs to one side: ``F.Cu`` footprints read
    ``F.SilkS`` (falling back to ``F.Fab`` reference text — tube111 keeps
    ``${REFERENCE}`` there with graphics-only silk), ``B.Cu`` footprints
    read the ``B.*`` pair. Ranking is silk-over-fab, reference-text first;
    ties keep file order. ``${REFERENCE}`` resolves to the footprint ref.
    Returns None when the footprint carries no same-side text.
    """
    side = "F" if fp_layer.startswith("F") else ("B" if fp_layer.startswith("B") else "")
    silk_layer = f"{side}.SilkS" if side else ""
    fab_layer = f"{side}.Fab" if side else ""

    best = None
    best_rank = None
    for ft in node.children("fp_text"):
        if len(ft.args) < 2:
            continue
        raw_text = ft.args[1] if isinstance(ft.args[1], str) else str(ft.args[1])
        layer = _layer_of(ft)
        if side and layer not in (silk_layer, fab_layer):
            continue
        at = ft.find("at")
        if at is None or len(at.args) < 2:
            continue
        try:
            lx = float(at.args[0])
            ly = float(at.args[1])
            la = float(at.args[2]) if len(at.args) > 2 else 0.0
        except (TypeError, ValueError):
            continue
        is_ref = raw_text in ("${REFERENCE}", "${REF}") or (fp_ref and raw_text == fp_ref)
        rank = (0 if layer == silk_layer else 1, 0 if is_ref else 1)
        if best_rank is not None and rank >= best_rank:
            continue
        text = fp_ref if raw_text in ("${REFERENCE}", "${REF}") else raw_text
        size = (1.0, 1.0)
        thickness = 0.15
        effects = ft.find("effects")
        font = effects.find("font") if effects is not None else None
        if font is not None:
            size_node = font.find("size")
            if size_node is not None and len(size_node.args) >= 2:
                try:
                    size = (float(size_node.args[0]), float(size_node.args[1]))
                except (TypeError, ValueError):
                    pass
            thick_node = font.find("thickness")
            if thick_node is not None and thick_node.args:
                try:
                    thickness = float(thick_node.args[0])
                except (TypeError, ValueError):
                    pass
        uuid_node = ft.find("uuid")
        ft_uuid = str(uuid_node.args[0]) if uuid_node is not None and uuid_node.args else ""
        best_rank = rank
        best = SilkLabel(
            text=text,
            local_x_mm=lx,
            local_y_mm=ly,
            local_angle_deg=la,
            layer=layer,
            size_mm=size,
            thickness_mm=thickness,
            uuid=ft_uuid,
        )
    return best

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
                label=_parse_label(node, layer, ref),
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

def _zone_fill(zone_node: SExpr, head: str) -> Optional[float]:
    """First numeric arg of ``(head …)`` inside a zone's ``(fill …)`` block."""
    fill = zone_node.find("fill")
    if fill is None:
        return None
    return _first_number(fill.find(head))

def _zone_layers(zone_node: SExpr) -> Tuple[str, ...]:
    """Layer names of a zone: plural ``(layers …)`` or singular ``(layer …)``.

    KiCad writes single-layer zones (e.g. tube111's GND pours) with the
    singular form; the old parser read only the plural and silently
    dropped every such zone's layers (which in turn disabled ground
    stitching's same-layer pairing downstream).
    """
    layers_node = zone_node.find("layers")
    if layers_node is not None:
        return tuple(
            str(a) for a in layers_node.args if not isinstance(a, SExpr)
        )
    layer_node = zone_node.find("layer")
    if layer_node is not None and layer_node.args:
        return (str(layer_node.args[0]),)
    return ()

def _filled_polygons(zone_node: SExpr) -> Tuple[FilledPolygon, ...]:
    """Cached KiCad fills of a zone (stale-after-edit, visualization only)."""
    out = []
    for fp in zone_node.children("filled_polygon"):
        layer_node = fp.find("layer")
        layer = str(layer_node.args[0]) if layer_node is not None and layer_node.args else ""
        pts_node = fp.find("pts")
        if pts_node is None:
            continue
        pts = tuple(
            Point(float(a.args[0]), float(a.args[1]))
            for a in pts_node.args
            if isinstance(a, SExpr) and a.head == "xy" and len(a.args) >= 2
        )
        if len(pts) < 3:
            continue
        out.append(FilledPolygon(layer=layer, polygon=pts))
    return tuple(out)

def zones(tree: SExpr) -> list[Zone]:
    """Extract every copper zone with its outline polygon and fill rules."""
    result = []
    for zone in tree.children("zone"):
        uuid_node = zone.find("uuid")
        connect = zone.find("connect_pads")
        clearance = _first_number(
            connect.find("clearance") if connect is not None else None
        )
        min_thickness = _first_number(zone.find("min_thickness"))
        result.append(
            Zone(
                uuid=str(uuid_node.args[0]) if uuid_node is not None and uuid_node.args else "",
                layers=_zone_layers(zone),
                polygon=_zone_polygon(zone),
                net_name=_net_name(zone.find("net")),
                clearance_mm=clearance if clearance is not None else 0.5,
                min_thickness_mm=min_thickness if min_thickness is not None else 0.25,
                thermal_gap_mm=_zone_fill(zone, "thermal_gap"),
                thermal_bridge_width_mm=_zone_fill(zone, "thermal_bridge_width"),
                island_removal_mode=int(
                    _zone_fill(zone, "island_removal_mode") or 0
                ),
                priority=int(_first_number(zone.find("priority")) or 0),
                filled_polygons=_filled_polygons(zone),
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
        uuid_node = zone.find("uuid")
        zones.append(
            KeepoutZone(
                uuid=str(uuid_node.args[0])
                if uuid_node is not None and uuid_node.args
                else "",
                layers=_zone_layers(zone),
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
        start = _point_or_none(seg.find("start"))
        end = _point_or_none(seg.find("end"))
        if start is None or end is None:
            continue
        width = _first_number(seg.find("width"))
        result.append(
            Track(
                start=start,
                end=end,
                width_mm=width if width is not None else 0.25,
                layer=_layer_of(seg),
                net_name=_net_name(seg.find("net")),
            )
        )
    return result

def arcs(tree: SExpr) -> list[ArcTrack]:
    """Extract copper track arcs (``(arc ...)``) in board coordinates.

    KiCad ``(arc start mid end)`` are three points ON the arc (not Bezier
    controls). Nodes missing any endpoint are skipped, same as segments.
    """
    result = []
    for arc in tree.children("arc"):
        start = _point_or_none(arc.find("start"))
        mid = _point_or_none(arc.find("mid"))
        end = _point_or_none(arc.find("end"))
        if start is None or mid is None or end is None:
            continue
        width = _first_number(arc.find("width"))
        result.append(
            ArcTrack(
                start=start,
                mid=mid,
                end=end,
                width_mm=width if width is not None else 0.25,
                layer=_layer_of(arc),
                net_name=_net_name(arc.find("net")),
            )
        )
    return result

def vias(tree: SExpr) -> list[Via]:
    """Extract vias in board coordinates."""
    result = []
    for via in tree.children("via"):
        at = _point_or_none(via.find("at"))
        if at is None:
            continue
        layers_node = via.find("layers")
        size = _first_number(via.find("size"))
        drill = _first_number(via.find("drill"))
        result.append(
            Via(
                position=at,
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
        start = _point_or_none(line.find("start"))
        end = _point_or_none(line.find("end"))
        if start is None or end is None:
            continue
        segments.append((start, end))
    for rect in tree.children("gr_rect"):
        if _layer_of(rect) != "Edge.Cuts":
            continue
        start = _point_or_none(rect.find("start"))
        end = _point_or_none(rect.find("end"))
        if start is None or end is None:
            continue
        sx, sy = start.x_mm, start.y_mm
        ex, ey = end.x_mm, end.y_mm
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
        start = _point_or_none(arc.find("start"))
        mid = _point_or_none(arc.find("mid"))
        end = _point_or_none(arc.find("end"))
        if start is None or mid is None or end is None:
            continue
        arcs.append(EdgeCutArc(start=start, mid=mid, end=end))
    return tuple(arcs)

def netclasses(tree: SExpr) -> Tuple[Tuple[NetClass, ...], Dict[str, str]]:
    """Parse ``(net_class ...)`` declarations: (classes, net->class map).

    KiCad form: ``(net_class "Name" (clearance mm) (trace_width mm)
    (via_dia ..) (via_drill ..) ... (add_net "NET") ...)``. Missing numeric
    fields take KiCad-ish defaults; boards without netclasses (e.g. our
    tube111 fixture) yield empty results.
    """
    classes: List[NetClass] = []
    net_to_class: Dict[str, str] = {}
    for node in tree.children("net_class"):
        name = str(node.args[0]) if node.args else ""
        clearance_mm = 0.2
        trace_width_mm = 0.25
        clear_node = node.find("clearance")
        if clear_node is not None and clear_node.args:
            try:
                clearance_mm = float(clear_node.args[0])
            except (TypeError, ValueError):
                pass
        width_node = node.find("trace_width")
        if width_node is not None and width_node.args:
            try:
                trace_width_mm = float(width_node.args[0])
            except (TypeError, ValueError):
                pass
        members: List[str] = []
        for add in node.children("add_net"):
            if add.args and isinstance(add.args[0], str) and add.args[0]:
                members.append(add.args[0])
                net_to_class.setdefault(add.args[0], name or "Default")
        classes.append(NetClass(
            name=name or "Default",
            clearance_mm=clearance_mm,
            trace_width_mm=trace_width_mm,
            nets=tuple(members),
        ))
    return tuple(classes), net_to_class

def netclass_clearance(
    model: BoardModel, net_name: Optional[str], default_mm: float = 0.2
) -> float:
    """Clearance for a net from the board's netclass declarations."""
    if net_name:
        class_name = model.net_netclass.get(net_name)
        if class_name:
            for cls in model.net_classes:
                if cls.name == class_name and cls.clearance_mm > 0:
                    return cls.clearance_mm
    return default_mm

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

def _is_copper_layer_name(name: str) -> bool:
    """True for copper layer names (``F.Cu`` / ``B.Cu`` / ``InN.Cu``)."""
    return isinstance(name, str) and name.endswith(".Cu")

def _sort_copper_layers(names: Collection[str]) -> Tuple[str, ...]:
    """Order copper names in stackup order: F.Cu, InN.Cu (numeric), B.Cu."""
    def _key(name: str) -> Tuple[int, int, str]:
        if name == "F.Cu":
            return (0, 0, name)
        if name == "B.Cu":
            return (2, 0, name)
        if name.startswith("In") and name.endswith(".Cu"):
            try:
                return (1, int(name[2:-3]), name)
            except ValueError:
                return (1, 1 << 30, name)
        return (1, 1 << 30, name)

    seen: List[str] = []
    for name in names:
        if name not in seen:
            seen.append(name)
    return tuple(sorted(seen, key=_key))

def copper_layers(tree: SExpr) -> Tuple[str, ...]:
    """Stackup-ordered copper layer names declared by the board file.

    Prefers ``(setup (stackup (layer "NAME" (type "copper")) …))`` order
    (the physical stackup); falls back to the ``(layers …)`` header. Only
    names ending in ``.Cu`` are kept. Boards without either block (or
    without copper entries) default to ``("F.Cu", "B.Cu")`` so old callers
    and tests keep working.
    """
    stacked: List[str] = []
    setup = tree.find("setup")
    stackup = setup.find("stackup") if setup is not None else None
    if stackup is not None:
        for layer in stackup.children("layer"):
            if not layer.args or not isinstance(layer.args[0], str):
                continue
            name = layer.args[0]
            if not _is_copper_layer_name(name):
                continue
            type_node = layer.find("type")
            if type_node is not None and type_node.args:
                if str(type_node.args[0]).lower() != "copper":
                    continue
            stacked.append(name)
    if stacked:
        return _sort_copper_layers(stacked)
    header: List[str] = []
    layers_node = tree.find("layers")
    if layers_node is not None:
        for child in layers_node.args:
            if not isinstance(child, SExpr):
                continue
            for arg in child.args:
                if _is_copper_layer_name(arg):
                    header.append(arg)
                    break
    if header:
        return _sort_copper_layers(header)
    return ("F.Cu", "B.Cu")

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

    # Net class declarations (may be absent — callers fall back to params)
    parsed_net_classes, parsed_net_netclass = netclasses(tree)

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
        arcs=tuple(arcs(tree)),
        vias=tuple(vias(tree)),
        zones=tuple(zones(tree)),
        edge_cuts=edge_cuts_list,
        edge_arcs=edge_arcs_list,
        board_regions=board_regions,
        footprint_region=footprint_region,
        version=version,
        version_warning=version_warning,
        net_classes=parsed_net_classes,
        net_netclass=parsed_net_netclass,
        copper_layers=copper_layers(tree),
    )
