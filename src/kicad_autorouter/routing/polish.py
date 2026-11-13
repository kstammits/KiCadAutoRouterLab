"""Stage D (opt-in): fine re-smoothing against the pinned final matrix.

After rough routing (low ``smooth_effort``) or order search, Polish
re-smooths already-routed nets one at a time at a higher effort: each
net's copper is lifted from a fresh matrix holding every other net's
final copper, re-smoothed through the effort ladder, and kept only when the result is
gate-clean *and* shorter than the incoming copper. Between rungs the
roomiest within 10% of the shortest wins, so a wider swoop beats a
grazer when it costs almost nothing. Nothing is re-searched (no A*),
via/layer topology never moves, and a net that cannot improve keeps its
incoming copper — Polish never fails a routed net.
"""

from __future__ import annotations

import math
from typing import Collection, Dict, List, Optional, Tuple

import numpy as np

from ..board_model import BoardModel
from .grid import RoutingGrid
from .obstacles import (
    build_fanout_cost,
    build_occupancy_grid,
    own_net_hard_cells,
    static_fingerprint,
    track_corridor_radius_cells,
    via_halo_radius_cells,
)
from .params import RoutingParams, resolve_clearance
from .route_raw import (
    _apply_route_to_grid,
    _branch_cells,
    _branch_paths,
    _rip_up_nets_from_model,
    _smooth_result,
    _terminals_for_net,
    _width_cells,
)
from .router import SECONDARY_BLOCKED, RoutingCancelled
from .router import RouteResult as NetRouteResult
from .smooth import (
    effort_rungs,
    own_net_soft_mask,
    resolve_config as resolve_smooth_config,
)
from .stitch import _region_masks as _board_region_masks
from .verify import fitted_pieces_violate, gate_touch_margin, share_group_key


# Tolerance band for rung selection: among gate-clean rungs within 10% of
# the shortest, keep the roomiest (largest true copper margin) instead of
# the shortest. A wider swoop wins only when it costs almost nothing;
# adoption below still requires beating the incoming copper, so polish
# never grows wire.
POLISH_WIRE_TOLERANCE = 1.10


