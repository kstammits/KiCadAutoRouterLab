"""Build occupancy/cost grid from board model."""

from __future__ import annotations

import math
from typing import List, Tuple

import numpy as np

from ..board_model import BoardModel, Footprint, Pad, sample_quadratic_bezier
from .grid import RoutingGrid, board_to_grid


# Cell cost values (int8)
FREE = 0
COURTYARD = 25     # placement guide only — not a copper keepout
BLOCKED = 100      # impassable (tracks, vias, zones)
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


def build_occupancy_grid(
    model,
    grid,
    net_class_clearance: dict = None,
    default_clearance_mm: float = 0.2,
) -> np.ndarray:
    """Build occupancy cost grid from board model.

    Returns (n_layers, H, W) int8 array with cost values.
    Higher = less desirable for routing.
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
    for fp in model.footprints:
        for pad in fp.pads:
            _mark_pad(cost_grid, grid, fp, pad, clearance_cells, default_clearance_cells)

    # 3. Existing tracks -> blocked on their layer
    for track in model.tracks:
        _mark_track(cost_grid, grid, track)

    # 4. Vias -> blocked on both layers at location
    for via in model.vias:
        _mark_via(cost_grid, grid, via)

    # 5. Zones (copper pours) -> blocked on their layer
    for zone in model.zones:
        _mark_zone(cost_grid, grid, zone)

    # 6. Edge cuts -> edge keepout zone (using actual Edge.Cuts lines)
    _mark_edge_keepout(cost_grid, grid, model, keepout_mm=0.5)

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
) -> np.ndarray:
    """Build the per-net secondary (fanout) cost grid for ``current_net``.

    Returns a ``(n_layers, H, W)`` int16 grid of soft-shoulder halos over
    every *other* net's pads: ``FANOUT_HALO_INNER`` within pad extent +
    clearance + half track width, ``FANOUT_HALO_OUTER`` for one further
    clearance-width ring. The current net's own pads are excluded so its
    terminals can still escape. All other cells are 0.

    Soft halos are added to the entry cost (never to blockage), so they
    steer around foreign copper without ever trapping a route. The one
    exception is foreign through-hole pads: their inner disc carries
    ``THT_EXCLUSION`` (impassable at ``router.SECONDARY_BLOCKED``), because
    a plated hole with leads plus track width plus manufacturing tolerance
    must be kept clear, not merely tolled. Own-net exclusion still applies,
    so the owner connects and hops for free.
    """
    n_layers = len(grid.layers)
    H, W = grid.height_cells, grid.width_cells
    secondary = np.zeros((n_layers, H, W), dtype=np.int16)
    res_mm = grid.resolution_mm
    if res_mm <= 0:
        return secondary

    outer_extra = pad_halo_radius_cells(0.0, clearance_mm, 0.0, res_mm)
    for fp in model.footprints:
        for pad in fp.pads:
            if not pad.net_name or pad.net_name == current_net:
                continue
            col, row = board_to_grid(grid, pad.position.x_mm, pad.position.y_mm)
            layers = (0, 1) if pad.is_through_hole else (0 if fp.layer == "F.Cu" else 1,)
            inner = pad_halo_radius_cells(
                _pad_half_extent_mm(pad), clearance_mm, track_half_mm, res_mm
            )
            for layer_idx in layers:
                if layer_idx >= n_layers:
                    continue
                _mark_circular_zone(
                    secondary[layer_idx], row, col, inner + outer_extra,
                    FANOUT_HALO_OUTER,
                )
                if pad.is_through_hole:
                    # Hard ring: foreign copper keeps clear of the hole,
                    # annulus, track width and tolerance (own net excluded
                    # above, so its escape + free hop are unaffected).
                    _mark_circular_zone(
                        secondary[layer_idx], row, col, inner, THT_EXCLUSION
                    )
                else:
                    _mark_circular_zone(
                        secondary[layer_idx], row, col, inner, FANOUT_HALO_INNER
                    )
    return secondary


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
    mask = np.zeros((H, W), dtype=bool)
    res_mm = grid.resolution_mm
    if res_mm <= 0:
        return mask

    for fp in model.footprints:
        for pad in fp.pads:
            if pad.is_through_hole:
                continue
            col, row = board_to_grid(grid, pad.position.x_mm, pad.position.y_mm)
            radius = pad_halo_radius_cells(
                _pad_half_extent_mm(pad), clearance_mm, track_half_mm, res_mm
            )
            for (r, c) in disc_cells(row, col, radius, H, W):
                mask[r, c] = True

    # Carve drill holes back out: a pre-drilled hole is a legal hop site
    # even when an adjacent SMD halo overlaps it (THT wins ties).
    for fp in model.footprints:
        for pad in fp.pads:
            if pad.is_through_hole:
                for (c, r) in tht_drill_cells(pad, grid):
                    mask[r, c] = False
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
    """Largest half-extent of a pad (covers rect/circle/trapezoid footprints)."""
    try:
        return max(float(s) for s in pad.size_mm) / 2.0
    except (TypeError, ValueError):
        return 0.0


def pad_halo_radius_cells(
    pad_half_extent_mm: float,
    clearance_mm: float,
    track_half_mm: float,
    res_mm: float,
) -> int:
    """Halo radius in cells covering pad copper + clearance + track half-width.

    Single spelling of the ``max(1, ceil(...))`` formula previously inlined
    at every pad-stamping call site.
    """
    if res_mm <= 0:
        return 1
    return max(1, int(math.ceil(
        (pad_half_extent_mm + clearance_mm + track_half_mm) / res_mm
    )))


def drill_radius_cells(drill_mm: float, res_mm: float) -> int:
    """Drill-hole radius in cells (the copper-free free-hop disc)."""
    if res_mm <= 0 or drill_mm <= 0:
        return 1
    return max(1, int(math.ceil((drill_mm / 2.0) / res_mm)))


def disc_cells(row: int, col: int, radius: int, H: int, W: int):
    """Yield bound-clipped ``(r, c)`` of the filled disc (same scan as the
    vectorized ``_mark_circular_zone``: ``dr²+dc² ≤ r²``)."""
    r2 = radius * radius
    for r in range(max(0, row - radius), min(H, row + radius + 1)):
        dr = r - row
        dc_max = int(math.sqrt(max(0, r2 - dr * dr)))
        for c in range(max(0, col - dc_max), min(W, col + dc_max + 1)):
            yield r, c


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
    """
    x_mm, y_mm = pad.position.x_mm, pad.position.y_mm
    col, row = board_to_grid(grid, x_mm, y_mm)

    # Determine which layers this pad occupies
    is_through_hole = pad.is_through_hole
    pad_layers = [0, 1] if is_through_hole else [0 if fp.layer == "F.Cu" else 1]

    # Get clearance for this pad's net
    net_name = pad.net_name
    clearance = clearance_cells.get(net_name, default_clearance) if net_name else default_clearance

    # Widen the ring to cover the pad copper itself (+ track half width).
    res_mm = grid.resolution_mm
    extra_cells = int(math.ceil(
        (_pad_half_extent_mm(pad) + track_half_mm) / res_mm
    )) if res_mm > 0 else 0
    radius = max(1, clearance + extra_cells)

    for layer_idx in pad_layers:
        if layer_idx < cost_grid.shape[0]:
            _mark_circular_zone(cost_grid[layer_idx], row, col, radius, HIGH_COST)


