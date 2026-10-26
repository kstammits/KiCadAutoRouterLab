"""Routing stage: grid-based Lee/A* routing with rip-up and retry."""

from .grid import RoutingGrid, create_grid_from_model
from .obstacles import build_occupancy_grid
from .router import (
    SingleNetRouter,
    RouteResult,
    CostMap,
    RoutingCancelled,
    RipUpManager,
    Window,
    for_pair,
    full_board,
    grow,
    build_via_ok_mask,
    kruskal_connect,
    manhattan,
    ordered_pairs,
)

__all__ = [
    "RoutingGrid",
    "create_grid_from_model",
    "build_occupancy_grid",
    "SingleNetRouter",
    "RouteResult",
    "CostMap",
    "RoutingCancelled",
    "RipUpManager",
    "own_net_soft_mask",
    "smooth_grid_path",
    "smoothed_wire_mm",
    "Window",
    "for_pair",
    "full_board",
    "grow",
    "build_via_ok_mask",
    "kruskal_connect",
    "manhattan",
    "ordered_pairs",
]