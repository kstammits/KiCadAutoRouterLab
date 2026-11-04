"""Board data: dataclasses, net predicates, width policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Collection, Dict, List, Optional, Set, Tuple

@dataclass(frozen=True)
class Point:
    """A position in board millimeters."""

    x_mm: float
    y_mm: float

Segment = Tuple[Point, Point]

_POWER_KEYWORDS = (
    "vcc", "vdd", "vin", "v+", "v-", "pwr", "power",
    "+5v", "-5v", "+12v", "-12v", "3v3",
)

_GROUND_KEYWORDS = ("gnd", "ground", "vgnd")

def is_power_net(net_name: Optional[str]) -> bool:
    """True for power-rail nets (VCC, VDD, V+/V-, +12V, 3V3, …). Pure name test."""
    if not net_name:
        return False
    lowered = net_name.lower()
    return any(kw in lowered for kw in _POWER_KEYWORDS)

def is_ground_net(net_name: Optional[str]) -> bool:
    """True for ground nets (GND, GROUND, VGND). Pure name test."""
    if not net_name:
        return False
    lowered = net_name.lower()
    return any(kw in lowered for kw in _GROUND_KEYWORDS)

def is_power_net_effective(
    net_name: Optional[str], extra_power: Collection[str] = ()
) -> bool:
    """True when a net counts as a power rail: keyword match OR user-marked.

    ``extra_power`` is the user-owned power-membership set (toggled in the
    app's nets table, seeded from power-named KiCad netclasses on load).
    Ground stays keyword-only; on overlap power wins (e.g. "GND_PWR").
    """
    if net_name and net_name in extra_power:
        return True
    return is_power_net(net_name)

def seed_power_nets_from_netclasses(model: "BoardModel") -> Set[str]:
    """Net names sitting in power-keyword-named KiCad netclasses.

    Load-time seed for the user-owned power set: a class literally called
    "Power" (or matching the power keywords) marks its member nets. Only
    nets present on the board are returned (stale names pruned). Keyword
    power nets need no seeding — they classify automatically.
    """
    out: Set[str] = set()
    for cls in model.net_classes:
        if cls.name and is_power_net(cls.name):
            out.update(n for n in cls.nets if n in model.nets)
    return out

def resolve_track_width_mm(
    net_name: Optional[str],
    *,
    net_widths: Optional[Dict[str, float]] = None,
    net_netclass: Optional[Dict[str, str]] = None,
    net_classes: Tuple["NetClass", ...] = (),
    default_mm: float = 0.25,
    power_mm: float = 0.5,
    extra_power: Collection[str] = (),
) -> float:
    """Canonical emitted trace width for a net (single source of truth).

    Precedence: explicit per-net widths → board ``(net_class trace_width)``
    → effective-power (keyword OR user-marked) → default. Plain-argument
    shape so both ``routing/pipeline.py`` and ``routing/output.py`` can
    share it without an import cycle (params types live in pipeline).
    """
    if net_name and net_widths and net_name in net_widths:
        try:
            if float(net_widths[net_name]) > 0:
                return float(net_widths[net_name])
        except (TypeError, ValueError):
            pass
    if net_name and net_netclass and net_name in net_netclass:
        class_name = net_netclass[net_name]
        for cls in net_classes:
            if cls.name == class_name and cls.trace_width_mm > 0:
                return cls.trace_width_mm
    if is_power_net_effective(net_name, extra_power):
        return max(default_mm, power_mm)
    return default_mm

@dataclass(frozen=True)
class Pad:
    """One pad of a footprint; ``position`` is already in board coordinates.

    ``net_name`` is None for unconnected pads (e.g. mounting holes).
    ``size_mm`` is the pad's (width, height); ``shape`` is the KiCad shape
    symbol (circle/rect/...); ``angle_deg`` is the pad's effective board
    orientation in degrees (absolute, as stored in the file's ``(at ...)``;
    verified via pcbnew: tube111 Q1 fp 90 stores pads at 90 = GetOrientation
    90, FPRel 0 — i.e. NOT footprint-relative). It rotates rigidly with the
    footprint (see ``apply_deltas``).
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
class ArcTrack:
    """A copper track arc in board millimeters (KiCad ``(arc)`` node).

    ``start``/``mid``/``end`` are three points ON the arc (KiCad semantics,
    not Bezier controls). Single circular arcs are constant-curvature;
    freeform curvature is expressed as tangent-joined chains of these.
    """

    start: Point
    mid: Point
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
class FilledPolygon:
    """One cached KiCad zone fill island in board millimeters.

    Mirrors a ``(filled_polygon (layer ...) (pts ...))`` block inside a
    ``(zone ...)``. This is KiCad's own fill output (thermal spokes, pullback,
    island removal applied) and is used for visualization only: it goes stale
    the moment copper moves, and KiCad regenerates it on ``--refill-zones``.
    """

    layer: str
    polygon: Tuple[Point, ...]

@dataclass(frozen=True)
class Zone:
    """A copper zone with its outline polygon in board millimeters.

    Fill rules mirror the ``(zone …)`` block so pour analysis can apply
    KiCad's own semantics: ``clearance_mm`` (``connect_pads/clearance``)
    is the pour's pullback from different-net copper, ``min_thickness_mm``
    drops sub-width slivers, ``thermal_gap_mm``/``thermal_bridge_width_mm``
    describe pad spokes (continuity treats pads as solid connections;
    spokes matter only for current capacity), ``island_removal_mode``
    mirrors KiCad's remove-islands setting (reported, not enforced —
    KiCad owns removal on refill), ``priority`` orders overlapping zones.
    ``(filled_polygon …)`` caches are also parsed (``filled_polygons``) for
    visualization of KiCad's own fill (thermal spokes included). They are
    stale the moment copper moves — KiCad regenerates them on refill —
    so routing/DRC must not treat them as ground truth.
    Missing blocks (e.g. keepout zones) yield documented defaults.
    """

    uuid: str
    layers: Tuple[str, ...]
    polygon: Tuple[Point, ...]
    net_name: Optional[str] = None
    clearance_mm: float = 0.5
    min_thickness_mm: float = 0.25
    thermal_gap_mm: Optional[float] = None
    thermal_bridge_width_mm: Optional[float] = None
    island_removal_mode: int = 0
    priority: int = 0
    filled_polygons: Tuple[FilledPolygon, ...] = ()

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
    to target footprints by Reference: (target_ref, ka).
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
    ghost_attractions: Tuple[Tuple[str, float], ...] = ()

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
class NetClass:
    """A KiCad net class: clearance + trace width with member net names."""

    name: str
    clearance_mm: float = 0.2
    trace_width_mm: float = 0.25
    nets: Tuple[str, ...] = ()

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
    arcs: Tuple[ArcTrack, ...] = ()
    vias: Tuple[Via, ...] = ()
    zones: Tuple[Zone, ...] = ()
    edge_cuts: Tuple[Segment, ...] = ()
    edge_arcs: Tuple[EdgeCutArc, ...] = ()
    board_regions: Tuple[BoardRegion, ...] = ()
    footprint_region: Dict[str, int] = field(default_factory=dict)  # uuid -> region_idx
    version: str = ""  # KiCad generator_version (e.g., "10.0", "9.0", "11.0")
    version_warning: Optional[str] = None  # Warning for v11+ boards
    net_classes: Tuple[NetClass, ...] = ()  # (net_class ...) declarations
    net_netclass: Dict[str, str] = field(default_factory=dict)  # net name -> netclass name
    copper_layers: Tuple[str, ...] = ("F.Cu", "B.Cu")  # stackup-ordered copper layers
