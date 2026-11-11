"""Routing configuration: params, validation, per-net width/clearance policy.

Leaf module: configuration + validation + per-net width/clearance
resolution (pure policy over ``BoardModel`` predicates). Grid/terminal
geometry (``RoutingGrid``, ``board_to_grid``) stays in ``pipeline.py``.
``pipeline.py`` re-exports these names so existing imports keep working.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Collection, Dict, List, Optional

from ..board_model import (
    BoardModel,
    netclass_clearance,
    resolve_track_width_mm,
)


@dataclass
class RoutingParams:
    """Routing configuration parameters."""
    grid_resolution_mm: float = 0.1
    margin_mm: float = 2.0
    via_cost_mm: float = 5.0          # Via cost in mm wire equivalent
    track_width_mm: float = 0.25      # Default signal track width
    power_width_mm: float = 0.5       # Power/ground track width
    clearance_mm: float = 0.2         # Default clearance between tracks
    via_size_mm: float = 0.8          # Via outer diameter
    via_drill_mm: float = 0.4         # Via drill diameter
    max_attempts_per_net: int = 3     # Retry attempts per net
    run_drc: bool = False             # Run DRC after routing (slow)
    debug: bool = False               # Enable debug output (cost grid, search frontier)
    net_widths: Dict[str, float] = field(default_factory=dict)  # Explicit per-net trace widths (override)
    heuristic_weight: float = 3.0     # Weighted-A* greediness (lower respects penalties)
    max_expansions: int = 2_000_000   # A* node-expansion cap per search (fail fast, don't hang)
    max_expansions_per_net: int = 4_000_000  # Fail-fast cap on total
    # expansions across one net's pair-searches (0 = no cap). Bounds the
    # ~n-1 capped A* searches × window retries that make dense multi-terminal
    # nets burn tens of millions of expansions; failing nets report the
    # budget in their failure reason instead of hanging the job. Sized so
    # one budget-burn stays under ~100s even at the worst observed rate
    # (~20ms/kexp on large boards); healthy nets route far below this
    # (DCCF 20-net batch: 1.3M total).
    max_terminals_per_net: int = 16  # Piecemeal tripwire on net size
    # (grid terminals); larger nets auto-split into terminal chunks
    # (see piecemeal_chunk_terminals) instead of burning hours of
    # Steiner pair-searches in a single flight. 0 = no cap (route whole,
    # no chunking).
    piecemeal_chunk_terminals: int = 12  # Terminals per piecemeal chunk
    # for nets over max_terminals_per_net (consecutive chunks overlap by
    # one terminal so sub-trees join). 0 disables chunking: over-cap nets
    # fail fast with an explicit reason (prior behavior).
    stitch_gnd_vias: bool = True      # Auto-repair GND pour continuity after routing
    strip_gnd_blockers: bool = True  # Evict GND vias/tracks upfront (rebuildable via stitcher);
    # GND-only exception to protected_nets: signal/power copper is never
    # evicted except when explicitly requested, so tracks can't rip each other.
    stitch_vias_max: int = 32         # Cap on auto stitching vias
    stitch_bridges_max: int = 16      # Cap on auto GND bridge tracks (repairs)
    stitch_bridge_max_mm: float = 30.0  # Longest single bridge track
    stitch_bridge_width_mm: float = 0.3  # Bridge track width (narrower than
    # power_width_mm: bridges squeeze through tight pour gaps, so a thin
    # width avoids clearance violations there.
    stitch_min_island_mm2: float = 2.0  # Unfed pour islands below this area
    # are ignored (never stitched, never counted unjoined): a via joining
    # a part-less sliver to the pour connects nothing.
    pour_severance_weight: float = 30.0  # Soft cost for tracks severing a pour (0 disables)
    fanout_margin_mm: float = 0.0  # Extra steering push around foreign pads (soft outer halo + smoother corridor + reservation); 0 = DRC clearance only
    window_margin_cells: int = 32  # Initial A* bbox dilation around a pair (cells)
    window_growth: float = 2.0  # Search-window expansion factor per retry
    window_retries: int = 3  # Window attempts per pair (last = full board)
    corridor: bool = True  # Attempt coarse corridor guidance when initial window fails
    refill_zones: bool = False  # Refill copper pours after routing (kicad-cli --refill-zones --save-board)
    arc_fillets: bool = True  # Tangent-arc corner fillets (biarc post-pass)
    # When True, smoothed branches get sharp interior corners replaced with
    # tangent circular arcs (clearance-gated, vias/pins untouched) and emit
    # KiCad (arc) copper. False = straight-segment output (prior behavior).
    arc_max_radius_mm: float = 10.0  # Tangent-length cap for corner fillets; R = t / tan(phi/2). API/POST accepts larger (e.g. 25.4 for a full inch); radii sliders stay hidden, set-and-forget at calibrated default.
    arc_min_leg_mm: float = 0.3  # Corners with a shorter leg keep the sharp joint
    arc_sample_mm: float = 0.05  # Arc clearance-gate sample spacing
    arc_keep_min_radius_mm: float = 0.0  # Spare open corners admitting >= this fillet radius from the pull-string
    # (same pass, same clearance gate — kept corners always become arcs).
    # Census-tuned: 0.5 adds arcs with fewer sharp leftovers and ~2% wire;
    # 1.0 is nearly inert at default cap, 0.25 buys more arcs at ~4% wire
    # with occasional sharp regressions. 0.0 = legacy shortcut-everything.
    # Staircases still collapse; only corners with roomy legs survive, so
    # this buys broad arcs with air.
    smooth_effort: float = 2.0  # Smoother engagement: 0 skips smoothing
    # (raw A* Manhattan), 1 is shortcut-only without arcs, 2 is the full
    # default (arcs + keep radius). Fractional values interpolate the keep
    # radius between anchors; (2, 3] adds a bounded deterministic
    # re-attempt ladder (denser arc sampling, stronger keep, extra passes).
    # 3 is the high-effort retry rung. Resolved by smooth.resolve_config.
    fanout_planes: bool = False  # Via-to-plane stubs for plane-owned nets (inner zones)
    # When True, a ground/power-effective net owning an inner-copper zone
    # (In*.Cu) is satisfied with short outer stubs + through-vias tapping
    # the plane instead of long outer MST tracks. Two-layer preset forces
    # False (prior behavior retained); four-layer preset forces True.
    fanout_plane_vias_max: int = 16  # Cap on plane-fanout vias per route_nets call
    refine_seeds: int = 3  # Candidate orderings tried by refine_routing
    # (baseline current copper + refine_seeds shuffled re-routes; best wins).
    refine_seed: int = 0  # Base RNG seed for refine order shuffles (0 = fixed
    # sequence; reproducible across runs).
    retry_stages: bool = True  # Run the staged post-trial chain in
    # route_with_retry (refine relay, casualty repair, GND/signal shove).
    # False returns the trial-0 result untouched, so a clean board provably
    # costs one pass and recovery stays out of the UI path unless asked.
    allowed_layers: Optional[List[str]] = None  # Copper subset to route on (None = all stackup layers).
    # Enforced in route_nets: terminals on disallowed layers fail the net
    # with an explicit reason, and A* moves / via landings are confined to
    # the allowed subset via the grid shape. The grid itself is outer-only
    # (F.Cu/B.Cu); inner-layer entries validate but do not add routable
    # layers — inner-layer routing is not supported (see board_mode_spec).

    def to_dict(self) -> dict:
        return {
            "grid_resolution_mm": self.grid_resolution_mm,
            "margin_mm": self.margin_mm,
            "via_cost_mm": self.via_cost_mm,
            "track_width_mm": self.track_width_mm,
            "power_width_mm": self.power_width_mm,
            "clearance_mm": self.clearance_mm,
            "via_size_mm": self.via_size_mm,
            "via_drill_mm": self.via_drill_mm,
            "max_attempts_per_net": self.max_attempts_per_net,
            "run_drc": self.run_drc,
            "debug": self.debug,
            "net_widths": dict(self.net_widths),
            "heuristic_weight": self.heuristic_weight,
            "max_expansions": self.max_expansions,
            "max_expansions_per_net": self.max_expansions_per_net,
            "max_terminals_per_net": self.max_terminals_per_net,
            "piecemeal_chunk_terminals": self.piecemeal_chunk_terminals,
            "stitch_gnd_vias": self.stitch_gnd_vias,
            "strip_gnd_blockers": self.strip_gnd_blockers,
            "stitch_vias_max": self.stitch_vias_max,
            "stitch_bridges_max": self.stitch_bridges_max,
            "stitch_bridge_max_mm": self.stitch_bridge_max_mm,
            "stitch_bridge_width_mm": self.stitch_bridge_width_mm,
            "stitch_min_island_mm2": self.stitch_min_island_mm2,
            "pour_severance_weight": self.pour_severance_weight,
            "fanout_margin_mm": self.fanout_margin_mm,
            "window_margin_cells": self.window_margin_cells,
            "window_growth": self.window_growth,
            "window_retries": self.window_retries,
            "corridor": self.corridor,
            "refill_zones": self.refill_zones,
            "arc_fillets": self.arc_fillets,
            "arc_max_radius_mm": self.arc_max_radius_mm,
            "arc_min_leg_mm": self.arc_min_leg_mm,
            "arc_sample_mm": self.arc_sample_mm,
            "arc_keep_min_radius_mm": self.arc_keep_min_radius_mm,
            "smooth_effort": self.smooth_effort,
            "fanout_planes": self.fanout_planes,
            "fanout_plane_vias_max": self.fanout_plane_vias_max,
            "refine_seeds": self.refine_seeds,
            "refine_seed": self.refine_seed,
            "retry_stages": self.retry_stages,
            "allowed_layers": list(self.allowed_layers) if self.allowed_layers is not None else None,
        }

    @classmethod
    def from_dict(cls, d: dict, strict: bool = False) -> "RoutingParams":
        # Filter to only known keys
        valid_keys = {
            "grid_resolution_mm", "margin_mm", "via_cost_mm",
            "track_width_mm", "power_width_mm", "clearance_mm", "via_size_mm",
            "via_drill_mm", "max_attempts_per_net", "run_drc", "debug",
            "net_widths", "heuristic_weight", "max_expansions",
            "max_expansions_per_net", "max_terminals_per_net",
            "piecemeal_chunk_terminals",
            "stitch_gnd_vias", "strip_gnd_blockers", "stitch_vias_max", "stitch_bridges_max",
            "stitch_bridge_max_mm", "stitch_bridge_width_mm", "stitch_min_island_mm2", "pour_severance_weight", "fanout_margin_mm",
            "window_margin_cells", "window_growth", "window_retries",
            "corridor", "refill_zones", "allowed_layers",
            "arc_fillets", "arc_max_radius_mm", "arc_min_leg_mm",
            "arc_sample_mm", "arc_keep_min_radius_mm", "smooth_effort",
            "fanout_planes", "fanout_plane_vias_max",
            "refine_seeds", "refine_seed", "retry_stages",
        }
        unknown = sorted(k for k in d if k not in valid_keys)
        if strict and unknown:
            raise ValueError(f"unknown routing params: {unknown}")
        filtered = {k: v for k, v in d.items() if k in valid_keys}
        if isinstance(filtered.get("allowed_layers"), (list, tuple)):
            filtered["allowed_layers"] = list(filtered["allowed_layers"])
        # Anything else (e.g. a bare string, which list() would shred
        # into characters) is left for _validate_routing_params to reject.
        params = cls(**filtered)
        _validate_routing_params(params)
        return params


def _validate_routing_params(params: "RoutingParams") -> None:
    """Reject corrupt routing params (unvalidated client input otherwise
    reaches grid math as zero-divisors or DoS-scale budgets)."""
    nonneg = (
        "margin_mm", "via_cost_mm", "track_width_mm", "power_width_mm",
        "clearance_mm", "via_size_mm", "via_drill_mm", "heuristic_weight",
        "stitch_bridge_max_mm", "stitch_bridge_width_mm", "stitch_min_island_mm2",
        "pour_severance_weight", "fanout_margin_mm",
        "arc_max_radius_mm", "arc_min_leg_mm", "arc_sample_mm",
        "arc_keep_min_radius_mm",
    )
    # smooth_effort has its own range check below (0..3), so it is not
    # part of the plain nonneg group.
    for name in nonneg:
        value = getattr(params, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a number")
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
        if value < 0:
            raise ValueError(f"{name} must be >= 0")
    if (
        isinstance(params.grid_resolution_mm, bool)
        or not isinstance(params.grid_resolution_mm, (int, float))
        or not math.isfinite(params.grid_resolution_mm)
        or params.grid_resolution_mm <= 0
    ):
        raise ValueError("grid_resolution_mm must be a positive finite number")
    for name in (
        "max_attempts_per_net", "max_expansions", "max_expansions_per_net", "max_terminals_per_net",
        "piecemeal_chunk_terminals", "stitch_vias_max",
        "stitch_bridges_max", "fanout_plane_vias_max", "window_margin_cells", "window_retries",
        "refine_seeds", "refine_seed",
    ):
        value = getattr(params, name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
        if value < 0:
            raise ValueError(f"{name} must be >= 0")
    if params.max_expansions > 100_000_000:
        raise ValueError("max_expansions is capped at 100M per search (single-flight server)")
    if params.max_expansions_per_net > 1_000_000_000:
        raise ValueError("max_expansions_per_net is capped at 1B per net (single-flight server)")
    if params.stitch_vias_max > 100_000 or params.stitch_bridges_max > 100_000:
        raise ValueError("stitch budgets are capped at 100k")
    if params.fanout_plane_vias_max > 100_000:
        raise ValueError("fanout_plane_vias_max is capped at 100k")
    if params.refine_seeds > 8:
        raise ValueError("refine_seeds is capped at 8 (each seed re-routes the set)")
    if (
        not isinstance(params.window_growth, (int, float))
        or isinstance(params.window_growth, bool)
        or not math.isfinite(params.window_growth)
        or params.window_growth <= 0
    ):
        raise ValueError("window_growth must be a positive finite number")
    if (
        isinstance(params.smooth_effort, bool)
        or not isinstance(params.smooth_effort, (int, float))
        or not math.isfinite(params.smooth_effort)
        or not 0.0 <= params.smooth_effort <= 3.0
    ):
        raise ValueError("smooth_effort must be a finite number in [0, 3]")
    for name in (
        "run_drc", "debug", "stitch_gnd_vias", "strip_gnd_blockers",
        "corridor", "refill_zones", "fanout_planes", "arc_fillets",
        "retry_stages",
    ):
        if not isinstance(getattr(params, name), bool):
            raise ValueError(f"{name} must be a boolean")
    if not isinstance(params.net_widths, dict) or any(
        not isinstance(v, (int, float)) or isinstance(v, bool)
        or not math.isfinite(v) or v < 0
        for v in params.net_widths.values()
    ):
        raise ValueError("net_widths must map net names to non-negative finite widths")
    if params.allowed_layers is not None and (
        not isinstance(params.allowed_layers, (list, tuple))
        or any(not isinstance(layer, str) for layer in params.allowed_layers)
    ):
        raise ValueError("allowed_layers must be null or a list of layer names")


    # Upper bound on total routing cells (all layers) per route_nets call.
# DCCF at 0.1mm ~= 9.6M cells (routes in ~1s); at 0.05mm ~= 38M cells
# (multi-minute A* + ~150MB g-score arrays). Fail fast above this.
MAX_GRID_CELLS_TOTAL = 12_000_000


def resolve_track_width(model: BoardModel, params: RoutingParams, net_name: Optional[str], extra_power: Collection[str] = ()) -> float:
    """Emitted trace width for a net (thin wrapper over the canonical resolver).

    Precedence: explicit ``params.net_widths`` → board ``(net_class
    trace_width)`` → effective-power heuristic → ``params.track_width_mm``.
    """
    return resolve_track_width_mm(
        net_name,
        net_widths=params.net_widths,
        net_netclass=model.net_netclass,
        net_classes=model.net_classes,
        default_mm=params.track_width_mm,
        power_mm=params.power_width_mm,
        extra_power=extra_power,
    )


def resolve_clearance(model: BoardModel, params: RoutingParams, net_name: Optional[str]) -> float:
    """Clearance for a net: board netclass → ``params.clearance_mm``."""
    if net_name and net_name in model.net_netclass:
        return netclass_clearance(model, net_name, params.clearance_mm)
    return params.clearance_mm