def polish_routes(
    all_results: Dict[str, NetRouteResult],
    width_map: Dict[str, float],
    model: BoardModel,
    routing_grid: RoutingGrid,
    params: RoutingParams,
    net_names: List[str],
    effort: float,
    extra_power_nets: Collection[str] = (),
    progress_cb=None,  # fn(net_name, done, total) — best-effort
    should_cancel=None,  # fn() -> bool — checked between nets
) -> Tuple[Dict[str, NetRouteResult], dict]:
    """Re-smooth routed nets at ``effort`` against the pinned matrix.

    Args:
        all_results: net -> route result (only ``success`` entries with a
            path are candidates; the dict itself is never mutated).
        width_map: net -> emitted track width (route-time resolution).
        model: current board model (holds the routed copper as tracks).
        routing_grid: grid the routes were built on.
        params: routing params (clearance/via geometry; ``smooth_effort``
            is overridden by ``effort``).
        net_names: nets to polish, in order (each sees earlier nets'
            accepted copper).
        effort: target smoother engagement (see ``smooth.resolve_config``;
            <= 0 is a no-op returning the inputs unchanged).

    Returns ``(new_results, report)`` where ``new_results`` maps every
    input net to its (possibly improved) result and ``report`` carries
    per-net ``{status, wire_before_mm, wire_after_mm}`` plus totals.
    ``status`` is ``polished`` / ``kept`` (clean but not shorter, or
    nothing to gain) / ``reverted`` (no gate-clean rung) / ``skipped``
    (unroutable input: missing result, no path, or unknown width).
    """
    report: dict = {"nets": {}, "polished": 0, "kept": 0, "reverted": 0,
                    "skipped": 0, "wire_saved_mm": 0.0, "effort": float(effort)}
    new_results = dict(all_results)
    if float(effort) <= 0.0:
        for name in net_names:
            report["nets"][name] = {"status": "skipped",
                                    "wire_before_mm": 0.0, "wire_after_mm": 0.0}
            report["skipped"] += 1
        return new_results, report

    res_mm = routing_grid.resolution_mm
    targets = [n for n in net_names if isinstance(n, str) and n]
    ripped = _rip_up_nets_from_model(model, targets)
    try:
        base = build_occupancy_grid(ripped, routing_grid,
                                    default_clearance_mm=params.clearance_mm)
    except Exception:
        for name in targets:
            report["nets"][name] = {"status": "skipped",
                                    "wire_before_mm": 0.0, "wire_after_mm": 0.0}
            report["skipped"] += 1
        return new_results, report
    try:
        region_masks = _board_region_masks(ripped, routing_grid)
    except Exception:
        region_masks = []
    multi_region = bool(getattr(model, "board_regions", ())) and len(region_masks) > 1

    cur = {n: r for n, r in all_results.items()
           if getattr(r, "success", False) and getattr(r, "path", None)}
    total = len(targets)
    for done, net_name in enumerate(targets):
        if progress_cb is not None:
            try:
                progress_cb(net_name, done, total)
            except Exception:  # user callback must not break routing
                pass
        if should_cancel is not None and should_cancel():
            raise RoutingCancelled("cancelled before polish of "
                                   f"{net_name}")
        current = cur.get(net_name)
        width_mm = width_map.get(net_name)
        if current is None or width_mm is None:
            report["nets"][net_name] = {"status": "skipped",
                                        "wire_before_mm": 0.0,
                                        "wire_after_mm": 0.0}
            report["skipped"] += 1
            continue
        wire_before = float(getattr(current, "wire_mm", 0.0) or 0.0)
        try:
            improved = _polish_one(
                net_name, current, float(width_mm), model, ripped,
                routing_grid, base, cur, width_map, params,
                float(effort), res_mm, region_masks, multi_region,
                extra_power_nets,
            )
        except RoutingCancelled:
            raise
        except Exception:
            improved = None
        if improved is None:
            report["nets"][net_name] = {"status": "reverted",
                                        "wire_before_mm": wire_before,
                                        "wire_after_mm": wire_before}
            report["reverted"] += 1
            continue
        wire_after = float(getattr(improved, "wire_mm", wire_before) or wire_before)
        if wire_after < wire_before - 1e-9:
            new_results[net_name] = improved
            cur[net_name] = improved
            report["nets"][net_name] = {"status": "polished",
                                        "wire_before_mm": wire_before,
                                        "wire_after_mm": wire_after}
            report["polished"] += 1
            report["wire_saved_mm"] += wire_before - wire_after
        else:
            report["nets"][net_name] = {"status": "kept",
                                        "wire_before_mm": wire_before,
                                        "wire_after_mm": wire_before}
            report["kept"] += 1
    if progress_cb is not None:
        try:
            progress_cb("", total, total)
        except Exception:  # user callback must not break routing
            pass
    report["wire_saved_mm"] = round(report["wire_saved_mm"], 3)
    return new_results, report


