"""Build occupancy/cost grid from board model."""

from __future__ import annotations

import math
from typing import List, Tuple

import numpy as np

from ..board_model import (
    ArcTrack,
    BoardModel,
    Footprint,
    Pad,
    Track,
    Via,
    sample_circular_arc,
)
from ._numba import njit
from .grid import RoutingGrid, board_to_grid, mm_to_cells


# Cell cost values (int8)
FREE = 0
COURTYARD = 25     # placement guide only — not a copper keepout
POUR = 35          # copper pour interior: passable at a toll (see CostMap.pour_penalty)
BLOCKED = 100      # impassable (tracks, vias, drilled holes)
HIGH_COST = 50     # near pads, clearance zones
EDGE_KEEPOUT = 80  # near board edge

# Per-net fanout halo values (live on the SECONDARY cost grid, added to the
# entry cost at search time — never baked into cell values, so they cannot
# push cells over BLOCKED or trap terminals). Soft shoulder: strong inner
# ring, weaker outer ring.
FANOUT_HALO_INNER = 500  # within pad extent + clearance + half track width
FANOUT_HALO_OUTER = 100  # one further clearance-width ring beyond inner

# Hard exclusion ring around FOREIGN through-hole pads (same inner radius as
# the halo, but impassable: router.SECONDARY_BLOCKED). A THT pad is tall
# copper + hole + leads with nonzero track width and manufacturing
# tolerance, so foreign copper must keep clear rather than merely pay a
# toll. The routed net's own THT pads are excluded (see build_fanout_cost),
# so its terminals still connect and hop for free.
THT_EXCLUSION = 10000  # >= router.SECONDARY_BLOCKED, fits int16

# Steep-but-passable toll for the clearance margin around foreign pads.
# The hard core covers copper + the routed track's half-width + raster
# guard; the clearance band beyond the track edge pays this instead of
# blocking, so A* may squeeze through a tight thread when no alternative
# exists. Must stay below router.SECONDARY_BLOCKED (1000) — enforced by test.
FANOUT_HALO_STEEP = 800


def build_occupancy_grid(
    model,
    grid,
    net_class_clearance: dict = None,
    default_clearance_mm: float = 0.2,
    skip_copper: bool = False,
) -> np.ndarray:
    """Build occupancy cost grid from board model.

    Returns (n_layers, H, W) int8 array with cost values.
    Higher = less desirable for routing.

    ``skip_copper`` omits sections 3/3b/4 (tracks, arcs, vias) so the
    incremental cache can reuse the static part and stamp copper itself;
    the composite is identical (every stamp takes the max, so stamp order
    is irrelevant).
    """
    n_layers = len(grid.layers)
    H, W = grid.height_cells, grid.width_cells
    cost_grid = np.full((n_layers, H, W), FREE, dtype=np.int16)

    # Default clearance in cells
    default_clearance_cells = int(np.ceil(default_clearance_mm / grid.resolution_mm))
    if default_clearance_cells < 1:
        default_clearance_cells = 1

    # Net class clearance map (net_name -> clearance_cells)
    clearance_cells = {}
    if net_class_clearance:
        for net_name, cls in net_class_clearance.items():
            cl_mm = getattr(cls, "clearance", default_clearance_mm)
            clearance_cells[net_name] = int(np.ceil(cl_mm / grid.resolution_mm))

    # 1. Footprint courtyards -> blocked on both layers
    for fp in model.footprints:
        _block_courtyard(cost_grid, grid, fp)

    # 2. Pad centers -> high cost with clearance margin
    # Also mark through-hole pads as blocked on both layers
    # Use conservative track half-width (0.125mm = 0.25mm/2) for pad halos
    conservative_track_half_mm = 0.125
    for fp in model.footprints:
        for pad in fp.pads:
            _mark_pad(cost_grid, grid, fp, pad, clearance_cells, default_clearance_cells, conservative_track_half_mm)

    if not skip_copper:
        # 3. Existing tracks -> blocked on their layer with width + clearance
        for track in model.tracks:
            clr = clearance_cells.get(track.net_name, default_clearance_cells) if track.net_name else default_clearance_cells
            clearance_mm = clr * grid.resolution_mm
            _mark_track(cost_grid, grid, track, clearance_mm)

        # 3b. Existing track arcs -> sampled polyline, same width+clearance halo.
        for arc in model.arcs:
            clr = clearance_cells.get(arc.net_name, default_clearance_cells) if arc.net_name else default_clearance_cells
            clearance_mm = clr * grid.resolution_mm
            _mark_arc(cost_grid, grid, arc, clearance_mm)

        # 4. Vias -> blocked on both layers with full copper+clearance halo
        for via in model.vias:
            clr = clearance_cells.get(via.net_name, default_clearance_cells) if via.net_name else default_clearance_cells
            clearance_mm = clr * grid.resolution_mm
            # Use default track width for the via's net (0.25mm default, or power width if net is power)
            track_width_mm = 0.25  # default signal width
            if via.net_name:
                # We don't have net class info here, but track half-width is used for halo
                # The actual track width will be resolved later during routing
                pass
            _mark_via(cost_grid, grid, via, clearance_mm=clearance_mm, track_half_mm=track_width_mm / 2.0)

    # 5. Zones (copper pours) -> blocked on their layer
    for zone in model.zones:
        _mark_zone(cost_grid, grid, zone)

    # 6. Edge cuts -> edge keepout zone (using actual Edge.Cuts lines)
    _mark_edge_keepout(cost_grid, grid, model, keepout_mm=0.5)

    # 6b. Hard outside-board block: cells outside every board polygon are
    # impassable, so copper can never hug the outline from the outside
    # (e.g. tube111 C2TubeOut ran left/below the region-2 edge at +40/cell
    # because EDGE_KEEPOUT is only a toll). The 2 mm grid margin stays for
    # halo math, but those cells are now BLOCKED.
    _mark_outside_board_blocked(cost_grid, grid, model)

    # 7. Keepout zones -> blocked on their layers (unless tracks allowed)
    for kz in getattr(model, "keepout_zones", ()):
        _mark_keepout(cost_grid, grid, kz)

    return cost_grid


