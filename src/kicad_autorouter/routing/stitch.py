"""Automatic GND continuity repair (Phase 2): stitching vias + bridge tracks.

Two mechanisms, both region-aware (tube111-style panels share one file
but are separate boards — copper must never join across regions):

- **Stitching vias** wherever an island overlaps same-net copper on the
  opposite layer (the 2-layer workhorse: pour over pour + via).
- **Bridge tracks** where no overlap exists: a short same-net track on
  one layer between nearby islands, routed with full clearance.

Same-layer gaps are never "bridged" by drawing over the gap filler: the
gap IS foreign copper + clearances, so a GND track across it would short.
Bridges route around through free space with A*.

Deterministic: groups ordered fed-first by size; raster-order argmax for
sites; bounded budgets.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from typing import Dict, List, Set, Tuple

import numpy as np
from scipy import ndimage

from ..board_model import BoardModel, Point, Track, Via
from .grid import RoutingGrid, board_to_grid, grid_to_board
from .obstacles import _fill_polygon, build_via_block_mask
from .pour import (
    _pad_layers,
    _paint_disc,
    _paint_pad,
    _paint_track,
    ground_nets_with_zones,
    labeled_copper,
)


@dataclass
class StitchPlan:
    """Continuity repair for the routed board."""

    vias: List[Via]
    tracks: List[Track]
    islands_before: int
    # Groups left unjoined (no valid site/path or over budget).
    islands_after: int
    nets: Tuple[str, ...] = ()


def _cells(radius_mm: float, res_mm: float) -> int:
    return max(0, int(math.ceil(radius_mm / res_mm))) if res_mm > 0 else 0


def _drill_mask(model: BoardModel, grid: RoutingGrid) -> np.ndarray:
    """Union of all drill-hole discs (any net) as (H, W) bool."""
    H, W = grid.height_cells, grid.width_cells
    mask = np.zeros((H, W), dtype=bool)
    for fp in model.footprints:
        for pad in fp.pads:
            if pad.is_through_hole and pad.drill_mm > 0:
                _paint_disc(mask, grid, pad.position.x_mm, pad.position.y_mm,
                            pad.drill_mm / 2.0)
    for via in model.vias:
        if via.drill_mm > 0:
            _paint_disc(mask, grid, via.position.x_mm, via.position.y_mm,
                        via.drill_mm / 2.0)
    return mask


def _foreign_mask(
    model: BoardModel, grid: RoutingGrid, net_name: str, layer: str,
    clearance_mm: float, include_drills: bool = True,
) -> np.ndarray:
    """Different-net copper inflated by clearance (plus drill holes)."""
    H, W = grid.height_cells, grid.width_cells
    try:
        layer_idx = grid.layers.index(layer)
    except ValueError:
        return np.zeros((H, W), dtype=bool)
    mask = np.zeros((H, W), dtype=bool)
    for fp in model.footprints:
        for pad in fp.pads:
            if pad.net_name is None or pad.net_name == net_name:
                continue
            if layer_idx not in _pad_layers(fp, pad):
                continue
            _paint_pad(mask, grid, pad, clearance_mm)
    for track in model.tracks:
        if track.net_name is None or track.net_name == net_name:
            continue
        if track.layer != layer:
            continue
        _paint_track(mask, grid, track.start.x_mm, track.start.y_mm,
                     track.end.x_mm, track.end.y_mm,
                     track.width_mm / 2.0 + clearance_mm)
    for via in model.vias:
        if via.net_name is not None and via.net_name != net_name:
            _paint_disc(mask, grid, via.position.x_mm, via.position.y_mm,
                        via.size_mm / 2.0 + clearance_mm)
    if include_drills:
        mask |= _drill_mask(model, grid)
    return mask


def _track_keepout_mask(model: BoardModel, grid: RoutingGrid, layer: str) -> np.ndarray:
    """Keepout polygons forbidding tracks, rasterized for one layer."""
    H, W = grid.height_cells, grid.width_cells
    mask = np.zeros((H, W), dtype=bool)
    for ko in model.keepout_zones:
        if ko.tracks_allowed or layer not in ko.layers:
            continue
        if not ko.polygon or len(ko.polygon) < 3:
            continue
        pts = [(r, c) for (c, r) in
               (board_to_grid(grid, p.x_mm, p.y_mm) for p in ko.polygon)]
        tmp = np.zeros((H, W), dtype=np.uint8)
        _fill_polygon(tmp, pts, 1)
        mask |= tmp.astype(bool)
    return mask


def _point_in_keepout_no_via(model: BoardModel, x_mm: float, y_mm: float, layer: str) -> bool:
    """True when a via at (x, y) would violate a keepout's via policy."""
    from shapely.geometry import Point as ShapelyPoint, Polygon

    pt = ShapelyPoint(x_mm, y_mm)
    for ko in model.keepout_zones:
        if ko.vias_allowed or layer not in ko.layers:
            continue
        if not ko.polygon or len(ko.polygon) < 3:
            continue
        try:
            poly = Polygon([(p.x_mm, p.y_mm) for p in ko.polygon])
        except Exception:
            continue
        if not poly.is_valid or poly.is_empty:
            continue
        if poly.contains(pt) or poly.touches(pt):
            return True
    return False


