"""Incremental obstacle cache + polygon raster (split from obstacles.py)."""
from __future__ import annotations
import math
from typing import Dict, List, Optional, Set, Tuple
import numpy as np
from ..board_model import ArcTrack, BoardModel, Footprint, Pad, Track, Via, sample_circular_arc
from ..board_model.query import rotate_offset
from ._numba import njit
from .grid import RoutingGrid, board_to_grid, mm_to_cells
from .occupancy import _build_tht_via_mask, build_occupancy_grid, build_via_block_mask
from .marking import _mark_arc, _mark_track, _mark_via
from .pad_geom import track_corridor_radius_cells, via_halo_radius_cells

from .raster import FILL_POLYGON_USE_NUMBA, _fill_polygon, _fill_polygon_numba, _fill_polygon_numpy, _stamp_square_region, bresenham_cells, stamp_square
from .obstacle_costs import (BLOCKED, COURTYARD, EDGE_KEEPOUT, FANOUT_HALO_INNER, FANOUT_HALO_OUTER, FANOUT_HALO_STEEP, FREE, HIGH_COST, POUR, THT_EXCLUSION)



class ObstacleCache:
    """Board obstacle state with incremental copper re-raster.

    Per-call ``build_occupancy_grid`` + THT/via-block mask rebuilds cost
    ~0.3-0.9 s on DCCF-class boards while the footprint/zone/keepout inputs
    rarely change between calls. This cache keeps, per
    ``(grid, clearance, track_half, static fingerprint)``:

    - ``static``: occupancy of everything that is not routed copper
      (courtyards, pads, zones, edge keepout, outside-board, keepouts);
    - ``base``: ``static`` max-stamped with every current copper item
      (track/arc/via), bit-identical to ``build_occupancy_grid`` output;
    - the footprint-only THT via mask and via-block mask;
    - per-item stamp records + spatial bins for dirty-region recompute;
    - via-landing dilation state (see ``router.build_via_ok_cached``).

    ``sync`` returns the live ``base`` array (callers copy it into their
    own grid). All stamps take the max, so stamp order never matters and
    the cached path stays bit-identical to a fresh rebuild.
    """

    def __init__(self, max_entries: int = 2):
        self._entries = []  # [(key, _ObstacleEntry)], MRU-first
        self.max_entries = max(1, int(max_entries))
        self.last_hit = False

    def sync(
        self,
        model,
        grid,
        clearance_mm: float = 0.2,
        track_half_mm: float = 0.125,
    ) -> np.ndarray:
        """Return the base occupancy grid for this model/grid/params.

        Rebuilds from scratch on a key miss (new board, grid, clearance,
        or footprint/zone/outline change); otherwise stamps added copper
        in place and recomputes only the bbox union of removed copper.
        """
        fingerprint = static_fingerprint(model)
        key = (grid, float(clearance_mm), float(track_half_mm), fingerprint)
        entry = self._lookup(key)
        if entry is None:
            entry = _ObstacleEntry(
                grid=grid,
                clearance_mm=clearance_mm,
                track_half_mm=track_half_mm,
                fingerprint=fingerprint,
                model=model,
            )
            self._entries.insert(0, (key, entry))
            del self._entries[self.max_entries:]
            self.last_hit = False
        else:
            self.last_hit = True
            entry.update_copper(model)
        return entry.base

    def masks(self):
        """(tht_via_mask, via_block_mask) of the active (MRU) entry."""
        if not self._entries:
            raise RuntimeError("ObstacleCache.masks() called before sync()")
        entry = self._entries[0][1]
        return entry.tht_via_mask, entry.via_block_mask

    def active_entry(self):
        """The active entry (for router via-ok state). Call after sync."""
        if not self._entries:
            raise RuntimeError(
                "ObstacleCache.active_entry() called before sync()"
            )
        return self._entries[0][1]

    def _lookup(self, key):
        for i, (k, entry) in enumerate(self._entries):
            if k == key:
                self._entries.insert(0, self._entries.pop(i))
                return entry
        return None


