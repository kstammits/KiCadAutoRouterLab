"""Staged routing service: trial -> refine relay -> repair -> shove.

One entry point shared by the headless pipeline and (later) the UI
worker, so both run the same failure recovery instead of drifting.
No printing here: progress flows through the callbacks, outcomes ride
the returned result plus the info dict; callers log.
"""

from __future__ import annotations

from dataclasses import replace as _replace
from typing import Collection, Dict, List, Optional, Tuple

from ..board_model import BoardModel, board_model
from ..sexpr import SExpr
from .pipeline import RoutingParams, RoutingResult, refine_routing, route_nets
from .shove import (
    ShoveReport,
    retry_failed_with_shove,
    retry_failed_with_signal_shove,
)


def _adopt_shove_fixes(result: RoutingResult, tree: SExpr,
                       reports: Dict[str, ShoveReport]) -> RoutingResult:
    """Fold routed shove fixes into the staged result (no-op when none)."""
    fixed = [n for n, rep in reports.items() if rep.routed]
    if not fixed:
        return result
    return _replace(
        result,
        pcb_tree=tree,
        nets_routed=result.nets_routed + len(fixed),
        nets_failed=result.nets_failed - len(fixed),
        failed_reasons={n: r for n, r in result.failed_reasons.items()
                        if n not in set(fixed)},
        failed_diagnostics={n: d for n, d in
                            (result.failed_diagnostics or {}).items()
                            if n not in set(fixed)},
    )


def _repair_casualties(
    result: RoutingResult,
    net_names: List[str],
    trial_result: RoutingResult,
    protected_nets: Collection[str],
    params: RoutingParams,
    *,
    extra_power_nets: Collection[str] = (),
    progress_cb=None,  # fn(net, done, total, expansions) — best-effort
    should_cancel=None,  # fn() -> bool — cooperative cancel
) -> Tuple[RoutingResult, Dict[str, ShoveReport]]:
    """Re-route refine casualties solo on the adopted board.

    Whole-board refine adoption can break nets trial 0 routed (order
    effects). Each casualty gets one bounded solo ``route_nets`` pass on
    the winner board; success folds through ``_adopt_shove_fixes``,
    failure keeps the winner untouched, so repair never regresses.
    Returns ``(result, reports)`` with a ``ShoveReport`` per casualty.
    """
    reports: Dict[str, ShoveReport] = {}
    trial_ok = set(net_names) - set(trial_result.failed_reasons or {})
    casualties = [n for n in net_names
                  if n in trial_ok and n in (result.failed_reasons or {})]
    for net in casualties:
        if result.pcb_tree is None:
            break
        solo, _, _ = route_nets(
            model=board_model(result.pcb_tree),
            net_names=[net],
            protected_nets=set(protected_nets),
            params=params,
            original_pcb_tree=result.pcb_tree,
            extra_power_nets=tuple(extra_power_nets),
            progress_cb=progress_cb,
            should_cancel=should_cancel,
        )
        if net not in (solo.failed_reasons or {}) and solo.pcb_tree is not None:
            reports[net] = ShoveReport(
                net=net, routed=True, reason="casualty repair adopted")
            result = _adopt_shove_fixes(result, solo.pcb_tree, {net: reports[net]})
        else:
            reason = (solo.failed_reasons or {}).get(net, "no path")
            reports[net] = ShoveReport(
                net=net, routed=False,
                reason=f"casualty repair failed: {reason}")
    return result, reports


