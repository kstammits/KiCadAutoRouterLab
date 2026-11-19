"""Stage A helpers: per-chunk flight (setup, exit stubs, route, gap repair)."""
from __future__ import annotations
import math
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Collection, Dict, List, Optional, Set, Tuple
import numpy as np
from ..board_model import BoardModel
from .grid import RoutingGrid, board_to_grid, create_grid_from_model, grid_to_board, mm_to_cells
from .obstacles import BLOCKED, FREE, ObstacleCache, bresenham_cells, build_fanout_cost, build_occupancy_grid, build_via_block_mask, drill_radius_cells, own_net_hard_cells, stamp_discs, stamp_square, static_fingerprint, tht_drill_cells, track_corridor_radius_cells, via_halo_radius_cells, _build_tht_via_mask, _mark_pad
from .params import MAX_GRID_CELLS_TOTAL, RoutingParams, resolve_clearance, resolve_track_width
from .router import SECONDARY_BLOCKED, SingleNetRouter, diagnose_attempts, CostMap
from .router import RouteResult as NetRouteResult
from .power import identify_power_nets
from .dsu import DisjointSet
from .pour import pour_corridors, pour_net_names, severance_cost
from .stitch import plan_stitches
from .stitch import _region_masks as _board_region_masks
from .verify import fitted_pieces_violate, gate_touch_margin, rescue_eligible, share_group_key
from .smooth import own_net_soft_mask, smooth_branch_pieces, smooth_grid_path, smoothed_wire_mm
from .smooth import resolve_config as resolve_smooth_config
from .biarc import pieces_wire_cells
from .output import routes_to_tracks_vias, apply_routes_to_tree
from .stage_chunks import _terminals_for_net, _group_terminals_by_region, _branch_paths, _apply_route_to_grid, _chunk_terminals, union_paths, _merge_chunk_results, _branch_cells, _flood_cover, _check_chunk_coverage, _resume_progress, _rip_up_nets_from_model, _strip_gnd_blockers_from_model
from .stage_grid import _default_cost_map, _default_transition_masks, _resolve_grid_layers, _disallowed_terminal_layers, _supply_root_cell, _punched_search_grid, _tht_sites_by_net, _seeded_coverage, _width_cells, _inner_plane_layers_for_net, _plan_plane_stubs
from .stage_smooth import _resolve_gate_trip, _route_escapes_board, _smooth_result



def _chunk_failure_diag(
    router, term_set, region_secondary, seed_cells, trunk_root,
    res, routing_grid, net_name, region_idx, chunk_idx, n_chunks,
    should_cancel, replay: bool,
):
    """Structured record of a failed chunk flight.

    Names the pin set (cells + board mm), the kept-copper seed it tried to
    join, every search window tried (cells + board-mm box), and a
    ``diagnosis`` with machine-usable retry hints — so a second try can do
    something differently instead of replaying the same ladder blind.

    When the first flight ran blind (router debug off) and ``replay`` is
    set, the identical search is re-run once with tracing on: a failed
    flight stamps nothing, so the replay reproduces the same searches
    deterministically. Skipped when cancellation is pending.
    """
    attempts = [dict(a) for a in (res.debug_attempts or ())]
    replayed = False
    if replay and not attempts and not (
        should_cancel is not None and should_cancel()
    ):
        dbg = SingleNetRouter(
            router.grid, router.cost_grid, router.cost_map,
            router.tht_via_mask, router.via_block_mask,
            max_expansions=router.max_expansions,
            via_clearance_cells=router.via_clearance_cells,
            window_margin_cells=router.window_margin_cells,
            window_growth=router.window_growth,
            window_retries=router.window_retries,
            corridor=router.corridor_enabled,
            debug=True,
            max_expansions_per_net=router.max_expansions_per_net,
        )
        dbg.via_ok_entry = router.via_ok_entry
        dbg_res = dbg.route(
            list(term_set), region_secondary,
            seed_cells=seed_cells, trunk_root=trunk_root,
        )
        attempts = [dict(a) for a in (dbg_res.debug_attempts or ())]
        replayed = True
    pins_mm = []
    for c, r, lyr in term_set:
        x_mm, y_mm = routing_grid.cell_center_mm(c, r)
        pins_mm.append([x_mm, y_mm, lyr])
    seed_list = [tuple(s) for s in (seed_cells or ())]
    seed_box_mm = None
    if seed_list:
        cols = [s[0] for s in seed_list]
        rows = [s[1] for s in seed_list]
        ox, oy = routing_grid.origin_mm
        res_mm = routing_grid.resolution_mm
        seed_box_mm = [round(ox + min(cols) * res_mm, 3),
                       round(oy + min(rows) * res_mm, 3),
                       round(ox + (max(cols) + 1) * res_mm, 3),
                       round(oy + (max(rows) + 1) * res_mm, 3)]
    diagnosis = diagnose_attempts(
        attempts,
        max_expansions=router.max_expansions,
        corridor_enabled=router.corridor_enabled,
        net_budget=router.max_expansions_per_net,
        has_seed=bool(seed_list),
    )
    if not attempts:
        diagnosis["hints"].append(
            "no attempt detail recorded (router debug off and replay skipped)"
        )
    return {
        "net": net_name,
        "region": region_idx,
        "chunk": [chunk_idx, n_chunks],
        "pins": [[c, r, lyr] for c, r, lyr in term_set],
        "pins_mm": pins_mm,
        "seed_cells": len(seed_list),
        "seed_box_mm": seed_box_mm,
        "expansions": res.expansions,
        "attempts": attempts,
        "replayed_with_trace": replayed,
        "diagnosis": diagnosis,
    }