class _ObstacleEntry:
    """Mutable per-board raster state owned by :class:`ObstacleCache`."""

    __slots__ = (
        "grid",
        "clearance_mm",
        "track_half_mm",
        "fingerprint",
        "static",
        "base",
        "tht_via_mask",
        "via_block_mask",
        "items",
        "bins",
        "via_ok_memo",
    )

    def __init__(self, grid, clearance_mm, track_half_mm, fingerprint, model):
        self.grid = grid
        # Quantize like build_occupancy_grid (ceil to cells * res) so
        # copper stamps match a fresh rebuild exactly. At coarse grids a
        # fractional clearance (e.g. 0.21 at 0.2 res) otherwise stamps a
        # smaller halo than fresh (0.4), breaking cache parity.
        res = getattr(grid, "resolution_mm", 0.0) or 0.0
        try:
            cells = int(math.ceil(float(clearance_mm) / res)) if res > 0 else 1
            quantized = max(1, cells) * res if res > 0 else float(clearance_mm)
        except (TypeError, ValueError):
            quantized = clearance_mm
        self.clearance_mm = quantized
        self.track_half_mm = track_half_mm
        self.fingerprint = fingerprint
        self.static = build_occupancy_grid(
            model, grid,
            default_clearance_mm=clearance_mm,
            skip_copper=True,
        )
        self.base = self.static.copy()
        self.tht_via_mask = _build_tht_via_mask(model, grid)
        self.via_block_mask = build_via_block_mask(
            model, grid,
            clearance_mm=clearance_mm,
            track_half_mm=track_half_mm,
        )
        self.items = {}  # copper item -> (r0, r1, c0, c1) stamp bbox
        self.bins = {}  # (bin_r, bin_c) -> set of copper items
        self.via_ok_memo = {}  # (base digest, sec digest, radius) -> via_ok
        for item in _copper_items(model):
            self._add_item(item)

    def update_copper(self, model) -> None:
        """Sync ``base`` with the model's current copper (add/remove)."""
        copper = _copper_items(model)
        for item in copper:
            if item not in self.items:
                self._add_item(item)
        removed = [item for item in self.items if item not in copper]
        if not removed:
            return
        for item in removed:
            del self.items[item]
        self._recompute_removed(removed)

    def _add_item(self, item) -> None:
        rec = _copper_bbox(item, self.grid, self.clearance_mm)
        if rec is None:
            return
        r0, r1, c0, c1, _layers = rec
        _stamp_copper_full(self.base, self.grid, item, self.clearance_mm)
        self.items[item] = (r0, r1, c0, c1)
        for br in range(r0 // _CACHE_BIN_CELLS, r1 // _CACHE_BIN_CELLS + 1):
            for bc in range(c0 // _CACHE_BIN_CELLS, c1 // _CACHE_BIN_CELLS + 1):
                self.bins.setdefault((br, bc), set()).add(item)

    def _recompute_removed(self, removed) -> None:
        """Recompute ``base`` over the union bbox of removed stamps.

        Scratch starts from ``static``; only items overlapping the region
        re-stamp it. Removal regions are net-local, so this stays cheap;
        board-wide removals (GND strip) fall back to a full recomposite.
        """
        boxes = [self._bbox_of(item) for item in removed]
        r0 = min(box[0] for box in boxes)
        r1 = max(box[1] for box in boxes)
        c0 = min(box[2] for box in boxes)
        c1 = max(box[3] for box in boxes)
        H, W = self.grid.height_cells, self.grid.width_cells
        if (r1 - r0 + 1) * (c1 - c0 + 1) > _CACHE_FULL_RECOMPUTE_FRAC * H * W:
            self.base[:] = self.static
            for item in self.items:
                _stamp_copper_full(
                    self.base, self.grid, item, self.clearance_mm
                )
            self._rebuild_bins()
            return
        scratch = self.static[:, r0:r1 + 1, c0:c1 + 1].copy()
        seen = set()
        touched = set()
        for br in range(r0 // _CACHE_BIN_CELLS, r1 // _CACHE_BIN_CELLS + 1):
            for bc in range(c0 // _CACHE_BIN_CELLS, c1 // _CACHE_BIN_CELLS + 1):
                touched.add((br, bc))
                for item in self.bins.get((br, bc), ()):
                    if item in self.items and item not in seen:
                        seen.add(item)
                        _stamp_copper_region(
                            scratch, self.grid, item,
                            self.clearance_mm, r0, c0,
                        )
        self.base[:, r0:r1 + 1, c0:c1 + 1] = scratch
        # Drop stale (removed-item) refs in the touched bins.
        for key in touched:
            live = self.bins.get(key)
            if live:
                live = {item for item in live if item in self.items}
                if live:
                    self.bins[key] = live
                else:
                    del self.bins[key]

    def _bbox_of(self, item):
        # Removed items keep no record; recompute the (cheap) analytic box.
        return _copper_bbox(item, self.grid, self.clearance_mm)[:4]

    def _rebuild_bins(self) -> None:
        """Re-index every registered item (after a full recomposite)."""
        self.bins = {}
        for item, (r0, r1, c0, c1) in self.items.items():
            for br in range(r0 // _CACHE_BIN_CELLS, r1 // _CACHE_BIN_CELLS + 1):
                for bc in range(c0 // _CACHE_BIN_CELLS, c1 // _CACHE_BIN_CELLS + 1):
                    self.bins.setdefault((br, bc), set()).add(item)


def static_fingerprint(model) -> int:
    """Hash of every model input that is NOT routed copper.

    Footprints, pads, zones, edge cuts/arcs, keepouts, and board regions
    drive the static raster; tracks/arcs/vias are handled by the copper
    registry instead. All inputs are frozen dataclasses, so this is a few
    milliseconds per call.
    """
    parts = [len(model.footprints)]
    for fp in model.footprints:
        parts.append(hash(fp))
    for coll in (
        model.keepout_zones,
        model.zones,
        model.edge_cuts,
        model.edge_arcs,
    ):
        parts.append(hash(tuple(coll)))
    # Board regions keyed by geometry, not uuid: the uuid is a parse-time
    # artifact regenerated on every load (same board, different uuids).
    for region in model.board_regions:
        parts.append(hash((tuple(region.polygon), region.bbox)))
    return hash(tuple(parts))


def _copper_items(model):
    """Set of current copper items (tracks, arcs, vias)."""
    return set(model.tracks) | set(model.arcs) | set(model.vias)


def _copper_bbox(item, grid, clearance_mm):
    """Superset ``(r0, r1, c0, c1, layers)`` of stamped cells, or None.

    Covers exactly what ``_mark_track``/``_mark_arc``/``_mark_via`` would
    stamp: Bresenham cells stay inside the endpoint box, square stamps add
    the halo radius, arc chords are bounded by their sample points.
    """
    res = grid.resolution_mm
    H, W = grid.height_cells, grid.width_cells
    n_layers = len(grid.layers)
    if isinstance(item, Track):
        layer = grid.layer_to_idx.get(item.layer)
        if layer is None or layer >= n_layers:
            return None
        layers = (layer,)
        radius = track_corridor_radius_cells(
            item.width_mm, clearance_mm, res)
        c1, r1 = board_to_grid(grid, item.start.x_mm, item.start.y_mm)
        c2, r2 = board_to_grid(grid, item.end.x_mm, item.end.y_mm)
        r0 = max(0, min(r1, r2) - radius)
        r1_ = min(H - 1, max(r1, r2) + radius)
        c0 = max(0, min(c1, c2) - radius)
        c1_ = min(W - 1, max(c1, c2) + radius)
        return (r0, r1_, c0, c1_, layers)
    if isinstance(item, Via):
        radius = via_halo_radius_cells(
            item.size_mm, clearance_mm, 0.125, res)
        col, row = board_to_grid(grid, item.position.x_mm, item.position.y_mm)
        if getattr(item, "layers", ()):
            targets = [
                grid.layer_to_idx[name]
                for name in item.layers
                if name in grid.layer_to_idx
            ]
            if not targets:
                return None
            layers = tuple(targets)
        else:
            layers = tuple(range(n_layers))
        return (
            max(0, row - radius), min(H - 1, row + radius),
            max(0, col - radius), min(W - 1, col + radius),
            layers,
        )
    if isinstance(item, ArcTrack):
        if (abs(item.start.x_mm - item.end.x_mm) < 1e-9
                and abs(item.start.y_mm - item.end.y_mm) < 1e-9):
            return None
        layer = grid.layer_to_idx.get(item.layer)
        if layer is None or layer >= n_layers:
            return None
        radius = track_corridor_radius_cells(
            item.width_mm, clearance_mm, res)
        pts = sample_circular_arc(
            (item.start.x_mm, item.start.y_mm),
            (item.mid.x_mm, item.mid.y_mm),
            (item.end.x_mm, item.end.y_mm),
            16,
        )
        cells = [board_to_grid(grid, x, y) for x, y in pts]
        rows = [r for _c, r in cells]
        cols = [c for c, _r in cells]
        return (
            max(0, min(rows) - radius), min(H - 1, max(rows) + radius),
            max(0, min(cols) - radius), min(W - 1, max(cols) + radius),
            (layer,),
        )
    return None


def _stamp_copper_full(layer_grid, grid, item, clearance_mm) -> None:
    """Stamp one copper item on a full-shape grid (same as a fresh build)."""
    if isinstance(item, Track):
        _mark_track(layer_grid, grid, item, clearance_mm)
    elif isinstance(item, ArcTrack):
        _mark_arc(layer_grid, grid, item, clearance_mm)
    elif isinstance(item, Via):
        _mark_via(layer_grid, grid, item, clearance_mm=clearance_mm,
                  track_half_mm=0.125)




def _stamp_copper_region(tile, grid, item, clearance_mm, ro, co) -> None:
    """Region-local twin of the ``_mark_*`` copper stampers.

    Stamps the same cells as :func:`_stamp_copper_full` restricted to the
    ``tile`` window (absolute origin ``ro``, ``co``); geometry and radii
    match the full-grid variants so recompute regions stay exact.
    """
    res = grid.resolution_mm
    H, W = grid.height_cells, grid.width_cells
    n_layers = len(grid.layers)
    if isinstance(item, Track):
        layer = grid.layer_to_idx.get(item.layer)
        if layer is None or layer >= n_layers:
            return
        radius = track_corridor_radius_cells(
            item.width_mm, clearance_mm, res)
        c1, r1 = board_to_grid(grid, item.start.x_mm, item.start.y_mm)
        c2, r2 = board_to_grid(grid, item.end.x_mm, item.end.y_mm)
        layer_grid = tile[layer]
        for c, r in bresenham_cells(c1, r1, c2, r2):
            if 0 <= r < H and 0 <= c < W:
                if radius:
                    _stamp_square_region(
                        layer_grid, r, c, radius, BLOCKED, ro, co)
                elif ro <= r < ro + layer_grid.shape[0] and co <= c < co + layer_grid.shape[1]:
                    layer_grid[r - ro, c - co] = BLOCKED
        return
    if isinstance(item, Via):
        radius = via_halo_radius_cells(
            item.size_mm, clearance_mm, 0.125, res)
        col, row = board_to_grid(grid, item.position.x_mm, item.position.y_mm)
        if getattr(item, "layers", ()):
            targets = [
                grid.layer_to_idx[name]
                for name in item.layers
                if name in grid.layer_to_idx
            ]
            if not targets:
                return
        else:
            targets = list(range(n_layers))
        for layer in targets:
            _stamp_square_region(tile[layer], row, col, radius, BLOCKED, ro, co)
        return
    if isinstance(item, ArcTrack):
        if (abs(item.start.x_mm - item.end.x_mm) < 1e-9
                and abs(item.start.y_mm - item.end.y_mm) < 1e-9):
            return
        layer = grid.layer_to_idx.get(item.layer)
        if layer is None or layer >= n_layers:
            return
        radius = track_corridor_radius_cells(
            item.width_mm, clearance_mm, res)
        # sample_circular_arc is a generator; materialize before subscripting.
        pts = list(sample_circular_arc(
            (item.start.x_mm, item.start.y_mm),
            (item.mid.x_mm, item.mid.y_mm),
            (item.end.x_mm, item.end.y_mm),
            16,
        ))
        layer_grid = tile[layer]
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            c1, r1 = board_to_grid(grid, x1, y1)
            c2, r2 = board_to_grid(grid, x2, y2)
            for c, r in bresenham_cells(c1, r1, c2, r2):
                if 0 <= r < H and 0 <= c < W:
                    if radius:
                        _stamp_square_region(
                            layer_grid, r, c, radius, BLOCKED, ro, co)
                    elif ro <= r < ro + layer_grid.shape[0] and co <= c < co + layer_grid.shape[1]:
                        layer_grid[r - ro, c - co] = BLOCKED









_CACHE_BIN_CELLS = 64
_CACHE_FULL_RECOMPUTE_FRAC = 0.4

