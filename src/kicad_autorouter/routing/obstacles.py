"""Build occupancy/cost grid from board model."""

from __future__ import annotations

from typing import List, Tuple

import numpy as np

from ..board_model import BoardModel, Footprint, Pad
from .grid import RoutingGrid, board_to_grid


# Cell cost values (int8)
FREE = 0
BLOCKED = 100      # impassable (tracks, vias, courtyards, zones)
HIGH_COST = 50     # near pads, clearance zones
EDGE_KEEPOUT = 80  # near board edge
VIA_COST = 50      # per via (5mm equivalent at 0.1mm resolution = 50 cells)


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

    return cost_grid


def _block_courtyard(cost_grid: np.ndarray, grid, fp):
    """Mark footprint courtyard as blocked on both layers."""
    if not fp.courtyard:
        return
    for layer_idx in range(cost_grid.shape[0]):
        for a, b in fp.courtyard:
            _draw_line_blocked(cost_grid[layer_idx], grid, a.x_mm, a.y_mm, b.x_mm, b.y_mm)


def _mark_pad(
    cost_grid: np.ndarray,
    grid,
    fp,
    pad: Pad,
    clearance_cells: dict,
    default_clearance: int,
):
    """Mark pad center and clearance zone as high cost.
    
    Through-hole pads: mark on BOTH layers (free via).
    """
    x_mm, y_mm = pad.position.x_mm, pad.position.y_mm
    col, row = board_to_grid(grid, x_mm, y_mm)
    
    # Determine which layers this pad occupies
    is_through_hole = pad.is_through_hole
    pad_layers = [0, 1] if is_through_hole else [fp.layer == "F.Cu" and 0 or 1]
    
    # Get clearance for this pad's net
    net_name = pad.net_name
    clearance = clearance_cells.get(net_name, default_clearance) if net_name else default_clearance
    
    for layer_idx in pad_layers:
        if layer_idx < cost_grid.shape[0]:
            _mark_circular_zone(cost_grid[layer_idx], row, col, clearance, HIGH_COST)


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


def _mark_zone(cost_grid: np.ndarray, grid, zone):
    """Mark copper zone as blocked on its layers."""
    if not zone.polygon or len(zone.polygon) < 3:
        return
    for layer_name in zone.layers:
        layer_idx = 0 if layer_name == "F.Cu" else (1 if layer_name == "B.Cu" else None)
        if layer_idx is None or layer_idx >= cost_grid.shape[0]:
            continue
        # Convert polygon to grid and fill
        points = [(board_to_grid(grid, p.x_mm, p.y_mm)) for p in zone.polygon]
        _fill_polygon(cost_grid[layer_idx], points, BLOCKED)


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
    
    dx = abs(c2 - c1)
    dy = abs(r2 - r1)
    sx = 1 if c1 < c2 else -1
    sy = 1 if r1 < r2 else -1
    err = dx - dy
    
    c, r = c1, r1
    while True:
        if 0 <= r < grid.shape[0] and 0 <= c < grid.shape[1]:
            # Mark a square of thickness around this point
            r_min = max(0, r - thickness_cells)
            r_max = min(grid.shape[0] - 1, r + thickness_cells)
            c_min = max(0, c - thickness_cells)
            c_max = min(grid.shape[1] - 1, c + thickness_cells)
            grid[r_min:r_max+1, c_min:c_max+1] = np.maximum(
                grid[r_min:r_max+1, c_min:c_max+1], EDGE_KEEPOUT
            )
        if c == c2 and r == r2:
            break
        e2 = 2 * err
        if e2 > -dy:
            err -= dy
            c += sx
        if e2 < dx:
            err += dx
            r += sy


def _draw_arc_keepout(grid: np.ndarray, grid_obj, sx_mm, sy_mm, mx_mm, my_mm, ex_mm, ey_mm, thickness_cells: int):
    """Draw keepout zone along a quadratic Bezier arc."""
    # Sample points along the Bezier curve
    num_samples = max(10, int(np.hypot(ex_mm - sx_mm, ey_mm - sy_mm) / grid_obj.resolution_mm / 2))
    prev_c, prev_r = None, None
    
    for i in range(num_samples + 1):
        t = i / num_samples
        # Quadratic Bezier: B(t) = (1-t)^2 * P0 + 2(1-t)t * P1 + t^2 * P2
        x = (1-t)**2 * sx_mm + 2*(1-t)*t * mx_mm + t**2 * ex_mm
        y = (1-t)**2 * sy_mm + 2*(1-t)*t * my_mm + t**2 * ey_mm
        c, r = board_to_grid(grid_obj, x, y)
        
        if 0 <= r < grid.shape[0] and 0 <= c < grid.shape[1]:
            # Mark thickness around this point
            r_min = max(0, r - thickness_cells)
            r_max = min(grid.shape[0] - 1, r + thickness_cells)
            c_min = max(0, c - thickness_cells)
            c_max = min(grid.shape[1] - 1, c + thickness_cells)
            grid[r_min:r_max+1, c_min:c_max+1] = np.maximum(
                grid[r_min:r_max+1, c_min:c_max+1], EDGE_KEEPOUT
            )


# --- Drawing primitives ---

def _draw_line_blocked(grid: np.ndarray, grid_obj, x1_mm, y1_mm, x2_mm, y2_mm):
    """Draw a line on the grid using Bresenham, marking as blocked."""
    c1, r1 = board_to_grid(grid_obj, x1_mm, y1_mm)
    c2, r2 = board_to_grid(grid_obj, x2_mm, y2_mm)
    
    dx = abs(c2 - c1)
    dy = abs(r2 - r1)
    sx = 1 if c1 < c2 else -1
    sy = 1 if r1 < r2 else -1
    err = dx - dy
    
    c, r = c1, r1
    while True:
        if 0 <= r < grid.shape[0] and 0 <= c < grid.shape[1]:
            grid[r, c] = max(grid[r, c], BLOCKED)
        if c == c2 and r == r2:
            break
        e2 = 2 * err
        if e2 > -dy:
            err -= dy
            c += sx
        if e2 < dx:
            err += dx
            r += sy


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
    """Fill polygon using scanline fill."""
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