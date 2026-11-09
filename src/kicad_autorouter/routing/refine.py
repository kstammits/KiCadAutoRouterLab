"""Stage C of the routing pipeline: order-space refinement search.

Order-space search: re-route the nets under several seeded orders. Trial 0 runs
the input order; trials 1..N permute within tangle groups. The best trial goes
straight to :func:`finish_routes` — no re-route replay.

:func:`refine_routing` calls :func:`route_raw`, :func:`finish_routes`, and
:func:`_tangle_groups` through the :mod:`routing.pipeline` namespace (resolved
at call time) so that callers/tests can monkeypatch ``pipeline.route_raw`` etc.
"""

from __future__ import annotations

import math
import random
from dataclasses import replace
from typing import Collection, Dict, List, Optional, Set, Tuple

import numpy as np

from ..board_model import BoardModel
from .dsu import DisjointSet
from .grid import RoutingGrid
from .params import RoutingParams
from .pipeline import RawRoutes, RoutingResult  # RoutingResult bound in pipeline first
from .router import RoutingCancelled


def refine_routing(
    model: BoardModel,
    net_names: List[str],
    protected_nets: Set[str],
    cost_grid: Optional[np.ndarray] = None,
    routing_grid: Optional[RoutingGrid] = None,
    obstacle_cache=None,
    params: Optional[RoutingParams] = None,
    original_pcb_tree: Optional = None,
    extra_power_nets: Collection[str] = (),
    progress_cb=None,  # fn(net_name, done, total, expansions) — best-effort
    should_cancel=None,  # fn() -> bool — checked between candidates/nets
    stage_cb=None,  # fn(candidate_idx, total_candidates) — best-effort
) -> Tuple[RoutingResult, np.ndarray, RoutingGrid]:
    """Order-space search: re-route the nets under several seeded orders.

    Sequential routing is order-dependent — an early track can wall off a
    later net. Trial 0 runs the input order; trials 1..N permute within
    tangle groups (nets whose pad boxes overlap; isolated nets hold their
    positions). Each trial relays the previous trial's failed nets first
    (failure-first ordering). The best trial by (nets routed, then
    ``total_wire_mm + via_cost_mm * vias``) goes straight to
    ``finish_routes`` — no re-route replay. Stitching runs once on the
    winner, never per trial, so trials compare raw routing alone. If every
    trial fails to route anything, the incoming copper is kept untouched
    (``winner_seed == -1``).

    Refused nets (protected, or over ``max_terminals_per_net``) are left
    untouched and reported in ``failed_reasons``.
    """
    # Resolved at call time so ``monkeypatch.setattr(pipeline, "route_raw", …)``
    # (and the same for finish_routes / _tangle_groups) takes effect.
    from .pipeline import route_raw as _route_raw, finish_routes as _finish_routes
    from .pipeline import _tangle_groups as _tangle_groups

    if params is None:
        params = RoutingParams()
    seeds = max(1, int(params.refine_seeds))
    base_seed = int(params.refine_seed)
    protected = set(protected_nets or ())

    def _report_stage(idx: int, total: int) -> None:
        if stage_cb is None:
            return
        try:
            stage_cb(idx, total)
        except Exception:
            pass

    # Gate: pad counts decide the over-cap refusals.
    pad_counts: Dict[str, int] = {}
    for fp in model.footprints:
        for pad in fp.pads:
            if pad.net_name:
                pad_counts[pad.net_name] = pad_counts.get(pad.net_name, 0) + 1
    eligible: List[str] = []
    refused: Dict[str, str] = {}
    for name in net_names:
        if name in protected:
            refused[name] = "protected — refine never rips protected copper"
        elif (
            params.max_terminals_per_net > 0
            and pad_counts.get(name, 0) > params.max_terminals_per_net
        ):
            refused[name] = (
                f"{pad_counts.get(name, 0)} pads over max_terminals_per_net "
                f"({params.max_terminals_per_net}) — shrink the set or raise the cap"
            )
        else:
            eligible.append(name)

    # Trials run stitch/DRC/refill-free (raw copper only); the winner goes
    # straight to finish_routes. Fresh cost grid per trial — rebuilt from
    # the same ripped model, so only the order varies.
    cand_params = replace(
        params, stitch_gnd_vias=False, run_drc=False, refill_zones=False)
    groups = _tangle_groups(eligible, model)
    candidates: List[dict] = []
    best_raw: Optional[RawRoutes] = None
    best_seed = -1
    best_routed, best_score = 0, math.inf
    best_expansions = 0
    cancelled = False
    seen_orders: Set[Tuple[str, ...]] = set()
    prev_failed: Set[str] = set()  # relay memory: trial k+1 routes trial k's failed nets first
    shared_pour = None  # corridor map: identical across trials (same set
    # of ripped nets, same grid), so trial 0 builds it and later trials
    # reuse it instead of re-rastering every pour on every seed.
    for k in range(seeds):
        if should_cancel is not None and should_cancel():
            cancelled = True
            break
        if k == 0:
            seed, order = base_seed, list(eligible)  # identity trial
        else:
            seed = base_seed + k
            order = _refine_order(eligible, groups, seed, prev_failed)
        _report_stage(k + 1, seeds)
        key = tuple(order)
        if key in seen_orders:
            continue  # duplicate order: routing is deterministic per order
        seen_orders.add(key)
        try:
            raw, _, _ = _route_raw(
                model=model,
                net_names=order,
                protected_nets=protected,
                cost_grid=None,
                routing_grid=routing_grid,
                obstacle_cache=obstacle_cache,
                params=cand_params,
                original_pcb_tree=original_pcb_tree,
                extra_power_nets=extra_power_nets,
                progress_cb=progress_cb,
                should_cancel=should_cancel,
                precomputed_pour=shared_pour,
            )
            if shared_pour is None and raw.pour_corridors is not None:
                shared_pour = (raw.pour_corridors, raw.pour_min_thick)
        except RoutingCancelled:
            cancelled = True
            break
        score = (raw.total_wire_mm
                 + params.via_cost_mm * (len(raw.vias) + len(raw.plane_vias)))
        candidates.append({
            "seed": seed, "order": order, "nets_routed": raw.nets_routed,
            "nets_failed": raw.nets_failed, "score": round(score, 3),
            "expansions": raw.expansions_total,
        })
        best_expansions += raw.expansions_total
        ripup = getattr(raw, "ripup", None)
        prev_failed = set(getattr(ripup, "net_failures", None) or ())
        if raw.nets_routed > 0 and (raw.nets_routed, -score) > (
            best_routed, -best_score
        ):
            best_raw, best_seed = raw, seed
            best_routed, best_score = raw.nets_routed, score

    if best_raw is None:
        # Every fresh trial failed to route anything: incoming grids/tree
        # untouched, prior copper kept.
        kept = RoutingResult(
            tracks_added=0, vias_added=0, nets_routed=0,
            nets_failed=len(refused), drc_clean=True, drc_violations=0,
            pcb_tree=original_pcb_tree, failed_reasons=dict(refused),
            candidates=tuple(candidates), winner_seed=-1,
            cancelled=cancelled, expansions=best_expansions,
        )
        return kept, cost_grid, routing_grid

    # Winner goes straight to the finish stage (stitch/tree/refill/DRC) —
    # no replay pass.
    _report_stage(seeds + 1, seeds + 1)
    final, final_cost, final_grid = _finish_routes(best_raw, params)
    final.failed_reasons.update(refused)
    final.nets_failed += len(refused)
    final.candidates = tuple(candidates)
    final.winner_seed = best_seed
    final.expansions = best_expansions
    final.cancelled = final.cancelled or cancelled
    return final, final_cost, final_grid