def build_fanout_cost(
    model,
    grid,
    current_net: str,
    clearance_mm: float = 0.2,
    track_half_mm: float = 0.125,
    extra_margin_mm: float = 0.0,
) -> np.ndarray:
    """Build the per-net secondary (fanout) cost grid for ``current_net``.

    Returns a ``(n_layers, H, W)`` int16 grid of graded halos over
    every *other* net's pads: ``THT_EXCLUSION`` on copper plus the routed
    track half-width plus a half-diagonal raster guard (cell-center stamps
    miss boundary cells), ``FANOUT_HALO_STEEP`` over the clearance + half-track margin,
    ``FANOUT_HALO_OUTER`` for one further clearance-width ring plus
    ``extra_margin_mm`` of steering push. The current net's own pads are
    excluded so its terminals can still escape. All other cells are 0.

    ``extra_margin_mm`` is steering-only (never DRC): it widens the soft
    outer shoulder without touching the hard inner ring, so dense escapes
    (e.g. between DIP pins) stay routable while the search prefers a wider
    berth around foreign copper.

    Soft halos are added to the entry cost (never to blockage), so they
    steer around foreign copper without ever trapping a route. The
    clearance margin pays a steep toll (``FANOUT_HALO_STEEP``, passable
    below ``router.SECONDARY_BLOCKED``) instead of blocking, so A* may
    squeeze through a tight thread when no alternative exists. Only
    copper plus the routed track half-width (plus the raster guard)
    carries ``THT_EXCLUSION`` (impassable): foreign tracks can never
    touch another net's copper, but they may graze its
    clearance halo at high cost (DRC remains the acceptance check).
    Own-net exclusion still applies, so the owner connects and hops
    for free.

    Geometry follows the copper: ovals stamp oriented capsules (spine
    along the major axis per ``pad.angle_deg``), rect-family pads stamp
    the halo-inflated rect as a polygon (bounding-box exact), round pads
    degenerate to discs — long slots no longer cast spherical shadows
    sized by their length, and square pins no longer balloon into
    capsules.
    """
    n_layers = len(grid.layers)
    H, W = grid.height_cells, grid.width_cells
    secondary = np.zeros((n_layers, H, W), dtype=np.int16)
    res_mm = grid.resolution_mm
    if res_mm <= 0:
        return secondary

    outer_extra = pad_halo_radius_cells(0.0, clearance_mm, 0.0, res_mm)
    if extra_margin_mm > 0:
        outer_extra += mm_to_cells(extra_margin_mm, res_mm, 0)
    outer_extra_mm = outer_extra * res_mm
    for fp in model.footprints:
        for pad in fp.pads:
            if pad.net_name == current_net:
                continue
            if pad.is_through_hole:
                layers = tuple(range(n_layers))
            else:
                idx = grid.layer_to_idx.get(fp.layer)
                layers = (idx,) if idx is not None else ()
            # Oriented pad stamps (capsules for ovals, inflated rects for
            # the rect family, discs for round/unknown pads). Three tiers
            # (max semantics, so stamp order is irrelevant): hard core on
            # copper + track half-width + guard (copper touch impossible),
            # steep-but-passable toll over the clearance + track-half
            # margin, soft shoulder beyond that.
            margin_mm = clearance_mm + track_half_mm
            outer_mm = margin_mm + outer_extra_mm
            for layer_idx in layers:
                if layer_idx >= n_layers:
                    continue
                layer = secondary[layer_idx]
                _stamp_pad_zone(
                    layer, grid, pad, outer_mm, FANOUT_HALO_OUTER
                )
                _stamp_pad_zone(
                    layer, grid, pad, margin_mm, FANOUT_HALO_STEEP
                )
                # Hard core: actual copper plus the routed track's half
                # width plus a half-diagonal raster guard. Center sampling
                # can miss boundary cells by up to ~0.07 mm, and a track
                # whose centerline threads such a cell still overlaps
                # copper by up to its half-width (tube111: 0.5 mm power
                # tracks grazing foreign SMD pads after the toll-road
                # search paid the steep halo toll). The steep tier above
                # keeps the clearance band soft; this tier makes copper
                # touch impossible at any track width.
                _stamp_pad_zone(
                    layer, grid, pad,
                    track_half_mm + res_mm * math.sqrt(2) / 2.0,
                    THT_EXCLUSION,
                )

    # Foreign vias: hard exclusion ring (like foreign THT pads)
    for via in model.vias:
        if not via.net_name or via.net_name == current_net:
            continue
        col, row = board_to_grid(grid, via.position.x_mm, via.position.y_mm)
        # A via only excludes the grid layers it pierces; inner-only blind
        # vias never halo the outer-only grid. Unspanned vias (legacy)
        # exclude every grid layer.
        if getattr(via, "layers", ()):
            layers = tuple(
                grid.layer_to_idx[name]
                for name in via.layers
                if name in grid.layer_to_idx
            )
        else:
            layers = tuple(range(n_layers))
        inner = via_halo_radius_cells(
            via.size_mm, clearance_mm, track_half_mm, res_mm
        )
        for layer_idx in layers:
            if layer_idx >= n_layers:
                continue
            # Hard exclusion: foreign copper must keep clear of the via annulus + clearance + track half-width
            _mark_circular_zone(secondary[layer_idx], row, col, inner, THT_EXCLUSION)
    return secondary


def own_net_hard_cells(
    model,
    grid,
    net_name: str,
    clearance_mm: float,
    track_half_mm: float,
) -> np.ndarray:
    """Boolean ``(n_layers, H, W)`` hard-exclusion cells of one net's own pads/vias.

    Stamps exactly the hard tier :func:`build_fanout_cost` would stamp for
    these pads/vias (copper plus routed track half-width plus the
    half-diagonal raster guard; via annulus halo) — i.e. precisely the
    cells the per-net secondary *carves out* for ``current_net=net_name``.
    Used to reconstruct the un-carved base mask (``base = net_mask |
    own_cells``) so the emit-gate STRtree index can be shared across nets
    instead of rebuilt per net. Millisecond-scale: only one net's pads.
    """
    n_layers = len(grid.layers)
    H, W = grid.height_cells, grid.width_cells
    res_mm = grid.resolution_mm
    scratch = np.zeros((n_layers, H, W), dtype=np.int16)
    if res_mm <= 0:
        return scratch >= THT_EXCLUSION
    guard_mm = track_half_mm + res_mm * math.sqrt(2) / 2.0
    for fp in model.footprints:
        for pad in fp.pads:
            if pad.net_name != net_name:
                continue
            if pad.is_through_hole:
                layers = tuple(range(n_layers))
            else:
                idx = grid.layer_to_idx.get(fp.layer)
                layers = (idx,) if idx is not None else ()
            for layer_idx in layers:
                if layer_idx >= n_layers:
                    continue
                _stamp_pad_zone(
                    scratch[layer_idx], grid, pad, guard_mm, THT_EXCLUSION
                )
    for via in model.vias:
        if not via.net_name or via.net_name != net_name:
            continue
        col, row = board_to_grid(grid, via.position.x_mm, via.position.y_mm)
        if getattr(via, "layers", ()):
            layers = tuple(
                grid.layer_to_idx[name]
                for name in via.layers
                if name in grid.layer_to_idx
            )
        else:
            layers = tuple(range(n_layers))
        inner = via_halo_radius_cells(
            via.size_mm, clearance_mm, track_half_mm, res_mm
        )
        for layer_idx in layers:
            if layer_idx >= n_layers:
                continue
            _mark_circular_zone(scratch[layer_idx], row, col, inner, THT_EXCLUSION)
    return scratch >= THT_EXCLUSION


def tht_drill_cells(pad, grid) -> set:
    """Grid ``{(col, row)}`` cells covered by a THT pad's drill hole.

    Shared by the free-hop mask (pipeline) and the via-block carve-out
    below, so both builders agree on what "the hole" is.
    """
    H, W = grid.height_cells, grid.width_cells
    col, row = board_to_grid(grid, pad.position.x_mm, pad.position.y_mm)
    radius = drill_radius_cells(pad.drill_mm, grid.resolution_mm)
    return {(c, r) for (r, c) in disc_cells(row, col, radius, H, W)}


def build_via_block_mask(
    model,
    grid,
    clearance_mm: float = 0.2,
    track_half_mm: float = 0.125,
) -> np.ndarray:
    """2D boolean mask where vias are forbidden (SMD pad solder/fanout areas).

    Returns ``(H, W)`` with True at cells covered by a non-through-hole
    pad's copper plus its fanout halo (pad extent + clearance + half track
    width — the same inner radius as :func:`build_fanout_cost`). This stops
    the router from diving through a far-side SMD pad with a last-minute
    via inside the solder area: the escape track must leave the pad on its
    own layer and only transition outside the fanout zone.

    Through-hole pads are excluded — they stay legal (free) transition
    sites via the THT mask, which wins ties in the router. Neighboring
    SMD halos that spill over a THT drill disc are carved back out below,
    so the two builders never disagree on hole cells.
    """
    H, W = grid.height_cells, grid.width_cells
    res_mm = grid.resolution_mm
    if res_mm <= 0:
        return np.zeros((H, W), dtype=bool)

    mask = np.zeros((H, W), dtype=bool)
    rows, cols, radii = [], [], []
    carve_rows, carve_cols, carve_radii = [], [], []
    for fp in model.footprints:
        for pad in fp.pads:
            col, row = board_to_grid(grid, pad.position.x_mm, pad.position.y_mm)
            if pad.is_through_hole:
                carve_rows.append(row)
                carve_cols.append(col)
                carve_radii.append(drill_radius_cells(pad.drill_mm, res_mm))
            else:
                rows.append(row)
                cols.append(col)
                radii.append(
                    pad_halo_radius_cells(
                        _pad_half_extent_mm(pad), clearance_mm,
                        track_half_mm, res_mm,
                    )
                )
    stamp_discs(mask, rows, cols, radii, True)

    # Carve drill holes back out: a pre-drilled hole is a legal hop site
    # even when an adjacent SMD halo overlaps it (THT wins ties).
    stamp_discs(mask, carve_rows, carve_cols, carve_radii, False)
    return mask