def _region_masks(model: BoardModel, grid: RoutingGrid) -> List[np.ndarray]:
    """One bool mask per board region (whole grid when no regions)."""
    H, W = grid.height_cells, grid.width_cells
    if not model.board_regions:
        return [np.ones((H, W), dtype=bool)]
    masks = []
    for region in model.board_regions:
        mask = np.zeros((H, W), dtype=bool)
        if region.polygon and len(region.polygon) >= 3:
            pts = [(r, c) for (c, r) in
                   (board_to_grid(grid, p.x_mm, p.y_mm) for p in region.polygon)]
            tmp = np.zeros((H, W), dtype=np.uint8)
            _fill_polygon(tmp, pts, 1)
            mask = tmp.astype(bool)
        else:
            min_x, max_x, min_y, max_y = region.bbox
            c0, r0 = board_to_grid(grid, min_x, min_y)
            c1, r1 = board_to_grid(grid, max_x, max_y)
            mask[max(0, r0):r1 + 1, max(0, c0):c1 + 1] = True
        masks.append(mask)
    return masks


class _UnionFind:
    def __init__(self) -> None:
        self.parent: Dict[Tuple[str, int], Tuple[str, int]] = {}

    def find(self, x: Tuple[str, int]) -> Tuple[str, int]:
        parent = self.parent
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(self, a: Tuple[str, int], b: Tuple[str, int]) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            if rb < ra:
                ra, rb = rb, ra
            self.parent[rb] = ra


def _barrel_cells(model: BoardModel, grid: RoutingGrid, net_name: str) -> np.ndarray:
    """Cells of same-net plated barrels (THT pads + vias) as (H, W) bool."""
    H, W = grid.height_cells, grid.width_cells
    mask = np.zeros((H, W), dtype=bool)
    for fp in model.footprints:
        for pad in fp.pads:
            if pad.net_name != net_name or not pad.is_through_hole:
                continue
            _paint_disc(mask, grid, pad.position.x_mm, pad.position.y_mm,
                        max(pad.drill_mm / 2.0, 0.2))
    for via in model.vias:
        if via.net_name != net_name:
            continue
        _paint_disc(mask, grid, via.position.x_mm, via.position.y_mm,
                    max(via.drill_mm / 2.0, 0.2))
    return mask