def _mark_track(cost_grid: np.ndarray, grid, track):
    """Mark track segment as blocked on its layer."""
    layer_idx = 0 if track.layer == "F.Cu" else 1
    if layer_idx >= cost_grid.shape[0]:
        return
    c1, r1 = board_to_grid(grid, track.start.x_mm, track.start.y_mm)
    c2, r2 = board_to_grid(grid, track.end.x_mm, track.end.y_mm)
    _draw_line_blocked(cost_grid[layer_idx], grid, track.start.x_mm, track.start.y_mm, track.end.x_mm, track.end.y_mm)


def _mark_via(cost_grid: np.ndarray, grid, via):
    """Mark via as blocked on all layers it spans."""
    col, row = board_to_grid(grid, via.position.x_mm, via.position.y_mm)
    for layer_idx in range(cost_grid.shape[0]):
        cost_grid[layer_idx, row, col] = max(cost_grid[layer_idx, row, col], BLOCKED)


def _mark_polygon_on_layers(cost_grid: np.ndarray, grid, polygon, layers, value: int) -> None:
    """Fill a board-mm polygon with ``value`` on the named copper layers.

    Shared core of ``_mark_zone`` / ``_mark_keepout``: unknown layers are
    ignored; ``board_to_grid`` returns (col, row) but ``_fill_polygon``
    takes (row, col) — swapped here, once.
    """
    if not polygon or len(polygon) < 3:
        return
    for layer_name in layers:
        layer_idx = 0 if layer_name == "F.Cu" else (1 if layer_name == "B.Cu" else None)
        if layer_idx is None or layer_idx >= cost_grid.shape[0]:
            continue
        points = [
            (row, col)
            for (col, row) in (board_to_grid(grid, p.x_mm, p.y_mm) for p in polygon)
        ]
        _fill_polygon(cost_grid[layer_idx], points, value)


