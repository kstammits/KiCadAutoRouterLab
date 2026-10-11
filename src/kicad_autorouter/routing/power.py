"""Specialized routing for power rails and decap fanout."""

from __future__ import annotations

from typing import List, Tuple

import numpy as np

from ..board_model import BoardModel, is_ground_net, is_power_net
from .grid import RoutingGrid, board_to_grid
from .router import SingleNetRouter


def identify_power_nets(model: BoardModel) -> Tuple[List[str], List[str], List[str]]:
    """Classify nets into power rails, ground, and signal.
    
    Returns (power_nets, ground_nets, signal_nets).
    Keyword rules live in board_model.is_power_net / is_ground_net,
    shared with placement (power wins on overlap, e.g. "GND_PWR").
    """
    power = []
    ground = []
    signal = []
    
    for net_name in model.nets:
        if is_power_net(net_name):
            power.append(net_name)
        elif is_ground_net(net_name):
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