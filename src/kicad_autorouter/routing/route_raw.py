"""Stage A of the routing pipeline: rip-up, grids, raw routing, track conversion.

Thin orchestrator (split for editor search): grid/terminal/chunk/smooth/flight
helpers live in stage_grid/stage_chunks/stage_smooth/stage_flight.
"""
from __future__ import annotations
import math
import time
from dataclasses import dataclass, field
from typing import Collection, Dict, List, Optional, Set, Tuple
import numpy as np
from ..board_model import ArcTrack, BoardModel, Point, Track, Via, is_ground_net, is_power_net_effective
from ..io import rip_up_nets
from . import RipUpManager, RoutingCancelled, SingleNetRouter, CostMap
from .grid import RoutingGrid, board_to_grid, create_grid_from_model
from .obstacles import BLOCKED, FREE, ObstacleCache, build_occupancy_grid, build_fanout_cost, build_via_block_mask, drill_radius_cells, own_net_hard_cells, stamp_discs, stamp_square, static_fingerprint, tht_drill_cells, track_corridor_radius_cells, via_halo_radius_cells, _build_tht_via_mask, _mark_pad
from .params import MAX_GRID_CELLS_TOTAL, RoutingParams, _validate_routing_params, resolve_clearance, resolve_track_width
from .output import routes_to_tracks_vias, apply_routes_to_tree
from .output import Arc as OutputArc, Track as OutputTrack, Via as OutputVia
from .router import RouteResult as NetRouteResult
from .router_via import ViaOkTracker
from .power import identify_power_nets
from .pour import pour_corridors, pour_net_names
from .stitch import plan_stitches
from .stage_grid import _default_cost_map, _default_transition_masks, _resolve_grid_layers, _disallowed_terminal_layers, _supply_root_cell, _punched_search_grid, _tht_sites_by_net, _seeded_coverage, _width_cells, _inner_plane_layers_for_net, _plan_plane_stubs
from .stage_chunks import _terminals_for_net, terminals_for_net, _group_terminals_by_region, _branch_paths, _apply_route_to_grid, _chunk_terminals, union_paths, _merge_chunk_results, _branch_cells, _flood_cover, _check_chunk_coverage, _resume_progress, _rip_up_nets_from_model, _strip_gnd_blockers_from_model, _mark_line_blocked, _stamp_fitted_pieces
from .stage_smooth import _resolve_gate_trip, _route_escapes_board, _smooth_result
from .stage_flight import _chunk_failure_diag, _FlightSetup, _setup_flight, _terminal_exit_stubs, _route_chunk, route_gap_terminals


@dataclass
class RawRoutes:
    """Unfinished routing: raw copper before stitching/tree-writeback/DRC.

    Bundle threading stage A (``route_raw``: rip, grids, routing, track
    conversion) into stage B (``finish_routes``: GND stitching, tree
    writeback, zone refill, DRC, result assembly). Lets refine trials score
    raw copper and finish only the winner — no re-route replay.
    """

    model: BoardModel  # ripped model (local replace() copies)
    pcb_tree: Optional = None  # ripped input tree (rebound local)
    cost_grid: Optional[np.ndarray] = None  # stamped BLOCKED through 10a
    routing_grid: Optional[RoutingGrid] = None
    all_results: Dict[str, NetRouteResult] = field(default_factory=dict)
    partial_results: Dict[str, NetRouteResult] = field(default_factory=dict)
    width_map: Dict[str, float] = field(default_factory=dict)
    plane_tracks: List[OutputTrack] = field(default_factory=list)
    plane_vias: List[OutputVia] = field(default_factory=list)
    ripup: Optional[RipUpManager] = None
    nets_routed: int = 0
    nets_failed: int = 0
    cancelled: bool = False
    expansions_total: int = 0
    timings: Dict[str, float] = field(default_factory=dict)
    t_total: float = 0.0
    gnd_stripped: int = 0
    # Structured failure records (pins, seed, windows tried, retry hints)
    # keyed by net — one entry per net whose chunk flight failed.
    failed_diagnostics: Dict[str, dict] = field(default_factory=dict)
    net_stats: Dict[str, dict] = field(default_factory=dict)  # net ->
    # per-net cost attribution: {ok, expansions, wall_s, timings{key: delta}}.
    # Numeric timing deltas around that net's _route_one_net call. One-time
    # lazy builds land on the group's first net (its smooth_verify_build_s
    # absorbs the shared base index) — honest as measured, note when reading.
    pour_corridors: Optional[Dict] = None  # corridor map built in 5b (shared
    # across refine trials on the same ripped model; None when skipped)
    pour_min_thick: float = 0.0
    tracks: List[OutputTrack] = field(default_factory=list)
    vias: List[OutputVia] = field(default_factory=list)
    route_arcs: List[OutputArc] = field(default_factory=list)
    partial_tracks: List[OutputTrack] = field(default_factory=list)
    partial_vias: List[OutputVia] = field(default_factory=list)
    partial_arcs: List[OutputArc] = field(default_factory=list)
    total_wire_mm: float = 0.0


