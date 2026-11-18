"""Occupancy grid + fanout/block masks (split from obstacles.py)."""
from __future__ import annotations
import math
from typing import Dict, List, Optional, Set, Tuple
import numpy as np
from ..board_model import ArcTrack, BoardModel, Footprint, Pad, Track, Via, sample_circular_arc
from ..board_model.query import rotate_offset
from ._numba import njit
from .grid import RoutingGrid, board_to_grid, mm_to_cells
from .pad_geom import _pad_half_extent_mm, drill_radius_cells, pad_halo_radius_cells, stamp_discs, via_halo_radius_cells, _stamp_capsule_zone, _stamp_pad_zone, _stamp_rect_zone
from .marking import _mark_arc, _mark_edge_keepout, _mark_keepout, _mark_pad, _mark_track, _mark_via, _mark_zone, inside_board_mask, _mark_outside_board_blocked
from .raster import _draw_line_blocked, _mark_circular_zone, disc_cells

from .obstacle_costs import (BLOCKED, COURTYARD, EDGE_KEEPOUT, FANOUT_HALO_INNER, FANOUT_HALO_OUTER, FANOUT_HALO_STEEP, FREE, HIGH_COST, POUR, THT_EXCLUSION)



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
