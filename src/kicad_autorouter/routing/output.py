"""Convert routing results to KiCad track/via S-expressions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Collection, List, Tuple

from ..board_model import is_power_net_effective
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


@dataclass
class Arc:
    """KiCad track arc (start/mid/end are ON the arc)."""
    start: Tuple[float, float]  # (x_mm, y_mm)
    mid: Tuple[float, float]
    end: Tuple[float, float]
    width_mm: float
    layer: str
    net_name: str


def routes_to_tracks_vias(
    routes: dict[str, RouteResult],
    grid: RoutingGrid,
    net_class_widths: dict = None,
    default_width_mm: float = 0.25,
    power_width_mm: float = 0.5,
    via_size_mm: float = 0.8,
    via_drill_mm: float = 0.4,
    tht_sites: dict = None,
    extra_power: Collection[str] = (),
) -> Tuple[List[Track], List[Via], List[Arc]]:
    """Convert routing results to KiCad tracks, vias and track arcs.

    ``tht_sites`` maps net name -> grid ``{(col, row)}`` cells covered by
    that net's own through-hole drill holes. A layer transition on one of
    those cells reuses the existing plated barrel, so no new ``Via`` is
    emitted (drilling into a hole would be drill-on-copper). Per-net
    scoping matters: a hop onto foreign THT copper still emits a via
    (and the router's exclusion ring normally prevents such hops anyway).

    Widths come from ``net_class_widths`` (resolved upstream, including
    user-marked power membership); the fallback below only bites for
    callers that pass no map and honors effective-power membership (keyword
    OR ``extra_power``) via the shared helper.
    """
    
    tracks = []
    vias = []
    arcs_out: List[Arc] = []
    
    # Default widths per net class
    width_map = net_class_widths or {}
    
    for net_name, result in routes.items():
        if not result.success or not result.path:
            continue

        # Determine track width for this net (shared effective-power rule)
        track_width = width_map.get(net_name, default_width_mm)
        if is_power_net_effective(net_name, extra_power):
            track_width = max(track_width, power_width_mm)

        # Branch geometry: one continuous terminal-to-terminal path per
        # MST edge. The flattened ``result.path`` union is NOT a polyline
        # for multi-branch nets (emitting it would draw teleport tracks
        # between spokes) — so emit each branch separately. Legacy
        # single-path results fall back to ``[result.path]``.
        branch_paths = getattr(result, "branches", ())
        if branch_paths:
            branches = [list(b) for b in branch_paths]
        else:
            branches = [result.path]

        # Fitted arc chains replace chord emission branch by branch
        # (vias still come from the grid-path layer transitions below).
        fitted = getattr(result, "arc_pieces", ())
        use_fitted = bool(fitted) and len(fitted) == len(branches)
        ox, oy = grid.origin_mm
        res = grid.resolution_mm

        def _to_board_mm(c: float, r: float) -> Tuple[float, float]:
            return (ox + (c + 0.5) * res, oy + (r + 0.5) * res)

        for bi, path in enumerate(branches):
            if len(path) < 2:
                continue

            # Convert grid path to board coordinates
            board_path = grid_segment_to_board(grid, path)

            # Vias from layer transitions (fitter never touches these).
            for i in range(len(board_path) - 1):
                c1, r1, l1 = path[i]
                c2, r2, l2 = path[i + 1]
                if l1 == l2:
                    continue
                # Layer transition: a hop on the net's own THT drill hole
                # reuses the plated barrel — no new via drilled. (Hops
                # never move within a cell: c1 == c2 and r1 == r2 here.)
                if tht_sites and (c1, r1) in tht_sites.get(net_name, ()):
                    continue
                x1, y1 = board_path[i][0], board_path[i][1]
                x2, y2 = board_path[i + 1][0], board_path[i + 1][1]
                via_x = (x1 + x2) / 2
                via_y = (y1 + y2) / 2
                vias.append(Via(
                    position=(via_x, via_y),
                    size_mm=via_size_mm,
                    drill_mm=via_drill_mm,
                    layers=(grid.layers[0], grid.layers[-1]),
                    net_name=net_name,
                ))

            # Copper: fitted pieces when present, else legacy chords.
            if use_fitted and fitted[bi]:
                for p in fitted[bi]:
                    if p[0] == "via":
                        continue
                    if p[0] == "seg":
                        x1, y1 = _to_board_mm(p[1], p[2])
                        x2, y2 = _to_board_mm(p[3], p[4])
                        if x1 == x2 and y1 == y2:
                            continue
                        tracks.append(Track(
                            start=(x1, y1),
                            end=(x2, y2),
                            width_mm=track_width,
                            layer=grid.layers[p[5]],
                            net_name=net_name,
                        ))
                    elif p[0] == "arc":
                        xs, ys = _to_board_mm(p[1], p[2])
                        xm, ym = _to_board_mm(p[3], p[4])
                        xe, ye = _to_board_mm(p[5], p[6])
                        if xs == xe and ys == ye:
                            continue
                        arcs_out.append(Arc(
                            start=(xs, ys),
                            mid=(xm, ym),
                            end=(xe, ye),
                            width_mm=track_width,
                            layer=grid.layers[p[7]],
                            net_name=net_name,
                        ))
                continue

            # Legacy straight-chord emission.
            for i in range(len(board_path) - 1):
                c1, r1, l1 = path[i]
                c2, r2, l2 = path[i + 1]

                x1, y1 = board_path[i][0], board_path[i][1]
                x2, y2 = board_path[i + 1][0], board_path[i + 1][1]
                layer1 = board_path[i][2]

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

    # Dedupe exact-duplicate vias per net (MST-merge revisit artifacts can
    # emit two layer changes at the same cell → drill-on-drill DRC).
    seen_via = set()
    unique_vias = []
    for via in vias:
        key = (via.net_name, round(via.position[0], 6), round(via.position[1], 6))
        if key in seen_via:
            continue
        seen_via.add(key)
        unique_vias.append(via)
    vias = unique_vias
    
    # Merge collinear track segments
    tracks = _merge_collinear_tracks(tracks)

    return tracks, vias, arcs_out


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


def apply_routes_to_tree(
    tree,
    tracks: List[Track],
    vias: List[Via],
    arcs=(),
):
    """Insert tracks, track arcs and vias into a PCB S-expression tree.

    ``arcs`` accepts model ``ArcTrack`` (``start``/``mid``/``end`` Points)
    or output-style tuples. Returns a new tree (SExpr is immutable).
    """
    from ..sexpr import SExpr
    
    new_args = list(tree.args)
    
    # Add track segments
    for t in tracks:
        seg = SExpr("segment", (
            SExpr("start", (round(t.start[0], 3), round(t.start[1], 3))),
            SExpr("end", (round(t.end[0], 3), round(t.end[1], 3))),
            SExpr("width", (round(t.width_mm, 3),)),
            SExpr("layer", (t.layer,)),
            SExpr("net", (t.net_name,)),
        ))
        new_args.append(seg)
    
    # Add track arcs (KiCad start/mid/end are three points ON the arc).
    for a in arcs:
        start = a.start if hasattr(a.start, "x_mm") else a.start
        mid = a.mid if hasattr(a.mid, "x_mm") else a.mid
        end = a.end if hasattr(a.end, "x_mm") else a.end
        sx, sy = (start.x_mm, start.y_mm) if hasattr(start, "x_mm") else tuple(start)
        mx, my = (mid.x_mm, mid.y_mm) if hasattr(mid, "x_mm") else tuple(mid)
        ex, ey = (end.x_mm, end.y_mm) if hasattr(end, "x_mm") else tuple(end)
        width = a.width_mm if hasattr(a, "width_mm") else a[3]
        layer = a.layer if hasattr(a, "layer") else a[4]
        net = a.net_name if hasattr(a, "net_name") else a[5]
        arc = SExpr("arc", (
            SExpr("start", (round(sx, 3), round(sy, 3))),
            SExpr("mid", (round(mx, 3), round(my, 3))),
            SExpr("end", (round(ex, 3), round(ey, 3))),
            SExpr("width", (round(width, 3),)),
            SExpr("layer", (layer,)),
            SExpr("net", (net,)),
        ))
        new_args.append(arc)

    # Add vias
    for v in vias:
        via = SExpr("via", (
            SExpr("at", (round(v.position[0], 3), round(v.position[1], 3))),
            SExpr("size", (round(v.size_mm, 3),)),
            SExpr("drill", (round(v.drill_mm, 3),)),
            SExpr("layers", tuple(v.layers)),
            SExpr("net", (v.net_name,)),
        ))
        new_args.append(via)
    
    # Create new tree with updated args
    return SExpr(tree.head, tuple(new_args))
