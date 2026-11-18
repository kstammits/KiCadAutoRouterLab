"""Copper/keepout marking onto cost grids (split from obstacles.py)."""
from __future__ import annotations
import math
from typing import Dict, List, Optional, Set, Tuple
import numpy as np
from ..board_model import ArcTrack, BoardModel, Footprint, Pad, Track, Via, sample_circular_arc
from ..board_model.query import rotate_offset
from ._numba import njit
from .grid import RoutingGrid, board_to_grid, mm_to_cells

from .pad_geom import _stamp_capsule_zone, _stamp_pad_zone, _stamp_rect_zone, track_corridor_radius_cells, via_halo_radius_cells
from .raster import bresenham_cells, stamp_square, _draw_line_blocked, _fill_polygon, _mark_circular_zone
from .obstacle_costs import (BLOCKED, COURTYARD, EDGE_KEEPOUT, FANOUT_HALO_INNER, FANOUT_HALO_OUTER, FANOUT_HALO_STEEP, FREE, HIGH_COST, POUR, THT_EXCLUSION)



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