def route_with_retry(
    model: BoardModel,
    tree: SExpr,
    net_names: List[str],
    protected_nets: Collection[str],
    params: RoutingParams,
    *,
    extra_power_nets: Collection[str] = (),
    progress_cb=None,  # fn(net, done, total, expansions) — best-effort
    stage_cb=None,  # fn(idx, total) — refine candidate progress
    should_cancel=None,  # fn() -> bool — cooperative cancel
) -> Tuple[RoutingResult, dict]:
    """Route a net list with failure-gated retries.

    Trial 0 is always a single ``route_nets`` pass. When it leaves
    failures and ``params.refine_seeds > 1``, a ``refine_routing``
    retry follows (trial 0 redone plus failure-first relay trials);
    the retry wins only on a strict routed-count win, so clean runs
    cost one pass and ties keep trial 0. Nets the adoption broke
    (routed in trial 0, failed in the winner) get one solo repair
    pass each on the adopted board. Remaining failures then go
    through ``retry_failed_with_shove`` (GND sever plus partial-tolerant
    restore, strict-improvement-or-rollback per net) and finally
    ``retry_failed_with_signal_shove`` (signal sever plus restore or
    gap reconnect, rollback per net otherwise). Returns ``(result, info)``
    where ``info``
    carries the trial-0 result (``info["trial_result"]``), the refine
    run (``info["refine_result"]``, None when skipped), whether the
    refine run was adopted (``info["refine_adopted"]``), the per-net
    ``info["repair_reports"]`` for adoption casualties, and the per-net
    ``info["shove_reports"]`` / ``info["signal_shove_reports"]`` for
    caller logging.
    """
    result, _, _ = route_nets(
        model=model,
        net_names=list(net_names),
        protected_nets=set(protected_nets),
        params=params,
        original_pcb_tree=tree,
        extra_power_nets=tuple(extra_power_nets),
        progress_cb=progress_cb,
        should_cancel=should_cancel,
    )
    trial_result = result
    refine_result: Optional[RoutingResult] = None
    refine_adopted = False
    if result.nets_failed and params.refine_seeds > 1:
        refine_result, _, _ = refine_routing(
            model=model,
            net_names=list(net_names),
            protected_nets=set(protected_nets),
            params=params,
            original_pcb_tree=tree,
            extra_power_nets=tuple(extra_power_nets),
            progress_cb=progress_cb,
            should_cancel=should_cancel,
            stage_cb=stage_cb,
        )
        if refine_result.nets_routed > result.nets_routed:
            result = refine_result
            refine_adopted = True
    repair_reports: Dict[str, ShoveReport] = {}
    if result.nets_failed and result.pcb_tree is not None:
        result, repair_reports = _repair_casualties(
            result, list(net_names), trial_result, set(protected_nets),
            params, extra_power_nets=tuple(extra_power_nets),
            progress_cb=progress_cb, should_cancel=should_cancel,
        )
    shove_reports: Dict[str, ShoveReport] = {}
    if result.nets_failed and result.pcb_tree is not None:
        shove_model = board_model(result.pcb_tree)
        still_failed = [n for n in net_names if n in result.failed_reasons]
        shove_model, shove_tree, shove_reports = retry_failed_with_shove(
            shove_model, result.pcb_tree, still_failed, set(protected_nets),
            params, progress_cb=progress_cb, should_cancel=should_cancel,
        )
        result = _adopt_shove_fixes(result, shove_tree, shove_reports)
    signal_shove_reports: Dict[str, ShoveReport] = {}
    if result.nets_failed and result.pcb_tree is not None:
        sig_model = board_model(result.pcb_tree)
        still_failed = [n for n in net_names if n in result.failed_reasons]
        sig_model, sig_tree, signal_shove_reports = (
            retry_failed_with_signal_shove(
                sig_model, result.pcb_tree, still_failed,
                set(protected_nets), params,
                extra_power=tuple(extra_power_nets),
                progress_cb=progress_cb, should_cancel=should_cancel,
            )
        )
        result = _adopt_shove_fixes(result, sig_tree, signal_shove_reports)
    return result, {"trial_result": trial_result,
                     "refine_result": refine_result,
                     "refine_adopted": refine_adopted,
                     "repair_reports": repair_reports,
                     "shove_reports": shove_reports,
                     "signal_shove_reports": signal_shove_reports}