def _build_tht_via_mask(model, grid) -> np.ndarray:
    """Build a 2D boolean mask of free layer-transition sites at THT holes.

    Each through-hole pad contributes a disc of drill radius (the copper-
    free hole the barrel already provides), not just its center cell, so
    the free hop is hittable at fine grids too. The surrounding copper
    annulus stays protected by the per-net exclusion ring in the secondary
    grid (foreign nets) — this mask only zeroes the transition price.

    Returns (H, W) boolean array where True = free transition site.
    """
    H, W = grid.height_cells, grid.width_cells
    res_mm = grid.resolution_mm
    mask = np.zeros((H, W), dtype=bool)
    rows, cols, radii = [], [], []
    for fp in model.footprints:
        for pad in fp.pads:
            if pad.is_through_hole:
                col, row = board_to_grid(
                    grid, pad.position.x_mm, pad.position.y_mm
                )
                rows.append(row)
                cols.append(col)
                radii.append(drill_radius_cells(pad.drill_mm, res_mm))
    stamp_discs(mask, rows, cols, radii, True)
    return mask


def _block_courtyard(cost_grid: np.ndarray, grid, fp):
    """Mark footprint courtyard outline as COURTYARD on both layers.
    Courtyards are placement guides, not copper keepouts: tracks route
    through them freely. COURTYARD (25) is below SOFT_THRESHOLD (50) so the
    smoother can shortcut through courtyard outlines, and below HIGH_COST
    so the A* router gives pad clearance priority over courtyard avoidance.
    """
    if not fp.courtyard:
        return
    for layer_idx in range(cost_grid.shape[0]):
        for a, b in fp.courtyard:
            _draw_line_blocked(cost_grid[layer_idx], grid, a.x_mm, a.y_mm, b.x_mm, b.y_mm,
                               value=COURTYARD)


def _pad_half_extent_mm(pad: Pad) -> float:
    """Half-extent of a pad covering its copper in every direction.

    Round pads (``circle``, ``oval`` with equal w/h): circular radius
    ``max/2`` is exact. Rectangular pads (``rect`` pin 1 on DIP sockets,
    ``roundrect``/``chamfered``/``trapezoid``/``custom``): the corner sticks
    out to the half-diagonal ``hypot(w,h)/2``, so a circular halo must use
    that radius or diagonals clip the corner. Non-rect shapes keep the
    tighter circular radius (oval capsules are already conservatively
    covered along their length by the ``max/2`` disc).
    """
    try:
        w, h = float(pad.size_mm[0]), float(pad.size_mm[1])
    except (TypeError, ValueError, IndexError):
        return 0.0
    shape = (getattr(pad, "shape", "") or "").lower()
    if shape in ("rect", "roundrect", "chamfered", "trapezoid", "custom"):
        return math.hypot(w, h) / 2.0
    return max(w, h) / 2.0


def pad_halo_radius_cells(
    pad_half_extent_mm: float,
    clearance_mm: float,
    track_half_mm: float,
    res_mm: float,
) -> int:
    """Halo radius in cells covering pad copper + clearance + track half-width.

    Single spelling of the ``max(1, ceil(...))`` formula used at pad-stamping
    call sites.
    """
    return mm_to_cells(
        pad_half_extent_mm + clearance_mm + track_half_mm, res_mm)


def drill_radius_cells(drill_mm: float, res_mm: float) -> int:
    """Drill-hole radius in cells (the copper-free free-hop disc)."""
    if drill_mm <= 0:
        return 1
    return mm_to_cells(drill_mm / 2.0, res_mm)


def via_halo_radius_cells(
    via_size_mm: float,
    clearance_mm: float,
    track_half_mm: float,
    res_mm: float,
) -> int:
    """Halo radius in cells covering via copper + clearance + track half-width.

    A drilled via is an annulus of ``via_size_mm`` OD plus manufacturing
    tolerance; foreign copper (prior tracks/vias, exclusion rings) must keep
    ``clearance_mm + track_half_mm`` beyond the annulus edge. Same
    ``max(1, ceil(...))`` spelling as :func:`pad_halo_radius_cells`.
    """
    return mm_to_cells(
        via_size_mm / 2.0 + clearance_mm + track_half_mm, res_mm)


def track_corridor_radius_cells(
    width_mm: float,
    clearance_mm: float,
    res_mm: float,
) -> int:
    """Corridor radius in cells reserving emitted track + clearance halo.

    Covers the full track width plus the clearance to neighboring copper:
    another track's centerline must stay outside this radius, and its own
    half-width then leaves at least ``clearance_mm`` edge-to-edge. Reserving
    only half-width + clearance short-changes two-track separation by half
    a track width.
    """
    if res_mm <= 0:
        return 1
    half_width = max(1, int(math.ceil(width_mm / res_mm / 2.0)))
    clearance_radius = mm_to_cells(clearance_mm + width_mm, res_mm, 0)
    return max(max(0, half_width - 1), clearance_radius)


def disc_cells(row: int, col: int, radius: int, H: int, W: int):
    """Yield bound-clipped ``(r, c)`` of the filled disc (same scan as the
    vectorized ``_mark_circular_zone``: ``dr²+dc² ≤ r²``)."""
    r2 = radius * radius
    for r in range(max(0, row - radius), min(H, row + radius + 1)):
        dr = r - row
        dc_max = int(math.sqrt(max(0, r2 - dr * dr)))
        for c in range(max(0, col - dc_max), min(W, col + dc_max + 1)):
            yield r, c


_RECT_PAD_FAMILY = ("rect", "roundrect", "chamfered", "trapezoid", "custom")


def _pad_spine_mm(pad: Pad) -> Tuple[float, float, float, float, float]:
    """Copper center-line segment + minor half-extent of a pad, board mm.

    Returns ``(x1, y1, x2, y2, minor_half_mm)``. Rotation uses
    ``pad.angle_deg`` with the board-geometry sign convention R(-angle) —
    same as ``_to_board`` and ``pad_copper_polygon`` (verified against
    pcbnew; R(+angle) mirrors copper against its own pad row).

    Only meaningful for ``oval``/``circle`` (exact stadium spine, half
    length ``major-minor``; degenerates to a point for round pads) and
    unknown shapes (zero-length segment with ``minor = max/2`` — the prior
    conservative disc). Rect-family pads inflate via
    :func:`_pad_rect_corners_mm` instead: a full-length capsule would
    balloon past the pad ends, and a shortened one would clip the top
    edge near the corners — the inflated rect is bounding-box exact
    with negligible corner overcover.
    """
    cx, cy = pad.position.x_mm, pad.position.y_mm
    try:
        w, h = float(pad.size_mm[0]), float(pad.size_mm[1])
    except (TypeError, ValueError, IndexError):
        return cx, cy, cx, cy, 0.0
    ang = math.radians(-float(pad.angle_deg or 0.0))
    ca, sa = math.cos(ang), math.sin(ang)
    if w >= h:
        ax, ay = ca, sa
        major, minor = w / 2.0, h / 2.0
    else:
        ax, ay = -sa, ca
        major, minor = h / 2.0, w / 2.0
    shape = (getattr(pad, "shape", "") or "").lower()
    if shape in _RECT_PAD_FAMILY:
        return cx, cy, cx, cy, minor
    if shape in ("oval", "circle"):
        seg_half = major - minor
        return (cx - ax * seg_half, cy - ay * seg_half,
                cx + ax * seg_half, cy + ay * seg_half, minor)
    return cx, cy, cx, cy, major


def _pad_rect_corners_mm(pad: Pad, halo_mm: float) -> list:
    """Four inflated corners ``[(x, y)] * 4`` of a rect-family pad, board mm.

    The copper rect (half extents ``w/2``, ``h/2`` about the pad position,
    rotated R(-angle) like :func:`_pad_spine_mm`) grown by ``halo_mm`` on
    every side, head-to-tail ring order for :func:`_fill_polygon`.
    Bounding-box exact vs ``rect ⊕ disc(halo)``; the square corners
    overcover only the tiny corner squares outside the true rounded
    corners (conservative, never short). ``roundrect`` cutouts and
    chamfers/trapezoids sit inside the rect, hence stay covered.
    """
    cx, cy = pad.position.x_mm, pad.position.y_mm
    try:
        w, h = float(pad.size_mm[0]), float(pad.size_mm[1])
    except (TypeError, ValueError, IndexError):
        w, h = 0.0, 0.0
    ang = math.radians(-float(getattr(pad, "angle_deg", 0.0) or 0.0))
    ca, sa = math.cos(ang), math.sin(ang)
    hw, hh = w / 2.0 + halo_mm, h / 2.0 + halo_mm

    def rot(sx: float, sy: float) -> tuple:
        return (cx + sx * ca - sy * sa, cy + sx * sa + sy * ca)

    return [rot(-hw, -hh), rot(hw, -hh), rot(hw, hh), rot(-hw, hh)]


