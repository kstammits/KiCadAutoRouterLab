"""Pipeline entry point: parse → place → route → writeback → DRC."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

from ..board_model import BoardModel, board_model
from ..io import nudge_footprint_by_uuid, save_pair, parse_file, load_pair
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
    
    for net_name in ordered:
        if net_name in power_nets or net_name in ground_nets:
            continue  # already routed
        
        conns = model.nets.get(net_name, [])
        if len(conns) < 2:
            continue
        
        # Convert to grid terminals
        terminals = []
        for conn in conns:
            fp = model.by_ref.get(conn.ref)
            if not fp:
                continue
            pad = next((p for p in fp.pads if p.number == conn.pad_number), None)
            if not pad:
                continue
            col, row = board_to_grid(grid, pad.position.x_mm, pad.position.y_mm)
            layer_idx = 0 if fp.layer == "F.Cu" else 1
            terminals.append((col, row, layer_idx))
        
        if len(terminals) < 2:
            continue
        
        # Try routing with retries
        success = False
        while ripup.can_retry(net_name):
            result = router.route(terminals)
            if result.success:
                # Apply to cost grid
                _apply_route_to_grid(result, cost_grid, grid)
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
        default_width_mm=0.25,
        power_width_mm=power_width_mm,
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