"""Low-level raster primitives (single owner; split from obstacles.py)."""
from __future__ import annotations
import math
from typing import List, Tuple
import numpy as np
from ._numba import njit
from .grid import board_to_grid
from .obstacle_costs import BLOCKED


def disc_cells(row: int, col: int, radius: int, H: int, W: int):
    """Yield bound-clipped ``(r, c)`` of the filled disc (same scan as the
    vectorized ``_mark_circular_zone``: ``dr²+dc² ≤ r²``)."""
    r2 = radius * radius
    for r in range(max(0, row - radius), min(H, row + radius + 1)):
        dr = r - row
        dc_max = int(math.sqrt(max(0, r2 - dr * dr)))
        for c in range(max(0, col - dc_max), min(W, col + dc_max + 1)):
            yield r, c


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


@njit
def _scanline_fill_nb(grid, er1, ec1, er2, ec2, r_min, r_max, W, value):
    """Scalar scanline fill (numba path).

    Same half-open edge rule as :func:`_fill_polygon_numpy`: an edge
    contributes to row ``r`` iff ``(y1 <= r < y2) or (y2 <= r < y1)``
    (horizontal edges never satisfy it, so no divide-by-zero); the
    intersection column is the exact scalar formula, crossings are
    insertion-sorted (few per row), and pairs fill with max semantics
    and board clipping.
    """
    n = er1.shape[0]
    xs = np.empty(n, dtype=np.float64)
    for r in range(r_min, r_max + 1):
        rf = float(r)
        k = 0
        for i in range(n):
            y1 = er1[i]
            y2 = er2[i]
            if (y1 <= rf and rf < y2) or (y2 <= rf and rf < y1):
                xs[k] = ec1[i] + (rf - y1) / (y2 - y1) * (ec2[i] - ec1[i])
                k += 1
        for a in range(1, k):
            key = xs[a]
            b = a - 1
            while b >= 0 and xs[b] > key:
                xs[b + 1] = xs[b]
                b -= 1
            xs[b + 1] = key
        j = 0
        while j + 1 < k:
            c0 = math.ceil(xs[j])
            c1 = math.floor(xs[j + 1])
            if c0 < 0:
                c0 = 0
            if c1 >= W:
                c1 = W - 1
            if c0 <= c1:
                for c in range(c0, c1 + 1):
                    if value > grid[r, c]:
                        grid[r, c] = value
            j += 2


def _fill_polygon(grid: np.ndarray, points: List[Tuple[int, int]], value: int):
    """Fill polygon using scanline fill (numba kernel or numpy mirror).

    ``points`` are (row, col) cell coordinates (note: ``board_to_grid``
    returns (col, row), so callers must swap). Both paths implement the
    same rule and write bit-identical grids; see ``FILL_POLYGON_USE_NUMBA``.
    """
    if FILL_POLYGON_USE_NUMBA:
        _fill_polygon_numba(grid, points, value)
    else:
        _fill_polygon_numpy(grid, points, value)


def _fill_polygon_numba(grid: np.ndarray, points: List[Tuple[int, int]],
                        value: int):
    """Njit scanline fill: edge setup in numpy, rows in one kernel call."""
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
    _scanline_fill_nb(grid, er1, ec1, er2, ec2, r_min, r_max, W, value)


def _fill_polygon_numpy(grid: np.ndarray, points: List[Tuple[int, int]],
                        value: int):
    """Fill polygon using scanline fill (numpy/Python mirror).

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

FILL_POLYGON_USE_NUMBA = True
