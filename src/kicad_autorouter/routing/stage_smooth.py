"""Stage A helpers: emit gate, smoothing, copper stamping (split from route_raw)."""
from __future__ import annotations
import math
import time
from dataclasses import replace
from typing import Dict, List, Optional, Tuple
import numpy as np
from ..board_model import BoardModel
from .grid import RoutingGrid
from .obstacles import BLOCKED, stamp_square
from .router import RouteResult as NetRouteResult
from .verify import fitted_pieces_violate, gate_touch_margin, rescue_eligible
from .smooth import smooth_branch_pieces, smooth_grid_path, smoothed_wire_mm
from .smooth import resolve_config as resolve_smooth_config
from .biarc import pieces_wire_cells
from .stage_chunks import _branch_paths, _branch_cells, union_paths



def _resolve_gate_trip(raw_res, smoothed_res, violations,
                       region_blocked_mask, routing_grid, model, net_name,
                       track_half_mm, clearance_mm=0.0, verify_share=None):
    """Decide a gate trip: rule-clear copper vs loud failure.

    ``violations`` tripped on the smoothed/fitted copper. ``is_touch`` now
    means the emitted track violates ``clearance_mm`` (overlap or
    rule-dirty graze the raster hard tier cannot see), so such trips fail
    loudly instead of falling back to the raw path — raw shares the same
    hole. When the track clears ``clearance_mm`` the trip is dust
    (guard-ring graze or cell-corner clip): if :func:`rescue_eligible`
    proves it endpoint-local *and* clear of every hard-exclusion class
    by ``clearance_mm``, the smoothed copper is stamped as-is; otherwise
    fall back to the pre-smooth search path, which the gate is re-run
    against, and a clean raw path is returned for stamping instead of
    failing the net. A raw path that also trips (with no rescue) stays a
    loud ``("gate", reason)`` failure.

    Returns ``(result|None, failure|None, fell_back: bool)``. Single
    fallback only — the raw re-gate is final, so this cannot loop. Future
    cost-matrix re-smoothing replaces the ``fell_back`` branch (same seam,
    same acceptance check).
    """
    is_touch, margin = gate_touch_margin(
        list(_branch_paths(smoothed_res)),
        tuple(getattr(smoothed_res, "arc_pieces", ()) or ()),
        routing_grid, model, net_name, track_half_mm, clearance_mm,
    )
    if math.isinf(margin):
        margin_txt = "no foreign copper nearby"
    else:
        margin_txt = f"min track-to-copper {margin:.3f}mm"
    if is_touch:
        detail = "; ".join(violations[:3])
        if len(violations) > 3:
            detail += f" (+{len(violations) - 3} more)"
        return None, (
            f"emit gate: fitted copper crosses hard exclusion "
            f"({detail}; {margin_txt}); failing loudly instead "
            f"of writing DRC-dirty copper"
        ), False
    if not is_touch:
        cells: list = []
        try:
            fitted_pieces_violate(
                list(_branch_paths(smoothed_res)),
                tuple(getattr(smoothed_res, "arc_pieces", ()) or ()),
                region_blocked_mask, routing_grid,
                share=verify_share, hit_cells=cells, full_scan=True,
            )
        except Exception:
            cells = []
        try:
            rescued = rescue_eligible(
                cells, list(_branch_paths(smoothed_res)),
                tuple(getattr(smoothed_res, "arc_pieces", ()) or ()),
                routing_grid, model, net_name, track_half_mm,
                clearance_mm,
            )
        except Exception:
            rescued = False
        if rescued:
            return smoothed_res, None, False
    raw_violations = fitted_pieces_violate(
        list(_branch_paths(raw_res)), (),
        region_blocked_mask, routing_grid,
    )
    if raw_violations:
        detail = "; ".join(raw_violations[:3])
        if len(raw_violations) > 3:
            detail += f" (+{len(raw_violations) - 3} more)"
        return None, (
            f"emit gate: smoothed copper grazes hard exclusion "
            f"({margin_txt}) but the raw search path also trips "
            f"({detail}); failing loudly instead "
            f"of writing DRC-dirty copper"
        ), False
    return raw_res, None, True


def _route_escapes_board(result, grid, model, term_set=None) -> bool:
    """True when any routed branch cell lies outside every board polygon.

    Raster-consistent with the hard outside-board block: terminal cells
    are exempt (the region raster can clip a pad cell at the polygon
    edge, and A* already allows terminals on blocked cells).
    """
    from .obstacles import inside_board_mask

    inside = inside_board_mask(grid, model)
    if inside is None:
        return False
    exempt = {(c, r) for c, r, _ in (term_set or ())}
    for c, r, _ in _branch_cells(result):
        if (c, r) in exempt:
            continue
        if not (0 <= r < inside.shape[0] and 0 <= c < inside.shape[1]):
            return True
        if not inside[r, c]:
            return True
    return False




