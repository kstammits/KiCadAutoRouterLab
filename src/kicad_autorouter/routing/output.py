"""Convert routing results to KiCad track/via S-expressions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from ..board_model import BoardModel
from .grid import RoutingGrid, grid_segment_to_board
from .router import RouteResult


@dataclass
class Track:
    """KiCad track segment."""
    start: Tuple[float, float]  # (x_mm, y_mm)
    end: Tuple[float, float]
    width_mm: float
    layer: str
    net_name: str


@dataclass
class Via:
    """KiCad via."""
    position: Tuple[float, float]  # (x_mm, y_mm)
    size_mm: float
    drill_mm: float
    layers: Tuple[str, ...]
    net_name: str


def routes_to_tracks_vias(
    routes: dict[str, RouteResult],
    grid: RoutingGrid,
    net_class_widths: dict = None,
    default_width_mm: float = 0.25,
    power_width_mm: float = 0.5,
    via_size_mm: float = 0.8,
    via_drill_mm: float = 0.4,
) -> Tuple[List[Track], List[Via]]:
    """Convert routing results to KiCad tracks and vias."""
    
    tracks = []
    vias = []
    
    # Default widths per net class
    width_map = net_class_widths or {}
    
    for net_name, result in routes.items():
        if not result.success or not result.path:
            continue
        
        # Determine track width for this net
        track_width = width_map.get(net_name, default_width_mm)
        net_lower = net_name.lower()
        if any(kw in net_lower for kw in ("vcc", "vdd", "v+", "v-", "pwr", "power", "gnd", "ground")):
            track_width = max(track_width, power_width_mm)
        
        path = result.path
        if len(path) < 2:
            continue
        
        # Convert grid path to board coordinates
        board_path = grid_segment_to_board(grid, path)
        
        # Process segments
        for i in range(len(board_path) - 1):
            c1, r1, l1 = path[i]
            c2, r2, l2 = path[i + 1]
            
            x1, y1 = board_path[i][0], board_path[i][1]
            x2, y2 = board_path[i + 1][0], board_path[i + 1][1]
            layer1 = board_path[i][2]
            layer2 = board_path[i + 1][2]
            
            if l1 == l2:
                # Same layer - track segment
                if x1 == x2 and y1 == y2:
                    continue
                tracks.append(Track(
                    start=(x1, y1),
                    end=(x2, y2),
                    width_mm=track_width,
                    layer=layer1,
                    net_name=net_name,
                ))
            else:
                # Layer transition - via
                via_x = (x1 + x2) / 2
                via_y = (y1 + y2) / 2
                vias.append(Via(
                    position=(via_x, via_y),
                    size_mm=0.8,
                    drill_mm=0.4,
                    layers=("F.Cu", "B.Cu"),
                    net_name=net_name,
                ))
    
    # Merge collinear track segments
    tracks = _merge_collinear_tracks(tracks)
    
    return tracks, vias


def _merge_collinear_tracks(tracks: List[Track], tolerance: float = 1e-6) -> List[Track]:
    """Merge adjacent collinear track segments on same layer/net."""
    if not tracks:
        return []
    
    # Group by (layer, net_name)
    groups = {}
    for t in tracks:
        key = (t.layer, t.net_name, t.width_mm)
        groups.setdefault(key, []).append(t)
    
    merged = []
    for key, group in groups.items():
        # Sort by start position
        group.sort(key=lambda t: (t.start[0], t.start[1]))
        
        current = group[0]
        for next_t in group[1:]:
            # Check if collinear and adjacent
            if _segments_adjacent(current, next_t, tolerance):
                # Extend current
                current = Track(
                    start=current.start,
                    end=next_t.end,
                    width_mm=current.width_mm,
                    layer=current.layer,
                    net_name=current.net_name,
                )
            else:
                merged.append(current)
                current = next_t
        merged.append(current)
    
    return merged


def _segments_adjacent(t1: Track, t2: Track, tolerance: float) -> bool:
    """Check if two track segments are collinear and share an endpoint."""
    # Check same line
    x1, y1 = t1.start
    x2, y2 = t1.end
    x3, y3 = t2.start
    x4, y4 = t2.end
    
    # Check if they share an endpoint
    same_start = abs(x1 - x3) < tolerance and abs(y1 - y3) < tolerance
    same_end = abs(x2 - x4) < tolerance and abs(y2 - y4) < tolerance
    if not (same_start or same_end):
        return False
    
    # Check collinear (cross product = 0)
    dx1, dy1 = x2 - x1, y2 - y1
    dx2, dy2 = x4 - x3, y4 - y3
    
    cross = dx1 * dy2 - dy1 * dx2
    return abs(cross) < tolerance


def tracks_vias_to_sexpr(
    tracks: List[Track],
    vias: List[Via],
    existing_pcb: str = None,
) -> str:
    """Generate S-expression fragments for tracks and vias."""
    lines = []
    
    for t in tracks:
        lines.append(
            f'(segment (start {t.start[0]:.3f} {t.start[1]:.3f}) '
            f'(end {t.end[0]:.3f} {t.end[1]:.3f}) '
            f'(width {t.width_mm:.3f}) (layer {t.layer}) '
            f'(net {t.net_name}))'
        )
    
    for v in vias:
        layers_str = " ".join(v.layers)
        lines.append(
            f'(via (at {v.position[0]:.3f} {v.position[1]:.3f}) '
            f'(size {v.size_mm:.3f}) (drill {v.drill_mm:.3f}) '
            f'(layers {layers_str}) (net {v.net_name}))'
        )
    
    return "\n".join(lines)


def apply_routes_to_tree(
    tree,
    tracks: List[Track],
    vias: List[Via],
):
    """Insert tracks and vias into a PCB S-expression tree.
    
    Returns a new tree with the routes added (SExpr is immutable).
    """
    from ..sexpr import SExpr
    
    new_args = list(tree.args)
    
    # Add track segments
    for t in tracks:
        seg = SExpr("segment", (
            SExpr("start", (f"{t.start[0]:.3f}", f"{t.start[1]:.3f}")),
            SExpr("end", (f"{t.end[0]:.3f}", f"{t.end[1]:.3f}")),
            SExpr("width", (f"{t.width_mm:.3f}",)),
            SExpr("layer", (t.layer,)),
            SExpr("net", (t.net_name,)),
        ))
        new_args.append(seg)
    
    # Add vias
    for v in vias:
        via = SExpr("via", (
            SExpr("at", (f"{v.position[0]:.3f}", f"{v.position[1]:.3f}")),
            SExpr("size", (f"{v.size_mm:.3f}",)),
            SExpr("drill", (f"{v.drill_mm:.3f}",)),
            SExpr("layers", tuple(v.layers)),
            SExpr("net", (v.net_name,)),
        ))
        new_args.append(via)
    
    # Create new tree with updated args
    return SExpr(tree.head, tuple(new_args))


def tracks_vias_to_sexpr(
    tracks: List[Track],
    vias: List[Via],
) -> str:
    """Generate S-expression string for tracks and vias."""
    lines = []
    
    for t in tracks:
        lines.append(
            f'(segment (start {t.start[0]:.3f} {t.start[1]:.3f}) '
            f'(end {t.end[0]:.3f} {t.end[1]:.3f}) '
            f'(width {t.width_mm:.3f}) (layer {t.layer}) '
            f'(net {t.net_name}))'
        )
    
    for v in vias:
        layers_expr = " ".join(v.layers)
        lines.append(
            f'(via (at {v.position[0]:.3f} {v.position[1]:.3f}) '
            f'(size {v.size_mm:.3f}) (drill {v.drill_mm:.3f}) '
            f'(layers {layers_expr}) (net {v.net_name}))'
        )
    
    return "\n".join(lines)