def _stamp_rect_zone(layer_grid: np.ndarray, grid, pad: Pad,
                     halo_mm: float, value: int) -> None:
    """Max-stamp a rect-family pad's inflated rect (board-mm polygon fill)."""
    res = grid.resolution_mm
    ox, oy = grid.origin_mm
    if res <= 0:
        return
    floats = [((y - oy) / res, (x - ox) / res)
              for (x, y) in _pad_rect_corners_mm(pad, halo_mm)]
    # _fill_polygon takes integer cells: round each corner outward from
    # the centroid (conservative by <1 cell, ring order preserved).
    cr = sum(p[0] for p in floats) / len(floats)
    cc = sum(p[1] for p in floats) / len(floats)
    points = [
        (math.floor(r) if r <= cr else math.ceil(r),
         math.floor(c) if c <= cc else math.ceil(c))
        for (r, c) in floats
    ]
    _fill_polygon(layer_grid, points, value)


def _stamp_pad_zone(layer_grid: np.ndarray, grid, pad: Pad,
                    halo_mm: float, value: int) -> None:
    """Max-stamp one halo tier of a pad, dispatching on shape family.

    Rect family inflates the copper rect as a polygon (bounding-box
    exact); everything else stamps a capsule around the copper spine
    (:func:`_pad_spine_mm`) with radius ``minor_half + halo``. ``halo_mm``
    is the margin beyond copper (0 for the hard core tier).
    """
    shape = (getattr(pad, "shape", "") or "").lower()
    if shape in _RECT_PAD_FAMILY:
        _stamp_rect_zone(layer_grid, grid, pad, halo_mm, value)
        return
    x1, y1, x2, y2, minor_half = _pad_spine_mm(pad)
    _stamp_capsule_zone(layer_grid, grid, x1, y1, x2, y2,
                        minor_half + halo_mm, value)


def _stamp_capsule_zone(
    layer_grid: np.ndarray,
    grid,
    x1_mm: float,
    y1_mm: float,
    x2_mm: float,
    y2_mm: float,
    radius_mm: float,
    value: int,
) -> None:
    """Max-stamp cells whose center lies within ``radius_mm`` of a segment.

    Board-mm twin of :func:`_mark_circular_zone` for oriented pads: the
    segment is the copper center-line (:func:`_pad_spine_mm`), the radius
    the minor half-extent plus halo. Degenerate (zero-length) segments
    stamp discs. Vectorized over the segment's cell bbox.
    """
    H, W = layer_grid.shape
    res = grid.resolution_mm
    ox, oy = grid.origin_mm
    if res <= 0:
        col, row = board_to_grid(grid, (x1_mm + x2_mm) / 2.0, (y1_mm + y2_mm) / 2.0)
        if 0 <= row < H and 0 <= col < W:
            layer_grid[row, col] = max(layer_grid[row, col], value)
        return
    if radius_mm < 0:
        return
    slack = radius_mm + res
    c_lo = max(0, int(math.floor((min(x1_mm, x2_mm) - slack - ox) / res)))
    c_hi = min(W - 1, int(math.ceil((max(x1_mm, x2_mm) + slack - ox) / res)))
    r_lo = max(0, int(math.floor((min(y1_mm, y2_mm) - slack - oy) / res)))
    r_hi = min(H - 1, int(math.ceil((max(y1_mm, y2_mm) + slack - oy) / res)))
    if c_lo > c_hi or r_lo > r_hi:
        return
    cc, rr = np.meshgrid(np.arange(c_lo, c_hi + 1), np.arange(r_lo, r_hi + 1))
    xs = ox + (cc + 0.5) * res
    ys = oy + (rr + 0.5) * res
    dx, dy = x2_mm - x1_mm, y2_mm - y1_mm
    seg2 = dx * dx + dy * dy
    if seg2 <= 0:
        dist2 = (xs - x1_mm) ** 2 + (ys - y1_mm) ** 2
    else:
        t = np.clip(((xs - x1_mm) * dx + (ys - y1_mm) * dy) / seg2, 0.0, 1.0)
        dist2 = (xs - (x1_mm + t * dx)) ** 2 + (ys - (y1_mm + t * dy)) ** 2
    hit = dist2 <= radius_mm * radius_mm
    if np.any(hit):
        sub = layer_grid[r_lo:r_hi + 1, c_lo:c_hi + 1]
        sub[hit] = np.maximum(sub[hit], value)


@njit(cache=True)
def _stamp_discs_nb(
    mask: np.ndarray,
    rows: np.ndarray,
    cols: np.ndarray,
    radii: np.ndarray,
    value: bool,
) -> None:
    """Stamp (``value=True``) or carve (``value=False``) Euclidean discs.

    Compiled twin of the ``disc_cells`` scan (``dr²+dc² ≤ r²`` with
    ``int()`` truncation): same loop bounds, same per-row ``dc_max``,
    bound-clipped. Operates in place.
    """
    H, W = mask.shape
    n = rows.shape[0]
    for k in range(n):
        r0 = rows[k]
        c0 = cols[k]
        rad = radii[k]
        r2 = rad * rad
        rmin = r0 - rad
        if rmin < 0:
            rmin = 0
        rmax = r0 + rad + 1
        if rmax > H:
            rmax = H
        for r in range(rmin, rmax):
            dr = r - r0
            rem = r2 - dr * dr
            if rem < 0:
                rem = 0
            dc_max = int(np.sqrt(float(rem)))
            cmin = c0 - dc_max
            if cmin < 0:
                cmin = 0
            cmax = c0 + dc_max + 1
            if cmax > W:
                cmax = W
            for c in range(cmin, cmax):
                mask[r, c] = value


def stamp_discs(
    mask: np.ndarray,
    rows,
    cols,
    radii,
    value: bool,
) -> None:
    """Stamp/carve Euclidean discs at ``(rows[i], cols[i])`` (in place).

    Batches all centers into one :func:`_stamp_discs_nb` call (Numba fast
    path); falls back to the ``disc_cells`` Python loop when Numba is
    unavailable. Results are identical either way.
    """
    rows = np.asarray(rows, dtype=np.int64)
    cols = np.asarray(cols, dtype=np.int64)
    radii = np.asarray(radii, dtype=np.int64)
    if rows.size == 0:
        return
    # With Numba this is the compiled kernel; without it (no-op
    # decorator) the same body runs interpreted — identical results.
    _stamp_discs_nb(mask, rows, cols, radii, bool(value))


def _mark_pad(
    cost_grid: np.ndarray,
    grid,
    fp,
    pad: Pad,
    clearance_cells: dict,
    default_clearance: int,
    track_half_mm: float = 0.0,
):
    """Mark pad center and clearance zone as high cost.

    The ring covers the pad's own extent plus clearance (plus half the
    routing track width when known), so coarse grids cannot leave FREE
    cells inside a pad's copper.

    Through-hole pads: mark on BOTH layers (free via).
    NPTH (np_thru_hole) and no-net mounting holes get the board hole
    clearance (0.25 mm) instead of the trace clearance.
    """
    # Determine which layers this pad occupies: through-hole spans every
    # grid layer; SMD lives on its footprint layer (skipped when that layer
    # is not routed, e.g. an inner-layer footprint on the outer-only grid).
    is_through_hole = pad.is_through_hole
    n_layers = cost_grid.shape[0]
    if is_through_hole:
        pad_layers = list(range(n_layers))
    else:
        idx = grid.layer_to_idx.get(fp.layer)
        pad_layers = [idx] if idx is not None else []

    # Get clearance for this pad's net
    net_name = pad.net_name
    clearance = clearance_cells.get(net_name, default_clearance) if net_name else default_clearance

    # NPTH mounting holes and no-net through-hole pads use hole clearance (0.25 mm)
    if is_through_hole and (pad.pad_type == "np_thru_hole" or not net_name):
        # Hole clearance is 0.25 mm per board setup constraints
        hole_clearance_mm = 0.25
        hole_clearance_cells = max(1, int(math.ceil(hole_clearance_mm / grid.resolution_mm)))
        if hole_clearance_cells > clearance:
            clearance = hole_clearance_cells

    # Widen the ring to cover the pad copper itself (+ track half width).
    # Shape-dispatched like the secondary halos: inflated rects for the
    # rect family, capsules for ovals, discs for round/unknown pads.
    res_mm = grid.resolution_mm
    halo_mm = clearance * res_mm + track_half_mm

    for layer_idx in pad_layers:
        if layer_idx < cost_grid.shape[0]:
            _stamp_pad_zone(
                cost_grid[layer_idx], grid, pad, halo_mm, HIGH_COST
            )


