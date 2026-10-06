"""Specialized routing for power rails, ground stitching, and decap fanout."""

from __future__ import annotations

from typing import List, Tuple, Optional

import numpy as np

from ..board_model import BoardModel, Footprint, Pad
from .grid import RoutingGrid, board_to_grid
from .router import SingleNetRouter, RouteResult
from .obstacles import (
    BLOCKED, HIGH_COST, EDGE_KEEPOUT,
    board_to_grid as _btg,
)


def identify_power_nets(model: BoardModel) -> Tuple[List[str], List[str], List[str]]:
    """Classify nets into power rails, ground, and signal.
    
    Returns (power_nets, ground_nets, signal_nets)
    """
    power_keywords = ("vcc", "vdd", "v+", "v-", "pwr", "power", "+5v", "-5v", "+12v", "-12v", "3v3", "1v8", "1v2", "vbat")
    ground_keywords = ("gnd", "ground", "vgnd")
    
    power = []
    ground = []
    signal = []
    
    for net_name in model.nets:
        net_lower = net_name.lower()
        if any(kw in net_lower for kw in power_keywords):
            power.append(net_name)
        elif any(kw in net_lower for kw in ground_keywords):
            ground.append(net_name)
        else:
            signal.append(net_name)
    
    return power, ground, signal


def route_power_rails(
    model: BoardModel,
    grid: RoutingGrid,
    cost_grid: np.ndarray,
    router: "SingleNetRouter",
    power_nets: List[str],
    track_width_mm: float = 0.5,
) -> dict:
    """Route power rails with wide tracks on preferred layers.
    
    Strategy:
    - F.Cu: positive rails (V+, VCC, etc.)
    - B.Cu: negative rails (V-, GND returns)
    - Use wide tracks, minimize vias
    - Daisy-chain or star from connector
    """
    results = {}
    
    for net_name in power_nets:
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
            layer_idx = 0 if pad.position.y_mm > 0 else 1  # heuristic
            terminals.append((col, row, layer_idx))
        
        if len(terminals) >= 2:
            # Prefer specific layer for power
            net_lower = net_name.lower()
            preferred_layer = 0  # F.Cu for positive
            if any(kw in net_name.lower() for kw in ("v-", "neg", "gnd")):
                preferred_layer = 1  # B.Cu for negative/ground
            
            # Force terminals to preferred layer
            for i, (c, r, _) in enumerate(terminals):
                terminals[i] = (terminals[i][0], terminals[i][1], preferred_layer)
            
            result = router.route(terminals)
            results[net_name] = result
    
    return results


def route_ground_stitching(
    model: BoardModel,
    grid: RoutingGrid,
    cost_grid: np.ndarray,
    router: "SingleNetRouter",
    ground_nets: List[str],
) -> dict:
    """Connect ground plane splits with stitching vias.
    
    Strategy:
    - Find ground zone fragments on each layer
    - Add vias at boundaries between fragments
    - Prioritize near high-current chips
    """
    results = {}
    
    for net_name in ground_nets:
        # Find all ground zone polygons
        ground_zones = [z for z in model.zones if net_name in (z.net_name or "")]
        if len(ground_zones) < 2:
            continue
        
        # Find boundary gaps between zones on same layer
        # This is simplified - in practice would need polygon intersection
        # For now, route between zone centroids
        for i, z1 in enumerate(ground_zones):
            for z2 in ground_zones[i+1:]:
                if not z1.layers or not z2.layers:
                    continue  # skip zones without layer info
                if z1.layers != z2.layers:
                    continue  # different layers - will be stitched via vias
                
                # Route between centroids
                c1 = _zone_centroid(z1, grid)
                c2 = _zone_centroid(z2, grid)
                
                if c1 and c2:
                    layer_idx = 0 if z1.layers[0] == "F.Cu" else 1
                    terminals = [(c1[0], c1[1], layer_idx), (c2[0], c2[1], layer_idx)]
                    result = router.route(terminals)
                    results[f"{net_name}_stitch_{i}"] = result
    
    return results


def route_decap_fanout(
    model: BoardModel,
    grid: RoutingGrid,
    cost_grid: np.ndarray,
    router: "SingleNetRouter",
    decap_refs: List[str] = None,
) -> dict:
    """Route decoupling cap fanout - short, direct, minimal vias.
    
    THT caps: use both layers (free via)
    SMT caps: route on same layer, add via to power/ground
    """
    if decap_refs is None:
        # Auto-detect: caps with 2 terminals near ICs
        decap_refs = [
            fp.ref for fp in model.footprints
            if fp.ref.startswith("C") and len(fp.pads) == 2
        ]
    
    results = {}
    power_nets, ground_nets, _ = identify_power_nets(model)
    
    for ref in decap_refs:
        fp = model.by_ref.get(ref)
        if not fp or len(fp.pads) != 2:
            continue
        
        pad1, pad2 = fp.pads[0], fp.pads[1]
        net1, net2 = pad1.net_name, pad2.net_name
        
        # Check if one side is power and other is ground
        is_power = net1 in power_nets or net2 in power_nets
        is_ground = net1 in ground_nets or net2 in ground_nets
        
        if not (is_power and is_ground):
            continue  # not a decap
        
        # Route each pad to its net
        for pad in [pad1, pad2]:
            target_nets = power_nets if pad.net_name in power_nets else ground_nets
            if not target_nets:
                continue
            
            # Find nearest terminal of target net
            # Simplified: route to nearest pad of same net
            col, row = board_to_grid(grid, pad.position.x_mm, pad.position.y_mm)
            layer_idx = 0 if fp.layer == "F.Cu" else 1
            
            # For THT caps, use both layers
            terminals = [(col, row, 0), (col, row, 1)]
            
            result = router.route(terminals)
            results[f"{ref}_{pad.number}"] = result
    
    return results


def _zone_centroid(zone, grid: RoutingGrid) -> Optional[Tuple[int, int]]:
    """Get centroid of zone polygon in grid coordinates."""
    if not zone.polygon or len(zone.polygon) < 3:
        return None
    
    from ..board_model import Point
    pts = zone.polygon
    cx = sum(p.x_mm for p in pts) / len(pts)
    cy = sum(p.y_mm for p in pts) / len(pts)
    
    col, row = board_to_grid(grid, cx, cy)
    return (col, row)