def _mark_zone(cost_grid: np.ndarray, grid, zone):
    """Mark copper zone as blocked on its layers."""
    _mark_polygon_on_layers(cost_grid, grid, zone.polygon, zone.layers, BLOCKED)


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


def _draw_thick_line_keepout(grid: np.ndarray, grid_obj, x1_mm, y1_mm, x2_mm, y2_mm, thickness_cells: int):
    """Draw a thick line (keepout zone) along the line segment."""
    # Use Bresenham to get the center line, then expand by thickness
    c1, r1 = board_to_grid(grid_obj, x1_mm, y1_mm)
    c2, r2 = board_to_grid(grid_obj, x2_mm, y2_mm)

    for c, r in bresenham_cells(c1, r1, c2, r2):
        if 0 <= r < grid.shape[0] and 0 <= c < grid.shape[1]:
            stamp_square(grid, r, c, thickness_cells, EDGE_KEEPOUT)


def _draw_arc_keepout(grid: np.ndarray, grid_obj, sx_mm, sy_mm, mx_mm, my_mm, ex_mm, ey_mm, thickness_cells: int):
    """Draw keepout zone along a quadratic Bezier arc."""
    # Sample points along the Bezier curve
    num_samples = max(10, int(np.hypot(ex_mm - sx_mm, ey_mm - sy_mm) / grid_obj.resolution_mm / 2))

    for x, y in sample_quadratic_bezier(
        (sx_mm, sy_mm), (mx_mm, my_mm), (ex_mm, ey_mm), num_samples
    ):
        c, r = board_to_grid(grid_obj, x, y)

        if 0 <= r < grid.shape[0] and 0 <= c < grid.shape[1]:
            stamp_square(grid, r, c, thickness_cells, EDGE_KEEPOUT)


# --- Drawing primitives ---

def bresenham_cells(c1: int, r1: int, c2: int, r2: int):
    """Yield ``(c, r)`` along the Bresenham line (single shared stepping core).

    Previously triplicated across ``_draw_line_blocked``,
    ``_draw_thick_line_keepout`` and ``pipeline._mark_line_blocked``.
    """
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


def _fill_polygon(grid: np.ndarray, points: List[Tuple[int, int]], value: int):
    """Fill polygon using scanline fill.

    ``points`` are (row, col) cell coordinates (note: ``board_to_grid``
    returns (col, row), so callers must swap).
    """
    if len(points) < 3:
        return
    H, W = grid.shape
    
    # Find y bounds
    rows = [p[0] for p in points]
    r_min = max(0, min(rows))
    r_max = min(H - 1, max(rows))
    
    for r in range(r_min, r_max + 1):
        # Find intersections with scanline
        intersections = []
        for i in range(len(points)):
            p1 = points[i]
            p2 = points[(i + 1) % len(points)]
            r1, c1 = p1
            r2, c2 = p2
            
            if r1 == r2:
                continue  # horizontal edge - skip
            
            # Check if scanline intersects this edge
            if (r1 <= r < r2) or (r2 <= r < r1):
                # Linear interpolation
                t = (r - r1) / (r2 - r1)
                c = c1 + t * (c2 - c1)
                intersections.append(c)
        
        intersections.sort()
        # Fill between pairs
        for i in range(0, len(intersections), 2):
            if i + 1 < len(intersections):
                c_start = max(0, int(np.ceil(intersections[i])))
                c_end = min(W - 1, int(np.floor(intersections[i + 1])))
                if c_start <= c_end:
                    grid[r, c_start:c_end + 1] = np.maximum(grid[r, c_start:c_end + 1], value)