def _mark_track(cost_grid: np.ndarray, grid, track, clearance_mm: float = 0.2):
    """Mark track segment as blocked on its layer with width + clearance halo.

    Tracks on layers outside the routing grid (e.g. inner-plane copper on
    the outer-only ``F.Cu``/``B.Cu`` grid) are skipped: they live on another
    plane and must never become phantom blockers on the back layer.
    """
    layer_idx = grid.layer_to_idx.get(track.layer)
    if layer_idx is None or layer_idx >= cost_grid.shape[0]:
        return
    radius = track_corridor_radius_cells(track.width_mm, clearance_mm, grid.resolution_mm)
    c1, r1 = board_to_grid(grid, track.start.x_mm, track.start.y_mm)
    c2, r2 = board_to_grid(grid, track.end.x_mm, track.end.y_mm)
    layer_grid = cost_grid[layer_idx]
    for c, r in bresenham_cells(c1, r1, c2, r2):
        if 0 <= r < layer_grid.shape[0] and 0 <= c < layer_grid.shape[1]:
            if radius:
                stamp_square(layer_grid, r, c, radius, BLOCKED)
            else:
                layer_grid[r, c] = BLOCKED


def _mark_arc(cost_grid: np.ndarray, grid, arc, clearance_mm: float = 0.2):
    """Mark a copper track arc as blocked via its sampled polyline.

    Circular arcs have no closed-form rasterizer here, so the arc is
    sampled (16 segments, matching edge-cut arcs) and each chord is
    stamped with the same width + clearance halo as straight tracks.
    Layers outside the routing grid are skipped, like ``_mark_track``.
    Degenerate arcs (start == end: zero copper, invisible to analysis
    and emission) stamp nothing — no phantom blockers.
    """
    if abs(arc.start.x_mm - arc.end.x_mm) < 1e-9 and abs(arc.start.y_mm - arc.end.y_mm) < 1e-9:
        return
    layer_idx = grid.layer_to_idx.get(arc.layer)
    if layer_idx is None or layer_idx >= cost_grid.shape[0]:
        return
    radius = track_corridor_radius_cells(arc.width_mm, clearance_mm, grid.resolution_mm)
    pts = list(sample_circular_arc(
        (arc.start.x_mm, arc.start.y_mm),
        (arc.mid.x_mm, arc.mid.y_mm),
        (arc.end.x_mm, arc.end.y_mm),
        16,
    ))
    layer_grid = cost_grid[layer_idx]
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        c1, r1 = board_to_grid(grid, x1, y1)
        c2, r2 = board_to_grid(grid, x2, y2)
        for c, r in bresenham_cells(c1, r1, c2, r2):
            if 0 <= r < layer_grid.shape[0] and 0 <= c < layer_grid.shape[1]:
                if radius:
                    stamp_square(layer_grid, r, c, radius, BLOCKED)
                else:
                    layer_grid[r, c] = BLOCKED


def _mark_via(
    cost_grid: np.ndarray,
    grid,
    via,
    clearance_mm: float = 0.2,
    track_half_mm: float = 0.125,
):
    """Mark via with full copper+clearance halo on the layers it pierces.

    The halo covers the via annulus (size/2) plus clearance and half the
    track width, so subsequent nets keep DRC distance to the via copper.
    Vias with an explicit ``layers`` span only stamp intersecting grid
    layers (an inner-only blind via never blocks the outer-only grid);
    vias without a span (legacy/unknown) stamp every grid layer.
    """
    col, row = board_to_grid(grid, via.position.x_mm, via.position.y_mm)
    radius = via_halo_radius_cells(via.size_mm, clearance_mm, track_half_mm, grid.resolution_mm)
    if getattr(via, "layers", ()):
        targets = [
            grid.layer_to_idx[name]
            for name in via.layers
            if name in grid.layer_to_idx
        ]
        if not targets:
            return
    else:
        targets = list(range(cost_grid.shape[0]))
    for layer_idx in targets:
        stamp_square(cost_grid[layer_idx], row, col, radius, BLOCKED)


def _mark_polygon_on_layers(cost_grid: np.ndarray, grid, polygon, layers, value: int) -> None:
    """Fill a board-mm polygon with ``value`` on the named copper layers.

    Shared core of ``_mark_zone`` / ``_mark_keepout``: unknown layers are
    ignored; ``board_to_grid`` returns (col, row) but ``_fill_polygon``
    takes (row, col) — swapped here, once.
    """
    if not polygon or len(polygon) < 3:
        return
    for layer_name in layers:
        layer_idx = grid.layer_to_idx.get(layer_name)
        if layer_idx is None or layer_idx >= cost_grid.shape[0]:
            continue
        points = [
            (row, col)
            for (col, row) in (board_to_grid(grid, p.x_mm, p.y_mm) for p in polygon)
        ]
        _fill_polygon(cost_grid[layer_idx], points, value)


def _mark_zone(cost_grid: np.ndarray, grid, zone):
    """Mark copper zone interior as passable-at-toll on its layers.

    Pours are NOT blockers: real boards route tracks across pours and
    KiCad refills around them with the zone clearance. Hard-blocking
    pours made any pad inside a pour unroutable (escape steps refused)
    and denied the router the crossing it needs; the pour-severance
    secondary (Phase 3) steers tracks toward wide corridors instead,
    and auto-stitching repairs residual fragments. ``_fill_polygon``
    takes the max, so pad HIGH_COST and track BLOCKED cells survive.
    """
    _mark_polygon_on_layers(cost_grid, grid, zone.polygon, zone.layers, POUR)


def _mark_keepout(cost_grid: np.ndarray, grid, keepout) -> None:
    """Mark a ``KeepoutZone`` as BLOCKED on its layers.

    Zones that allow tracks (``tracks_allowed``) are skipped entirely.
    V1 limitation: BLOCKED regardless of ``vias_allowed`` — a
    vias-allowed/tracks-forbidden keepout letting a hop through is a
    follow-up (the router checks blockage on the via landing cell).
    Unknown layers are ignored.
    """
    if getattr(keepout, "tracks_allowed", False):
        return
    _mark_polygon_on_layers(
        cost_grid, grid, getattr(keepout, "polygon", None), keepout.layers, BLOCKED
    )


