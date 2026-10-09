"""Pipeline entry point: parse → place → route → writeback → DRC."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple, List, Set, Dict

import numpy as np

from ..board_model import BoardModel, board_model, netclass_trace_width, netclass_clearance
from ..io import nudge_footprint_by_uuid, save_pair, parse_file, load_pair, rip_up_nets
from ..placement import PlacementParams, PlacementProposal, run_placement
from ..drc import run_drc_on_tree, DRCResult, write_pcb_tree
from . import (
    build_occupancy_grid,
    create_grid_from_model,
    order_nets,
    RipUpManager,
    SingleNetRouter,
    CostMap,
)
from .output import routes_to_tracks_vias, apply_routes_to_tree
from .power import identify_power_nets, route_power_rails, route_ground_stitching, route_decap_fanout
from .grid import RoutingGrid, board_to_grid
from .obstacles import BLOCKED, build_fanout_cost


@dataclass
class RoutingResult:
    """Complete routing result."""
    tracks_added: int
    vias_added: int
    nets_routed: int
    nets_failed: int
    drc_clean: bool
    drc_violations: int
    drc_result: Optional[DRCResult] = None
    pcb_tree: Optional = None  # Modified PCB tree after routing


@dataclass
class RoutingParams:
    """Routing configuration parameters."""
    grid_resolution_mm: float = 0.1
    margin_mm: float = 2.0
    via_cost_mm: float = 5.0          # Via cost in mm wire equivalent
    max_via_count: int = 100          # Maximum total vias allowed
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

    def to_dict(self) -> dict:
        return {
            "grid_resolution_mm": self.grid_resolution_mm,
            "margin_mm": self.margin_mm,
            "via_cost_mm": self.via_cost_mm,
            "max_via_count": self.max_via_count,
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
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RoutingParams":
        # Filter to only known keys
        valid_keys = {
            "grid_resolution_mm", "margin_mm", "via_cost_mm", "max_via_count",
            "track_width_mm", "power_width_mm", "clearance_mm", "via_size_mm",
            "via_drill_mm", "max_attempts_per_net", "run_drc", "debug",
            "net_widths", "heuristic_weight"
        }
        filtered = {k: v for k, v in d.items() if k in valid_keys}
        return cls(**filtered)


_POWER_KEYWORDS = ("vcc", "vdd", "v+", "v-", "pwr", "power", "gnd", "ground")


def resolve_track_width(model: BoardModel, params: RoutingParams, net_name: Optional[str]) -> float:
    """Emitted trace width for a net.

    Precedence: explicit ``params.net_widths`` → board ``(net_class
    trace_width)`` → power-keyword heuristic → ``params.track_width_mm``.
    """
    if net_name and net_name in params.net_widths:
        try:
            if float(params.net_widths[net_name]) > 0:
                return float(params.net_widths[net_name])
        except (TypeError, ValueError):
            pass
    if net_name and net_name in model.net_netclass:
        return netclass_trace_width(model, net_name, params.track_width_mm)
    net_lower = (net_name or "").lower()
    if any(kw in net_lower for kw in _POWER_KEYWORDS):
        return max(params.track_width_mm, params.power_width_mm)
    return params.track_width_mm


def resolve_clearance(model: BoardModel, params: RoutingParams, net_name: Optional[str]) -> float:
    """Clearance for a net: board netclass → ``params.clearance_mm``."""
    if net_name and net_name in model.net_netclass:
        return netclass_clearance(model, net_name, params.clearance_mm)
    return params.clearance_mm


def _terminals_for_net(model: BoardModel, grid: RoutingGrid, net_name: str) -> List[Tuple[int, int, int]]:
    """Grid terminals ``[(col, row, layer_idx)]`` for a net's pads."""
    terminals = []
    for conn in model.nets.get(net_name, []):
        fp = model.by_ref.get(conn.ref)
        if not fp:
            continue
        pad = next((p for p in fp.pads if p.number == conn.pad_number), None)
        if not pad:
            continue
        col, row = board_to_grid(grid, pad.position.x_mm, pad.position.y_mm)
        layer_idx = 0 if fp.layer == "F.Cu" else 1
        terminals.append((col, row, layer_idx))
    return terminals


