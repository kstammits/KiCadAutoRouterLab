"""Build occupancy/cost grid from board model."""

from __future__ import annotations

import math
from typing import List, Tuple

import numpy as np

from ..board_model import BoardModel, Footprint, Pad, sample_circular_arc
from .grid import RoutingGrid, board_to_grid

try:
    from numba import njit
except ImportError:  # pragma: no cover - Numba is a hard dependency.

    def njit(*args, **kwargs):
        """No-op fallback when Numba is unavailable (slow but correct)."""
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]

        def wrap(fn):
            return fn

        return wrap


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
# The hard core (THT_EXCLUSION) covers actual copper only; the
# clearance + track-half band pays this instead of blocking, so A* may
# squeeze through a tight thread when no alternative exists. Must stay
# below router.SECONDARY_BLOCKED (1000) — enforced by test.
FANOUT_HALO_STEEP = 800


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
    # Use conservative track half-width (0.125mm = 0.25mm/2) for pad halos
    conservative_track_half_mm = 0.125
    for fp in model.footprints:
        for pad in fp.pads:
            _mark_pad(cost_grid, grid, fp, pad, clearance_cells, default_clearance_cells, conservative_track_half_mm)

    # 3. Existing tracks -> blocked on their layer with width + clearance
    for track in model.tracks:
        clr = clearance_cells.get(track.net_name, default_clearance_cells) if track.net_name else default_clearance_cells
        clearance_mm = clr * grid.resolution_mm
        _mark_track(cost_grid, grid, track, clearance_mm)

    # 3b. Existing track arcs -> sampled polyline, same width+clearance halo.
    for arc in getattr(model, "arcs", ()):
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
    every *other* net's pads: ``THT_EXCLUSION`` on actual copper only,
    ``FANOUT_HALO_STEEP`` over the clearance + half-track margin,
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
    actual copper carries ``THT_EXCLUSION`` (impassable): foreign tracks
    can never touch another net's copper, but they may graze its
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
        outer_extra += max(0, int(math.ceil(extra_margin_mm / res_mm)))
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
            # actual copper only, steep-but-passable toll over the
            # clearance + track-half margin, soft shoulder beyond that.
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
                _stamp_pad_zone(
                    layer, grid, pad, 0.0, THT_EXCLUSION
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
            _mark_circular_zone(
                secondary[layer_idx], row, col, inner, THT_EXCLUSION
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
    if res_mm <= 0:
        return 1
    return max(1, int(math.ceil(
        (via_size_mm / 2.0 + clearance_mm + track_half_mm) / res_mm
    )))


def track_corridor_radius_cells(
    width_mm: float,
    clearance_mm: float,
    res_mm: float,
) -> int:
    """Corridor radius in cells reserving emitted track + clearance halo.

    Covers the track half-width plus the clearance to neighboring copper so
    later nets' tracks and via landings keep DRC distance.
    """
    if res_mm <= 0:
        return 1
    half_width = max(1, int(math.ceil(width_mm / res_mm / 2.0)))
    clearance_radius = max(0, int(math.ceil(
        (clearance_mm + width_mm / 2.0) / res_mm
    )))
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
    """
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