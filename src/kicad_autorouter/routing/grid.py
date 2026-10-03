"""Routing grid definition and coordinate transforms."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

import numpy as np


@dataclass(frozen=True)
class RoutingGrid:
    """Uniform square grid for routing.

    All coordinates in millimeters unless noted.
    """

    resolution_mm: float = 0.1  # cell size
    layers: Tuple[str, ...] = ("F.Cu", "B.Cu")
    origin_mm: Tuple[float, float] = (0.0, 0.0)  # bottom-left in board coords
    width_cells: int = 0
    height_cells: int = 0

    # Derived
    @property
    def width_mm(self) -> float:
        return self.width_cells * self.resolution_mm

    @property
    def height_mm(self) -> float:
        return self.height_cells * self.resolution_mm

    @property
    def layer_to_idx(self) -> dict[str, int]:
        return {layer: i for i, layer in enumerate(self.layers)}

    def bbox_mm(self) -> Tuple[float, float, float, float]:
        """Return (min_x, min_y, max_x, max_y) in board coordinates."""
        ox, oy = self.origin_mm
        return (ox, oy, ox + self.width_mm, oy + self.height_mm)


def create_grid_from_model(
    model,
    resolution_mm: float = 0.1,
    margin_mm: float = 2.0,
) -> RoutingGrid:
    """Create a routing grid that covers the board outline + margin.

    Uses the board's edge cuts to determine the bounding box.
    """
    if not model.edge_cuts and not model.edge_arcs:
        # Fallback: use footprint extents
        xs, ys = [], []
        for fp in model.footprints:
            for a, b in fp.courtyard:
                xs.extend([a.x_mm, b.x_mm])
                ys.extend([a.y_mm, b.y_mm])
        for pad in fp.pads:
            xs.append(pad.position.x_mm)
            ys.append(pad.position.y_mm)
        min_x, max_x = (min(xs), max(xs)) if xs else (0, 100)
        min_y, max_y = (min(ys), max(ys)) if ys else (0, 100)
    else:
        xs, ys = [], []
        for a, b in model.edge_cuts:
            xs.extend([a.x_mm, b.x_mm])
            ys.extend([a.y_mm, b.y_mm])
        for arc in model.edge_arcs:
            xs.extend([arc.start.x_mm, arc.mid.x_mm, arc.end.x_mm])
            ys.extend([arc.start.y_mm, arc.mid.y_mm, arc.end.y_mm])
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)

    min_x -= margin_mm
    min_y -= margin_mm
    max_x += margin_mm
    max_y += margin_mm

    width_mm = max_x - min_x
    height_mm = max_y - min_y

    width_cells = int(np.ceil(width_mm / resolution_mm))
    height_cells = int(np.ceil(height_mm / resolution_mm))

    return RoutingGrid(
        resolution_mm=resolution_mm,
        layers=("F.Cu", "B.Cu"),
        origin_mm=(min_x, min_y),
        width_cells=width_cells,
        height_cells=height_cells,
    )


# Coordinate transforms

def board_to_grid(grid: RoutingGrid, x_mm: float, y_mm: float) -> Tuple[int, int]:
    """Convert board coordinates (mm) to grid cell indices (col, row).
    
    Board Y increases upward; grid row 0 = top (min_y).
    """
    ox, oy = grid.origin_mm
    col = int((x_mm - ox) / grid.resolution_mm)
    row = int((y_mm - oy) / grid.resolution_mm)
    # Clamp
    col = max(0, min(grid.width_cells - 1, col))
    row = max(0, min(grid.height_cells - 1, row))
    return col, row


def grid_to_board(grid: RoutingGrid, col: int, row: int) -> Tuple[float, float]:
    """Convert grid cell indices to board coordinates (center of cell)."""
    ox, oy = grid.origin_mm
    x_mm = ox + (col + 0.5) * grid.resolution_mm
    y_mm = oy + (row + 0.5) * grid.resolution_mm
    return x_mm, y_mm


def grid_segment_to_board(
    grid: RoutingGrid, path: List[Tuple[int, int, int]]
) -> List[Tuple[float, float, str]]:
    """Convert grid path [(col, row, layer_idx)] to board coords [(x, y, layer_name)]."""
    return [(grid_to_board(grid, c, r)[0], grid_to_board(grid, c, r)[1], grid.layers[li])
            for c, r, li in path]