@dataclass
class _FlightSetup:
    """Per-net search context shared by full routes and gap flights.

    Extracted from ``_route_one_net`` so single-flight setup (router,
    masks, emit-gate share) cannot drift from batch routing: identical
    inputs produce an identical context.
    """
    width_mm: float
    secondary: np.ndarray  # base per-net cost overlay (unconfined)
    region_masks: List
    router: SingleNetRouter
    corridor: int  # corridor width in cells for stamping/guidance
    clearance_mm: float
    clearance_radius: int
    soft_mask: np.ndarray
    secondary_for: Callable  # (region_idx, term_set) -> (secondary, blocked)
    verify_share_for: Callable  # (region_idx) -> emit-gate share tuple


def _setup_flight(
    model: BoardModel,
    routing_grid: RoutingGrid,
    cost_grid: np.ndarray,
    cost_map,
    tht_via_mask,
    via_block_mask,
    obstacle_cache,
    params,
    net_name: str,
    extra_power_nets: Collection[str] = (),
    corridors=None,
    corridor_min_thick: float = 0.0,
    pour_nets: Collection[str] = (),
    static_fp: Optional[int] = None,
    via_ok_tracker=None,  # run-level ViaOkTracker (None = legacy one-shot mask)
) -> _FlightSetup:
    """Build the per-net search context (code-motion from _route_one_net).

    ``static_fp`` is the batch-level :func:`static_fingerprint` value:
    non-copper inputs never change as nets route, so callers route the
    batch through one computation instead of paying ~17 ms per net.
    None (single-flight callers) computes it directly.
    """
    region_masks: List = []
    if model.board_regions:
        try:
            region_masks = _board_region_masks(model, routing_grid)
        except Exception:
            region_masks = []
    width_mm = resolve_track_width(model, params, net_name, extra_power_nets)
    margin_mm = max(0.0, float(getattr(params, "fanout_margin_mm", 0.0) or 0.0))
    secondary = build_fanout_cost(
        model, routing_grid, net_name,
        clearance_mm=resolve_clearance(model, params, net_name),
        track_half_mm=width_mm / 2.0,
        extra_margin_mm=margin_mm,
    )
    # Per-net router on the punched search grid: own THT drill
    # discs read FREE so free-hole hops beat adjacent vias. The
    # shared cost grid keeps every stamp for later nets.
    router = SingleNetRouter(
        routing_grid, _punched_search_grid(
            model, routing_grid, cost_grid, net_name,
            clearance_mm=resolve_clearance(model, params, net_name)),
        cost_map, tht_via_mask, via_block_mask,
        max_expansions=params.max_expansions,
        window_margin_cells=params.window_margin_cells,
        window_growth=params.window_growth,
        window_retries=params.window_retries,
        corridor=params.corridor,
        debug=params.debug,
        max_expansions_per_net=params.max_expansions_per_net,
    )
    if obstacle_cache is not None:
        router.via_ok_entry = obstacle_cache.active_entry()
    if via_ok_tracker is not None:
        router.via_ok_tracker = via_ok_tracker
    if corridors is not None and net_name not in pour_nets:
        # Pour-severance toll: tracks may still cross narrow pour
        # channels, but prefer paths that keep pours whole.
        secondary = secondary + severance_cost(
            corridors, corridor_min_thick, routing_grid,
            width_mm=width_mm,
            clearance_mm=resolve_clearance(model, params, net_name),
            weight=params.pour_severance_weight,
        )
    corridor = _width_cells(width_mm, params.grid_resolution_mm)
    clearance_mm = resolve_clearance(model, params, net_name)
    clearance_radius = track_corridor_radius_cells(
        width_mm, clearance_mm + margin_mm, params.grid_resolution_mm,
    )
    router.via_clearance_cells = via_halo_radius_cells(
        params.via_size_mm, clearance_mm, width_mm / 2.0,
        params.grid_resolution_mm,
    )
    # The window must admit at least a halo-width detour around both
    # terminals, or every pair pays a growth retry.
    router.window_margin_cells = max(
        params.window_margin_cells,
        router.via_clearance_cells,
        clearance_radius,
    )
    soft_mask = own_net_soft_mask(
        model, routing_grid, net_name,
        clearance_mm=clearance_mm, track_half_mm=width_mm / 2.0,
    )
    # Cross-net shared emit-gate index: this net's own hard cells plus
    # a group key per region confinement, so later nets reuse the first
    # net's un-carved base index instead of rebuilding per mask.
    # Millisecond-scale (one net's pads); the sharing itself happens in
    # verify.fitted_pieces_violate.
    _verify_own = own_net_hard_cells(
        model, routing_grid, net_name,
        clearance_mm=clearance_mm, track_half_mm=width_mm / 2.0,
    )
    try:
        _verify_vias_fp = hash(tuple(model.vias))
    except TypeError:
        _verify_vias_fp = hash(repr(tuple(model.vias)))
    _verify_base_fp = (
        static_fp if static_fp is not None else static_fingerprint(model),
        _verify_vias_fp,
    )
    _verify_group_keys: Dict[int, tuple] = {}

    def _verify_share_for(region_idx: int) -> tuple:
        tag = (region_idx if 0 <= region_idx < len(region_masks)
               else -1)
        if tag not in _verify_group_keys:
            _verify_group_keys[tag] = share_group_key(
                _verify_base_fp[0], _verify_base_fp[1], routing_grid,
                clearance_mm, width_mm / 2.0, margin_mm, tag)
        return (_verify_group_keys[tag], _verify_own)

    # Per-region confinement: copy the base secondary and mark every
    # cell outside the group's region BLOCKED, so A*, corridor
    # guidance, and the smoother can never wander across the
    # inter-panel gap or outside the board outline (single-region
    # nets included — e.g. tube111 C2TubeOut hugging the edge from
    # outside). Region -1 (outside all polygons) stays
    # unconstrained, preserving single-board behavior.
    def _secondary_for_region(region_idx: int, term_set=None):
        if (
            region_idx < 0
            or region_idx >= len(region_masks)
        ):
            return secondary, secondary >= SECONDARY_BLOCKED
        mask = region_masks[region_idx]
        confined = secondary.copy()
        # copyto, not boolean-mask assignment: the fancy-index scatter
        # costs ~26 ms on a 2x1385x1190 grid, the single C pass ~1.4 ms.
        # Elementwise-identical (same cells written, same value).
        np.copyto(confined, SECONDARY_BLOCKED, where=~mask)
        if term_set:
            # Terminals stay enterable: the region raster can clip a
            # pad cell at the polygon edge, and the secondary has no
            # goal exemption by design — so restore this group's own
            # terminal cells to their unmasked values.
            for (c, r, l) in term_set:
                if (0 <= l < confined.shape[0]
                        and 0 <= r < confined.shape[1]
                        and 0 <= c < confined.shape[2]):
                    confined[l, r, c] = secondary[l, r, c]
        return confined, confined >= SECONDARY_BLOCKED

    return _FlightSetup(
        width_mm=width_mm, secondary=secondary, region_masks=region_masks,
        router=router, corridor=corridor, clearance_mm=clearance_mm,
        clearance_radius=clearance_radius, soft_mask=soft_mask,
        secondary_for=_secondary_for_region,
        verify_share_for=_verify_share_for,
    )


