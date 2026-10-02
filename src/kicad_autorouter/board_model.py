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
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from .sexpr import SExpr


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
    """

    number: str
    net_name: Optional[str]
    position: Point
    size_mm: Tuple[float, float] = (1.0, 1.0)
    shape: str = "circle"
    angle_deg: float = 0.0


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
class BoardModel:
    """Aggregate board-space view used by the autorouter core.

    ``by_ref`` maps reference -> footprint with first-occurrence-wins semantics;
    refs are not guaranteed unique in masked fixtures, and empty refs are omitted.
    """

    footprints: Tuple[Footprint, ...]
    keepout_zones: Tuple[KeepoutZone, ...]
    nets: Dict[str, Tuple[NetConnection, ...]]
    by_ref: Dict[str, Footprint]
    tracks: Tuple[Track, ...] = ()
    vias: Tuple[Via, ...] = ()
    zones: Tuple[Zone, ...] = ()
    edge_cuts: Tuple[Segment, ...] = ()
    edge_arcs: Tuple[EdgeCutArc, ...] = ()


def _to_board(
    local_x_mm: float,
    local_y_mm: float,
    x_mm: float,
    y_mm: float,
    angle_deg: float,
    layer: str,
) -> Point:
    """Map a footprint-local point to board coordinates.

    B.Cu footprints are mirrored across the local X axis before rotation,
    matching ``pcbnew::TRANSFORM`` for back-side items.
    """
    x = -local_x_mm if layer == "B.Cu" else local_x_mm
    y = local_y_mm
    rad = math.radians(angle_deg)
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
    """Net name from a ``(net ...)`` node; empty/numeric refs map to None."""
    if net_node is None or not net_node.args:
        return None
    value = net_node.args[0]
    name = str(value) if isinstance(value, str) else None
    return name or None


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
    return Pad(
        number=number,
        net_name=_net_name(node.find("net")),
        position=_to_board(px, py, x_mm, y_mm, angle_deg, layer),
        size_mm=size,
        shape=shape,
        angle_deg=pangle,
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
    return tuple(segments)


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
        updated_footprints.append(
            Footprint(
                ref=fp.ref,
                footprint_id=fp.footprint_id,
                layer=fp.layer,
                x_mm=fp.x_mm + dx,
                y_mm=fp.y_mm + dy,
                angle_deg=fp.angle_deg + da,
                pads=fp.pads,
                courtyard=fp.courtyard,
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


def board_model(tree: SExpr) -> BoardModel:
    """Build the aggregate :class:`BoardModel` from a parsed board tree."""
    fps = footprints(tree)
    by_ref: Dict[str, Footprint] = {}
    for fp in fps:  # first occurrence wins, matching io.find_footprint
        if fp.ref and fp.ref not in by_ref:
            by_ref[fp.ref] = fp
    return BoardModel(
        footprints=tuple(fps),
        keepout_zones=tuple(keepout_zones(tree)),
        nets=netlist(tree),
        by_ref=by_ref,
        tracks=tuple(tracks(tree)),
        vias=tuple(vias(tree)),
        zones=tuple(zones(tree)),
        edge_cuts=edge_cuts(tree),
        edge_arcs=edge_arcs(tree),
    )
