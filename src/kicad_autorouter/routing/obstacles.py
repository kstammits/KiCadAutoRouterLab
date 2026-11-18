"""Occupancy/cost grid (split for editor search).

Canonical code lives in occupancy/pad_geom/marking/obstacle_cache;
this module re-exports for existing importers.
"""
from __future__ import annotations
from .occupancy import (
    build_occupancy_grid, build_fanout_cost, own_net_hard_cells,
    tht_drill_cells, build_via_block_mask, _build_tht_via_mask, _block_courtyard,
)
from .pad_geom import (
    _pad_half_extent_mm, pad_halo_radius_cells, drill_radius_cells,
    via_halo_radius_cells, track_corridor_radius_cells,
    _pad_spine_mm, _pad_rect_corners_mm, _stamp_rect_zone, _stamp_pad_zone,
    _stamp_capsule_zone,
)
from .raster import (
    bresenham_cells, disc_cells, stamp_discs, stamp_square,
    _draw_line_blocked, _mark_circular_zone, _scanline_fill_nb,
    _stamp_discs_nb, _stamp_square_region, _fill_polygon,
    _fill_polygon_numba, _fill_polygon_numpy,
)
from .marking import (
    _mark_pad, _mark_track, _mark_arc, _mark_via, _mark_polygon_on_layers,
    _mark_zone, _mark_keepout, _mark_edge_keepout, inside_board_mask,
    _mark_outside_board_blocked, _draw_thick_line_keepout, _draw_arc_keepout,
)
from .raster import FILL_POLYGON_USE_NUMBA
from .obstacle_cache import (
    ObstacleCache, _ObstacleEntry, static_fingerprint, _copper_items,
    _copper_bbox, _stamp_copper_full,
    _stamp_copper_region,
)
from .obstacle_costs import (BLOCKED, COURTYARD, EDGE_KEEPOUT, FANOUT_HALO_INNER, FANOUT_HALO_OUTER, FANOUT_HALO_STEEP, FREE, HIGH_COST, POUR, THT_EXCLUSION)
