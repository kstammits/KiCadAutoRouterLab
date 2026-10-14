#!/usr/bin/env python3
"""Routing profiling: measure hot paths on 1-2 nets from tube111/DCCF.

Usage:
    .venv/bin/python scripts/profile_routing.py [--fixture tube111|DCCF] [--nets 1|2] [--resolution 0.1]
"""

from __future__ import annotations

import argparse
import cProfile
import io
import json
import pstats
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kicad_autorouter.sexpr import parse_file
from kicad_autorouter.board_model import board_model
from kicad_autorouter.routing import (
    create_grid_from_model,
    build_occupancy_grid,
    SingleNetRouter,
    CostMap,
    own_net_soft_mask,
    smooth_grid_path,
)
from kicad_autorouter.routing.pipeline import (
    route_nets,
)
from kicad_autorouter.routing.obstacles import (
    build_fanout_cost,
    build_via_block_mask,
)
from kicad_autorouter.routing.router import SECONDARY_BLOCKED
from kicad_autorouter.routing.pipeline import (
    RoutingParams,
    _terminals_for_net,
    _rip_up_nets_from_model,
    _build_tht_via_mask,
    _width_cells,
    resolve_track_width,
    resolve_clearance,
    track_corridor_radius_cells,
    via_halo_radius_cells,
    _apply_route_to_grid,
)
from kicad_autorouter.routing.power import identify_power_nets


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"


def load_board(fixture_name: str):
    """Load a board fixture."""
    path = FIXTURES / f"{fixture_name}.kicad_pcb"
    if not path.exists():
        path = FIXTURES / f"{fixture_name}.sved.kicad_pcb"
    if not path.exists():
        raise FileNotFoundError(f"Fixture not found: {path}")
    return board_model(parse_file(path))


def pick_nets(model, n: int = 1):
    """Pick n signal nets with 2-4 pads for profiling."""
    signal_nets = []
    for net_name, conns in model.nets.items():
        if 2 <= len(conns) <= 4:
            # Skip power/ground
            if net_name.upper() in ("GND", "VIN", "+12V", "-12V", "VDD", "VCC"):
                continue
            signal_nets.append((net_name, len(conns)))
    signal_nets.sort(key=lambda x: x[1])  # smallest first
    return [name for name, _ in signal_nets[:n]]


