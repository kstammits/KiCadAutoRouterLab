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
    max_terminals_per_net: int = 32  # Fail-fast cap on net size (grid
    # terminals); larger nets fail with an explicit reason instead of
    # burning hours of Steiner pair-searches. 0 = no cap.
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
    pour_severance_weight: float = 30.0  # Soft cost for tracks severing a pour (0 disables)
    fanout_margin_mm: float = 0.0  # Extra steering push around foreign pads (soft outer halo + smoother corridor + reservation); 0 = DRC clearance only
    window_margin_cells: int = 32  # Initial A* bbox dilation around a pair (cells)
    window_growth: float = 2.0  # Search-window expansion factor per retry
    window_retries: int = 3  # Window attempts per pair (last = full board)
    corridor: bool = True  # Attempt coarse corridor guidance when initial window fails
    refill_zones: bool = False  # Refill copper pours after routing (kicad-cli --refill-zones --save-board)
    fanout_planes: bool = False  # Via-to-plane stubs for plane-owned nets (inner zones)
    # When True, a ground/power-effective net owning an inner-copper zone
    # (In*.Cu) is satisfied with short outer stubs + through-vias tapping
    # the plane instead of long outer MST tracks. Two-layer preset forces
    # False (prior behavior retained); four-layer preset forces True.
    fanout_plane_vias_max: int = 16  # Cap on plane-fanout vias per route_nets call
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
            "stitch_gnd_vias": self.stitch_gnd_vias,
            "strip_gnd_blockers": self.strip_gnd_blockers,
            "stitch_vias_max": self.stitch_vias_max,
            "stitch_bridges_max": self.stitch_bridges_max,
            "stitch_bridge_max_mm": self.stitch_bridge_max_mm,
            "stitch_bridge_width_mm": self.stitch_bridge_width_mm,
            "pour_severance_weight": self.pour_severance_weight,
            "fanout_margin_mm": self.fanout_margin_mm,
            "window_margin_cells": self.window_margin_cells,
            "window_growth": self.window_growth,
            "window_retries": self.window_retries,
            "corridor": self.corridor,
            "refill_zones": self.refill_zones,
            "fanout_planes": self.fanout_planes,
            "fanout_plane_vias_max": self.fanout_plane_vias_max,
            "allowed_layers": list(self.allowed_layers) if self.allowed_layers is not None else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RoutingParams":
        # Filter to only known keys
        valid_keys = {
            "grid_resolution_mm", "margin_mm", "via_cost_mm",
            "track_width_mm", "power_width_mm", "clearance_mm", "via_size_mm",
            "via_drill_mm", "max_attempts_per_net", "run_drc", "debug",
            "net_widths", "heuristic_weight", "max_expansions",
            "max_expansions_per_net", "max_terminals_per_net",
            "stitch_gnd_vias", "strip_gnd_blockers", "stitch_vias_max", "stitch_bridges_max",
            "stitch_bridge_max_mm", "stitch_bridge_width_mm", "pour_severance_weight", "fanout_margin_mm",
            "window_margin_cells", "window_growth", "window_retries",
            "corridor", "refill_zones", "allowed_layers",
            "fanout_planes", "fanout_plane_vias_max",
        }
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
        "stitch_bridge_max_mm", "stitch_bridge_width_mm",
        "pour_severance_weight", "fanout_margin_mm",
    )
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
        "max_attempts_per_net", "max_expansions", "max_expansions_per_net", "max_terminals_per_net", "stitch_vias_max",
        "stitch_bridges_max", "fanout_plane_vias_max", "window_margin_cells", "window_retries",
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
    if (
        not isinstance(params.window_growth, (int, float))
        or isinstance(params.window_growth, bool)
        or not math.isfinite(params.window_growth)
        or params.window_growth <= 0
    ):
        raise ValueError("window_growth must be a positive finite number")
    for name in (
        "run_drc", "debug", "stitch_gnd_vias", "strip_gnd_blockers",
        "corridor", "refill_zones", "fanout_planes",
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
