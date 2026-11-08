"""Pipeline entry point: route → finish → refine.

Public entry points (:func:`route_nets`, :func:`refine_routing`,
:class:`RoutingResult`); re-exports stage-A/B/C helpers importable from
``kicad_autorouter.routing.pipeline``.

Stage A (raw routing) lives in :mod:`routing.route_raw`, stage B
(stitch/writeback/refill/DRC) in :mod:`routing.finish_routes`, stage C
(order-space refinement) in :mod:`routing.refine`. :class:`RoutingResult` is
defined here *before* the submodule imports so those modules can import it
without a circular-import problem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Collection, Dict, List, Optional, Set, Tuple

import numpy as np

from ..board_model import BoardModel
from .params import (
    MAX_GRID_CELLS_TOTAL,
    RoutingParams,
    _validate_routing_params,
    resolve_clearance,
    resolve_track_width,
)
from .grid import RoutingGrid, create_grid_from_model


@dataclass
class RoutingResult:
    """Complete routing result."""
    tracks_added: int
    vias_added: int
    nets_routed: int
    nets_failed: int
    drc_clean: bool
    drc_violations: int
    drc_result: Optional[object] = None  # Modified PCB tree after routing
    pcb_tree: Optional = None  # Modified PCB tree after routing
    stitch_vias_added: int = 0
    stitch_tracks_added: int = 0
    gnd_islands_before: int = 0
    gnd_islands_after: int = 0
    gnd_stripped: int = 0  # GND vias+tracks evicted as blockers (rebuilt by stitcher)
    cancelled: bool = False  # True when should_cancel fired mid-run
    expansions: int = 0  # Total A* node expansions across all pair-searches
    timings: Dict[str, float] = field(default_factory=dict)  # Stage seconds + grid cells
    refill_zones_done: bool = False  # True if copper pour refill was executed
    fanout_vias_added: int = 0  # Plane-tap stubs (four-layer strategy; additive, never folded into vias_added inputs)
    fanout_tracks_added: int = 0  # Stub tracks accompanying plane-tap vias
    failed_reasons: Dict[str, str] = field(default_factory=dict)  # net -> explicit failure reason
    partial_tracks: Tuple = ()  # Display-only copper from failed nets' routed chunks (never written back)
    partial_vias: Tuple = ()  # Display-only vias from failed nets' routed chunks (never written back)
    partial_arcs: Tuple = ()  # Display-only arcs ditto (overlay ignores them for now)
    arcs_added: int = 0  # KiCad (arc) bends emitted by the biarc post-pass
    total_wire_mm: float = 0.0  # Raw route wire: tracks + arc chord-sums
    # (stitch bridges excluded so candidates compare on routing alone)
    candidates: Tuple[dict, ...] = ()  # refine_routing order trials
    winner_seed: int = 0  # Shuffle seed of the winning refine candidate
    # (-1 = kept pre-refine copper without re-routing)


from .route_raw import (
    RawRoutes,
    route_raw,
    _resolve_grid_layers,
    _disallowed_terminal_layers,
    _terminals_for_net,
    terminals_for_net,
    _group_terminals_by_region,
    _width_cells,
    _inner_plane_layers_for_net,
    _plan_plane_stubs,
    _branch_paths,
    _apply_route_to_grid,
    _chunk_terminals,
    _merge_chunk_results,
    _branch_cells,
    _check_chunk_coverage,
    _resume_progress,
    _route_chunk,
    _route_escapes_board,
    _mark_line_blocked,
    _smooth_result,
    _stamp_fitted_pieces,
    _tht_sites_by_net,
    _build_tht_via_mask,
    _rip_up_nets_from_model,
    _strip_gnd_blockers_from_model,
    track_corridor_radius_cells,
    via_halo_radius_cells,
)
from .finish_routes import finish_routes
from .refine import refine_routing, _tangle_groups, _refine_order


def route_nets(
    model: BoardModel,
    net_names: List[str],
    protected_nets: Set[str],
    cost_grid: Optional[np.ndarray] = None,
    routing_grid: Optional[RoutingGrid] = None,
    obstacle_cache=None,
    params: Optional[RoutingParams] = None,
    original_pcb_tree: Optional = None,
    extra_power_nets: Collection[str] = (),
    progress_cb=None,  # fn(net_name, pairs_done, pairs_total, expansions) — best-effort
    should_cancel=None,  # fn() -> bool — checked between nets/attempts/pairs
) -> Tuple[RoutingResult, np.ndarray, RoutingGrid]:
    """Route a specific list of nets.

    This function supports incremental routing by accepting and returning
    the cost_grid and routing_grid, allowing them to be persisted between calls.

    Args:
        model: BoardModel with footprints, nets, tracks, vias
        net_names: List of net names to route (in priority order)
        protected_nets: Set of net names that are protected from rip-up
        cost_grid: Pre-built occupancy grid (optional, will be built if None)
        routing_grid: Pre-built routing grid (optional, will be built if None)
        obstacle_cache: Optional ``obstacles.ObstacleCache`` for incremental
            mask re-raster (optional, legacy full rebuilds if None)
        params: RoutingParams configuration (optional, uses defaults if None)
        original_pcb_tree: Original PCB S-expression tree for writeback
        extra_power_nets: User-marked power membership (app nets table,
            seeded from power-named KiCad netclasses on load). Marked nets
            route first with wide tracks and join the protected family via
            the caller's protected set.
        progress_cb: Optional per-pair progress hook for job UIs. Called as
            ``progress_cb(net_name, pairs_done, pairs_total, expansions)``.
            Exceptions are swallowed (reporting must never break routing).
        should_cancel: Optional cooperative-cancel predicate. Checked
            between nets, between retry attempts, and inside A* (every 4096
            expansions). Firing sets ``RoutingResult.cancelled`` and stops
            with partial results — already-routed nets are kept.

    Returns:
        Tuple of (RoutingResult, updated_cost_grid, routing_grid)
    """
    raw, cost_grid, routing_grid = route_raw(
        model=model,
        net_names=net_names,
        protected_nets=protected_nets,
        cost_grid=cost_grid,
        routing_grid=routing_grid,
        obstacle_cache=obstacle_cache,
        params=params,
        original_pcb_tree=original_pcb_tree,
        extra_power_nets=extra_power_nets,
        progress_cb=progress_cb,
        should_cancel=should_cancel,
    )
    if params is None:
        params = RoutingParams()
    return finish_routes(raw, params)