def _mark_edge_keepout(cost_grid: np.ndarray, grid, model, keepout_mm: float = 0.5):
    """Mark edge keepout zone around board boundary using actual Edge.Cuts lines."""
    keepout_cells = int(np.ceil(keepout_mm / grid.resolution_mm))
    H, W = cost_grid.shape[1], cost_grid.shape[2]
    
    for layer_idx in range(cost_grid.shape[0]):
        # Draw keepout along each Edge.Cuts segment
        for a, b in model.edge_cuts:
            # Draw a thick line along the edge cut with keepout width
            _draw_thick_line_keepout(
                cost_grid[layer_idx], grid, 
                a.x_mm, a.y_mm, b.x_mm, b.y_mm, 
                keepout_cells
            )
        
        # Also handle edge arcs
        for arc in model.edge_arcs:
            _draw_arc_keepout(
                cost_grid[layer_idx], grid,
                arc.start.x_mm, arc.start.y_mm,
                arc.mid.x_mm, arc.mid.y_mm,
                arc.end.x_mm, arc.end.y_mm,
                keepout_cells
            )
        
        # Also add rectangular keepout at grid boundaries as fallback
        # (in case Edge.Cuts is incomplete) - use EDGE_KEEPOUT not BLOCKED
        cost_grid[layer_idx, :keepout_cells, :] = np.maximum(
            cost_grid[layer_idx, :keepout_cells, :], EDGE_KEEPOUT
        )
        cost_grid[layer_idx, -keepout_cells:, :] = np.maximum(
            cost_grid[layer_idx, -keepout_cells:, :], EDGE_KEEPOUT
        )
        cost_grid[layer_idx, :, :keepout_cells] = np.maximum(
            cost_grid[layer_idx, :, :keepout_cells], EDGE_KEEPOUT
        )
        cost_grid[layer_idx, :, -keepout_cells:] = np.maximum(
            cost_grid[layer_idx, :, -keepout_cells:], EDGE_KEEPOUT
        )


def inside_board_mask(grid, model) -> "np.ndarray | None":
    """Bool (H, W) mask of cells inside any board polygon (None = no outline).

    Union over ``model.board_regions`` polygons; rectangular bbox fallback
    per region when the polygon is degenerate. Callers treat None as
    unconstrained (single-board legacy path with no Edge.Cuts).
    """
    import numpy as _np

    regions = getattr(model, "board_regions", ())
    if not regions:
        return None
    H, W = grid.height_cells, grid.width_cells
    inside = _np.zeros((H, W), dtype=bool)
    for region in regions:
        poly = getattr(region, "polygon", ())
        if poly and len(poly) >= 3:
            pts = [
                (row, col)
                for (col, row) in (board_to_grid(grid, p.x_mm, p.y_mm) for p in poly)
            ]
            tmp = _np.zeros((H, W), dtype=_np.uint8)
            _fill_polygon(tmp, pts, 1)
            inside |= tmp.astype(bool)
        else:
            try:
                min_x, max_x, min_y, max_y = region.bbox
            except Exception:
                continue
            c0, r0 = board_to_grid(grid, min_x, min_y)
            c1, r1 = board_to_grid(grid, max_x, max_y)
            inside[max(0, r0):r1 + 1, max(0, c0):c1 + 1] = True
    return inside


def _mark_outside_board_blocked(cost_grid: np.ndarray, grid, model) -> None:
    """Stamp BLOCKED on every routing layer outside all board polygons."""
    inside = inside_board_mask(grid, model)
    if inside is None or bool(inside.any()) is False:
        return
    for layer_idx in range(cost_grid.shape[0]):
        cost_grid[layer_idx][~inside] = np.maximum(
            cost_grid[layer_idx][~inside], BLOCKED
        )


def _draw_thick_line_keepout(grid: np.ndarray, grid_obj, x1_mm, y1_mm, x2_mm, y2_mm, thickness_cells: int):
    """Draw a thick line (keepout zone) along the line segment."""
    # Use Bresenham to get the center line, then expand by thickness
    c1, r1 = board_to_grid(grid_obj, x1_mm, y1_mm)
    c2, r2 = board_to_grid(grid_obj, x2_mm, y2_mm)

    for c, r in bresenham_cells(c1, r1, c2, r2):
        if 0 <= r < grid.shape[0] and 0 <= c < grid.shape[1]:
            stamp_square(grid, r, c, thickness_cells, EDGE_KEEPOUT)


def _draw_arc_keepout(grid: np.ndarray, grid_obj, sx_mm, sy_mm, mx_mm, my_mm, ex_mm, ey_mm, thickness_cells: int):
    """Draw keepout zone along a circular arc (start/mid/end ON the arc)."""
    # Sample points along the circular arc
    num_samples = max(10, int(np.hypot(ex_mm - sx_mm, ey_mm - sy_mm) / grid_obj.resolution_mm / 2))

    for x, y in sample_circular_arc(
        (sx_mm, sy_mm), (mx_mm, my_mm), (ex_mm, ey_mm), num_samples
    ):
        c, r = board_to_grid(grid_obj, x, y)

        if 0 <= r < grid.shape[0] and 0 <= c < grid.shape[1]:
            stamp_square(grid, r, c, thickness_cells, EDGE_KEEPOUT)


# --- Drawing primitives ---

def bresenham_cells(c1: int, r1: int, c2: int, r2: int):
    """Yield ``(c, r)`` along the Bresenham line (single shared stepping core)."""
    dx = abs(c2 - c1)
    dy = abs(r2 - r1)
    sx = 1 if c1 < c2 else -1
    sy = 1 if r1 < r2 else -1
    err = dx - dy

    c, r = c1, r1
    while True:
        yield c, r
        if c == c2 and r == r2:
            break
        e2 = 2 * err
        if e2 > -dy:
            err -= dy
            c += sx
        if e2 < dx:
            err += dx
            r += sy


def stamp_square(layer_grid: np.ndarray, r: int, c: int, radius: int, value: int) -> None:
    """max-stamp a ``(2*radius+1)`` square (bound-clipped)."""
    H, W = layer_grid.shape
    r_min, r_max = max(0, r - radius), min(H - 1, r + radius)
    c_min, c_max = max(0, c - radius), min(W - 1, c + radius)
    layer_grid[r_min:r_max + 1, c_min:c_max + 1] = np.maximum(
        layer_grid[r_min:r_max + 1, c_min:c_max + 1], value
    )


def _draw_line_blocked(grid: np.ndarray, grid_obj, x1_mm, y1_mm, x2_mm, y2_mm, value: int = BLOCKED):
    """Draw a line on the grid using Bresenham, marking cells with ``value``.

    Defaults to BLOCKED (tracks); pass HIGH_COST for soft discouragement
    (e.g. courtyard outlines that pads must remain able to cross).
    """
    c1, r1 = board_to_grid(grid_obj, x1_mm, y1_mm)
    c2, r2 = board_to_grid(grid_obj, x2_mm, y2_mm)

    for c, r in bresenham_cells(c1, r1, c2, r2):
        if 0 <= r < grid.shape[0] and 0 <= c < grid.shape[1]:
            grid[r, c] = max(grid[r, c], value)


def _mark_circular_zone(grid: np.ndarray, center_row: int, center_col: int, radius: int, value: int):
    """Mark a circular zone with given value (max with existing)."""
    H, W = grid.shape
    r_min = max(0, center_row - radius)
    r_max = min(H - 1, center_row + radius)
    c_min = max(0, center_col - radius)
    c_max = min(W - 1, center_col + radius)
    r2 = radius * radius
    
    for r in range(r_min, r_max + 1):
        dr = r - center_row
        dc_max = int(np.sqrt(max(0, r2 - dr * dr)))
        c_start = max(c_min, center_col - dc_max)
        c_end = min(c_max, center_col + dc_max)
        grid[r, c_start:c_end + 1] = np.maximum(grid[r, c_start:c_end + 1], value)


_CACHE_BIN_CELLS = 64
_CACHE_FULL_RECOMPUTE_FRAC = 0.4