def route_raw(
    model: BoardModel,
    net_names: List[str],
    protected_nets: Set[str],
    cost_grid: Optional[np.ndarray] = None,
    routing_grid: Optional[RoutingGrid] = None,
    obstacle_cache: Optional[ObstacleCache] = None,
    params: Optional[RoutingParams] = None,
    original_pcb_tree: Optional = None,
    extra_power_nets: Collection[str] = (),
    progress_cb=None,  # fn(net_name, done, total, expansions) — best-effort
    should_cancel=None,  # fn() -> bool — checked between nets/attempts/pairs
    precomputed_pour=None,  # (corridors, min_thick) from a prior identical
    # call (refine trials share one ripped model + grid); shapes are
    # validated, None computes as before. Refine-internal: callers must
    # guarantee the same ripped model and grid resolution.
) -> Tuple[RawRoutes, np.ndarray, RoutingGrid]:
    """Route nets to raw copper (no stitching, tree writeback, refill, DRC).

    Steps 0–10a of the pipeline; same signature as :func:`route_nets`.
    Returns the :class:`RawRoutes` bundle plus grids for :func:`finish_routes`.

    Accepts and returns the cost_grid and routing_grid, allowing them to be
    persisted between calls. Args mirror :func:`route_nets`.
    """
    if params is None:
        params = RoutingParams()
    t_total = time.perf_counter()
    timings: Dict[str, float] = {}
    expansions_total = 0
    cancelled = False
    # Run-level incremental via-ok mask: consecutive nets' dilation
    # inputs differ by <1% of cells, so one full-board dilation per
    # run plus cheap crop updates replaces one full dilation per net.
    # Bit-identical output; never crosses runs (footprints fixed here).
    via_ok_tracker = ViaOkTracker()

    def _report(net_name: str, done: int, total: int, expansions: int) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(net_name, done, total, expansions)
        except Exception:  # user callback must not break routing
            pass

    # 0. Rip up selected nets from model so router can find new paths
    # (Existing tracks for these nets would block routing). Protected nets
    # are never ripped here — except GND (see 0b): signal/power copper is
    # only removed when explicitly requested, so tracks can't rip each other.
    if net_names:
        model = _rip_up_nets_from_model(model, net_names)
        # Also rip up from PCB tree if provided
        if original_pcb_tree is not None:
            original_pcb_tree = rip_up_nets(original_pcb_tree, set(net_names), protected_nets)

    # 0b. GND-only blocker eviction (upfront bulk strip). Pre-existing
    # zone-pour stitch vias and old GND tracks would otherwise wall off
    # new routes as hard BLOCKED cells; GND is cheaply rebuilt (zones
    # refill + plan_stitches), so evict it even when protected. Never
    # touches signal/power copper.
    gnd_stripped = 0
    if params.strip_gnd_blockers:
        model, gnd_stripped = _strip_gnd_blockers_from_model(model)
        if gnd_stripped and original_pcb_tree is not None:
            # Collect GND net names present on the tree (model is already
            # stripped, so scan the tree instead via a net-name pass).
            from ..board_model import _net_name as _tree_net_name
            gnd_nets = set()
            for node in (*original_pcb_tree.children("segment"), *original_pcb_tree.children("arc"), *original_pcb_tree.children("via")):
                name = _tree_net_name(node.find("net"))
                if is_ground_net(name):
                    gnd_nets.add(name)
            if gnd_nets:
                # Bypass protected_nets for GND only (rebuildable); the
                # affected set contains solely ground nets, so signal/power
                # copper is untouched regardless.
                original_pcb_tree = rip_up_nets(original_pcb_tree, gnd_nets, set())

    # 1. Create or reuse grid. A reused grid is only valid when it matches
    # the requested resolution AND covers the same board area AND routes
    # the same layer set: a resolution, margin, or allowed_layers change
    # must rebuild, otherwise the new params are silently ignored (and
    # stale BLOCKED cells from the previous route persist).
    grid_layers = _resolve_grid_layers(model, params)
    t_grid = time.perf_counter()
    if routing_grid is None:
        routing_grid = create_grid_from_model(
            model, params.grid_resolution_mm, params.margin_mm, layers=grid_layers
        )
    else:
        expected = create_grid_from_model(
            model, params.grid_resolution_mm, params.margin_mm, layers=grid_layers
        )
        if (
            routing_grid.resolution_mm != params.grid_resolution_mm
            or routing_grid.width_cells != expected.width_cells
            or routing_grid.height_cells != expected.height_cells
            or routing_grid.origin_mm != expected.origin_mm
            or tuple(routing_grid.layers) != tuple(expected.layers)
        ):
            routing_grid = expected

    # Guard: fine grids on large boards explode (DCCF at 0.05mm = 38M cells
    # across 2 layers; each A* also allocates a ~4 byte/cell g-score array).
    # Fail fast with an actionable message instead of hanging for minutes.
    total_cells = len(routing_grid.layers) * routing_grid.width_cells * routing_grid.height_cells
    timings["grid_build_s"] = time.perf_counter() - t_grid
    timings["grid_cells"] = float(total_cells)
    timings["grid_resolution_mm"] = float(params.grid_resolution_mm)
    if total_cells > MAX_GRID_CELLS_TOTAL:
        raise ValueError(
            f"routing grid too large: {routing_grid.width_cells}x{routing_grid.height_cells}"
            f"x{len(routing_grid.layers)} ({total_cells / 1e6:.1f}M cells) at"
            f" {params.grid_resolution_mm}mm resolution (limit"
            f" {MAX_GRID_CELLS_TOTAL / 1e6:.0f}M). Use a coarser grid_resolution_mm"
            f" (e.g. 0.1mm) or a smaller margin_mm."
        )

    # 2. Build or refresh the occupancy grid from the RIPPED model.
    # Refresh-in-place when shapes match: drops BLOCKED cells of the nets
    # being re-routed (they were ripped from the model above) while keeping
    # prior unrelated routes (still present as tracks in the model) blocked.

    # Known tradeoff: a rebuilt grid marks pre-existing tracks with the
    # default width + clearance halo (obstacles._mark_track/_mark_arc),
    # while in-batch _apply_route_to_grid reserves the full emitted-width
    # corridor (per-net widths). So one-net-at-a-time callers (the chunked
    # UI) let later nets run slightly tighter to earlier copper than a
    # single batched call would (measured ~5% fewer tracks on 3 DCCF nets;
    # routed/failed sets identical). Making rebuilds width-aware belongs
    # with the GND-continuity work (docs/roadmap.md).
    if obstacle_cache is not None:
        # Incremental re-raster: sync the cached base grid (static obstacles
        # + current copper) instead of rebuilding the full occupancy from
        # the ripped model. Content is identical to a fresh build, so the
        # caller's grid just copies it.
        t_occ = time.perf_counter()
        fresh = obstacle_cache.sync(
            model, routing_grid,
            clearance_mm=params.clearance_mm,
            track_half_mm=params.track_width_mm / 2.0,
        )
        timings["occupancy_s"] = time.perf_counter() - t_occ
        timings["occupancy_cached"] = (
            1.0 if obstacle_cache.last_hit else 0.0
        )
        if cost_grid is not None and cost_grid.shape == fresh.shape:
            cost_grid[:] = fresh
        else:
            cost_grid = fresh.copy()
    elif cost_grid is None or cost_grid.shape != (
        len(routing_grid.layers), routing_grid.height_cells, routing_grid.width_cells
    ):
        t_occ = time.perf_counter()
        cost_grid = build_occupancy_grid(model, routing_grid, default_clearance_mm=params.clearance_mm)
        timings["occupancy_s"] = time.perf_counter() - t_occ
    else:
        t_occ = time.perf_counter()
        fresh = build_occupancy_grid(model, routing_grid, default_clearance_mm=params.clearance_mm)
        cost_grid[:] = fresh
        timings["occupancy_s"] = time.perf_counter() - t_occ

    # Board fill / density metrics from the occupancy grid (all layers).
    # blocked_frac: impassable cells (>= BLOCKED). occupied_frac: any
    # non-free cell (courtyards, pours, halos, blocked). tolled_frac:
    # passable-but-costly cells (steering penalties without blocking).
    blocked = cost_grid >= BLOCKED
    timings["blocked_frac"] = float(blocked.mean())
    timings["occupied_frac"] = float((cost_grid > 0).mean())
    timings["tolled_frac"] = float(((cost_grid > 0) & (cost_grid < BLOCKED)).mean())
    if obstacle_cache is not None:
        # Footprint-only inputs, already keyed by the sync above.
        tht_via_mask, via_block_mask = obstacle_cache.masks()
    else:
        # 3/3b. Free THT hop sites + via-block mask (SMD solder/fanout).
        tht_via_mask, via_block_mask = _default_transition_masks(
            model, routing_grid, params)

    # 4. Via price in mm; the router scales to cells. The router itself
    # is built per net below (own THT drill discs punched FREE), since
    # the search grid differs per net while stamping stays on the
    # shared cost grid.
    cost_map = _default_cost_map(params)

    # 5. Identify net classes (user-marked power membership included)
    power_nets, ground_nets, signal_nets = identify_power_nets(model, extra_power_nets)

    # 5b. Pour-corridor map for severance-aware routing (computed once on
    # the ripped model; pour nets themselves route without the penalty).
    # Skipped entirely when no requested net can consume it (every
    # requested net owns the pour) — the severance term is provably
    # unread then (see _route_one_net).
    pour_nets = set(pour_net_names(model))
    corridors, corridor_min_thick = (None, 0.0)
    timings["pour_s"] = 0.0
    wants_severance = (
        params.pour_severance_weight > 0
        and bool(pour_nets)
        and any(n not in pour_nets for n in net_names)
    )
    if wants_severance:
        reused = (
            precomputed_pour is not None
            and isinstance(precomputed_pour[0], dict)
            and all(
                layer in precomputed_pour[0]
                and precomputed_pour[0][layer].shape
                == (routing_grid.height_cells, routing_grid.width_cells)
                for layer in routing_grid.layers
            )
        )
        if reused:
            corridors, corridor_min_thick = precomputed_pour
        else:
            t_pour = time.perf_counter()
            corridors, corridor_min_thick = pour_corridors(model, routing_grid)
            timings["pour_s"] = time.perf_counter() - t_pour

    # 6. Separate requested nets into categories
    requested_power = [n for n in net_names if n in power_nets]
    requested_ground = [n for n in net_names if n in ground_nets]
    requested_signal = [n for n in net_names if n in signal_nets]

    # Track results
    all_results = {}
    partial_results: Dict[str, NetRouteResult] = {}
    ripup = RipUpManager(max_attempts=params.max_attempts_per_net)
    nets_routed = 0
    nets_failed = 0
    width_map: Dict[str, float] = {}
    plane_tracks: List[OutputTrack] = []
    plane_vias: List[OutputVia] = []
    plane_done: Set[str] = set()
    fanout_cap = params.fanout_plane_vias_max

    # 6b. Plane-tap fanout (four-layer strategy only; two-layer forces it
    # off so prior behavior is retained verbatim). A ground/power-effective
    # net owning an inner-copper zone is satisfied with short outer stubs +
    # through-vias tapping the plane: the plane itself connects the stubs.
    # SMD-only (THT barrels already pierce the plane); capped; pour-gated
    # clean no-op (no inner zone → normal outer routing below).
    if params.fanout_planes:
        res_mm = routing_grid.resolution_mm
        for net_name in list(net_names):
            if net_name in plane_done or net_name in ripup.net_failures:
                continue
            if not _inner_plane_layers_for_net(model, net_name):
                continue
            if not (
                is_ground_net(net_name)
                or is_power_net_effective(net_name, extra_power_nets)
            ):
                continue
            if len(model.nets.get(net_name, ())) < 2:
                continue
            budget_left = (
                max(0, fanout_cap - len(plane_vias)) if fanout_cap > 0 else None
            )
            stubs, via_pts, width_mm, reason = _plan_plane_stubs(
                model, routing_grid, cost_grid, via_block_mask,
                params, net_name, extra_power_nets, budget_left,
            )
            if reason is not None:
                ripup.record_failure(net_name, reason)
                nets_failed += 1
                plane_done.add(net_name)
                continue
            # Commit stubs: emit + reserve halos so later MST nets keep
            # clearance. Stub tracks live on one outer layer; vias pierce,
            # so the via halo stamps every grid layer.
            clearance_mm = resolve_clearance(model, params, net_name)
            track_radius = track_corridor_radius_cells(width_mm, clearance_mm, res_mm)
            via_radius = via_halo_radius_cells(
                params.via_size_mm, clearance_mm, width_mm / 2.0, res_mm)
            for (x0, y0, x1, y1, layer) in stubs:
                plane_tracks.append(OutputTrack(
                    start=(x0, y0), end=(x1, y1),
                    width_mm=width_mm, layer=layer, net_name=net_name,
                ))
                li = routing_grid.layer_to_idx.get(layer, 0)
                c0, r0 = board_to_grid(routing_grid, x0, y0)
                c1, r1 = board_to_grid(routing_grid, x1, y1)
                _mark_line_blocked(cost_grid[li], r0, c0, r1, c1, track_radius)
            for (vx_mm, vy_mm) in via_pts:
                plane_vias.append(OutputVia(
                    position=(vx_mm, vy_mm),
                    size_mm=params.via_size_mm, drill_mm=params.via_drill_mm,
                    layers=("F.Cu", "B.Cu"), net_name=net_name,
                ))
                cc, rr = board_to_grid(routing_grid, vx_mm, vy_mm)
                for li in range(len(routing_grid.layers)):
                    stamp_square(cost_grid[li], rr, cc, via_radius, BLOCKED)
            ripup.record_success(net_name, [])
            width_map[net_name] = width_mm
            nets_routed += 1
            plane_done.add(net_name)

    def _route_one_net(net_name: str) -> bool:
        """Route one net with its resolved width/clearance + fanout halo.

        Records into all_results/ripup, reserves the emitted-width corridor
        on success. Returns True on success. Raises :class:`RoutingCancelled`
        when ``should_cancel`` fires (partial per-net results are kept by
        the caller).
        """
        nonlocal nets_routed, nets_failed, expansions_total
        conns = model.nets.get(net_name, [])
        if len(conns) < 2:
            return False
        if params.allowed_layers is not None:
            bad = _disallowed_terminal_layers(model, net_name, params.allowed_layers)
            if bad:
                ripup.record_failure(
                    net_name,
                    f"terminal on disallowed layer {bad} "
                    f"(allowed_layers={sorted(params.allowed_layers)})",
                )
                nets_failed += 1
                return False
        terminals = _terminals_for_net(
            model, routing_grid, net_name, params.allowed_layers
        )
        if len(terminals) < 2:
            return False
        # Trunk growth for ground nets: root the tree at the supply
        # connector so branches share trunk copper outward instead of
        # chaining pad-to-pad (which sums locally-optimal hops into wraps
        # like the U4 GND ring). Chunks not containing the root fall back
        # to Kruskal order inside router.route.
        trunk_root = (
            _supply_root_cell(model, routing_grid, net_name, terminals)
            if is_ground_net(net_name) else None
        )
        # Panel-aware split: group terminals by board region so each board
        # routes independently. Same-net pads on different panels (e.g.
        # tube111 +12V in regions 1+2) must never share copper across the
        # inter-panel gap — assembly bridges join them, and each side only
        # needs a local route up to its own header pads.
        region_groups = _group_terminals_by_region(
            model, routing_grid, net_name, params.allowed_layers
        )
        multi_region = len(region_groups) > 1
        # Piecemeal chunking applies within each region group (adjacent
        # row-major bands), never across regions.
        grouped_chunks: List[Tuple[int, List[List[Tuple[int, int, int]]]]] = []
        for region_idx, group_terms in region_groups:
            if len(group_terms) < 2:
                continue  # single-pad region: nothing to connect locally
            if params.max_terminals_per_net > 0 and len(group_terms) > params.max_terminals_per_net:
                if params.piecemeal_chunk_terminals >= 2:
                    ordered = sorted(group_terms, key=lambda t: (t[1], t[0]))
                    grouped_chunks.append(
                        (region_idx, _chunk_terminals(ordered, params.piecemeal_chunk_terminals))
                    )
                else:
                    ripup.record_failure(
                        net_name,
                        f"{len(group_terms)} terminals exceeds max_terminals_per_net="
                        f"{params.max_terminals_per_net} (massive nets do not route "
                        f"in a single flight — split into smaller subsets, enable "
                        f"piecemeal_chunk_terminals, or rely on copper pours + "
                        f"GND stitching for planes)",
                    )
                    nets_failed += 1
                    return False
            else:
                grouped_chunks.append((region_idx, [group_terms]))
        if not grouped_chunks:
            return False

        pairs_total = sum(
            len(c) * (len(c) - 1) // 2 for _, chunks in grouped_chunks for c in chunks
        )
        _report(net_name, 0, pairs_total, expansions_total)
        setup = _setup_flight(
            model, routing_grid, cost_grid, cost_map, tht_via_mask,
            via_block_mask, obstacle_cache, params, net_name,
            extra_power_nets, corridors, corridor_min_thick, pour_nets,
            static_fp=batch_static_fp,
            via_ok_tracker=via_ok_tracker,
        )
        width_mm = setup.width_mm
        width_map[net_name] = width_mm
        secondary = setup.secondary
        router = setup.router
        if params.debug:
            # Sealed-terminal probe: bounded flood-fill per terminal, runs
            # in milliseconds. Tiny counts mean pair-searches will burn cap.
            timings[f"debug_escape_{net_name}"] = router.terminal_escape_counts(
                terminals, secondary
            )
        corridor = setup.corridor
        clearance_mm = setup.clearance_mm
        clearance_radius = setup.clearance_radius
        soft_mask = setup.soft_mask
        _verify_share_for = setup.verify_share_for
        _secondary_for_region = setup.secondary_for

        success = False
        fatal = False
        region_results: List[NetRouteResult] = []

        routed_regions: Set[int] = set()
        # Per-group routed chunks persist across attempts so a retry reruns
        # only the failed chunk onward (prior copper stays stamped as both
        # obstacle and join target) — same resume rule as single-region.
        group_progress: Dict[int, List[NetRouteResult]] = {}
        # Fail fast on no-progress retries: a failed attempt stamps nothing,
        # so an attempt that routes no new chunk reruns the identical
        # searches on a bit-identical grid. Only attempts that advance
        # (new kept copper to resume from) earn another try — the attempt
        # budget then covers nets that keep advancing, not doomed replays.
        attempt_advanced = True
        while not fatal and not success and ripup.can_retry(net_name):
            if should_cancel is not None and should_cancel():
                raise RoutingCancelled(f"cancelled before attempt for {net_name}")
            if not attempt_advanced:
                # Deterministic on an unchanged grid like budget exhaustion:
                # nothing was stamped, so the next attempt would burn the
                # same searches for the same outcome — fail immediately.
                fatal = True
                break
            attempt_advanced = False
            # Resume after already-routed chunks: recompute progress so a
            # retry continues where the previous attempt stopped.
            done_base = _resume_progress(grouped_chunks, group_progress)
            # Route each region group independently; seeds stay within the
            # same region so kept copper never pulls a search across the gap.
            for region_idx, chunks in grouped_chunks:
                # Skip groups already routed in a previous attempt.
                if region_idx in routed_regions:
                    continue
                n_chunks = len(chunks)
                chunk_results = group_progress.setdefault(region_idx, [])
                group_ok = True
                for ci in range(len(chunk_results), n_chunks):
                    chunk = chunks[ci]
                    region_tag = f"region {region_idx} " if multi_region else ""
                    chunk_tag = f"chunk {ci + 1}/{n_chunks}: " if n_chunks > 1 else ""
                    tag = f"{region_tag}{chunk_tag}"
                    seed = ([cell for r in chunk_results
                             for cell in _branch_cells(r)] or None)
                    region_secondary, region_blocked = _secondary_for_region(
                        region_idx, chunk
                    )
                    chunk_pairs = len(chunk) * (len(chunk) - 1) // 2

                    def _pair_cb(done: int, total: int, exp: int) -> None:
                        _report(net_name, done_base + done, pairs_total,
                                expansions_total + exp)

                    result, failure = _route_chunk(
                        router, chunk, region_secondary, _pair_cb, should_cancel,
                        seed, cost_grid, routing_grid, corridor, soft_mask,
                        clearance_radius, region_blocked,
                        params.arc_fillets, params.arc_max_radius_mm,
                        params.arc_min_leg_mm, params.arc_sample_mm,
                        params.arc_keep_min_radius_mm, params, model, timings,
                        net_name, tag, width_mm,
                        verify_share=_verify_share_for(region_idx),
                        trunk_root=trunk_root,
                        region_idx=region_idx, chunk_idx=ci,
                        n_chunks=n_chunks,
                    )
                    if result is not None:
                        expansions_total += result.expansions
                    _report(net_name, done_base + chunk_pairs, pairs_total,
                            expansions_total)
                    if failure is None:
                        assert result is not None
                        chunk_results.append(result)
                        done_base += chunk_pairs
                        attempt_advanced = True
                        continue
                    kind, reason, diag = failure
                    ripup.record_failure(net_name, reason)
                    if diag is not None:
                        failed_diagnostics[net_name] = diag
                        timings[f"failed_{net_name}"] = diag
                    group_ok = False
                    if kind in ("budget", "gate"):
                        # Budget exhaustion and emit-gate trips are both
                        # deterministic on an unchanged grid: the next
                        # attempt would burn the same budget for the same
                        # outcome, so stop retrying and fail.
                        fatal = True
                    break
                if not group_ok:
                    break
                merged = (
                    _merge_chunk_results(chunk_results)
                    if len(chunk_results) > 1 else chunk_results[0]
                )
                problem = (
                    _check_chunk_coverage(chunks[0] if len(chunks) == 1 else
                                          [t for c in chunks for t in c], merged)
                    if n_chunks > 1 else None
                )
                if problem is not None:
                    if multi_region:
                        problem = f"region {region_idx}: {problem}"
                    ripup.record_failure(net_name, problem)
                    fatal = True
                    break
                region_results.append(merged)
                routed_regions.add(region_idx)
            else:
                merged_all = _merge_chunk_results(region_results)
                if multi_region:
                    timings[f"partitioned_{net_name}"] = (
                        f"routed per-region "
                        f"{sorted(r for r, _ in grouped_chunks)}; join via assembly bridge"
                    )
                ripup.record_success(net_name, merged_all.path)
                all_results[net_name] = merged_all
                nets_routed += 1
                success = True
        if not success:
            # Keep successfully routed region groups/chunks for failure
            # review (display only — never written back). Matches the
            # single-region rule: any routed chunk of a chunked group is
            # kept; single-chunk groups have nothing partial (all-or-nothing).
            keep = list(region_results)
            for ri, chunks in grouped_chunks:
                if len(chunks) > 1 and ri not in routed_regions:
                    prog = group_progress.get(ri, [])
                    if prog:
                        keep.append(_merge_chunk_results(prog))
            if keep:
                partial_results[net_name] = _merge_chunk_results(keep)
            nets_failed += 1
        return success

    # 7-9. Route requested nets by class (power, then ground, then signal).
    # Cooperative cancel stops with partial results: already-routed nets
    # are kept, unreached nets count as failed with reason "cancelled".
    net_stats: Dict[str, dict] = {}
    # Structured failure records (pins, seed, windows tried, retry hints)
    # keyed by net — one entry per net whose chunk flight failed.
    failed_diagnostics: Dict[str, dict] = {}

    def _route_one_net_timed(net_name: str) -> bool:
        """_route_one_net plus per-net cost attribution (benchmarks only).

        Snapshots the numeric timing accumulators and the expansion
        counter around the call; deltas are that net's honest cost except
        for one-time lazy builds, which land on the group's first net
        (e.g. its smooth_verify_build_s absorbs the shared emit-gate
        base index). Recorded even on cancel (partial costs kept).
        """
        t0 = time.perf_counter()
        exp0 = expansions_total
        fail0 = timings.get("expansions_failed", 0)
        snap = {
            k: v for k, v in timings.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        }
        try:
            return _route_one_net(net_name)
        finally:
            delta = {}
            for k, v in timings.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    d = v - snap.get(k, 0.0)
                    if d:
                        delta[k] = d
            delta.pop("expansions_failed", None)
            net_stats[net_name] = {
                "ok": net_name in all_results,
                "expansions": expansions_total - exp0,
                "expansions_failed": timings.get("expansions_failed", 0) - fail0,
                "wall_s": time.perf_counter() - t0,
                "timings": delta,
            }

    # Batch static fingerprint (non-copper inputs only: footprints,
    # pads, zones, regions — routed copper appended below never changes
    # it, and rip-up/GND-strip above remove copper alone). Threaded into
    # each flight so the ~17 ms recompute is paid once, not per net.
    batch_static_fp = static_fingerprint(model)

    t_route = time.perf_counter()
    try:
        # 7. Route requested power nets (respects pad layers)
        for net_name in requested_power:
            if net_name not in plane_done:
                _route_one_net_timed(net_name)

        # 8. Route requested ground nets
        for net_name in requested_ground:
            if net_name not in plane_done:
                _route_one_net_timed(net_name)

        # 9. Route signal nets
        for net_name in requested_signal:
            if net_name not in plane_done:
                _route_one_net_timed(net_name)
    except RoutingCancelled:
        cancelled = True
        for net_name in net_names:
            if net_name not in all_results and net_name not in ripup.get_failed_nets():
                ripup.record_failure(net_name, "cancelled")
                nets_failed += 1
    timings["route_s"] = time.perf_counter() - t_route

    # Aggregate corridor guidance stats across all routed nets.
    ctried = sum(r.corridor_tried for r in all_results.values())
    cfnd = sum(r.corridor_found for r in all_results.values())
    cused = sum(r.corridor_used for r in all_results.values())
    ccells = sum(r.corridor_cells for r in all_results.values())
    timings["corridor_tried"] = float(ctried)
    timings["corridor_found"] = float(cfnd)
    timings["corridor_used"] = float(cused)
    timings["corridor_cells"] = float(ccells)
    timings["corridor_use_rate"] = (
        round(cused / max(ctried, 1), 4) if ctried else 0.0
    )
    # 10. Convert all results to tracks/vias/arcs (per-net resolved widths;
    # own-THT hops reuse the barrel: no Via emitted there)
    tht_sites = _tht_sites_by_net(model, routing_grid)
    tracks, vias, route_arcs = routes_to_tracks_vias(
        all_results,
        routing_grid,
        net_class_widths=width_map,
        default_width_mm=params.track_width_mm,
        power_width_mm=params.power_width_mm,
        via_size_mm=params.via_size_mm,
        via_drill_mm=params.via_drill_mm,
        tht_sites=tht_sites,
        extra_power=extra_power_nets,
    )
    # Display-only copper from failed nets' routed chunks (review in the
    # viewer overlay; excluded from writeback, stitching, and DRC).
    partial_tracks, partial_vias, partial_arcs = routes_to_tracks_vias(
        partial_results,
        routing_grid,
        net_class_widths=width_map,
        default_width_mm=params.track_width_mm,
        power_width_mm=params.power_width_mm,
        via_size_mm=params.via_size_mm,
        via_drill_mm=params.via_drill_mm,
        tht_sites=tht_sites,
        extra_power=extra_power_nets,
    )
    # 10a. Plane-tap stubs (four-layer strategy): additive, never folded
    # into MST results. Reserved in the cost grid at emission, so stitching
    # below sees them as existing copper.
    tracks.extend(plane_tracks)
    vias.extend(plane_vias)

    # Raw route wire for refine scoring (tracks exact, arcs chord-summed;
    # stitch copper appended later is excluded so candidates compare on
    # routing alone).
    total_wire_mm = sum(
        math.hypot(t.end[0] - t.start[0], t.end[1] - t.start[1]) for t in tracks
    ) + sum(
        math.hypot(a.mid[0] - a.start[0], a.mid[1] - a.start[1])
        + math.hypot(a.end[0] - a.mid[0], a.end[1] - a.mid[1])
        for a in route_arcs
    )

    raw = RawRoutes(
        model=model,
        pcb_tree=original_pcb_tree,
        cost_grid=cost_grid,
        routing_grid=routing_grid,
        all_results=all_results,
        partial_results=partial_results,
        width_map=width_map,
        plane_tracks=plane_tracks,
        plane_vias=plane_vias,
        ripup=ripup,
        nets_routed=nets_routed,
        nets_failed=nets_failed,
        cancelled=cancelled,
        expansions_total=expansions_total,
        timings=timings,
        t_total=t_total,
        gnd_stripped=gnd_stripped,
        net_stats=net_stats,
        failed_diagnostics=failed_diagnostics,
        pour_corridors=corridors,
        pour_min_thick=corridor_min_thick,
        tracks=tracks,
        vias=vias,
        route_arcs=route_arcs,
        partial_tracks=partial_tracks,
        partial_vias=partial_vias,
        partial_arcs=partial_arcs,
        total_wire_mm=total_wire_mm,
    )
    return raw, cost_grid, routing_grid