def profile_routing(fixture_name: str, net_names: list[str], resolution_mm: float = 0.1):
    """Profile routing for specific nets."""
    print(f"\n{'='*60}")
    print(f"Profiling: {fixture_name} | nets={net_names} | resolution={resolution_mm}mm")
    print(f"{'='*60}")
    
    model = load_board(fixture_name)
    print(f"Board: {len(model.footprints)} footprints, {len(model.nets)} nets, "
          f"{len(model.tracks)} tracks, {len(model.vias)} vias, {len(model.zones)} zones")
    
    # Pick nets
    if not net_names:
        net_names = pick_nets(model, 2)
    print(f"Routing nets: {net_names}")
    
    params = RoutingParams(
        grid_resolution_mm=resolution_mm,
        margin_mm=2.0,
        max_attempts_per_net=1,
        max_expansions=500_000,
        window_margin_cells=32,
        window_growth=2.0,
        window_retries=2,
    )
    
    # Build grid
    t0 = time.perf_counter()
    grid = create_grid_from_model(model, params.grid_resolution_mm, params.margin_mm)
    t_grid = time.perf_counter() - t0
    total_cells = len(grid.layers) * grid.width_cells * grid.height_cells
    print(f"Grid: {grid.width_cells}x{grid.height_cells}x{len(grid.layers)} = {total_cells/1e6:.2f}M cells")
    print(f"Grid build: {t_grid*1000:.1f}ms")
    
    # Build occupancy grid
    t0 = time.perf_counter()
    cost_grid = build_occupancy_grid(model, grid, default_clearance_mm=params.clearance_mm)
    t_occ = time.perf_counter() - t0
    print(f"Occupancy grid: {t_occ*1000:.1f}ms")
    
    # THT mask
    t0 = time.perf_counter()
    tht_via_mask = _build_tht_via_mask(model, grid)
    t_tht = time.perf_counter() - t0
    print(f"THT via mask: {t_tht*1000:.1f}ms")
    
    # Via block mask
    t0 = time.perf_counter()
    via_block_mask = build_via_block_mask(
        model, grid,
        clearance_mm=params.clearance_mm,
        track_half_mm=params.track_width_mm / 2.0,
    )
    t_via_block = time.perf_counter() - t0
    print(f"Via block mask: {t_via_block*1000:.1f}ms")
    
    # Cost map & router
    cost_map = CostMap(
        base_cost=1,
        via_cost_mm=params.via_cost_mm,
        blocked_threshold=100,
        heuristic_weight=params.heuristic_weight,
    )
    router = SingleNetRouter(
        grid, cost_grid, cost_map, tht_via_mask, via_block_mask,
        max_expansions=params.max_expansions,
        window_margin_cells=params.window_margin_cells,
        window_growth=params.window_growth,
        window_retries=params.window_retries,
    )
    
    # Identify net classes
    power_nets, ground_nets, signal_nets = identify_power_nets(model, ())
    
    # Rip up selected nets
    model_ripped = _rip_up_nets_from_model(model, net_names)
    
    total_expansions = 0
    results = {}
    
    for net_name in net_names:
        print(f"\n--- Routing {net_name} ---")
        conns = model.nets.get(net_name, [])
        terminals = _terminals_for_net(model, grid, net_name)
        print(f"  Terminals: {len(terminals)} -> {terminals}")
        
        if len(terminals) < 2:
            print("  Skipping: < 2 terminals")
            continue
        
        width_mm = resolve_track_width(model, params, net_name, ())
        clearance_mm = resolve_clearance(model, params, net_name)
        print(f"  Width: {width_mm}mm, Clearance: {clearance_mm}mm")
        
        # Fanout cost
        t0 = time.perf_counter()
        secondary = build_fanout_cost(
            model_ripped, grid, net_name,
            clearance_mm=clearance_mm,
            track_half_mm=width_mm / 2.0,
            extra_margin_mm=params.fanout_margin_mm,
        )
        t_fanout = time.perf_counter() - t0
        print(f"  Fanout cost: {t_fanout*1000:.1f}ms")
        
        # Router params
        corridor = _width_cells(width_mm, params.grid_resolution_mm)
        clearance_radius = track_corridor_radius_cells(
            width_mm, clearance_mm + params.fanout_margin_mm, params.grid_resolution_mm,
        )
        router.via_clearance_cells = via_halo_radius_cells(
            params.via_size_mm, clearance_mm, width_mm / 2.0,
            params.grid_resolution_mm,
        )
        router.window_margin_cells = max(
            params.window_margin_cells,
            router.via_clearance_cells,
            clearance_radius,
        )
        
        soft_mask = own_net_soft_mask(
            model, grid, net_name,
            clearance_mm=clearance_mm, track_half_mm=width_mm / 2.0,
        )
        blocked_mask = secondary >= SECONDARY_BLOCKED
        
        # Route
        pairs_total = len(terminals) * (len(terminals) - 1) // 2
        print(f"  Pair searches needed: {pairs_total}")
        
        t0 = time.perf_counter()
        result = router.route(
            terminals, secondary,
            progress_cb=None,
            should_cancel=None,
        )
        t_route = time.perf_counter() - t0
        total_expansions += result.expansions
        
        print(f"  A* route: {t_route*1000:.1f}ms, success={result.success}, "
              f"expansions={result.expansions}, cost={result.cost:.1f}")
        
        if result.success:
            # Smooth (per-branch, mirroring pipeline._smooth_result)
            from dataclasses import replace
            from kicad_autorouter.routing.smooth import smoothed_wire_mm
            t0 = time.perf_counter()
            pinned = {(c, r) for c, r, _ in terminals}
            branches = list(getattr(result, "branches", ()) or [result.path])
            new_branches = [
                smooth_grid_path(
                    list(b), cost_grid,
                    blocked_threshold=100,
                    corridor_radius=max(max(0, corridor - 1), max(0, clearance_radius)),
                    pinned=pinned, soft_mask=soft_mask,
                    hard_radius=max(0, corridor - 1),
                    blocked_mask=blocked_mask,
                    angle_step_rad=3.14159 / 18,
                )
                for b in branches
            ]
            t_smooth = time.perf_counter() - t0
            print(f"  Smooth: {t_smooth*1000:.1f}ms, branches={len(new_branches)}")
            
            # Apply to grid
            t0 = time.perf_counter()
            _apply_route_to_grid(result, cost_grid, grid, corridor, extra_radius=clearance_radius)
            t_apply = time.perf_counter() - t0
            print(f"  Apply to grid: {t_apply*1000:.1f}ms")
        
        results[net_name] = {
            "success": result.success,
            "expansions": result.expansions,
            "cost": result.cost,
            "wire_mm": result.wire_mm if hasattr(result, 'wire_mm') else 0,
        }
    
    print(f"\n{'='*60}")
    print(f"SUMMARY")
    print(f"{'='*60}")
    print(f"Total expansions: {total_expansions}")
    for net_name, r in results.items():
        print(f"  {net_name}: success={r['success']}, expansions={r['expansions']}, "
              f"cost={r['cost']:.1f}")
    
    return results


def profile_with_cprofile(fixture_name: str, net_names: list[str], resolution_mm: float = 0.1):
    """Run full route_nets under cProfile."""
    print(f"\n{'='*60}")
    print(f"cProfile: {fixture_name} | nets={net_names}")
    print(f"{'='*60}")
    
    model = load_board(fixture_name)
    params = RoutingParams(
        grid_resolution_mm=resolution_mm,
        max_attempts_per_net=1,
        max_expansions=500_000,
    )
    
    pr = cProfile.Profile()
    pr.enable()
    
    result, cost_grid, grid = route_nets(
        model, net_names, set(),
        params=params,
        progress_cb=None,
        should_cancel=None,
    )
    
    pr.disable()
    
    # Print top 30 by cumulative time
    buf = io.StringIO()
    ps = pstats.Stats(pr, stream=buf).sort_stats("cumulative")
    ps.print_stats(30)
    print(buf.getvalue())
    
    # Also print by total time
    buf2 = io.StringIO()
    ps2 = pstats.Stats(pr, stream=buf2).sort_stats("tottime")
    ps2.print_stats(20)
    print("\n--- By total time (tottime) ---")
    print(buf2.getvalue())
    
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fixture", choices=["tube111", "DCCF"], default="tube111",
                    help="Board fixture to use")
    ap.add_argument("--nets", type=int, default=1, choices=[1, 2],
                    help="Number of nets to route")
    ap.add_argument("--resolution", type=float, default=0.1,
                    help="Grid resolution in mm")
    ap.add_argument("--cprofile", action="store_true",
                    help="Run full pipeline under cProfile")
    args = ap.parse_args()
    
    model = load_board(args.fixture)
    net_names = pick_nets(model, args.nets)
    
    if args.cprofile:
        profile_with_cprofile(args.fixture, net_names, args.resolution)
    else:
        profile_routing(args.fixture, net_names, args.resolution)


if __name__ == "__main__":
    main()