def _smooth_result(result, cost_grid, routing_grid, corridor: int, blocked_threshold: int = 100,
                   pinned=None, soft_mask=None, clearance_radius: int = 0,
                   blocked_mask=None,
                   fit_arcs: bool = False, max_radius_mm: float = 1.0,
                   min_leg_mm: float = 0.3, sample_mm: float = 0.05,
                   keep_min_radius_mm: float = 0.0, timings=None):
    """String-pull a successful route before corridor reservation.

    Smoothing runs against the pre-route grid (which must NOT yet contain
    this path's own corridor) so shortcuts are clearance-checked against
    real obstacles. Each MST branch is smoothed independently (branch
    endpoints are terminals, so no spoke can be skipped and no teleport
    segment is ever created); via nodes/layers and ``pinned`` terminals
    are preserved by ``smooth_grid_path``. Returns a result with smoothed
    branches, rebuilt union path, and recomputed wire length.

    With ``fit_arcs``, the pull-string also ranks chords by admitted
    fillet radius and each smoothed run is filleted in place (single
    clearance gate, where the runs live); fitted arc chains ride on
    ``result.arc_pieces`` for arc-aware reservation/emission. Topology
    stays the smoother's job — arcs only round corners, never move them.

    ``keep_min_radius_mm > 0`` additionally spares open corners admitting
    at least that fillet radius from the pull-string (same gate the
    fitter uses, so kept corners always become arcs). Staircases keep
    collapsing — only corners with roomy legs survive. ``0`` keeps legacy
    shortcut-everything smoothing.

    The shortcut footprint covers ``max(corridor - 1, clearance_radius)``
    so smoothed copper keeps clearance to neighboring tracks, and
    penalized cells (other nets' pad rings, edge keepout) block shortcuts
    except inside ``soft_mask`` (this net's own pads). ``blocked_mask`` is
    the per-net hard exclusion (foreign-THT rings): shortcuts may never
    cross it, with no soft exemption. The exclusion disc already bakes pad
    copper + clearance + track half-width, so it is tested with the track
    copper half-width (``corridor - 1``) rather than the full clearance
    halo — otherwise clearance is counted twice and no detour chord is
    ever walkable. The cost grid keeps the full halo.
    """
    corridor_radius = max(max(0, corridor - 1), max(0, clearance_radius))
    hard_radius = max(0, corridor - 1)

    # Collect all branch endpoints as pinned points (Steiner connections).
    # New branches attach to interior points of existing branches via goal_mask;
    # smoothing must not shortcut past those attachment points.
    all_pinned = set(pinned or set())
    for branch in _branch_paths(result):
        if branch:
            all_pinned.add((branch[0][0], branch[0][1]))   # branch start
            all_pinned.add((branch[-1][0], branch[-1][1])) # branch end

    res_mm = routing_grid.resolution_mm
    max_r_cells = max_radius_mm / res_mm if res_mm > 0 else 0.0
    new_branches = []
    new_pieces = []
    changed = False
    any_arc = False
    for branch in _branch_paths(result):
        if fit_arcs and res_mm > 0:
            smoothed, pieces = smooth_branch_pieces(
                branch,
                cost_grid,
                blocked_threshold=blocked_threshold,
                corridor_radius=corridor_radius,
                pinned=all_pinned,
                soft_mask=soft_mask,
                blocked_mask=blocked_mask,
                hard_radius=hard_radius,
                rank_radius_cells=max_r_cells,
                max_radius_cells=max_r_cells,
                min_leg_cells=min_leg_mm / res_mm,
                sample_spacing_cells=max(sample_mm / res_mm, 0.25),
                keep_min_radius_cells=(keep_min_radius_mm / res_mm
                                        if res_mm > 0 else 0.0),
                timings=timings,
            )
            new_branches.append(smoothed)
            new_pieces.append(tuple(pieces))
            if any(p[0] == "arc" for p in pieces):
                any_arc = True
            if len(smoothed) != len(branch):
                changed = True
        else:
            t0 = time.perf_counter()
            smoothed = smooth_grid_path(
                branch,
                cost_grid,
                blocked_threshold=blocked_threshold,
                corridor_radius=corridor_radius,
                pinned=all_pinned,
                soft_mask=soft_mask,
                blocked_mask=blocked_mask,
                hard_radius=hard_radius,
            )
            if timings is not None:
                timings["smooth_pull_s"] = timings.get("smooth_pull_s", 0.0) + (
                    time.perf_counter() - t0)
            new_branches.append(smoothed)
            new_pieces.append(())
            if len(smoothed) != len(branch):
                changed = True
    if not changed and not any_arc:
        return result
    union_path = union_paths(new_branches)
    # Wire matches emission branch by branch: fitted pieces where arcs
    # exist (emission uses them), smoothed chords elsewhere (legacy path).
    wire_mm = 0.0
    for smoothed, pieces in zip(new_branches, new_pieces):
        if any(p[0] == "arc" for p in pieces):
            wire_mm += pieces_wire_cells(
                [p for p in pieces if p[0] in ("seg", "arc")]) * res_mm
        else:
            wire_mm += smoothed_wire_mm(smoothed, res_mm)
    return replace(
        result,
        path=union_path,
        branches=tuple(tuple(b) for b in new_branches),
        arc_pieces=tuple(new_pieces) if any_arc else (),
        wire_mm=wire_mm,
    )