def _width_cells(width_mm: float, resolution_mm: float) -> int:
    """Corridor half-width in cells for reserving an emitted track's footprint."""
    if resolution_mm <= 0:
        return 1
    return max(1, int(math.ceil(width_mm / resolution_mm / 2.0)))


def _apply_route_to_grid(
    result,
    cost_grid: np.ndarray,
    grid: RoutingGrid,
    half_width_cells: int = 1,
) -> None:
    """Mark routed tracks and vias as BLOCKED in the cost grid.

    The corridor covers ``half_width_cells`` around the centerline so wide
    tracks reserve their true emitted footprint for subsequent nets.
    Updates the cost_grid in-place.

    Args:
        result: RouteResult containing the path [(col, row, layer_idx)]
        cost_grid: (n_layers, H, W) int16 array to update
        grid: RoutingGrid for coordinate transforms
        half_width_cells: corridor half-width in cells (>= 1)
    """
    if not result.success or not result.path:
        return

    path = result.path
    n_layers = cost_grid.shape[0]
    radius = max(0, half_width_cells - 1)

    for i in range(len(path) - 1):
        c1, r1, l1 = path[i]
        c2, r2, l2 = path[i + 1]

        # Mark the segment cells as blocked
        if l1 == l2:
            # Same layer - mark track corridor along the line
            _mark_line_blocked(cost_grid[l1], r1, c1, r2, c2, radius)
        else:
            # Layer transition - mark via at both layers
            if 0 <= l1 < n_layers and 0 <= r1 < cost_grid.shape[1] and 0 <= c1 < cost_grid.shape[2]:
                cost_grid[l1, r1, c1] = BLOCKED
            if 0 <= l2 < n_layers and 0 <= r2 < cost_grid.shape[1] and 0 <= c2 < cost_grid.shape[2]:
                cost_grid[l2, r2, c2] = BLOCKED


def _mark_line_blocked(layer_grid: np.ndarray, r1: int, c1: int, r2: int, c2: int, radius: int = 0) -> None:
    """Bresenham line drawing to mark cells as BLOCKED (plus ``radius`` halo)."""
    H, W = layer_grid.shape
    dr = abs(r2 - r1)
    dc = abs(c2 - c1)
    sr = 1 if r1 < r2 else -1
    sc = 1 if c1 < c2 else -1
    err = dc - dr

    def _stamp(r: int, c: int) -> None:
        r_min, r_max = max(0, r - radius), min(H - 1, r + radius)
        c_min, c_max = max(0, c - radius), min(W - 1, c + radius)
        layer_grid[r_min:r_max + 1, c_min:c_max + 1] = np.maximum(
            layer_grid[r_min:r_max + 1, c_min:c_max + 1], BLOCKED
        )

    r, c = r1, c1
    while True:
        if 0 <= r < H and 0 <= c < W:
            if radius:
                _stamp(r, c)
            else:
                layer_grid[r, c] = BLOCKED
        if r == r2 and c == c2:
            break
        e2 = 2 * err
        if e2 > -dr:
            err -= dr
            c += sc
        if e2 < dc:
            err += dc
            r += sr


