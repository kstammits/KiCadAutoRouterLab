"""Pad geometry + stamping primitives (split from obstacles.py)."""
from __future__ import annotations
import math
from typing import Dict, List, Optional, Set, Tuple
import numpy as np
from ..board_model import ArcTrack, BoardModel, Footprint, Pad, Track, Via, sample_circular_arc
from ..board_model.query import rotate_offset
from ._numba import njit
from .grid import RoutingGrid, board_to_grid, mm_to_cells

from .raster import _fill_polygon, disc_cells, stamp_discs, _stamp_discs_nb
from .obstacle_costs import (BLOCKED, COURTYARD, EDGE_KEEPOUT, FANOUT_HALO_INNER, FANOUT_HALO_OUTER, FANOUT_HALO_STEEP, FREE, HIGH_COST, POUR, THT_EXCLUSION)



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
    tolerance; foreign copper must keep ``clearance_mm + track_half_mm``
    beyond the annulus edge. Same formula as pads (half-extent +
    clearance + track half), so it delegates.
    """
    return pad_halo_radius_cells(
        via_size_mm / 2.0, clearance_mm, track_half_mm, res_mm)


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
        return rotate_offset(cx, cy, ca, sa, sx, sy)

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





_RECT_PAD_FAMILY = ("rect", "roundrect", "chamfered", "trapezoid", "custom")