def _terminal_exit_stubs(raw_res, model, net_name, grid, cost_grid,
                         soft_mask, blocked_mask, clearance_mm,
                         track_half_mm, corridor_radius, hard_radius,
                         pinned):
    """Pad exit stubs for branch endpoints on crowded sibling pads.

    Returns ``(branches, extra_pinned)`` with a straight stub waypoint
    inserted after/before each terminal pad whose own footprint carries a
    *different-net* sibling pad too close to fit a track between them
    (gap < 2 * (clearance + track half) + one grid cell): the xcross
    case, where a diagonal pulled straight out of the pad grazes the
    sibling. The stub runs from the pad center directly away from the
    nearest such sibling, long enough to clear the pad halo, and is only
    inserted when the stub leg itself is walkable — otherwise that end
    keeps legacy behavior. Pads without a crowding sibling (the common
    case) are untouched, so open boards route exactly as before.

    Callers add ``extra_pinned`` to the smoother's pinned set so the
    pull never shortcuts across a stub (``_split_runs`` keeps each
    terminal→stub leg verbatim) and starts its chords from the stub end
    instead of from inside the pad halo.
    """
    from .smooth import _line_walkable

    res_mm = float(getattr(grid, "resolution_mm", 0.0) or 0.0)
    if not (res_mm > 0.0):
        return None, set()
    try:
        branches = [list(b) for b in _branch_paths(raw_res)]
    except Exception:
        return None, set()
    if not branches:
        return None, set()
    try:
        ox, oy = grid.origin_mm
    except (TypeError, ValueError, AttributeError):
        return None, set()
    try:
        layer_names = list(getattr(grid, "layers", None) or ())
        layer_to_idx = dict(getattr(grid, "layer_to_idx", None)
                            or {n: i for i, n in enumerate(layer_names)})
    except (TypeError, ValueError, AttributeError):
        return None, set()
    try:
        cg = np.asarray(cost_grid)
        sm = (np.asarray(soft_mask) if soft_mask is not None else None)
        bm = (np.asarray(blocked_mask) if blocked_mask is not None else None)
        H, W = int(cg.shape[1]), int(cg.shape[2])
    except (TypeError, ValueError, AttributeError, IndexError):
        return None, set()

    # Same-net pads with crowding siblings (same footprint, other net,
    # gap too tight for a track to pass between).
    try:
        fit_gap_mm = 2.0 * (float(clearance_mm) + float(track_half_mm))
    except (TypeError, ValueError):
        return None, set()
    crowded = []
    for fp in getattr(model, "footprints", ()):
        fp_layer = getattr(fp, "layer", "")
        try:
            own_here = [p for p in getattr(fp, "pads", (None,)) or ()
                        if getattr(p, "net_name", None) == net_name]
            sibs = [p for p in getattr(fp, "pads", (None,)) or ()
                    if getattr(p, "net_name", None) not in (None, "", net_name)]
        except (TypeError, ValueError, AttributeError):
            continue
        if not own_here or not sibs:
            continue
        for pad in own_here:
            try:
                px = float(pad.position.x_mm)
                py = float(pad.position.y_mm)
                w = float(pad.size_mm[0])
                h = float(pad.size_mm[1])
                thru = bool(getattr(pad, "is_through_hole", False))
            except (TypeError, ValueError, AttributeError, IndexError):
                continue
            pc = int(round((px - ox) / res_mm - 0.5))
            pr = int(round((py - oy) / res_mm - 0.5))
            half = max(w, h) / 2.0
            for sib in sibs:
                try:
                    sx = float(sib.position.x_mm)
                    sy = float(sib.position.y_mm)
                    sw = float(sib.size_mm[0])
                    sh = float(sib.size_mm[1])
                except (TypeError, ValueError, AttributeError, IndexError):
                    continue
                gap = (math.hypot(sx - px, sy - py) - half
                       - max(sw, sh) / 2.0)
                if gap < fit_gap_mm + res_mm:
                    crowded.append((px, py, pc, pr, half, fp_layer, thru,
                                    sx, sy))
                    break
    if not crowded:
        return None, set()

    def _walkable(c0, r0, c1, r1, layer):
        try:
            cL = cg[layer] if layer < cg.shape[0] else None
            sL = (sm[layer] if sm is not None and layer < sm.shape[0]
                  else None)
            bL = (bm[layer] if bm is not None and layer < bm.shape[0]
                  else None)
        except (IndexError, TypeError, ValueError):
            return False
        if cL is None:
            return False
        try:
            return bool(_line_walkable(
                int(c0), int(r0), int(c1), int(r1), cL, sL,
                int(BLOCKED), int(corridor_radius),
                bL, int(hard_radius)))
        except Exception:
            return False

    new_branches = []
    extra_pinned = set()
    for branch in branches:
        if len(branch) < 2:
            new_branches.append(list(branch))
            continue
        new_branch = list(branch)
        # (end_idx, neighbor_idx): start runs forward, end runs backward.
        for end_idx, n_idx in ((0, 1), (len(branch) - 1, len(branch) - 2)):
            ec = int(branch[end_idx][0])
            er = int(branch[end_idx][1])
            try:
                el = int(branch[end_idx][2])
            except (TypeError, ValueError, IndexError):
                continue
            match = None
            for (px, py, pc, pr, half, fp_layer, thru, sx, sy) in crowded:
                if abs(pc - ec) + abs(pr - er) > 3:
                    continue
                try:
                    li = int(layer_to_idx.get(fp_layer, el))
                except (TypeError, ValueError):
                    li = el
                # Layer must agree unless the pad is plated through.
                if li != el and not thru:
                    continue
                match = (px, py, pc, pr, half, sx, sy)
                break
            if match is None:
                continue
            px, py, pc, pr, half, sx, sy = match
            # Straight out, away from the crowding sibling.
            dx, dy = px - sx, py - sy
            norm = math.hypot(dx, dy)
            if norm <= 1e-9:
                continue
            ux, uy = dx / norm, dy / norm
            stub_len_mm = half + clearance_mm + track_half_mm + res_mm
            ex = int(round((px + ux * stub_len_mm - ox) / res_mm - 0.5))
            ey = int(round((py + uy * stub_len_mm - oy) / res_mm - 0.5))
            if not (0 <= ey < H and 0 <= ex < W):
                continue
            if (ex, ey) == (ec, er):
                continue
            if not _walkable(ec, er, ex, ey, el):
                continue
            if end_idx == 0:
                new_branch = [(ec, er, el), (ex, ey, el)] + [
                    w for w in new_branch[1:]
                    if tuple(w[:2]) in pinned
                    or tuple(w[:2]) in extra_pinned
                    or abs(int(w[0]) - ec) + abs(int(w[1]) - er) > max(
                        1, int(round(stub_len_mm / res_mm)) + 1)
                    or w == new_branch[-1]
                ]
            else:
                new_branch = [
                    w for w in new_branch[:-1]
                    if tuple(w[:2]) in pinned
                    or tuple(w[:2]) in extra_pinned
                    or abs(int(w[0]) - ec) + abs(int(w[1]) - er) > max(
                        1, int(round(stub_len_mm / res_mm)) + 1)
                    or w == new_branch[0]
                ] + [(ex, ey, el), (ec, er, el)]
            extra_pinned.add((ex, ey))
        new_branches.append(new_branch)
    if not extra_pinned:
        return None, set()
    return new_branches, extra_pinned


