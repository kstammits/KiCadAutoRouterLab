"""Stage A helpers: grid/layers, THT masks, plane fanout (split from route_raw)."""
from __future__ import annotations
import math
import time
from typing import Collection, Dict, List, Optional, Tuple
import numpy as np
from ..board_model import BoardModel, is_ground_net, is_power_net_effective
from .grid import RoutingGrid, board_to_grid, grid_to_board, mm_to_cells
from .obstacles import BLOCKED, FREE, ObstacleCache, build_via_block_mask, tht_drill_cells, _build_tht_via_mask, _mark_pad
from .params import RoutingParams, resolve_track_width
from .router import CostMap
from .stage_chunks import _branch_cells, _flood_cover



def _default_cost_map(params) -> CostMap:
    """Per-net search pricing: via price in mm, fixed block threshold."""
    return CostMap(
        base_cost=1,
        via_cost_mm=params.via_cost_mm,
        blocked_threshold=100,
        heuristic_weight=params.heuristic_weight,
    )


def _default_transition_masks(model, routing_grid, params):
    """Free THT hop sites + via-block mask at default clearance/width."""
    return (
        _build_tht_via_mask(model, routing_grid),
        build_via_block_mask(
            model, routing_grid,
            clearance_mm=params.clearance_mm,
            track_half_mm=params.track_width_mm / 2.0,
        ),
    )


def _resolve_grid_layers(model: BoardModel, params: RoutingParams) -> Tuple[str, ...]:
    """Ordered grid layers for this route: the outer-only grid intersected
    with ``params.allowed_layers`` (``None`` = all stackup layers, i.e.
    the full outer grid).

    Entries in ``allowed_layers`` must name copper layers on this board;
    anything else (and an empty intersection) raises ``ValueError``.
    Inner-layer entries validate but add no routable layers: the grid is
    outer-only by design.
    """
    outers = ("F.Cu", "B.Cu")
    if params.allowed_layers is None:
        return outers
    known = set(model.copper_layers) | set(outers)
    unknown = [l for l in params.allowed_layers if l not in known]
    if unknown:
        raise ValueError(
            f"allowed_layers has unknown copper layers for this board: {unknown}"
        )
    resolved = tuple(l for l in outers if l in params.allowed_layers)
    if not resolved:
        raise ValueError(
            f"allowed_layers {list(params.allowed_layers)} leaves no routable outer layer"
        )
    return resolved


def _disallowed_terminal_layers(
    model: BoardModel, net_name: str, allowed_layers: Collection[str]
) -> List[str]:
    """Sorted footprint layers of a net's pads that fall outside ``allowed_layers``."""
    bad: List[str] = []
    for conn in model.nets.get(net_name, []):
        fp = model.by_ref.get(conn.ref)
        if not fp:
            continue
        if not any(p.number == conn.pad_number for p in fp.pads):
            continue
        if fp.layer not in allowed_layers and fp.layer not in bad:
            bad.append(fp.layer)
    return sorted(bad)


def _supply_root_cell(model, routing_grid, net_name, terminals):
    """Trunk root cell: this net's pad on a supply-connector (J-) footprint.

    First J-ref pad of the net in model order, else the first terminal.
    Deterministic; ground nets grow their tree outward from this cell
    (``trunk_root``) instead of chaining pad-to-pad.
    """
    wanted = set()
    for fp in model.footprints:
        if (fp.ref or "").upper().startswith("J"):
            for pad in fp.pads:
                if pad.net_name == net_name:
                    c, r = board_to_grid(
                        routing_grid, pad.position.x_mm, pad.position.y_mm)
                    wanted.add((c, r))
    terms = [tuple(t) for t in terminals]
    for t in terms:
        if (t[0], t[1]) in wanted:
            return t
    return terms[0] if terms else None


def _punched_search_grid(model, routing_grid, cost_grid, net_name,
                         clearance_mm=0.2):
    """Per-net search grid: own pads read FREE.

    Pad halos price every net's pads as toll ground — including the
    routed net's own. A through-hole pad then charges rent to hop its
    own barrel, so searches via up beside the pad instead of through
    it (the -12V/D7 case: F.Cu stopped 1.5mm short of a legal free
    hole and drilled a fresh via). Punching the zone makes the hole
    the cheapest legal transition wherever geometry allows. Only
    passable cells are lowered (foreign BLOCKED stays blocked, so a
    genuinely walled hole keeps the adjacent via as the correct
    fallback); foreign halo tolls live in the per-net secondary and
    are untouched. Returns ``cost_grid`` itself when the net owns no
    pads.
    """
    import math
    own = [(fp, pad) for fp in model.footprints for pad in fp.pads
           if pad.net_name == net_name]
    if not own:
        return cost_grid
    res_mm = routing_grid.resolution_mm
    cells = max(1, int(math.ceil(clearance_mm / res_mm))) if res_mm > 0 else 1
    # Same zone geometry as the occupancy stamp (pad copper + clearance
    # + routing half width), evaluated on a scratch grid used as a mask.
    scratch = np.zeros_like(cost_grid)
    for fp, pad in own:
        _mark_pad(scratch, routing_grid, fp, pad, {net_name: cells}, cells,
                  track_half_mm=0.125)
    mask = (scratch > 0) & (cost_grid < BLOCKED)
    if not mask.any():
        return cost_grid
    punched = cost_grid.copy()
    punched[mask] = FREE
    return punched