def route_nets(
    model: BoardModel,
    net_names: List[str],
    protected_nets: Set[str],
    cost_grid: Optional[np.ndarray] = None,
    routing_grid: Optional[RoutingGrid] = None,
    params: Optional[RoutingParams] = None,
    original_pcb_tree: Optional = None,
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
        params: RoutingParams configuration (optional, uses defaults if None)
        original_pcb_tree: Original PCB S-expression tree for writeback
    
    Returns:
        Tuple of (RoutingResult, updated_cost_grid, routing_grid)
    """
    if params is None:
        params = RoutingParams()
    
    # 0. Rip up selected nets from model so router can find new paths
    # (Existing tracks for these nets would block routing)
    if net_names:
        model = _rip_up_nets_from_model(model, net_names)
        # Also rip up from PCB tree if provided
        if original_pcb_tree is not None:
            original_pcb_tree = rip_up_nets(original_pcb_tree, set(net_names), protected_nets)
    
    # 1. Create or reuse grid
    if routing_grid is None:
        routing_grid = create_grid_from_model(model, params.grid_resolution_mm, params.margin_mm)
    
    # 2. Build or reuse occupancy grid
    if cost_grid is None:
        cost_grid = build_occupancy_grid(model, routing_grid, default_clearance_mm=params.clearance_mm)
    
    # 3. Create THT via mask for free layer transitions at through-hole pads
    tht_via_mask = _build_tht_via_mask(model, routing_grid)
    
    # 4. Create router
    cost_map = CostMap(
        base_cost=1,
        via_cost=int(params.via_cost_mm / params.grid_resolution_mm),
        blocked_threshold=100,
        heuristic_weight=params.heuristic_weight,
    )
    router = SingleNetRouter(routing_grid, cost_grid, cost_map, tht_via_mask)
    
    # 5. Identify net classes
    power_nets, ground_nets, signal_nets = identify_power_nets(model)
    
    # 6. Separate requested nets into categories
    requested_power = [n for n in net_names if n in power_nets]
    requested_ground = [n for n in net_names if n in ground_nets]
    requested_signal = [n for n in net_names if n in signal_nets]
    
    # Track results
    all_results = {}
    ripup = RipUpManager(max_attempts=params.max_attempts_per_net)
    nets_routed = 0
    nets_failed = 0
    width_map: Dict[str, float] = {}

    def _route_one_net(net_name: str) -> bool:
        """Route one net with its resolved width/clearance + fanout halo.

        Records into all_results/ripup, reserves the emitted-width corridor
        on success. Returns True on success.
        """
        nonlocal nets_routed, nets_failed
        conns = model.nets.get(net_name, [])
        if len(conns) < 2:
            return False
        terminals = _terminals_for_net(model, routing_grid, net_name)
        if len(terminals) < 2:
            return False
        width_mm = resolve_track_width(model, params, net_name)
        width_map[net_name] = width_mm
        secondary = build_fanout_cost(
            model, routing_grid, net_name,
            clearance_mm=resolve_clearance(model, params, net_name),
            track_half_mm=width_mm / 2.0,
        )
        corridor = _width_cells(width_mm, params.grid_resolution_mm)
        success = False
        while ripup.can_retry(net_name):
            result = router.route(terminals, secondary)
            if result.success:
                _apply_route_to_grid(result, cost_grid, routing_grid, corridor)
                ripup.record_success(net_name, result.path)
                all_results[net_name] = result
                nets_routed += 1
                success = True
                break
            else:
                ripup.record_failure(net_name, "no path found")
        if not success:
            nets_failed += 1
        return success
    
    # 7. Route requested power nets (respects pad layers)
    for net_name in requested_power:
        _route_one_net(net_name)
    
    # 8. Route requested ground nets
    for net_name in requested_ground:
        _route_one_net(net_name)
    
    # 9. Route signal nets
    for net_name in requested_signal:
        _route_one_net(net_name)
    
    # 10. Convert all results to tracks/vias (per-net resolved widths)
    tracks, vias = routes_to_tracks_vias(
        all_results,
        routing_grid,
        net_class_widths=width_map,
        default_width_mm=params.track_width_mm,
        power_width_mm=params.power_width_mm,
        via_size_mm=params.via_size_mm,
        via_drill_mm=params.via_drill_mm,
    )
    
    # 11. Apply routes to PCB tree
    pcb_tree = None
    if original_pcb_tree is not None:
        pcb_tree = apply_routes_to_tree(original_pcb_tree, tracks, vias)
    
    # 12. Run DRC if requested
    drc_result = None
    if params.run_drc and pcb_tree is not None:
        drc_result = run_drc_on_tree(pcb_tree, prefix="route_")
    
    return RoutingResult(
        tracks_added=len(tracks),
        vias_added=len(vias),
        nets_routed=nets_routed,
        nets_failed=nets_failed,
        drc_clean=drc_result.clean if drc_result else True,
        drc_violations=drc_result.total_issues if drc_result else 0,
        drc_result=drc_result,
        pcb_tree=pcb_tree,
    ), cost_grid, routing_grid


def run_routing(
    model: BoardModel,
    grid_resolution_mm: float = 0.1,
    margin_mm: float = 2.0,
    via_cost: float = 5.0,
    max_via_count: int = 100,
    track_width_mm: float = 0.25,
    power_width_mm: float = 0.5,
    via_size_mm: float = 0.8,
    via_drill_mm: float = 0.4,
    max_attempts_per_net: int = 3,
    run_drc: bool = True,
    original_pcb_tree: Optional = None,
) -> RoutingResult:
    """Run complete routing on a board model.
    
    Returns routing statistics and modifies model's track/via lists.
    """
    # 1. Create grid
    grid = create_grid_from_model(model, grid_resolution_mm, margin_mm)
    
    # 2. Build occupancy grid
    cost_grid = build_occupancy_grid(model, grid)
    
    # 3. Create THT via mask for free layer transitions at through-hole pads
    tht_via_mask = _build_tht_via_mask(model, grid)

    # 4. Create router
    cost_map = CostMap(
        base_cost=1,
        via_cost=int(via_cost / grid_resolution_mm),
        blocked_threshold=100,
        heuristic_weight=3.0,
    )
    router = SingleNetRouter(grid, cost_grid, cost_map, tht_via_mask)
    
    # 4. Identify net classes
    power_nets, ground_nets, signal_nets = identify_power_nets(model)
    
    # 5. Route power rails first
    power_results = route_power_rails(
        model, grid, cost_grid, router, power_nets, power_width_mm
    )
    
    # 6. Route ground stitching
    ground_results = route_ground_stitching(
        model, grid, cost_grid, router, ground_nets
    )
    
    # 7. Route decoupling caps
    decap_refs = [fp.ref for fp in model.footprints if fp.ref.startswith("C") and len(fp.pads) == 2]
    decap_results = route_decap_fanout(
        model, grid, cost_grid, router, decap_refs
    )
    
    # 8. Route remaining nets with rip-up
    all_nets = list(model.nets.keys())
    ordered = [n for n in signal_nets if n in all_nets]  # signal nets last
    
    ripup = RipUpManager(max_attempts=max_attempts_per_net)

    # Width resolution for the legacy signature (file netclass → heuristic).
    legacy_params = RoutingParams(
        track_width_mm=track_width_mm, power_width_mm=power_width_mm
    )

    for net_name in ordered:
        if net_name in power_nets or net_name in ground_nets:
            continue  # already routed

        conns = model.nets.get(net_name, [])
        if len(conns) < 2:
            continue

        terminals = _terminals_for_net(model, grid, net_name)

        if len(terminals) < 2:
            continue

        width_mm = resolve_track_width(model, legacy_params, net_name)
        secondary = build_fanout_cost(
            model, grid, net_name,
            clearance_mm=0.2,
            track_half_mm=width_mm / 2.0,
        )
        corridor = _width_cells(width_mm, grid_resolution_mm)

        # Try routing with retries
        success = False
        while ripup.can_retry(net_name):
            result = router.route(terminals, secondary)
            if result.success:
                # Apply to cost grid
                _apply_route_to_grid(result, cost_grid, grid, corridor)
                ripup.record_success(net_name, result.path)
                success = True
                break
            else:
                ripup.record_failure(net_name, "no path found")
        
        if not success:
            ripup.record_failure(net_name, "max attempts exceeded")
    
    # 9. Combine all results
    all_results = {}
    all_results.update(power_results)
    all_results.update(ground_results)
    all_results.update(decap_results)
    all_results.update({n: ripup.routed_paths[n] for n in ripup.routed_paths})
    
    # Convert to tracks/vias
    tracks, vias = routes_to_tracks_vias(
        all_results,
        grid,
        default_width_mm=track_width_mm,
        power_width_mm=power_width_mm,
        via_size_mm=via_size_mm,
        via_drill_mm=via_drill_mm,
    )
    
    # 10. Apply routes to PCB tree
    pcb_tree = None
    if original_pcb_tree is not None:
        pcb_tree = apply_routes_to_tree(original_pcb_tree, tracks, vias)
    
    # 11. Run DRC if requested
    drc_result = None
    if run_drc and pcb_tree is not None:
        drc_result = run_drc_on_tree(pcb_tree, prefix="route_")
    
    # Stats
    nets_routed = len(ripup.routed_paths) + len(power_results) + len(ground_results)
    nets_failed = len(ripup.get_failed_nets())
    
    return RoutingResult(
        tracks_added=len(tracks),
        vias_added=len(vias),
        nets_routed=nets_routed,
        nets_failed=nets_failed,
        drc_clean=drc_result.clean if drc_result else True,
        drc_violations=drc_result.total_issues if drc_result else 0,
        drc_result=drc_result,
        pcb_tree=pcb_tree,
    )


def _build_tht_via_mask(model, grid) -> np.ndarray:
    """Build a 2D boolean mask marking THT pad locations for free via transitions.
    
    Returns (H, W) boolean array where True = through-hole pad at that grid cell.
    """
    from .grid import board_to_grid
    H, W = grid.height_cells, grid.width_cells
    mask = np.zeros((H, W), dtype=bool)
    
    for fp in model.footprints:
        for pad in fp.pads:
            if pad.is_through_hole:
                col, row = board_to_grid(grid, pad.position.x_mm, pad.position.y_mm)
                if 0 <= row < H and 0 <= col < W:
                    mask[row, col] = True
    
    return mask


def _rip_up_nets_from_model(model: BoardModel, net_names: List[str]) -> BoardModel:
    """Return a new BoardModel with tracks/vias for specified nets removed.
    
    This allows the router to find new paths without being blocked by
    existing tracks for the nets being re-routed.
    """
    if not net_names:
        return model
    
    net_set = set(net_names)
    kept_tracks = tuple(t for t in model.tracks if t.net_name not in net_set)
    kept_vias = tuple(v for v in model.vias if v.net_name not in net_set)
    
    return BoardModel(
        footprints=model.footprints,
        keepout_zones=model.keepout_zones,
        nets=model.nets,
        by_ref=model.by_ref,
        tracks=kept_tracks,
        vias=kept_vias,
        zones=model.zones,
        edge_cuts=model.edge_cuts,
        edge_arcs=model.edge_arcs,
        board_regions=model.board_regions,
        footprint_region=model.footprint_region,
        version=model.version,
        version_warning=model.version_warning,
    )


def run_full_pipeline(
    pcb_path: Path,
    sch_path: Optional[Path] = None,
    output_pcb: Optional[Path] = None,
    output_sch: Optional[Path] = None,
    params: Optional = None,
    validate: bool = True,
) -> Tuple[BoardModel, RoutingResult]:
    """Complete pipeline: load → place → route → writeback → optional DRC."""
    from ..placement import PlacementParams, run_placement
    
    # Load
    if sch_path:
        pair = load_pair(pcb_path, sch_path)
        model = board_model(pair.pcb)
        original_pcb_tree = pair.pcb
    else:
        pcb_tree = parse_file(pcb_path)
        model = board_model(pcb_tree)
        original_pcb_tree = pcb_tree
    
    # Placement
    if params is None:
        params = PlacementParams()
    proposal = run_placement(model, params)
    
    # Apply placement
    from ..board_model import apply_deltas
    model = apply_deltas(model, proposal.deltas)
    
    # Route (with original PCB tree for writeback)
    result = run_routing(model, original_pcb_tree=original_pcb_tree, run_drc=validate)
    
    # Writeback
    if output_pcb and result.pcb_tree is not None:
        result.pcb_tree = write_pcb_tree(result.pcb_tree, output_pcb, "final_")
    
    return model, result


def route_single_board(
    pcb_path: Path,
    output_path: Path,
    params: Optional = None,
    validate: bool = True,
) -> RoutingResult:
    """Convenience: route a single board file."""
    pcb_tree = parse_file(pcb_path)
    model = board_model(pcb_tree)
    
    if params is None:
        from ..placement import PlacementParams
        params = PlacementParams()
    
    # Place
    proposal = run_placement(model, params)
    from ..board_model import apply_deltas
    model = apply_deltas(model, proposal.deltas)
    
    # Route
    result = run_routing(model, original_pcb_tree=pcb_tree, run_drc=validate)
    
    # Write output
    if result.pcb_tree is not None:
        write_pcb_tree(result.pcb_tree, output_path, "final_")
    
    return result