class ObstacleCache:
    """Board obstacle state with incremental copper re-raster.

    Per-call ``build_occupancy_grid`` + THT/via-block mask rebuilds cost
    ~0.3-0.9 s on DCCF-class boards while the footprint/zone/keepout inputs
    rarely change between calls. This cache keeps, per
    ``(grid, clearance, track_half, static fingerprint)``:

    - ``static``: occupancy of everything that is not routed copper
      (courtyards, pads, zones, edge keepout, outside-board, keepouts);
    - ``base``: ``static`` max-stamped with every current copper item
      (track/arc/via), bit-identical to ``build_occupancy_grid`` output;
    - the footprint-only THT via mask and via-block mask;
    - per-item stamp records + spatial bins for dirty-region recompute;
    - via-landing dilation state (see ``router.build_via_ok_cached``).

    ``sync`` returns the live ``base`` array (callers copy it into their
    own grid). All stamps take the max, so stamp order never matters and
    the cached path stays bit-identical to a fresh rebuild.
    """

    def __init__(self, max_entries: int = 2):
        self._entries = []  # [(key, _ObstacleEntry)], MRU-first
        self.max_entries = max(1, int(max_entries))
        self.last_hit = False

    def sync(
        self,
        model,
        grid,
        clearance_mm: float = 0.2,
        track_half_mm: float = 0.125,
    ) -> np.ndarray:
        """Return the base occupancy grid for this model/grid/params.

        Rebuilds from scratch on a key miss (new board, grid, clearance,
        or footprint/zone/outline change); otherwise stamps added copper
        in place and recomputes only the bbox union of removed copper.
        """
        fingerprint = static_fingerprint(model)
        key = (grid, float(clearance_mm), float(track_half_mm), fingerprint)
        entry = self._lookup(key)
        if entry is None:
            entry = _ObstacleEntry(
                grid=grid,
                clearance_mm=clearance_mm,
                track_half_mm=track_half_mm,
                fingerprint=fingerprint,
                model=model,
            )
            self._entries.insert(0, (key, entry))
            del self._entries[self.max_entries:]
            self.last_hit = False
        else:
            self.last_hit = True
            entry.update_copper(model)
        return entry.base

    def masks(self):
        """(tht_via_mask, via_block_mask) of the active (MRU) entry."""
        if not self._entries:
            raise RuntimeError("ObstacleCache.masks() called before sync()")
        entry = self._entries[0][1]
        return entry.tht_via_mask, entry.via_block_mask

    def active_entry(self):
        """The active entry (for router via-ok state). Call after sync."""
        if not self._entries:
            raise RuntimeError(
                "ObstacleCache.active_entry() called before sync()"
            )
        return self._entries[0][1]

    def _lookup(self, key):
        for i, (k, entry) in enumerate(self._entries):
            if k == key:
                self._entries.insert(0, self._entries.pop(i))
                return entry
        return None


