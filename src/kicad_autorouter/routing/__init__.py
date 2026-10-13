"""Routing stage: grid-based Lee/A* routing with rip-up and retry."""

from .grid import RoutingGrid, create_grid_from_model
from .obstacles import build_occupancy_grid
from .pair_cost import kruskal_connect, manhattan, ordered_pairs
from .router import SingleNetRouter, RouteResult, CostMap, RoutingCancelled
from .scheduler import RipUpManager
from .search_window import Window, for_pair, full_board, grow
from .smooth import own_net_soft_mask, smooth_grid_path, smoothed_wire_mm
from .via_sites import build_via_ok_mask

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