def _tht_sites_by_net(model, grid) -> dict:
    """Map net name -> drill-hole ``{(col, row)}`` for its own THT pads.

    Used by output to suppress Via emission on free hops (existing barrel).
    Per-net scoping: a hop onto foreign THT copper still emits a via.
    """
    sites: dict = {}
    for fp in model.footprints:
        for pad in fp.pads:
            if pad.is_through_hole and pad.net_name:
                sites.setdefault(pad.net_name, set()).update(tht_drill_cells(pad, grid))
    return sites


def _seeded_coverage(
    join_cells: List[Tuple[int, int, int]],
    seed_cells: Collection[Tuple[int, int, int]],
    result: NetRouteResult,
) -> Optional[str]:
    """Coverage over kept-plus-new copper: every join cell mutually reachable.

    Same raster semantics as :func:`_check_chunk_coverage` (8-neighbour
    steps plus layer vias), but the walkable set is the kept seed cells
    plus the flight's branch cells — so a gap flight that lands on kept
    copper counts as joined. ``join_cells`` are the terminals the flight
    had to connect (plus coverage-only pads the caller adds); seed and
    branch cells are walkable but not required. Returns None when whole,
    else a reason string.
    """
    cells = set(seed_cells or ()) | set(_branch_cells(result))
    cells |= set(join_cells)
    return _flood_cover(list(join_cells), cells,
                        "seeded", "kept+routed copper")


def _width_cells(width_mm: float, resolution_mm: float) -> int:
    """Corridor half-width in cells for reserving an emitted track's footprint."""
    if resolution_mm <= 0:
        return 1
    return max(1, int(math.ceil(width_mm / resolution_mm / 2.0)))


def _inner_plane_layers_for_net(model: BoardModel, net_name: str) -> Tuple[str, ...]:
    """Inner copper layers carrying a zone for ``net_name``.

    Empty when the net owns no inner plane: the caller falls back to normal
    outer routing. Pure zone-ownership test (no pour-refill inference).
    """
    seen: List[str] = []
    for zone in model.zones:
        if zone.net_name != net_name:
            continue
        for layer in zone.layers:
            if layer not in ("F.Cu", "B.Cu") and layer not in seen:
                seen.append(layer)
    return tuple(seen)


def _plan_plane_stubs(
    model: BoardModel,
    routing_grid: RoutingGrid,
    cost_grid: "np.ndarray",
    via_block_mask: "np.ndarray",
    params: "RoutingParams",
    net_name: str,
    extra_power_nets: Collection[str],
    budget_left: Optional[int],
) -> Tuple[list, list, float, Optional[str]]:
    """Stub geometry tapping a net's inner plane, or a failure reason.

    Returns ``(stubs, via_pts, width_mm, reason)``: ``stubs`` are
    ``(x0, y0, x1, y1, layer)`` pad→via segments, ``via_pts`` are
    ``(x_mm, y_mm)`` via positions, ``reason`` is None on success
    (including the all-THT no-op, which emits nothing — the barrels already
    pierce the plane). Pure search: reserves nothing; the caller commits
    and stamps halos. SMD-only; ``budget_left`` caps new vias (None = no
    cap).
    """
    res_mm = routing_grid.resolution_mm
    H, W = routing_grid.height_cells, routing_grid.width_cells
    width_mm = resolve_track_width(model, params, net_name, extra_power_nets)
    stubs: List[Tuple[float, float, float, float, str]] = []
    via_pts: List[Tuple[float, float]] = []
    need = 0
    for conn in model.nets.get(net_name, []):
        fp = model.by_ref.get(conn.ref)
        if not fp:
            continue
        pad = next((p for p in fp.pads if p.number == conn.pad_number), None)
        if not pad:
            continue
        if pad.is_through_hole:
            continue  # barrel already taps the plane
        need += 1
        if budget_left is not None and len(via_pts) >= budget_left:
            return [], [], width_mm, (
                f"plane fanout blocked ({len(via_pts)}/{need} stubs, "
                f"cap={params.fanout_plane_vias_max})"
            )
        c0, r0 = board_to_grid(routing_grid, pad.position.x_mm, pad.position.y_mm)
        layer_idx = routing_grid.layer_to_idx.get(fp.layer, 0)
        # Spiral search for a free via cell near the pad (≤2 mm).
        search_cells = mm_to_cells(2.0, res_mm) if res_mm > 0 else 8
        found = None
        for rad in range(0, search_cells + 1):
            ring_found = False
            for dr in range(-rad, rad + 1):
                for dc in range(-rad, rad + 1):
                    if max(abs(dr), abs(dc)) != rad:
                        continue
                    cc, rr = c0 + dc, r0 + dr
                    if not (0 <= cc < W and 0 <= rr < H):
                        continue
                    if bool(via_block_mask[rr, cc]):
                        continue
                    if cost_grid[layer_idx, rr, cc] >= BLOCKED:
                        continue
                    found = (cc, rr)
                    ring_found = True
                    break
                if ring_found:
                    break
            if found is not None:
                break
        if found is None:
            return [], [], width_mm, (
                f"plane fanout blocked ({len(via_pts)}/{need} stubs, "
                f"cap={params.fanout_plane_vias_max})"
            )
        vx_mm, vy_mm = grid_to_board(routing_grid, found[0], found[1])
        via_pts.append((vx_mm, vy_mm))
        stubs.append((pad.position.x_mm, pad.position.y_mm, vx_mm, vy_mm, fp.layer))
    return stubs, via_pts, width_mm, None