class _ObstacleEntry:
    """Mutable per-board raster state owned by :class:`ObstacleCache`."""

    __slots__ = (
        "grid",
        "clearance_mm",
        "track_half_mm",
        "fingerprint",
        "static",
        "base",
        "tht_via_mask",
        "via_block_mask",
        "items",
        "bins",
        "via_ok_memo",
    )

    def __init__(self, grid, clearance_mm, track_half_mm, fingerprint, model):
        self.grid = grid
        self.clearance_mm = clearance_mm
        self.track_half_mm = track_half_mm
        self.fingerprint = fingerprint
        self.static = build_occupancy_grid(
            model, grid,
            default_clearance_mm=clearance_mm,
            skip_copper=True,
        )
        self.base = self.static.copy()
        self.tht_via_mask = _build_tht_via_mask(model, grid)
        self.via_block_mask = build_via_block_mask(
            model, grid,
            clearance_mm=clearance_mm,
            track_half_mm=track_half_mm,
        )
        self.items = {}  # copper item -> (r0, r1, c0, c1) stamp bbox
        self.bins = {}  # (bin_r, bin_c) -> set of copper items
        self.via_ok_memo = {}  # (base digest, sec digest, radius) -> via_ok
        for item in _copper_items(model):
            self._add_item(item)

    def update_copper(self, model) -> None:
        """Sync ``base`` with the model's current copper (add/remove)."""
        copper = _copper_items(model)
        for item in copper:
            if item not in self.items:
                self._add_item(item)
        removed = [item for item in self.items if item not in copper]
        if not removed:
            return
        for item in removed:
            del self.items[item]
        self._recompute_removed(removed)

    def _add_item(self, item) -> None:
        rec = _copper_bbox(item, self.grid, self.clearance_mm)
        if rec is None:
            return
        r0, r1, c0, c1, _layers = rec
        _stamp_copper_full(self.base, self.grid, item, self.clearance_mm)
        self.items[item] = (r0, r1, c0, c1)
        for br in range(r0 // _CACHE_BIN_CELLS, r1 // _CACHE_BIN_CELLS + 1):
            for bc in range(c0 // _CACHE_BIN_CELLS, c1 // _CACHE_BIN_CELLS + 1):
                self.bins.setdefault((br, bc), set()).add(item)

    def _recompute_removed(self, removed) -> None:
        """Recompute ``base`` over the union bbox of removed stamps.

        Scratch starts from ``static``; only items overlapping the region
        re-stamp it. Removal regions are net-local, so this stays cheap;
        board-wide removals (GND strip) fall back to a full recomposite.
        """
        boxes = [self._bbox_of(item) for item in removed]
        r0 = min(box[0] for box in boxes)
        r1 = max(box[1] for box in boxes)
        c0 = min(box[2] for box in boxes)
        c1 = max(box[3] for box in boxes)
        H, W = self.grid.height_cells, self.grid.width_cells
        if (r1 - r0 + 1) * (c1 - c0 + 1) > _CACHE_FULL_RECOMPUTE_FRAC * H * W:
            self.base[:] = self.static
            for item in self.items:
                _stamp_copper_full(
                    self.base, self.grid, item, self.clearance_mm
                )
            self._rebuild_bins()
            return
        scratch = self.static[:, r0:r1 + 1, c0:c1 + 1].copy()
        seen = set()
        touched = set()
        for br in range(r0 // _CACHE_BIN_CELLS, r1 // _CACHE_BIN_CELLS + 1):
            for bc in range(c0 // _CACHE_BIN_CELLS, c1 // _CACHE_BIN_CELLS + 1):
                touched.add((br, bc))
                for item in self.bins.get((br, bc), ()):
                    if item in self.items and item not in seen:
                        seen.add(item)
                        _stamp_copper_region(
                            scratch, self.grid, item,
                            self.clearance_mm, r0, c0,
                        )
        self.base[:, r0:r1 + 1, c0:c1 + 1] = scratch
        # Drop stale (removed-item) refs in the touched bins.
        for key in touched:
            live = self.bins.get(key)
            if live:
                live = {item for item in live if item in self.items}
                if live:
                    self.bins[key] = live
                else:
                    del self.bins[key]

    def _bbox_of(self, item):
        # Removed items keep no record; recompute the (cheap) analytic box.
        return _copper_bbox(item, self.grid, self.clearance_mm)[:4]

    def _rebuild_bins(self) -> None:
        """Re-index every registered item (after a full recomposite)."""
        self.bins = {}
        for item, (r0, r1, c0, c1) in self.items.items():
            for br in range(r0 // _CACHE_BIN_CELLS, r1 // _CACHE_BIN_CELLS + 1):
                for bc in range(c0 // _CACHE_BIN_CELLS, c1 // _CACHE_BIN_CELLS + 1):
                    self.bins.setdefault((br, bc), set()).add(item)


def static_fingerprint(model) -> int:
    """Hash of every model input that is NOT routed copper.

    Footprints, pads, zones, edge cuts/arcs, keepouts, and board regions
    drive the static raster; tracks/arcs/vias are handled by the copper
    registry instead. All inputs are frozen dataclasses, so this is a few
    milliseconds per call.
    """
    parts = [len(model.footprints)]
    for fp in model.footprints:
        parts.append(hash(fp))
    for coll in (
        model.keepout_zones,
        model.zones,
        model.edge_cuts,
        model.edge_arcs,
    ):
        parts.append(hash(tuple(coll)))
    # Board regions keyed by geometry, not uuid: the uuid is a parse-time
    # artifact regenerated on every load (same board, different uuids).
    for region in model.board_regions:
        parts.append(hash((tuple(region.polygon), region.bbox)))
    return hash(tuple(parts))


def _copper_items(model):
    """Set of current copper items (tracks, arcs, vias)."""
    return set(model.tracks) | set(model.arcs) | set(model.vias)


def _copper_bbox(item, grid, clearance_mm):
    """Superset ``(r0, r1, c0, c1, layers)`` of stamped cells, or None.

    Covers exactly what ``_mark_track``/``_mark_arc``/``_mark_via`` would
    stamp: Bresenham cells stay inside the endpoint box, square stamps add
    the halo radius, arc chords are bounded by their sample points.
    """
    res = grid.resolution_mm
    H, W = grid.height_cells, grid.width_cells
    n_layers = len(grid.layers)
    if isinstance(item, Track):
        layer = grid.layer_to_idx.get(item.layer)
        if layer is None or layer >= n_layers:
            return None
        layers = (layer,)
        radius = track_corridor_radius_cells(
            item.width_mm, clearance_mm, res)
        c1, r1 = board_to_grid(grid, item.start.x_mm, item.start.y_mm)
        c2, r2 = board_to_grid(grid, item.end.x_mm, item.end.y_mm)
        r0 = max(0, min(r1, r2) - radius)
        r1_ = min(H - 1, max(r1, r2) + radius)
        c0 = max(0, min(c1, c2) - radius)
        c1_ = min(W - 1, max(c1, c2) + radius)
        return (r0, r1_, c0, c1_, layers)
    if isinstance(item, Via):
        radius = via_halo_radius_cells(
            item.size_mm, clearance_mm, 0.125, res)
        col, row = board_to_grid(grid, item.position.x_mm, item.position.y_mm)
        if getattr(item, "layers", ()):
            targets = [
                grid.layer_to_idx[name]
                for name in item.layers
                if name in grid.layer_to_idx
            ]
            if not targets:
                return None
            layers = tuple(targets)
        else:
            layers = tuple(range(n_layers))
        return (
            max(0, row - radius), min(H - 1, row + radius),
            max(0, col - radius), min(W - 1, col + radius),
            layers,
        )
    if isinstance(item, ArcTrack):
        if (abs(item.start.x_mm - item.end.x_mm) < 1e-9
                and abs(item.start.y_mm - item.end.y_mm) < 1e-9):
            return None
        layer = grid.layer_to_idx.get(item.layer)
        if layer is None or layer >= n_layers:
            return None
        radius = track_corridor_radius_cells(
            item.width_mm, clearance_mm, res)
        pts = sample_circular_arc(
            (item.start.x_mm, item.start.y_mm),
            (item.mid.x_mm, item.mid.y_mm),
            (item.end.x_mm, item.end.y_mm),
            16,
        )
        cells = [board_to_grid(grid, x, y) for x, y in pts]
        rows = [r for _c, r in cells]
        cols = [c for c, _r in cells]
        return (
            max(0, min(rows) - radius), min(H - 1, max(rows) + radius),
            max(0, min(cols) - radius), min(W - 1, max(cols) + radius),
            (layer,),
        )
    return None


def _stamp_copper_full(layer_grid, grid, item, clearance_mm) -> None:
    """Stamp one copper item on a full-shape grid (same as a fresh build)."""
    if isinstance(item, Track):
        _mark_track(layer_grid, grid, item, clearance_mm)
    elif isinstance(item, ArcTrack):
        _mark_arc(layer_grid, grid, item, clearance_mm)
    elif isinstance(item, Via):
        _mark_via(layer_grid, grid, item, clearance_mm=clearance_mm,
                  track_half_mm=0.125)


def _stamp_square_region(layer, r, c, radius, value, ro, co) -> None:
    """Region-local twin of :func:`stamp_square` (offsets ``ro``, ``co``)."""
    r_min = max(0, r - radius - ro)
    r_max = min(layer.shape[0] - 1, r + radius - ro)
    c_min = max(0, c - radius - co)
    c_max = min(layer.shape[1] - 1, c + radius - co)
    if r_min > r_max or c_min > c_max:
        return
    sub = layer[r_min:r_max + 1, c_min:c_max + 1]
    np.maximum(sub, value, out=sub)


def _stamp_copper_region(tile, grid, item, clearance_mm, ro, co) -> None:
    """Region-local twin of the ``_mark_*`` copper stampers.

    Stamps the same cells as :func:`_stamp_copper_full` restricted to the
    ``tile`` window (absolute origin ``ro``, ``co``); geometry and radii
    match the full-grid variants so recompute regions stay exact.
    """
    res = grid.resolution_mm
    H, W = grid.height_cells, grid.width_cells
    n_layers = len(grid.layers)
    if isinstance(item, Track):
        layer = grid.layer_to_idx.get(item.layer)
        if layer is None or layer >= n_layers:
            return
        radius = track_corridor_radius_cells(
            item.width_mm, clearance_mm, res)
        c1, r1 = board_to_grid(grid, item.start.x_mm, item.start.y_mm)
        c2, r2 = board_to_grid(grid, item.end.x_mm, item.end.y_mm)
        layer_grid = tile[layer]
        for c, r in bresenham_cells(c1, r1, c2, r2):
            if 0 <= r < H and 0 <= c < W:
                if radius:
                    _stamp_square_region(
                        layer_grid, r, c, radius, BLOCKED, ro, co)
                elif ro <= r < ro + layer_grid.shape[0] and co <= c < co + layer_grid.shape[1]:
                    layer_grid[r - ro, c - co] = BLOCKED
        return
    if isinstance(item, Via):
        radius = via_halo_radius_cells(
            item.size_mm, clearance_mm, 0.125, res)
        col, row = board_to_grid(grid, item.position.x_mm, item.position.y_mm)
        if getattr(item, "layers", ()):
            targets = [
                grid.layer_to_idx[name]
                for name in item.layers
                if name in grid.layer_to_idx
            ]
            if not targets:
                return
        else:
            targets = list(range(n_layers))
        for layer in targets:
            _stamp_square_region(tile[layer], row, col, radius, BLOCKED, ro, co)
        return
    if isinstance(item, ArcTrack):
        if (abs(item.start.x_mm - item.end.x_mm) < 1e-9
                and abs(item.start.y_mm - item.end.y_mm) < 1e-9):
            return
        layer = grid.layer_to_idx.get(item.layer)
        if layer is None or layer >= n_layers:
            return
        radius = track_corridor_radius_cells(
            item.width_mm, clearance_mm, res)
        # sample_circular_arc is a generator; materialize before subscripting.
        pts = list(sample_circular_arc(
            (item.start.x_mm, item.start.y_mm),
            (item.mid.x_mm, item.mid.y_mm),
            (item.end.x_mm, item.end.y_mm),
            16,
        ))
        layer_grid = tile[layer]
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            c1, r1 = board_to_grid(grid, x1, y1)
            c2, r2 = board_to_grid(grid, x2, y2)
            for c, r in bresenham_cells(c1, r1, c2, r2):
                if 0 <= r < H and 0 <= c < W:
                    if radius:
                        _stamp_square_region(
                            layer_grid, r, c, radius, BLOCKED, ro, co)
                    elif ro <= r < ro + layer_grid.shape[0] and co <= c < co + layer_grid.shape[1]:
                        layer_grid[r - ro, c - co] = BLOCKED


def _fill_polygon(grid: np.ndarray, points: List[Tuple[int, int]], value: int):
    """Fill polygon using scanline fill.

    ``points`` are (row, col) cell coordinates (note: ``board_to_grid``
    returns (col, row), so callers must swap).

    Edge intersections use the exact scalar formula below, evaluated per
    row over all edges with numpy instead of a Python per-edge loop.
    """
    if len(points) < 3:
        return
    H, W = grid.shape

    pts = np.asarray(points, dtype=np.float64)
    er1, ec1 = pts[:, 0], pts[:, 1]
    er2 = np.roll(er1, -1)
    ec2 = np.roll(ec1, -1)

    # Find y bounds
    r_min = max(0, int(np.min(er1)))
    r_max = min(H - 1, int(np.max(er1)))
    if r_max < r_min:
        return
    rows = np.arange(r_min, r_max + 1)[:, None]
    with np.errstate(divide="ignore", invalid="ignore"):
        # Horizontal edges (er1 == er2) never satisfy either half-open
        # test, matching the scalar skip; the 0/0 quotient there is
        # masked out before use.
        hits = ((er1 <= rows) & (rows < er2)) | ((er2 <= rows) & (rows < er1))
        t = (rows - er1) / (er2 - er1)
        cols = ec1 + t * (ec2 - ec1)

    for i, r in enumerate(range(r_min, r_max + 1)):
        intersections = np.sort(cols[i][hits[i]])
        # Fill between pairs
        for j in range(0, len(intersections) - 1, 2):
            c_start = max(0, int(np.ceil(intersections[j])))
            c_end = min(W - 1, int(np.floor(intersections[j + 1])))
            if c_start <= c_end:
                grid[r, c_start:c_end + 1] = np.maximum(grid[r, c_start:c_end + 1], value)