def _astar_path(free: np.ndarray, start: Tuple[int, int],
                goal_mask: np.ndarray) -> List[Tuple[int, int]]:
    """8-connectivity A* from start cell to any goal cell. Empty if none."""
    H, W = free.shape
    sr, sc = start
    if not (0 <= sr < H and 0 <= sc < W) or not free[sr, sc]:
        return []
    if goal_mask[sr, sc]:
        return [(sr, sc)]
    goals_r, goals_c = np.nonzero(goal_mask)
    if len(goals_r) == 0:
        return []
    # Heuristic: Chebyshev distance to the goal bbox (admissible).
    gr0, gr1, gc0, gc1 = goals_r.min(), goals_r.max(), goals_c.min(), goals_c.max()

    def heur(r: int, c: int) -> float:
        return max(gr0 - r, r - gr1, gc0 - c, c - gc1, 0)

    DIRS = ((-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
            (-1, -1, 1.4142), (-1, 1, 1.4142), (1, -1, 1.4142), (1, 1, 1.4142))
    g_score = {start: 0.0}
    came: Dict[Tuple[int, int], Tuple[int, int]] = {}
    heap: List[Tuple[float, Tuple[int, int]]] = [(heur(sr, sc), start)]
    closed: Set[Tuple[int, int]] = set()
    while heap:
        _, cur = heapq.heappop(heap)
        if cur in closed:
            continue
        closed.add(cur)
        if goal_mask[cur]:
            path = [cur]
            while path[-1] in came:
                path.append(came[path[-1]])
            path.reverse()
            return path
        r, c = cur
        for dr, dc, cost in DIRS:
            nr, nc = r + dr, c + dc
            if not (0 <= nr < H and 0 <= nc < W) or not free[nr, nc]:
                continue
            nxt = (nr, nc)
            if nxt in closed:
                continue
            g = g_score[cur] + cost
            if g < g_score.get(nxt, math.inf):
                g_score[nxt] = g
                came[nxt] = cur
                heapq.heappush(heap, (g + heur(nr, nc), nxt))
    return []


def _cells_to_tracks(
    grid: RoutingGrid, path: List[Tuple[int, int]], layer: str,
    width_mm: float, net_name: str,
) -> List[Track]:
    """Cell path → collinear-merged board-space tracks."""
    if len(path) < 2:
        return []
    pts = [grid_to_board(grid, c, r) for (r, c) in path]
    # Greedy collinear merge over the 8 grid directions.
    runs: List[List[Tuple[float, float]]] = [[pts[0], pts[1]]]
    for p in pts[2:]:
        (x0, y0), (x1, y1) = runs[-1][-2], runs[-1][-1]
        d1 = (round(x1 - x0, 9), round(y1 - y0, 9))
        d2 = (round(p[0] - x1, 9), round(p[1] - y1, 9))
        if d1 == d2:
            runs[-1].append(p)
        else:
            runs.append([(x1, y1), p])
    tracks = []
    for run in runs:
        if len(run) < 2:
            continue
        (x0, y0), (x1, y1) = run[0], run[-1]
        if x0 == x1 and y0 == y1:
            continue
        tracks.append(Track(
            start=Point(x0, y0), end=Point(x1, y1),
            width_mm=width_mm, layer=layer, net_name=net_name,
        ))
    return tracks


def plan_stitches(
    model: BoardModel,
    grid: RoutingGrid,
    size_mm: float = 0.8,
    drill_mm: float = 0.4,
    clearance_mm: float = 0.2,
    bridge_width_mm: float = 0.5,
    max_vias: int = 32,
    max_bridges: int = 16,
    max_bridge_mm: float = 30.0,
) -> StitchPlan:
    """Join pour islands within each board region (never across regions).

    Per zoned ground net: union-find islands linked by existing same-net
    barrels, then per region join every group into the largest fed group —
    first by overlap via, else by a clearance-respecting bridge track.
    Via sites must clear foreign copper by ``size/2 + clearance``,
    existing drills, the SMD via-block mask, and via-forbidding keepouts.
    Bridge tracks route on one layer through cells clear of foreign copper
    by ``width/2 + clearance``. Deterministic; bounded budgets (fed
    groups first, then larger groups).
    """
    res = grid.resolution_mm
    if res <= 0 or (max_vias <= 0 and max_bridges <= 0):
        return StitchPlan(vias=[], tracks=[], islands_before=0, islands_after=0)

    layers = list(grid.layers)
    if len(layers) < 2:
        return StitchPlan(vias=[], tracks=[], islands_before=0, islands_after=0)

    via_block = build_via_block_mask(
        model, grid, clearance_mm=clearance_mm,
        track_half_mm=size_mm / 2.0,
    )
    drills = _drill_mask(model, grid)
    drill_keepout = ndimage.binary_dilation(
        drills, iterations=max(1, int(math.ceil((drill_mm / 2.0 + 0.2) / res)))
    )
    regions = _region_masks(model, grid)
    keepout_tracks = {layer: _track_keepout_mask(model, grid, layer) for layer in layers}

    all_vias: List[Via] = []
    all_tracks: List[Track] = []
    before = 0
    unjoined = 0
    nets_done: List[str] = []
    planned_cells: Set[Tuple[int, int]] = set()
    min_spacing = max(1, int(math.ceil(size_mm * 2.0 / res)))

    for net_name in ground_nets_with_zones(model):
        if len(all_vias) >= max_vias and len(all_tracks) >= max_bridges:
            break
        labels = labeled_copper(model, grid, net_name)
        copper = {layer: labels[layer][0] for layer in layers}
        lab = {layer: labels[layer][1] for layer in layers}
        counts = {layer: labels[layer][2] for layer in layers}
        if sum(counts.values()) == 0:
            continue
        before += sum(counts.values())
        nets_done.append(net_name)

        # Island -> region by majority of cells.
        island_region: Dict[Tuple[str, int], int] = {}
        for layer in layers:
            for i in range(1, counts[layer] + 1):
                comp = (lab[layer] == i) & copper[layer]
                votes = [int((comp & rm).sum()) for rm in regions]
                island_region[(layer, i)] = int(np.argmax(votes))

        # Same-net seeds per layer (connection test).
        seed = {layer: np.zeros_like(copper[layer], dtype=bool) for layer in layers}
        for layer in layers:
            layer_idx = grid.layers.index(layer)
            for fp in model.footprints:
                for pad in fp.pads:
                    if pad.net_name != net_name:
                        continue
                    if layer_idx not in _pad_layers(fp, pad):
                        continue
                    _paint_pad(seed[layer], grid, pad, res)
            for via in model.vias:
                if via.net_name != net_name:
                    continue
                _paint_disc(seed[layer], grid, via.position.x_mm, via.position.y_mm,
                            via.size_mm / 2.0 + res)

        # Union-find islands sharing a barrel cell on both layers.
        uf = _UnionFind()
        nodes = [(layer, i) for layer in layers for i in range(1, counts[layer] + 1)]
        b_rows, b_cols = np.nonzero(_barrel_cells(model, grid, net_name))
        for r, c in zip(b_rows.tolist(), b_cols.tolist()):
            hit = [(layer, int(lab[layer][r, c])) for layer in layers
                   if int(lab[layer][r, c]) > 0]
            for k in range(len(hit)):
                for other in hit[k + 1:]:
                    uf.union(hit[k], other)
        groups: Dict[Tuple[str, int], List[Tuple[str, int]]] = {}
        for node in nodes:
            groups.setdefault(uf.find(node), []).append(node)

        def group_cells(members: List[Tuple[str, int]]) -> int:
            return sum(int(((lab[layer] == i) & copper[layer]).sum())
                       for layer, i in members)

        def group_fed(members: List[Tuple[str, int]]) -> bool:
            return any(bool((((lab[layer] == i) & copper[layer]) & seed[layer]).any())
                       for layer, i in members)

        # Partition groups per region; join within each region only.
        by_region: Dict[int, List[List[Tuple[str, int]]]] = {}
        for members in groups.values():
            by_region.setdefault(island_region[members[0]], []).append(members)

        foreign = {layer: _foreign_mask(model, grid, net_name, layer, 0.0,
                                            include_drills=False)
                   for layer in layers}
        annulus = max(1, int(math.ceil((size_mm / 2.0 + clearance_mm) / res)))
        foreign_dil = {layer: ndimage.binary_dilation(foreign[layer], iterations=annulus)
                       for layer in layers}
        clear = {layer: copper[layer] & ~foreign_dil[layer] for layer in layers}
        dt = {layer: ndimage.distance_transform_edt(~foreign[layer]) for layer in layers}
        # grown: joined-network copper including placed bridge tracks, so
        # later groups chain through earlier repairs.
        grown = {layer: copper[layer].copy() for layer in layers}
        bridge_half = max(1, int(math.ceil((bridge_width_mm / 2.0 + clearance_mm) / res)))
        bridge_free = {
            layer: ~(ndimage.binary_dilation(foreign[layer], iterations=bridge_half))
            & ~ndimage.binary_dilation(drills, iterations=bridge_half)
            & ~keepout_tracks[layer]
            for layer in layers
        }
        max_bridge_cells = max_bridge_mm / res if res > 0 else 0

        # Hoisted: binary erosion of copper is invariant per net (copper never
        # changes during stitch; only `grown` is updated). Compute once per net.
        copper_eroded = {layer: ndimage.binary_erosion(copper[layer])
                         for layer in layers}

        for region_idx in sorted(by_region):
            region_groups = sorted(
                by_region[region_idx],
                key=lambda m: (not group_fed(m), -group_cells(m)),
            )
            if not region_groups:
                continue
            joined: Set[Tuple[str, int]] = set(region_groups[0])
            remaining = list(region_groups[1:])

            # Incremental joined copper masks: updated when groups join.
            joined_copper = {layer: np.zeros_like(copper[layer], dtype=bool)
                             for layer in layers}
            # Seed with the initially joined group (region_groups[0]).
            for (ly, i) in joined:
                joined_copper[ly] |= (lab[ly] == i) & copper[ly]

            def joined_mask(layer: str) -> np.ndarray:
                # Joined copper + bridge tracks grown since last join.
                return joined_copper[layer] | (grown[layer] & ~copper[layer])

            absorbed: Set[int] = set()
            for idx, group in enumerate(remaining):
                if idx in absorbed:
                    continue
                if len(all_vias) >= max_vias and len(all_tracks) >= max_bridges:
                    unjoined += sum(1 for j in range(idx, len(remaining)) if j not in absorbed)
                    break
                members_F = [(ly, i) for (ly, i) in group if ly == layers[0]]
                members_B = [(ly, i) for (ly, i) in group if ly == layers[1]]

                placed = False
                # --- 1. Overlap via (either orientation) ---
                if len(all_vias) < max_vias:
                    gF = np.zeros_like(clear[layers[0]], dtype=bool)
                    gB = np.zeros_like(clear[layers[1]], dtype=bool)
                    for (ly, i) in members_F:
                        gF |= (lab[ly] == i) & copper[ly]
                    for (ly, i) in members_B:
                        gB |= (lab[ly] == i) & copper[ly]
                    jF, jB = joined_mask(layers[0]), joined_mask(layers[1])
                    overlap = ((gF & jB) | (gB & jF)) & clear[layers[0]] & clear[layers[1]]
                    overlap &= ~drill_keepout & ~via_block
                    overlap &= copper_eroded[layers[0]] & copper_eroded[layers[1]]
                    for (pr, pc) in planned_cells:
                        overlap[max(0, pr - min_spacing):pr + min_spacing + 1,
                                max(0, pc - min_spacing):pc + min_spacing + 1] = False
                    rows, cols = np.nonzero(overlap)
                    if len(rows):
                        score = np.minimum(dt[layers[0]][rows, cols],
                                           dt[layers[1]][rows, cols])
                        best = int(np.argmax(score))
                        r, c = int(rows[best]), int(cols[best])
                        x_mm, y_mm = grid_to_board(grid, c, r)
                        if not (
                            _point_in_keepout_no_via(model, x_mm, y_mm, layers[0])
                            or _point_in_keepout_no_via(model, x_mm, y_mm, layers[1])
                        ):
                            all_vias.append(Via(
                                position=Point(x_mm, y_mm),
                                size_mm=size_mm, drill_mm=drill_mm,
                                layers=(layers[0], layers[1]), net_name=net_name,
                            ))
                            planned_cells.add((r, c))
                            joined.update(group)
                            # Update incremental joined copper masks.
                            for (ly, i) in group:
                                joined_copper[ly] |= (lab[ly] == i) & copper[ly]
                            placed = True
                if placed:
                    continue
                # --- 2. Bridge track on one layer ---
                if len(all_tracks) >= max_bridges:
                    unjoined += 1
                    continue
                bridged = False
                for layer in layers:
                    if bridged:
                        break
                    gmask = np.zeros_like(copper[layer], dtype=bool)
                    for (ly, i) in group:
                        if ly == layer:
                            gmask |= (lab[ly] == i) & copper[ly]
                    if not gmask.any():
                        continue
                    jmask = joined_mask(layer)
                    if not jmask.any():
                        continue
                    # Nearest FREE group cell to joined copper (cells inside
                    # the annulus cannot host a track centerline).
                    free = bridge_free[layer] & regions[region_idx]
                    dist_to_joined = ndimage.distance_transform_edt(~jmask)
                    startable = gmask & free
                    if not startable.any():
                        continue
                    masked = np.where(startable, dist_to_joined, np.inf)
                    if not np.isfinite(masked).any():
                        continue
                    sr, sc = (int(v) for v in np.unravel_index(int(np.argmin(masked)), masked.shape))
                    if masked[sr, sc] * res > max_bridge_mm:
                        continue
                    # Windowed A*: bound the search to start ± (bridge cap +
                    # margin) so one bridge cannot explore the whole board.
                    H, W = free.shape
                    win = int(max_bridge_mm / res) + _cells(2.0, res) if res > 0 else H
                    r0, r1 = max(0, sr - win), min(H, sr + win + 1)
                    c0, c1 = max(0, sc - win), min(W, sc + win + 1)
                    sub = _astar_path(free[r0:r1, c0:c1], (sr - r0, sc - c0),
                                      jmask[r0:r1, c0:c1])
                    path = [(r + r0, c + c0) for (r, c) in sub]
                    if len(path) < 2 or (len(path) - 1) * res > max_bridge_mm:
                        continue
                    # Endpoints must sit on copper (start does by construction;
                    # goal cell is joined copper).
                    new_tracks = _cells_to_tracks(grid, path, layer, bridge_width_mm, net_name)
                    if not new_tracks:
                        continue
                    all_tracks.extend(new_tracks)
                    # Grow joined copper so later groups chain through it.
                    track_cells = np.zeros_like(grown[layer], dtype=bool)
                    for tr in new_tracks:
                        _paint_track(track_cells, grid, tr.start.x_mm, tr.start.y_mm,
                                     tr.end.x_mm, tr.end.y_mm, bridge_width_mm / 2.0)
                    grown[layer] |= track_cells
                    # A bridge crossing another pending island absorbs it.
                    for jdx in range(idx + 1, len(remaining)):
                        if jdx in absorbed:
                            continue
                        touch = any(
                            bool((((lab[ly] == i) & copper[ly]) & track_cells).any())
                            for (ly, i) in remaining[jdx] if ly == layer
                        )
                        if touch:
                            absorbed.add(jdx)
                            joined.update(remaining[jdx])
                            # Update incremental joined copper for absorbed group.
                            for (ly, i) in remaining[jdx]:
                                joined_copper[ly] |= (lab[ly] == i) & copper[ly]
                    joined.update(group)
                    # Update incremental joined copper masks for the joined group.
                    for (ly, i) in group:
                        joined_copper[ly] |= (lab[ly] == i) & copper[ly]
                    bridged = True
                if not bridged:
                    unjoined += 1

            # The surviving joined network counts as one remaining network.
            unjoined += 1

    return StitchPlan(vias=all_vias, tracks=all_tracks, islands_before=before,
                      islands_after=unjoined, nets=tuple(nets_done))