def _route_chunk(
    router, term_set, region_secondary, progress_cb, should_cancel,
    seed_cells, cost_grid, routing_grid, corridor, soft_mask,
    clearance_radius, region_blocked_mask, fit_arcs, max_radius_mm,
    min_leg_mm, sample_mm, keep_min_radius_mm, params, model, timings,
    net_name, tag="", width_mm=None, verify_share=None, trunk_root=None,
    region_idx=None, chunk_idx=None, n_chunks=None,
):
    """Route+smooth+stamp one terminal set within its region.

    Mirrors the former ``_fly`` closure: one A* flight, then smoothing,
    board-escape check, and corridor stamp. Returns
    ``(result|None, failure|None)`` where failure is ``(kind, reason,
    diag)``: kind is ``"budget"|"gate"`` on deterministic budget
    exhaustion / emit-gate trips or ``"open"`` otherwise, and ``diag`` is
    a structured failure record (pins, seed, windows tried, retry hints)
    for the failure log — None only when there is no search to describe.

    Expansion accounting and progress reporting are owned by the caller
    (``progress_cb`` plus post-route ``expansions``/_report bookkeeping), so
    retry/resume semantics and observable outputs are unchanged.
    """
    pinned = {(c, r) for c, r, _ in term_set}
    t_astar = time.perf_counter()
    res = router.route(
        list(term_set), region_secondary,
        progress_cb=progress_cb,
        should_cancel=should_cancel,
        seed_cells=seed_cells,
        trunk_root=trunk_root,
    )
    timings["astar_s"] = timings.get("astar_s", 0.0) + (
        time.perf_counter() - t_astar
    )
    search_expansions = res.expansions

    def _failed(kind: str, reason: str, diag=None):
        """Failure return that keeps the search's expansion count visible.

        Failed flights used to discard their RouteResult, so refused nets
        reported 0 expansions while burning millions of hidden pops;
        accumulate them under ``expansions_failed`` instead (success-side
        ``expansions`` semantics are unchanged).
        """
        timings["expansions_failed"] = (
            timings.get("expansions_failed", 0) + search_expansions
        )
        return None, (kind, reason, diag)

    def _search_diag(kind: str):
        """Failure record for a dead search; replays with tracing when blind.

        Only ``"open"`` (no path found / left board) replays, and only
        when ``params.debug`` is set: the replay re-runs the whole failed
        window ladder on a tracing router purely to populate ``attempts``
        for retry hints (nothing it computes is stamped), roughly
        doubling a failed net's wall time. Production runs keep the
        pins/seed/diagnosis record with the replay-skipped hint instead.
        Budget failures would burn another full budget, and gate trips
        are a fitting problem, not a search problem, so neither replays.
        """
        return _chunk_failure_diag(
            router, term_set, region_secondary, seed_cells, trunk_root,
            res, routing_grid, net_name, region_idx, chunk_idx, n_chunks,
            should_cancel,
            replay=(kind == "open" and bool(params.debug)),
        )
    if params.debug and res.debug_attempts:
        key = f"debug_attempts_{net_name}"
        timings[key] = timings.get(key, []) + [dict(a) for a in res.debug_attempts]
    if res.success:
        t_smooth = time.perf_counter()
        raw_res = res
        # Smoother engagement (RoutingParams.smooth_effort, default 2.0):
        # 0 emits the raw search path; otherwise a single configured pass
        # (2.0 reproduces the legacy kwargs exactly). Retries live in
        # polish (against the finished board), not here. Gate trips below
        # still funnel into the same touch-vs-graze resolution. (Pad exit
        # stubs live in the gate-trip path below: they re-run only when
        # the adopted copper trips on endpoint-local dust, so clean nets
        # are untouched.)
        effort = float(getattr(params, "smooth_effort", 2.0))
        if effort <= 0.0:
            res = raw_res
            t0 = time.perf_counter()
            violations = fitted_pieces_violate(
                list(_branch_paths(res)),
                tuple(getattr(res, "arc_pieces", ()) or ()),
                region_blocked_mask, routing_grid,
                timings=timings, share=verify_share,
            )
            timings["smooth_verify_s"] = timings.get("smooth_verify_s", 0.0) + (
                time.perf_counter() - t0)
        else:
            # Single pass: smooth once at the requested effort, verify
            # once. A clean pass is adopted; endpoint-local dust the emit
            # gate would rescue is kept (same verdict the gate would
            # reach); anything else falls through to the gate-trip path
            # below (stub retry, then loud failure or raw fallback).
            cfg = resolve_smooth_config(effort, params)
            res = raw_res
            violations: List[str] = []
            cand = _smooth_result(
                raw_res, cost_grid, routing_grid, corridor,
                pinned=pinned, soft_mask=soft_mask,
                clearance_radius=clearance_radius,
                blocked_mask=region_blocked_mask,
                fit_arcs=cfg["fit_arcs"],
                max_radius_mm=cfg["max_radius_mm"],
                min_leg_mm=cfg["min_leg_mm"],
                sample_mm=cfg["sample_mm"],
                keep_min_radius_mm=cfg["keep_min_radius_mm"],
                timings=timings,
            )
            t0 = time.perf_counter()
            cand_violations = fitted_pieces_violate(
                list(_branch_paths(cand)),
                tuple(getattr(cand, "arc_pieces", ()) or ()),
                region_blocked_mask, routing_grid,
                timings=timings, share=verify_share,
            )
            timings["smooth_verify_s"] = timings.get("smooth_verify_s", 0.0) + (
                time.perf_counter() - t0)
            if not cand_violations:
                res, violations = cand, []
            else:
                cells: list = []
                try:
                    fitted_pieces_violate(
                        list(_branch_paths(cand)),
                        tuple(getattr(cand, "arc_pieces", ()) or ()),
                        region_blocked_mask, routing_grid,
                        share=verify_share, hit_cells=cells,
                        full_scan=True,
                    )
                    rescued = rescue_eligible(
                        cells, list(_branch_paths(cand)),
                        tuple(getattr(cand, "arc_pieces", ()) or ()),
                        routing_grid, model, net_name,
                        resolve_track_width(model, params, net_name) / 2.0,
                        resolve_clearance(model, params, net_name),
                    )
                except Exception:
                    rescued = False
                if rescued:
                    res, violations = cand, []
                    timings["emit_gate_dust_rescued"] = (
                        timings.get("emit_gate_dust_rescued", 0) + 1
                    )
                else:
                    res, violations = cand, cand_violations
        # Emit gate: the fitter's stitching loop emits straight connector
        # stubs without re-checking them; a stub that skips collapsed
        # vertices can cut straight across foreign copper the search and
        # smoother both refused. Fail loudly here (before stamping) instead
        # of writing DRC-dirty copper downstream — unless the trip is
        # raster dust with no copper touched, in which case the legal
        # search path is stamped as-is. Before that fallback, try pad
        # exit stubs once: a trip hugging a terminal pad is often an
        # entry/exit graze that a short straight stub avoids entirely.
        # The retry only runs on tripped copper (never on clean runs),
        # and its result is adopted only when gate-clean and no longer
        # than the raw fallback — so clean runs and failing runs are
        # untouched.
        if violations:
            if width_mm is None:
                width_mm = resolve_track_width(model, params, net_name)
            stub_adopted = False
            if effort > 0.0:
                try:
                    _stub_width = resolve_track_width(
                        model, params, net_name)
                    _stub_branches, _stub_pinned = _terminal_exit_stubs(
                        raw_res, model, net_name, routing_grid, cost_grid,
                        soft_mask, region_blocked_mask,
                        resolve_clearance(model, params, net_name),
                        _stub_width / 2.0,
                        max(max(0, corridor - 1), max(0, clearance_radius)),
                        max(0, corridor - 1),
                        set(pinned),
                    )
                except Exception:
                    _stub_branches, _stub_pinned = None, set()
                if _stub_branches is not None:
                    try:
                        _union = union_paths(_stub_branches, coerce_int=True)
                        _stub_raw = replace(
                            raw_res,
                            branches=tuple(tuple(b) for b in _stub_branches),
                            path=_union,
                        )
                        _cfg2 = resolve_smooth_config(2.0, params)
                        _cand2 = _smooth_result(
                            _stub_raw, cost_grid, routing_grid, corridor,
                            pinned=set(pinned) | set(_stub_pinned),
                            soft_mask=soft_mask,
                            clearance_radius=clearance_radius,
                            blocked_mask=region_blocked_mask,
                            fit_arcs=_cfg2["fit_arcs"],
                            max_radius_mm=_cfg2["max_radius_mm"],
                            min_leg_mm=_cfg2["min_leg_mm"],
                            sample_mm=_cfg2["sample_mm"],
                            keep_min_radius_mm=_cfg2["keep_min_radius_mm"],
                            timings=timings,
                        )
                        _v2 = fitted_pieces_violate(
                            list(_branch_paths(_cand2)),
                            tuple(getattr(_cand2, "arc_pieces", ()) or ()),
                            region_blocked_mask, routing_grid,
                            timings=timings, share=verify_share,
                        )
                        try:
                            _raw_wire = float(getattr(raw_res, "wire_mm",
                                                      math.inf))
                            _w2 = float(getattr(_cand2, "wire_mm",
                                                math.inf))
                        except (TypeError, ValueError):
                            _raw_wire, _w2 = math.inf, math.inf
                        if not _v2 and _w2 <= _raw_wire:
                            res, violations = _cand2, []
                            stub_adopted = True
                            timings["emit_gate_stub_used"] = (
                                timings.get("emit_gate_stub_used", 0) + 1)
                    except (ValueError, TypeError, AttributeError, IndexError):
                        pass
            if not stub_adopted:
                chosen, gate_failure, fell_back = _resolve_gate_trip(
                    raw_res, res, violations, region_blocked_mask,
                    routing_grid, model, net_name, width_mm / 2.0,
                    resolve_clearance(model, params, net_name), verify_share,
                )
                if gate_failure is not None:
                    # Deterministic on an unchanged grid like budget exhaustion:
                    # the same search + smoother will reproduce the same fitted
                    # copper, so retrying only burns time — fail immediately.
                    return _failed("gate", f"{tag}{gate_failure}",
                                   _search_diag("gate"))
                res = chosen
                if fell_back:
                    timings["emit_gate_fallback_raw"] = (
                        timings.get("emit_gate_fallback_raw", 0) + 1
                    )
                elif violations:
                    timings["emit_gate_dust_rescued"] = (
                        timings.get("emit_gate_dust_rescued", 0) + 1
                    )
        timings["smooth_s"] = timings.get("smooth_s", 0.0) + (
            time.perf_counter() - t_smooth
        )
        if _route_escapes_board(res, routing_grid, model, term_set):
            return _failed("open", f"{tag}route left board outline",
                           _search_diag("open"))
        _apply_route_to_grid(
            res, cost_grid, routing_grid, corridor,
            extra_radius=clearance_radius,
            via_clearance_cells=router.via_clearance_cells,
        )
        return res, None
    if (
        params.max_expansions_per_net > 0
        and res.expansions >= params.max_expansions_per_net
    ):
        reason = (
            f"{tag}expansion budget exhausted ({res.expansions} >= "
            f"max_expansions_per_net={params.max_expansions_per_net}): "
            f"no DRC-clean path found within budget — try a narrower "
            f"track width, smaller piecemeal chunks, or a "
            f"coarser grid"
        )
        return _failed("budget", reason, _search_diag("budget"))
    return _failed("open", f"{tag}no path found", _search_diag("open"))


