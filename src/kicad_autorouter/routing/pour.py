"""GND-pour continuity: copper masks and island (fragment) detection.

A pour fragments when signal tracks, pads, or vias carve one zone outline
into disconnected copper regions. The router treats zones as pure BLOCKED
obstacles, so it never sees this; this module rebuilds an approximate
filled-pour model per layer and reports connected components ("islands").

Copper model per (net, layer), mirroring KiCad fill semantics loosely:
- zone outlines rasterized, then different-net copper is cut out inflated
  by the zone's own clearance (pour pullback);
- same-net copper (pads as solid discs/rects — thermals affect current
  capacity, not continuity — plus same-net tracks/vias/THT barrels) is
  added back uninflated;
- slivers narrower than the zone's min_thickness are removed (opening),
  mirroring KiCad's sliver removal;
- convex outline corners are NOT rounded like KiCad's smoothing; the
  0.1mm raster overestimates corner copper by ~1 cell, negligible for
  island topology (documented, not fixed).

``(filled_polygon …)`` caches from the file are deliberately unused: they
go stale the moment placement/routing moves copper.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy import ndimage

from ..board_model import BoardModel, Footprint, Pad, is_ground_net
from .grid import RoutingGrid, board_to_grid, grid_to_board
from .obstacles import _fill_polygon, bresenham_cells, stamp_discs


@dataclass(frozen=True)
class PourIsland:
    """One connected copper region of a pour net on one layer."""

    net_name: str
    layer: str
    area_mm2: float
    centroid_mm: Tuple[float, float]
    cell_count: int
    # True when the island touches a same-net pad or via (electrically
    # fed). False ≈ KiCad's "Isolated copper fill" DRC warning.
    has_connection: bool
    zone_uuids: Tuple[str, ...]
    bbox_mm: Tuple[float, float, float, float]


def _cells(radius_mm: float, res_mm: float) -> int:
    return max(0, int(math.ceil(radius_mm / res_mm))) if res_mm > 0 else 0


def _pad_layers(fp: Footprint, pad: Pad) -> List[int]:
    """Copper layer indices of a pad (mirrors obstacles._mark_pad)."""
    if pad.is_through_hole:
        return [0, 1]
    return [0 if fp.layer == "F.Cu" else 1]


def _paint_pad(mask: np.ndarray, grid: RoutingGrid, pad: Pad, inflate_mm: float) -> None:
    """OR pad copper (inflated) into a 2D bool mask. Capsule/rect exact-ish."""
    H, W = mask.shape
    res = grid.resolution_mm
    col, row = board_to_grid(grid, pad.position.x_mm, pad.position.y_mm)
    w, h = pad.size_mm
    ang = math.radians(pad.angle_deg)
    shape = (pad.shape or "").lower()
    if shape in ("circle",):
        r = _cells(max(w, h) / 2.0 + inflate_mm, res)
        # Compiled batch stamper (same cells as the disc_cells loop).
        stamp_discs(mask, [row], [col], [max(r, 1)], True)
        return
    # Rect / roundrect / oval: rotated corners (oval over-covers slightly).
    hw, hh = w / 2.0 + inflate_mm, h / 2.0 + inflate_mm
    ca, sa = math.cos(ang), math.sin(ang)
    corners = []
    for sx, sy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)):
        x = pad.position.x_mm + sx * ca - sy * sa
        y = pad.position.y_mm + sy * ca + sx * sa
        c, r = board_to_grid(grid, x, y)
        corners.append((r, c))
    tmp = np.zeros_like(mask, dtype=np.uint8)
    _fill_polygon(tmp, corners, 1)
    mask |= tmp.astype(bool)
    if shape in ("oval",):
        # capsule ends (both discs in one compiled stamp call)
        dx, dy = ca * (w - h) / 2.0, sa * (w - h) / 2.0
        r_end = _cells(min(w, h) / 2.0 + inflate_mm, res)
        end_rows, end_cols = [], []
        for ex, ey in (
            (pad.position.x_mm - dx, pad.position.y_mm - dy),
            (pad.position.x_mm + dx, pad.position.y_mm + dy),
        ):
            c, r = board_to_grid(grid, ex, ey)
            end_rows.append(r)
            end_cols.append(c)
        stamp_discs(mask, end_rows, end_cols,
                    [max(r_end, 1)] * len(end_rows), True)


def _paint_track(
    mask: np.ndarray, grid: RoutingGrid,
    x1: float, y1: float, x2: float, y2: float, half_width_mm: float,
) -> None:
    """OR a capsule (segment + width) into a 2D bool mask."""
    H, W = mask.shape
    res = grid.resolution_mm
    rad = _cells(half_width_mm, res)
    c1, r1 = board_to_grid(grid, x1, y1)
    c2, r2 = board_to_grid(grid, x2, y2)
    if rad <= 0:
        for c, r in bresenham_cells(c1, r1, c2, r2):
            if 0 <= r < H and 0 <= c < W:
                mask[r, c] = True
        return
    # One compiled stamp for the whole capsule spine (same cells as the
    # per-step disc_cells loops).
    spine = [(r, c) for c, r in bresenham_cells(c1, r1, c2, r2)]
    if spine:
        stamp_discs(mask, [r for r, _ in spine], [c for _, c in spine],
                    [rad] * len(spine), True)


def _paint_disc(mask: np.ndarray, grid: RoutingGrid, x_mm: float, y_mm: float,
                radius_mm: float) -> None:
    c, r = board_to_grid(grid, x_mm, y_mm)
    # Compiled batch stamper (same cells as the disc_cells loop).
    stamp_discs(mask, [r], [c],
                [max(_cells(radius_mm, grid.resolution_mm), 1)], True)


def pour_copper_mask(
    model: BoardModel, grid: RoutingGrid, net_name: str, layer: str
) -> np.ndarray:
    """Boolean (H, W) copper mask of one pour net on one layer.

    Only zones carrying ``net_name`` seed copper; other nets' zones are
    ignored (no shorting across nets in the model).
    """
    H, W = grid.height_cells, grid.width_cells
    res = grid.resolution_mm
    try:
        layer_idx = grid.layers.index(layer)
    except ValueError:
        return np.zeros((H, W), dtype=bool)

    copper = np.zeros((H, W), dtype=bool)
    min_thick: Optional[float] = None
    for zone in model.zones:
        if (zone.net_name or "") != net_name or layer not in zone.layers:
            continue
        if not zone.polygon or len(zone.polygon) < 3:
            continue
        min_thick = zone.min_thickness_mm if min_thick is None else min(min_thick, zone.min_thickness_mm)
        pts = [(r, c) for (c, r) in (board_to_grid(grid, p.x_mm, p.y_mm) for p in zone.polygon)]
        tmp = np.zeros((H, W), dtype=np.uint8)
        _fill_polygon(tmp, pts, 1)
        zone_mask = tmp.astype(bool)
        copper |= zone_mask
        clr = _cells(zone.clearance_mm, res)

        # Cut different-net copper, inflated by the zone clearance.
        for fp in model.footprints:
            for pad in fp.pads:
                if pad.net_name is None or pad.net_name == net_name:
                    continue
                if layer_idx not in _pad_layers(fp, pad):
                    continue
                cut = np.zeros((H, W), dtype=bool)
                _paint_pad(cut, grid, pad, zone.clearance_mm + max(pad.size_mm) * 0.0)
                copper &= ~cut
        for track in model.tracks:
            if track.net_name is None or track.net_name == net_name:
                continue
            if track.layer != layer:
                continue
            cut = np.zeros((H, W), dtype=bool)
            _paint_track(cut, grid, track.start.x_mm, track.start.y_mm,
                         track.end.x_mm, track.end.y_mm,
                         track.width_mm / 2.0 + zone.clearance_mm)
            copper &= ~cut
        for via in model.vias:
            if via.net_name is not None and via.net_name != net_name:
                cut = np.zeros((H, W), dtype=bool)
                _paint_disc(cut, grid, via.position.x_mm, via.position.y_mm,
                            via.size_mm / 2.0 + zone.clearance_mm)
                copper &= ~cut
        # Drill holes remove copper regardless of net.
        for fp in model.footprints:
            for pad in fp.pads:
                if not pad.is_through_hole or pad.drill_mm <= 0:
                    continue
                cut = np.zeros((H, W), dtype=bool)
                _paint_disc(cut, grid, pad.position.x_mm, pad.position.y_mm,
                            pad.drill_mm / 2.0)
                copper &= ~cut
        for via in model.vias:
            if via.drill_mm > 0:
                cut = np.zeros((H, W), dtype=bool)
                _paint_disc(cut, grid, via.position.x_mm, via.position.y_mm,
                            via.drill_mm / 2.0)
                copper &= ~cut

    # Add same-net copper back (uninflated: it IS the net) — but only on
    # layers carrying a zone of this net. A same-net THT barrel without a
    # pour on that side is just a pad, not a pour island worth stitching.
    zoned = any(
        (zone.net_name or "") == net_name and layer in zone.layers
        and zone.polygon and len(zone.polygon) >= 3
        for zone in model.zones
    )
    if not zoned:
        return copper
    for fp in model.footprints:
        for pad in fp.pads:
            if pad.net_name != net_name:
                continue
            if layer_idx not in _pad_layers(fp, pad):
                continue
            _paint_pad(copper, grid, pad, 0.0)
    for track in model.tracks:
        if track.net_name != net_name or track.layer != layer:
            continue
        _paint_track(copper, grid, track.start.x_mm, track.start.y_mm,
                     track.end.x_mm, track.end.y_mm, track.width_mm / 2.0)
    for via in model.vias:
        if via.net_name is not None and via.net_name != net_name:
            continue
        if via.net_name is None:
            continue
        _paint_disc(copper, grid, via.position.x_mm, via.position.y_mm,
                    via.size_mm / 2.0)

    # Sliver removal (KiCad min_thickness): opening with ~min_thickness/2.
    if min_thick is None:
        min_thick = 0.25
    if min_thick > 0:
        it = _cells(min_thick / 2.0, res)
        if it > 0:
            copper = ndimage.binary_opening(copper, iterations=it)

    return copper


def labeled_copper(
    model: BoardModel, grid: RoutingGrid, net_name: str
) -> Dict[str, Tuple[np.ndarray, np.ndarray, int]]:
    """``(copper_mask, labels, count)`` per layer (4-connectivity).

    Shared core of :func:`pour_islands` and the stitch planner: labels
    are 1-based island ids into ``labels``; ``labels == 0`` is background.
    """
    out: Dict[str, Tuple[np.ndarray, np.ndarray, int]] = {}
    struct = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]])
    for layer in grid.layers:
        copper = pour_copper_mask(model, grid, net_name, layer)
        labeled, n = ndimage.label(copper, structure=struct)
        out[layer] = (copper, labeled, int(n))
    return out


def pour_islands(
    model: BoardModel, grid: RoutingGrid, net_name: str
) -> Dict[str, List[PourIsland]]:
    """Connected copper components of one net per layer (4-connectivity)."""
    res = grid.resolution_mm
    out: Dict[str, List[PourIsland]] = {}
    for layer, (copper, labeled, n) in labeled_copper(model, grid, net_name).items():
        # same-net seed cells for the connection test
        seed = np.zeros_like(copper, dtype=bool)
        layer_idx = grid.layers.index(layer)
        for fp in model.footprints:
            for pad in fp.pads:
                if pad.net_name != net_name:
                    continue
                if layer_idx not in _pad_layers(fp, pad):
                    continue
                _paint_pad(seed, grid, pad, res)
        for via in model.vias:
            if via.net_name != net_name:
                continue
            _paint_disc(seed, grid, via.position.x_mm, via.position.y_mm,
                        via.size_mm / 2.0 + res)
        zone_masks: List[Tuple[str, np.ndarray]] = []
        for zone in model.zones:
            if (zone.net_name or "") != net_name or layer not in zone.layers:
                continue
            if not zone.polygon or len(zone.polygon) < 3:
                continue
            pts = [(r, c) for (c, r) in (board_to_grid(grid, p.x_mm, p.y_mm) for p in zone.polygon)]
            tmp = np.zeros_like(copper, dtype=np.uint8)
            _fill_polygon(tmp, pts, 1)
            zone_masks.append((zone.uuid, tmp.astype(bool)))

        islands: List[PourIsland] = []
        for i in range(1, n + 1):
            comp = labeled == i
            count = int(comp.sum())
            if count == 0:
                continue
            rows, cols = np.nonzero(comp)
            area = count * res * res
            cx = float(cols.mean())
            cy = float(rows.mean())
            bx0, by0 = grid_to_board(grid, int(cols.min()), int(rows.min()))
            bx1, by1 = grid_to_board(grid, int(cols.max()), int(rows.max()))
            gx, gy = grid_to_board(grid, int(round(cx)), int(round(cy)))
            islands.append(PourIsland(
                net_name=net_name,
                layer=layer,
                area_mm2=area,
                centroid_mm=(gx, gy),
                cell_count=count,
                has_connection=bool((comp & seed).any()),
                zone_uuids=tuple(u for u, zm in zone_masks if bool((comp & zm).any())),
                bbox_mm=(min(bx0, bx1), min(by0, by1), max(bx0, bx1), max(by0, by1)),
            ))
        # Largest first: deterministic order for suggestions/consumers.
        islands.sort(key=lambda isl: -isl.cell_count)
        out[layer] = islands
    return out


def ground_nets_with_zones(model: BoardModel) -> List[str]:
    """Ground nets that own at least one zone (analysis candidates)."""
    nets = []
    for zone in model.zones:
        name = zone.net_name or ""
        if name and is_ground_net(name) and name not in nets:
            nets.append(name)
    return nets


def pour_net_names(model: BoardModel) -> List[str]:
    """All nets owning at least one zone outline (any copper pour)."""
    nets = []
    for zone in model.zones:
        name = zone.net_name or ""
        if name and name not in nets:
            nets.append(name)
    return nets


def pour_corridors(
    model: BoardModel, grid: RoutingGrid
) -> Tuple[Dict[str, np.ndarray], float]:
    """Copper-corridor half-width (cells) per layer + max min_thickness.

    For every pour-zone net: copper mask per layer, Euclidean distance
    transform to non-copper = corridor half-width at each cell; the
    element-wise minimum across nets is conservative (narrowest pour
    governs). A track of half-width ``w/2`` with clearance ``c`` severs
    the pour where ``corridor < w/2 + c`` (plus sliver margin) — see
    :func:`severance_cost`. Empty (no zones) → all-zero corridors.
    """
    res = grid.resolution_mm
    corridors: Dict[str, np.ndarray] = {
        layer: np.zeros((grid.height_cells, grid.width_cells), dtype=np.float32)
        for layer in grid.layers
    }
    min_thick = 0.0
    first = True
    for net_name in pour_net_names(model):
        for zone in model.zones:
            if (zone.net_name or "") == net_name:
                min_thick = max(min_thick, zone.min_thickness_mm)
        for layer in grid.layers:
            copper = pour_copper_mask(model, grid, net_name, layer)
            half_width = ndimage.distance_transform_edt(copper)
            if first:
                corridors[layer] = half_width.astype(np.float32)
            else:
                np.minimum(corridors[layer], half_width, out=corridors[layer])
        first = False
    return corridors, min_thick


def severance_cost(
    corridors: Dict[str, np.ndarray],
    min_thickness_mm: float,
    grid: RoutingGrid,
    width_mm: float,
    clearance_mm: float,
    weight: float,
) -> np.ndarray:
    """Soft routing penalty for tracks that would sever a pour (Phase 3).

    Returns an ``(n_layers, H, W)`` int16 grid (same shape as the fanout
    secondary) with ``weight * (1 - corridor/threshold)`` where the copper
    corridor is narrower than ``width/2 + clearance + min_thickness/2``,
    0 elsewhere. Clipped at 399 so stacking onto the fanout secondary
    (max legit soft value 500) can never reach SECONDARY_BLOCKED (1000).
    """
    res = grid.resolution_mm
    n_layers = len(grid.layers)
    H, W = grid.height_cells, grid.width_cells
    out = np.zeros((n_layers, H, W), dtype=np.int16)
    if weight <= 0 or res <= 0:
        return out
    thresh_cells = (width_mm / 2.0 + clearance_mm + min_thickness_mm / 2.0) / res
    if thresh_cells <= 0:
        return out
    for idx, layer in enumerate(grid.layers):
        corr = corridors.get(layer)
        if corr is None:
            continue
        # Only copper cells can be severed: outside the pour the corridor
        # is 0 (no copper), which must NOT toll (it would pave free space).
        narrow = (corr < thresh_cells) & (corr > 0)
        if not narrow.any():
            continue
        penalty = weight * (1.0 - corr[narrow] / thresh_cells)
        out[idx][narrow] = np.minimum(penalty, 399).astype(np.int16)
    return out


def gnd_islands(model: BoardModel, grid: RoutingGrid) -> Dict[str, List[PourIsland]]:
    """Islands for every zoned ground net, keyed by layer (largest first)."""
    out: Dict[str, List[PourIsland]] = {layer: [] for layer in grid.layers}
    for net_name in ground_nets_with_zones(model):
        for layer, islands in pour_islands(model, grid, net_name).items():
            out[layer].extend(islands)
    for layer in out:
        out[layer].sort(key=lambda isl: -isl.cell_count)
    return out