def _polish_one(
    net_name: str,
    current: NetRouteResult,
    width_mm: float,
    model: BoardModel,
    ripped: BoardModel,
    routing_grid: RoutingGrid,
    base: np.ndarray,
    cur: Dict[str, NetRouteResult],
    width_map: Dict[str, float],
    params: RoutingParams,
    effort: float,
    res_mm: float,
    region_masks,
    multi_region: bool,
    extra_power_nets: Collection[str],
) -> Optional[NetRouteResult]:
    """Re-smooth one net against pinned copper; None when not improvable.

    ``base`` holds every net's copper except the polish targets (ripped at
    the model level); other targets' current branches are stamped on a
    copy. Strict gate: any violation reverts, no touch-vs-graze fallback —
    the incoming copper already passed emission once.
    """
    clearance_mm = resolve_clearance(model, params, net_name)
    margin_mm = max(0.0, float(getattr(params, "fanout_margin_mm", 0.0) or 0.0))
    track_half_mm = width_mm / 2.0
    secondary = build_fanout_cost(
        model, routing_grid, net_name,
        clearance_mm=clearance_mm,
        track_half_mm=track_half_mm,
        extra_margin_mm=margin_mm,
    )
    blocked: np.ndarray = secondary >= SECONDARY_BLOCKED
    if multi_region and region_masks:
        try:
            union = None
            for (c, r, _l) in _branch_cells(current):
                for m in region_masks:
                    if 0 <= r < m.shape[0] and 0 <= c < m.shape[1] and m[r, c]:
                        union = m if union is None else (union | m)
                        break
            if union is not None:
                confined = secondary.copy()
                confined[:, ~union] = SECONDARY_BLOCKED
                blocked = confined >= SECONDARY_BLOCKED
        except (ValueError, TypeError, AttributeError, IndexError):
            pass
    # Cross-net shared emit-gate index (same scheme as the route path):
    # own hard cells + a group key covering confinement identity, so
    # repeated polishes reuse one base index instead of rebuilding.
    # The unconfined case shares the route path's region tag (-1).
    try:
        _own_3d = own_net_hard_cells(
            model, routing_grid, net_name,
            clearance_mm=clearance_mm, track_half_mm=track_half_mm,
        )
        _vias_fp = hash(tuple(model.vias))
    except TypeError:
        _own_3d, _vias_fp = None, hash(repr(tuple(model.vias)))
    if _own_3d is None:
        _verify_share = None
    else:
        # Unconfined polishes share the route path's region tag (-1);
        # union-confined ones key on the union content.
        _tag = -1
        if multi_region and region_masks:
            try:
                _tag = ("union", hash(np.ascontiguousarray(union).tobytes()))
            except (NameError, TypeError, ValueError):
                _tag = -1
        _verify_share = (share_group_key(
            static_fingerprint(model), _vias_fp, routing_grid,
            clearance_mm, track_half_mm, margin_mm, _tag), _own_3d)
    cost = base.copy()
    corridor = _width_cells(width_mm, res_mm)
    clearance_radius = track_corridor_radius_cells(
        width_mm, clearance_mm + margin_mm, res_mm)
    via_radius = via_halo_radius_cells(
        params.via_size_mm, clearance_mm, track_half_mm, res_mm)
    for other, result in cur.items():
        if other == net_name:
            continue
        other_width = width_map.get(other)
        if other_width is None:
            continue
        _apply_route_to_grid(
            result, cost, routing_grid,
            _width_cells(float(other_width), res_mm),
            extra_radius=track_corridor_radius_cells(
                float(other_width), clearance_mm + margin_mm, res_mm),
            via_clearance_cells=via_halo_radius_cells(
                params.via_size_mm, clearance_mm,
                float(other_width) / 2.0, res_mm),
        )
    try:
        terminals = _terminals_for_net(
            model, routing_grid, net_name, params.allowed_layers)
    except Exception:
        terminals = []
    pinned = {(c, r) for c, r, _ in terminals}
    try:
        soft_mask = own_net_soft_mask(model, routing_grid, net_name,
                                      clearance_mm=clearance_mm,
                                      track_half_mm=track_half_mm)
    except Exception:
        soft_mask = None

    best: Optional[NetRouteResult] = None
    best_wire = math.inf
    clean: List[Tuple[object, float, float]] = []  # (cand, wire, margin)
    for sub in effort_rungs(effort):
        cfg = resolve_smooth_config(sub, params)
        cand = _smooth_result(
            best if best is not None else current,
            cost, routing_grid, corridor,
            pinned=pinned, soft_mask=soft_mask,
            clearance_radius=clearance_radius,
            blocked_mask=blocked,
            fit_arcs=cfg["fit_arcs"],
            max_radius_mm=cfg["max_radius_mm"],
            min_leg_mm=cfg["min_leg_mm"],
            sample_mm=cfg["sample_mm"],
            keep_min_radius_mm=cfg["keep_min_radius_mm"],
        )
        if fitted_pieces_violate(
            list(_branch_paths(cand)),
            tuple(getattr(cand, "arc_pieces", ()) or ()),
            blocked, routing_grid, share=_verify_share,
        ):
            continue
        if best is None:
            best, best_wire = cand, float(getattr(cand, "wire_mm", math.inf))
        wire = float(getattr(cand, "wire_mm", math.inf))
        if wire < best_wire:
            best, best_wire = cand, wire
        try:
            _, margin = gate_touch_margin(
                list(_branch_paths(cand)),
                tuple(getattr(cand, "arc_pieces", ()) or ()),
                routing_grid, model, net_name, track_half_mm,
            )
        except Exception:
            margin = float("-inf")
        clean.append((cand, wire, margin))
    if not clean:
        return None
    # Roomiest within 10% of the shortest wins; ties (including all-clear
    # boards where every margin is inf) fall back to the shortest.
    shortest = min(wire for _, wire, _ in clean)
    cap = shortest * POLISH_WIRE_TOLERANCE + 1e-9
    best, best_wire, _ = min(
        (c for c in clean if c[1] <= cap),
        key=lambda c: (-c[2], c[1]),
    )
    return best