def route_gap_terminals(
    model: BoardModel,
    net_name: str,
    cut_points: Collection[Tuple[float, float, str]],
    *,
    params,
    seed_tracks: Collection[Tuple[float, float, float, str]] = (),
    seed_vias: Collection[Tuple[float, float]] = (),
    punch_tracks: Collection[Tuple[float, float, float, str]] = (),
    punch_vias: Collection[Tuple[float, float]] = (),
    extra_power_nets: Collection[str] = (),
    progress_cb=None,
    should_cancel=None,
) -> Tuple[Optional[NetRouteResult], str, Optional[RoutingGrid], float,
           Optional[str]]:
    """Single gap-reconnect flight for a severed net (Tier-2 shove).

    ``cut_points`` are ``(x_mm, y_mm, layer)`` island endpoints to join
    — the ONLY search terminals (already-connected pads are deliberately
    excluded: a search ending on the nearest pad would join it without
    ever crossing the gap). ``seed_tracks`` / ``seed_vias`` are
    kept-copper geometry rasterized as walkable join targets
    (prior-chunk semantics: the flight terminates on kept copper
    instead of re-routing it). Builds fresh grids from ``model``,
    routes one flight through the shared setup (:func:`_setup_flight`,
    identical to batch routing), and returns ``(result|None, reason,
    grid|None, width_mm, coverage)``. ``result`` holds ONLY new gap
    copper (kept copper is not re-emitted); ``coverage`` is None when
    every cut, isolated pad, and net pad joins one island over
    kept-plus-new copper (pads added as coverage-only cells inside),
    else a reason string. Callers emit via
    :func:`output.routes_to_tracks_vias` and adopt on clean coverage.
    """
    width_mm = resolve_track_width(model, params, net_name, extra_power_nets)
    grid_layers = _resolve_grid_layers(model, params)
    routing_grid = create_grid_from_model(
        model, params.grid_resolution_mm, params.margin_mm, layers=grid_layers)
    total_cells = (len(routing_grid.layers) * routing_grid.width_cells
                   * routing_grid.height_cells)
    if total_cells > MAX_GRID_CELLS_TOTAL:
        return (None, f"gap: routing grid too large ({total_cells} cells)",
                None, width_mm, None)
    cost_grid = build_occupancy_grid(
        model, routing_grid, default_clearance_mm=params.clearance_mm)
    # Punch evicted copper back to FREE: kept stubs stamp their clearance
    # halo over the cleared gap, sealing it on the grid although the
    # copper is gone (hard BLOCKED, so a soft-cell rule would miss it).
    # Cells covered by current-model copper stay blocked — only stale
    # halo from removed copper yields — and the emit gate still
    # enforces clearance, so this only opens genuinely empty corridor.
    H0, W0 = cost_grid.shape[1], cost_grid.shape[2]
    hard_all, hard_foreign = set(), set()

    def _cover(x0, y0, x1, y1, layer, foreign: bool) -> None:
        li = routing_grid.layer_to_idx.get(layer)
        if li is None:
            return
        c0, r0 = board_to_grid(routing_grid, x0, y0)
        c1, r1 = board_to_grid(routing_grid, x1, y1)
        for (c, r) in bresenham_cells(c0, r0, c1, r1):
            if 0 <= r < H0 and 0 <= c < W0:
                hard_all.add((c, r, li))
                if foreign:
                    hard_foreign.add((c, r, li))

    for t in model.tracks:
        _cover(t.start.x_mm, t.start.y_mm, t.end.x_mm, t.end.y_mm,
               t.layer, t.net_name != net_name)
    for a in model.arcs:
        _cover(a.start.x_mm, a.start.y_mm, a.mid.x_mm, a.mid.y_mm,
               a.layer, a.net_name != net_name)
        _cover(a.mid.x_mm, a.mid.y_mm, a.end.x_mm, a.end.y_mm,
               a.layer, a.net_name != net_name)
    for v in model.vias:
        c, r = board_to_grid(routing_grid, v.position.x_mm, v.position.y_mm)
        if 0 <= r < H0 and 0 <= c < W0:
            for li in range(cost_grid.shape[0]):
                hard_all.add((c, r, li))
                if v.net_name != net_name:
                    hard_foreign.add((c, r, li))
    for (x0, y0, x1, y1, layer) in punch_tracks or ():
        li = routing_grid.layer_to_idx.get(layer)
        if li is None:
            continue
        c0, r0 = board_to_grid(routing_grid, x0, y0)
        c1, r1 = board_to_grid(routing_grid, x1, y1)
        for (c, r) in bresenham_cells(c0, r0, c1, r1):
            if (0 <= r < H0 and 0 <= c < W0
                    and (c, r, li) not in hard_all):
                cost_grid[li, r, c] = FREE
    for (x, y) in punch_vias or ():
        c, r = board_to_grid(routing_grid, x, y)
        if 0 <= r < H0 and 0 <= c < W0:
            for li in range(cost_grid.shape[0]):
                if (c, r, li) not in hard_all:
                    cost_grid[li, r, c] = FREE
    # Cut cells punch through own-copper hardness: a gap flight must be
    # able to terminate on the kept stub it joins — landing new copper
    # exactly on the severed end is the joint. Cells covered by foreign
    # copper stay blocked (landing there would short); the emit gate
    # double-guards the joint.
    terms_pre = []
    for (x, y, layer) in cut_points or ():
        li = routing_grid.layer_to_idx.get(layer)
        if li is None:
            continue
        c, r = board_to_grid(routing_grid, x, y)
        if 0 <= r < H0 and 0 <= c < W0:
            if (c, r, li) not in hard_foreign:
                cost_grid[li, r, c] = FREE
            terms_pre.append((c, r, li))
    tht_via_mask, via_block_mask = _default_transition_masks(
        model, routing_grid, params)
    cost_map = _default_cost_map(params)
    setup = _setup_flight(
        model, routing_grid, cost_grid, cost_map, tht_via_mask,
        via_block_mask, None, params, net_name, extra_power_nets,
    )
    H, W = routing_grid.height_cells, routing_grid.width_cells
    terms = list(terms_pre)
    seen = set()
    term_set = [t for t in terms if not (t in seen or seen.add(t))]
    if len(term_set) < 2:
        return None, "gap: fewer than 2 terminals", routing_grid, width_mm, None
    pad_terms = _terminals_for_net(
        model, routing_grid, net_name, params.allowed_layers)
    cover_set = term_set + [t for t in pad_terms if t not in seen]
    seed = []
    for (x0, y0, x1, y1, layer) in seed_tracks or ():
        li = routing_grid.layer_to_idx.get(layer)
        if li is None:
            continue
        c0, r0 = board_to_grid(routing_grid, x0, y0)
        c1, r1 = board_to_grid(routing_grid, x1, y1)
        for (c, r) in bresenham_cells(c0, r0, c1, r1):
            if 0 <= r < H and 0 <= c < W:
                seed.append((c, r, li))
    for (x, y) in seed_vias or ():
        c, r = board_to_grid(routing_grid, x, y)
        if 0 <= r < H and 0 <= c < W:
            for li in range(len(routing_grid.layers)):
                seed.append((c, r, li))
    region_idx = -1
    if setup.region_masks:
        for idx in range(len(setup.region_masks)):
            try:
                mask = setup.region_masks[idx]
            except (IndexError, TypeError):
                continue
            try:
                inside = all(0 <= r < H and 0 <= c < W and bool(mask[r, c])
                             for (c, r, _l) in term_set)
            except (IndexError, TypeError):
                continue
            if inside:
                region_idx = idx
                break
    region_secondary, region_blocked = setup.secondary_for(
        region_idx, term_set)

    def _pcb(done: int, total: int, exp: int) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(net_name, done, total, exp)
        except Exception:  # user callback must not break routing
            pass

    timings: Dict = {}
    result, failure = _route_chunk(
        setup.router, term_set, region_secondary, _pcb, should_cancel,
        seed or None, cost_grid, routing_grid, setup.corridor,
        setup.soft_mask, setup.clearance_radius, region_blocked,
        params.arc_fillets, params.arc_max_radius_mm, params.arc_min_leg_mm,
        params.arc_sample_mm, params.arc_keep_min_radius_mm, params, model,
        timings, net_name, "gap ", setup.width_mm,
        verify_share=setup.verify_share_for(region_idx),
        trunk_root=None, region_idx=region_idx, chunk_idx=0, n_chunks=1,
    )
    if failure is not None:
        kind, reason, _diag = failure
        return None, f"gap {kind}: {reason}", routing_grid, width_mm, None
    assert result is not None
    coverage = _seeded_coverage(cover_set, seed or (), result)
    return result, "", routing_grid, width_mm, coverage