def _tangle_groups(
    net_names: List[str], model: BoardModel
) -> List[List[str]]:
    """Group nets whose pad bounding boxes overlap (tangle candidates).

    Only nets that can physically interact reward order permutation;
    isolated nets keep their relative order in every trial. Groups come in
    first-appearance order; members follow ``net_names`` order. Pad-less
    (unknown) nets each form a singleton.
    """
    boxes: Dict[str, Tuple[float, float, float, float]] = {}
    wanted = set(net_names)
    for fp in model.footprints:
        for pad in fp.pads:
            if pad.net_name and pad.net_name in wanted:
                x, y = pad.position.x_mm, pad.position.y_mm
                if pad.net_name in boxes:
                    x0, y0, x1, y1 = boxes[pad.net_name]
                    boxes[pad.net_name] = (
                        min(x0, x), min(y0, y), max(x1, x), max(y1, y))
                else:
                    boxes[pad.net_name] = (x, y, x, y)
    ds = DisjointSet(net_names)
    names = [n for n in net_names if n in boxes]
    for i, a in enumerate(names):
        ax0, ay0, ax1, ay1 = boxes[a]
        for b in names[i + 1:]:
            bx0, by0, bx1, by1 = boxes[b]
            if ax0 <= bx1 and bx0 <= ax1 and ay0 <= by1 and by0 <= ay1:
                ds.union(a, b)
    groups: Dict[str, List[str]] = {}
    for name in net_names:
        groups.setdefault(ds.find(name), []).append(name)
    # First-appearance order of the group's earliest member.
    return sorted(groups.values(), key=lambda g: net_names.index(g[0]))


def _refine_order(
    net_names: List[str], groups: List[List[str]], seed: int,
    failed: Collection[str] = (),
) -> List[str]:
    """Permute within tangle groups; isolated nets hold their positions.

    With relay memory (``failed`` = previous trial's failed nets), those
    nets move to the front in input order and the remainder permutes
    within its clipped tangle groups. Deterministic for a given seed;
    singleton groups pass through untouched. Empty ``failed`` reproduces
    the legacy order exactly.
    """
    rng = random.Random(seed)
    failed_set = set(failed or ()) & set(net_names)
    if not failed_set:
        shuffled: Dict[str, List[str]] = {}
        for group in groups:
            if len(group) > 1:
                members = list(group)
                rng.shuffle(members)
                for name in group:
                    shuffled[name] = members
        order: List[str] = []
        emitted: Set[str] = set()
        for name in net_names:
            if name in emitted:
                continue
            if name in shuffled:
                for member in shuffled[name]:
                    if member not in emitted:
                        order.append(member)
                        emitted.add(member)
            else:
                order.append(name)
                emitted.add(name)
        return order
    head = [n for n in net_names if n in failed_set]
    rest = [n for n in net_names if n not in failed_set]
    rest_set = set(rest)
    shuffled = {}
    for group in groups:
        members = [m for m in group if m in rest_set]
        if len(members) > 1:
            clipped = list(members)
            rng.shuffle(clipped)
            for name in members:
                shuffled[name] = clipped
    order = list(head)
    emitted = set(head)
    for name in rest:
        if name in emitted:
            continue
        if name in shuffled:
            for member in shuffled[name]:
                if member not in emitted:
                    order.append(member)
                    emitted.add(member)
        else:
            order.append(name)
            emitted.add(name)
    return order
