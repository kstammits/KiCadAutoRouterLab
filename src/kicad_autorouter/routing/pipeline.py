"""Pipeline entry point: parse → place → route → writeback → DRC."""

from __future__ import annotations

import math
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Collection, Optional, Tuple, List, Set, Dict

import numpy as np

from ..board_model import BoardModel, Point, Track, Via, is_ground_net, is_power_net_effective
from ..io import rip_up_nets
from ..drc import run_drc_on_tree, DRCResult
from ..sexpr import parse_file, to_sexpr
from ..validate import refill_zones
from . import (
    build_occupancy_grid,
    create_grid_from_model,
    RipUpManager,
    RoutingCancelled,
    SingleNetRouter,
    CostMap,
)
from .output import routes_to_tracks_vias, apply_routes_to_tree
from .output import Track as OutputTrack, Via as OutputVia
from .smooth import own_net_soft_mask, smooth_grid_path, smoothed_wire_mm
from .router import SECONDARY_BLOCKED
from .power import identify_power_nets
from .grid import RoutingGrid, board_to_grid, grid_to_board
from .pour import pour_corridors, pour_net_names, severance_cost
from .stitch import plan_stitches
from .obstacles import BLOCKED, bresenham_cells, build_fanout_cost, build_via_block_mask, drill_radius_cells, stamp_discs, stamp_square, tht_drill_cells, track_corridor_radius_cells, via_halo_radius_cells


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
    stitch_vias_added: int = 0
    stitch_tracks_added: int = 0
    gnd_islands_before: int = 0
    gnd_islands_after: int = 0
    gnd_stripped: int = 0  # GND vias+tracks evicted as blockers (rebuilt by stitcher)
    cancelled: bool = False  # True when should_cancel fired mid-run
    expansions: int = 0  # Total A* node expansions across all pair-searches
    timings: Dict[str, float] = field(default_factory=dict)  # Stage seconds + grid cells
    refill_zones_done: bool = False  # True if copper pour refill was executed
    fanout_vias_added: int = 0  # Plane-tap stubs (four-layer strategy; additive, never folded into vias_added inputs)
    fanout_tracks_added: int = 0  # Stub tracks accompanying plane-tap vias
    failed_reasons: Dict[str, str] = field(default_factory=dict)  # net -> explicit failure reason


# Re-export: canonical home is routing.params (config + validation +
# per-net width/clearance policy). Import here keeps existing
# `from .pipeline import RoutingParams, ...` sites working.
from .params import (
    MAX_GRID_CELLS_TOTAL,
    RoutingParams,
    _validate_routing_params,
    resolve_clearance,
    resolve_track_width,
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


def _terminals_for_net(
    model: BoardModel,
    grid: RoutingGrid,
    net_name: str,
    allowed_layers: Optional[Collection[str]] = None,
) -> List[Tuple[int, int, int]]:
    """Grid terminals ``[(col, row, layer_idx)]`` for a net's pads.

    When ``allowed_layers`` is set, pads on other layers are skipped
    (never silently remapped): callers check
    :func:`_disallowed_terminal_layers` first and fail the net with an
    explicit reason.
    """
    terminals = []
    for conn in model.nets.get(net_name, []):
        fp = model.by_ref.get(conn.ref)
        if not fp:
            continue
        pad = next((p for p in fp.pads if p.number == conn.pad_number), None)
        if not pad:
            continue
        if allowed_layers is not None and fp.layer not in allowed_layers:
            continue
        col, row = board_to_grid(grid, pad.position.x_mm, pad.position.y_mm)
        layer_idx = grid.layer_to_idx.get(fp.layer, 0)
        terminals.append((col, row, layer_idx))
    return terminals


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
        search_cells = max(1, int(math.ceil(2.0 / res_mm))) if res_mm > 0 else 8
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


def _branch_paths(result) -> List[List[int]]:
    """Branch geometries for a route (fallback to ``[result.path]``).

    Legacy/foreign ``RouteResult``-likes without ``branches`` (e.g. direct
    ``_a_star`` results) carry a single continuous path; multi-terminal
    routes carry one branch per MST edge.
    """
    branches = getattr(result, "branches", ())
    if branches:
        return [list(b) for b in branches]
    if getattr(result, "path", None):
        return [list(result.path)]
    return []


def _apply_route_to_grid(
    result,
    cost_grid: np.ndarray,
    grid: RoutingGrid,
    half_width_cells: int = 1,
    extra_radius: int = 0,
    via_clearance_cells: int = 0,
) -> None:
    """Mark routed tracks and vias as BLOCKED in the cost grid.

    The corridor covers ``max(half_width_cells - 1, extra_radius)`` around
    the centerline so emitted tracks reserve their true footprint *plus*
    clearance to neighboring copper for subsequent nets. Via landings
    stamp a via-specific halo (via annulus + clearance + track half-width)
    on both layers. Every MST branch is reserved (not just a flattened union),
    so later nets keep clearance to the actual copper. Updates the cost_grid
    in-place.

    Args:
        result: RouteResult containing branch paths [(col, row, layer_idx)]
        cost_grid: (n_layers, H, W) int16 array to update
        grid: RoutingGrid for coordinate transforms
        half_width_cells: corridor half-width in cells (>= 1)
        extra_radius: clearance halo in cells (>= 0) for tracks
        via_clearance_cells: via halo radius in cells (via size/2 + clearance + track half-width)
    """
    if not result.success or not result.path:
        return

    n_layers = cost_grid.shape[0]
    H, W = cost_grid.shape[1], cost_grid.shape[2]
    track_radius = max(max(0, half_width_cells - 1), max(0, extra_radius))
    via_radius = max(1, via_clearance_cells)

    for path in _branch_paths(result):
        for i in range(len(path) - 1):
            c1, r1, l1 = path[i]
            c2, r2, l2 = path[i + 1]

            # Mark the segment cells as blocked
            if l1 == l2:
                # Same layer - mark track corridor along the line
                _mark_line_blocked(cost_grid[l1], r1, c1, r2, c2, track_radius)
            else:
                # Layer transition - mark via halo at both layers
                for _l, _r, _c in ((l1, r1, c1), (l2, r2, c2)):
                    if 0 <= _l < n_layers and 0 <= _r < H and 0 <= _c < W:
                        stamp_square(cost_grid[_l], _r, _c, via_radius, BLOCKED)


def _mark_line_blocked(layer_grid: np.ndarray, r1: int, c1: int, r2: int, c2: int, radius: int = 0) -> None:
    """Bresenham line drawing to mark cells as BLOCKED (plus ``radius`` halo)."""
    H, W = layer_grid.shape

    for c, r in bresenham_cells(c1, r1, c2, r2):
        if 0 <= r < H and 0 <= c < W:
            if radius:
                stamp_square(layer_grid, r, c, radius, BLOCKED)
            else:
                layer_grid[r, c] = BLOCKED


def _smooth_result(result, cost_grid, routing_grid, corridor: int, blocked_threshold: int = 100,
                   pinned=None, soft_mask=None, clearance_radius: int = 0,
                   blocked_mask=None, angle_step_rad: float = math.pi / 18):
    """String-pull a successful route before corridor reservation.

    Smoothing runs against the pre-route grid (which must NOT yet contain
    this path's own corridor) so shortcuts are clearance-checked against
    real obstacles. Each MST branch is smoothed independently (branch
    endpoints are terminals, so no spoke can be skipped and no teleport
    segment is ever created); via nodes/layers and ``pinned`` terminals
    are preserved by ``smooth_grid_path``. Returns a result with smoothed
    branches, rebuilt union path, and recomputed wire length.

    The shortcut footprint covers ``max(corridor - 1, clearance_radius)``
    so smoothed copper keeps clearance to neighboring tracks, and
    penalized cells (other nets' pad rings, edge keepout) block shortcuts
    except inside ``soft_mask`` (this net's own pads). ``blocked_mask`` is
    the per-net hard exclusion (foreign-THT rings): shortcuts may never
    cross it, with no soft exemption. The exclusion disc already bakes pad
    copper + clearance + track half-width, so it is tested with the track
    copper half-width (``corridor - 1``) rather than the full clearance
    halo — otherwise clearance is counted twice and no detour chord is
    ever walkable. The cost grid keeps the full halo.
    """
    corridor_radius = max(max(0, corridor - 1), max(0, clearance_radius))
    hard_radius = max(0, corridor - 1)

    # Collect all branch endpoints as pinned points (Steiner connections).
    # New branches attach to interior points of existing branches via goal_mask;
    # smoothing must not shortcut past those attachment points.
    all_pinned = set(pinned or set())
    for branch in _branch_paths(result):
        if branch:
            all_pinned.add((branch[0][0], branch[0][1]))   # branch start
            all_pinned.add((branch[-1][0], branch[-1][1])) # branch end

    new_branches = []
    changed = False
    for branch in _branch_paths(result):
        smoothed = smooth_grid_path(
            branch,
            cost_grid,
            blocked_threshold=blocked_threshold,
            corridor_radius=corridor_radius,
            pinned=all_pinned,
            soft_mask=soft_mask,
            blocked_mask=blocked_mask,
            hard_radius=hard_radius,
            angle_step_rad=angle_step_rad,
        )
        new_branches.append(smoothed)
        if len(smoothed) != len(branch):
            changed = True
    if not changed:
        return result
    seen = set()
    union_path = []
    for branch in new_branches:
        for cell in branch:
            if cell not in seen:
                seen.add(cell)
                union_path.append(cell)
    wire_mm = sum(
        smoothed_wire_mm(b, routing_grid.resolution_mm) for b in new_branches
    )
    return replace(
        result,
        path=union_path,
        branches=tuple(tuple(b) for b in new_branches),
        wire_mm=wire_mm,
    )


def route_nets(
    model: BoardModel,
    net_names: List[str],
    protected_nets: Set[str],
    cost_grid: Optional[np.ndarray] = None,
    routing_grid: Optional[RoutingGrid] = None,
    params: Optional[RoutingParams] = None,
    original_pcb_tree: Optional = None,
    extra_power_nets: Collection[str] = (),
    progress_cb=None,  # fn(net_name, pairs_done, pairs_total, expansions) — best-effort
    should_cancel=None,  # fn() -> bool — checked between nets/attempts/pairs
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
        extra_power_nets: User-marked power membership (app nets table,
            seeded from power-named KiCad netclasses on load). Marked nets
            route first with wide tracks and join the protected family via
            the caller's protected set.
        progress_cb: Optional per-pair progress hook for job UIs. Called as
            ``progress_cb(net_name, pairs_done, pairs_total, expansions)``.
            Exceptions are swallowed (reporting must never break routing).
        should_cancel: Optional cooperative-cancel predicate. Checked
            between nets, between retry attempts, and inside A* (every 4096
            expansions). Firing sets ``RoutingResult.cancelled`` and stops
            with partial results — already-routed nets are kept.

    Returns:
        Tuple of (RoutingResult, updated_cost_grid, routing_grid)
    """
    if params is None:
        params = RoutingParams()
    t_total = time.perf_counter()
    timings: Dict[str, float] = {}
    expansions_total = 0
    cancelled = False

    def _report(net_name: str, done: int, total: int, expansions: int) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(net_name, done, total, expansions)
        except Exception:
            pass
    
    # 0. Rip up selected nets from model so router can find new paths
    # (Existing tracks for these nets would block routing). Protected nets
    # are never ripped here — except GND (see 0b): signal/power copper is
    # only removed when explicitly requested, so tracks can't rip each other.
    if net_names:
        model = _rip_up_nets_from_model(model, net_names)
        # Also rip up from PCB tree if provided
        if original_pcb_tree is not None:
            original_pcb_tree = rip_up_nets(original_pcb_tree, set(net_names), protected_nets)

    # 0b. GND-only blocker eviction (upfront bulk strip). Pre-existing
    # zone-pour stitch vias and old GND tracks would otherwise wall off
    # new routes as hard BLOCKED cells; GND is cheaply rebuilt (zones
    # refill + plan_stitches), so evict it even when protected. Never
    # touches signal/power copper.
    gnd_stripped = 0
    if params.strip_gnd_blockers:
        model, gnd_stripped = _strip_gnd_blockers_from_model(model)
        if gnd_stripped and original_pcb_tree is not None:
            # Collect GND net names present on the tree (model is already
            # stripped, so scan the tree instead via a net-name pass).
            from ..board_model import _net_name as _tree_net_name
            gnd_nets = set()
            for node in (*original_pcb_tree.children("segment"), *original_pcb_tree.children("via")):
                name = _tree_net_name(node.find("net"))
                if is_ground_net(name):
                    gnd_nets.add(name)
            if gnd_nets:
                # Bypass protected_nets for GND only (rebuildable); the
                # affected set contains solely ground nets, so signal/power
                # copper is untouched regardless.
                original_pcb_tree = rip_up_nets(original_pcb_tree, gnd_nets, set())
    
    # 1. Create or reuse grid. A reused grid is only valid when it matches
    # the requested resolution AND covers the same board area AND routes
    # the same layer set: a resolution, margin, or allowed_layers change
    # must rebuild, otherwise the new params are silently ignored (and
    # stale BLOCKED cells from the previous route persist).
    grid_layers = _resolve_grid_layers(model, params)
    t_grid = time.perf_counter()
    if routing_grid is None:
        routing_grid = create_grid_from_model(
            model, params.grid_resolution_mm, params.margin_mm, layers=grid_layers
        )
    else:
        expected = create_grid_from_model(
            model, params.grid_resolution_mm, params.margin_mm, layers=grid_layers
        )
        if (
            routing_grid.resolution_mm != params.grid_resolution_mm
            or routing_grid.width_cells != expected.width_cells
            or routing_grid.height_cells != expected.height_cells
            or routing_grid.origin_mm != expected.origin_mm
            or tuple(routing_grid.layers) != tuple(expected.layers)
        ):
            routing_grid = expected

    # Guard: fine grids on large boards explode (DCCF at 0.05mm = 38M cells
    # across 2 layers; each A* also allocates a ~4 byte/cell g-score array).
    # Fail fast with an actionable message instead of hanging for minutes.
    total_cells = len(routing_grid.layers) * routing_grid.width_cells * routing_grid.height_cells
    timings["grid_build_s"] = time.perf_counter() - t_grid
    timings["grid_cells"] = float(total_cells)
    timings["grid_resolution_mm"] = float(params.grid_resolution_mm)
    if total_cells > MAX_GRID_CELLS_TOTAL:
        raise ValueError(
            f"routing grid too large: {routing_grid.width_cells}x{routing_grid.height_cells}"
            f"x{len(routing_grid.layers)} ({total_cells / 1e6:.1f}M cells) at"
            f" {params.grid_resolution_mm}mm resolution (limit"
            f" {MAX_GRID_CELLS_TOTAL / 1e6:.0f}M). Use a coarser grid_resolution_mm"
            f" (e.g. 0.1mm) or a smaller margin_mm."
        )

    # 2. Build or refresh the occupancy grid from the RIPPED model.
    # Refresh-in-place when shapes match: drops BLOCKED cells of the nets
    # being re-routed (they were ripped from the model above) while keeping
    # prior unrelated routes (still present as tracks in the model) blocked.

    # Known tradeoff: a rebuilt grid marks pre-existing tracks centerline-only
    # (obstacles._mark_track has no width inflation), while in-batch
    # _apply_route_to_grid reserves the full emitted-width corridor. So
    # one-net-at-a-time callers (the chunked UI) let later nets run slightly
    # tighter to earlier copper than a single batched call would (measured
    # ~5% fewer tracks on 3 DCCF nets; routed/failed sets identical).
    # Making rebuilds width-aware belongs with the GND-continuity work
    # (docs/roadmap.md).
    if cost_grid is None or cost_grid.shape != (
        len(routing_grid.layers), routing_grid.height_cells, routing_grid.width_cells
    ):
        t_occ = time.perf_counter()
        cost_grid = build_occupancy_grid(model, routing_grid, default_clearance_mm=params.clearance_mm)
        timings["occupancy_s"] = time.perf_counter() - t_occ
    else:
        t_occ = time.perf_counter()
        fresh = build_occupancy_grid(model, routing_grid, default_clearance_mm=params.clearance_mm)
        cost_grid[:] = fresh
        timings["occupancy_s"] = time.perf_counter() - t_occ

    # Board fill / density metrics from the occupancy grid (all layers).
    # blocked_frac: impassable cells (>= BLOCKED). occupied_frac: any
    # non-free cell (courtyards, pours, halos, blocked). tolled_frac:
    # passable-but-costly cells (steering penalties without blocking).
    blocked = cost_grid >= BLOCKED
    timings["blocked_frac"] = float(blocked.mean())
    timings["occupied_frac"] = float((cost_grid > 0).mean())
    timings["tolled_frac"] = float(((cost_grid > 0) & (cost_grid < BLOCKED)).mean())
    # 3. Create THT via mask for free layer transitions at through-hole pads
    tht_via_mask = _build_tht_via_mask(model, routing_grid)

    # 3b. Via-block mask: no vias inside SMD pad solder/fanout areas
    via_block_mask = build_via_block_mask(
        model, routing_grid,
        clearance_mm=params.clearance_mm,
        track_half_mm=params.track_width_mm / 2.0,
    )

    # 4. Create router (via price in mm; the router scales to cells)
    cost_map = CostMap(
        base_cost=1,
        via_cost_mm=params.via_cost_mm,
        blocked_threshold=100,
        heuristic_weight=params.heuristic_weight,
    )
    router = SingleNetRouter(
        routing_grid, cost_grid, cost_map, tht_via_mask, via_block_mask,
        max_expansions=params.max_expansions,
        window_margin_cells=params.window_margin_cells,
        window_growth=params.window_growth,
        window_retries=params.window_retries,
        corridor=params.corridor,
        debug=params.debug,
        max_expansions_per_net=params.max_expansions_per_net,
    )
    
    # 5. Identify net classes (user-marked power membership included)
    power_nets, ground_nets, signal_nets = identify_power_nets(model, extra_power_nets)

    # 5b. Pour-corridor map for severance-aware routing (computed once on
    # the ripped model; pour nets themselves route without the penalty).
    pour_nets = set(pour_net_names(model))
    corridors, corridor_min_thick = (None, 0.0)
    if params.pour_severance_weight > 0 and pour_nets:
        corridors, corridor_min_thick = pour_corridors(model, routing_grid)

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
    plane_tracks: List[OutputTrack] = []
    plane_vias: List[OutputVia] = []
    plane_done: Set[str] = set()
    fanout_cap = params.fanout_plane_vias_max

    # 6b. Plane-tap fanout (four-layer strategy only; two-layer forces it
    # off so prior behavior is retained verbatim). A ground/power-effective
    # net owning an inner-copper zone is satisfied with short outer stubs +
    # through-vias tapping the plane: the plane itself connects the stubs.
    # SMD-only (THT barrels already pierce the plane); capped; pour-gated
    # clean no-op (no inner zone → normal outer routing below).
    if params.fanout_planes:
        res_mm = routing_grid.resolution_mm
        for net_name in list(net_names):
            if net_name in plane_done or net_name in ripup.net_failures:
                continue
            if not _inner_plane_layers_for_net(model, net_name):
                continue
            if not (
                is_ground_net(net_name)
                or is_power_net_effective(net_name, extra_power_nets)
            ):
                continue
            if len(model.nets.get(net_name, ())) < 2:
                continue
            budget_left = (
                max(0, fanout_cap - len(plane_vias)) if fanout_cap > 0 else None
            )
            stubs, via_pts, width_mm, reason = _plan_plane_stubs(
                model, routing_grid, cost_grid, via_block_mask,
                params, net_name, extra_power_nets, budget_left,
            )
            if reason is not None:
                ripup.record_failure(net_name, reason)
                nets_failed += 1
                plane_done.add(net_name)
                continue
            # Commit stubs: emit + reserve halos so later MST nets keep
            # clearance. Stub tracks live on one outer layer; vias pierce,
            # so the via halo stamps every grid layer.
            clearance_mm = resolve_clearance(model, params, net_name)
            track_radius = track_corridor_radius_cells(width_mm, clearance_mm, res_mm)
            via_radius = via_halo_radius_cells(
                params.via_size_mm, clearance_mm, width_mm / 2.0, res_mm)
            for (x0, y0, x1, y1, layer) in stubs:
                plane_tracks.append(OutputTrack(
                    start=(x0, y0), end=(x1, y1),
                    width_mm=width_mm, layer=layer, net_name=net_name,
                ))
                li = routing_grid.layer_to_idx.get(layer, 0)
                c0, r0 = board_to_grid(routing_grid, x0, y0)
                c1, r1 = board_to_grid(routing_grid, x1, y1)
                _mark_line_blocked(cost_grid[li], r0, c0, r1, c1, track_radius)
            for (vx_mm, vy_mm) in via_pts:
                plane_vias.append(OutputVia(
                    position=(vx_mm, vy_mm),
                    size_mm=params.via_size_mm, drill_mm=params.via_drill_mm,
                    layers=("F.Cu", "B.Cu"), net_name=net_name,
                ))
                cc, rr = board_to_grid(routing_grid, vx_mm, vy_mm)
                for li in range(len(routing_grid.layers)):
                    stamp_square(cost_grid[li], rr, cc, via_radius, BLOCKED)
            ripup.record_success(net_name, [])
            width_map[net_name] = width_mm
            nets_routed += 1
            plane_done.add(net_name)

    def _route_one_net(net_name: str) -> bool:
        """Route one net with its resolved width/clearance + fanout halo.

        Records into all_results/ripup, reserves the emitted-width corridor
        on success. Returns True on success. Raises :class:`RoutingCancelled`
        when ``should_cancel`` fires (partial per-net results are kept by
        the caller).
        """
        nonlocal nets_routed, nets_failed, expansions_total
        conns = model.nets.get(net_name, [])
        if len(conns) < 2:
            return False
        if params.allowed_layers is not None:
            bad = _disallowed_terminal_layers(model, net_name, params.allowed_layers)
            if bad:
                ripup.record_failure(
                    net_name,
                    f"terminal on disallowed layer {bad} "
                    f"(allowed_layers={sorted(params.allowed_layers)})",
                )
                nets_failed += 1
                return False
        terminals = _terminals_for_net(
            model, routing_grid, net_name, params.allowed_layers
        )
        if len(terminals) < 2:
            return False
        if params.max_terminals_per_net > 0 and len(terminals) > params.max_terminals_per_net:
            # Heuristic guard: Steiner pair-searches scale ~n-1 capped A*
            # runs per net, so massive planes burn tens of millions of
            # expansions and still fail. Fail fast with an actionable
            # reason instead of hanging the job.
            ripup.record_failure(
                net_name,
                f"{len(terminals)} terminals exceeds max_terminals_per_net="
                f"{params.max_terminals_per_net} (massive nets do not route "
                f"in a single flight — split into smaller subsets, or rely "
                f"on copper pours + GND stitching for planes)",
            )
            nets_failed += 1
            return False
        pairs_total = len(terminals) * (len(terminals) - 1) // 2
        _report(net_name, 0, pairs_total, expansions_total)
        width_mm = resolve_track_width(model, params, net_name, extra_power_nets)
        width_map[net_name] = width_mm
        margin_mm = max(0.0, float(getattr(params, "fanout_margin_mm", 0.0) or 0.0))
        secondary = build_fanout_cost(
            model, routing_grid, net_name,
            clearance_mm=resolve_clearance(model, params, net_name),
            track_half_mm=width_mm / 2.0,
            extra_margin_mm=margin_mm,
        )
        if params.debug:
            # Sealed-terminal probe: bounded flood-fill per terminal, runs
            # in milliseconds. Tiny counts mean pair-searches will burn cap.
            timings[f"debug_escape_{net_name}"] = router.terminal_escape_counts(
                terminals, secondary
            )
        if corridors is not None and net_name not in pour_nets:
            # Pour-severance toll: tracks may still cross narrow pour
            # channels, but prefer paths that keep pours whole.
            secondary = secondary + severance_cost(
                corridors, corridor_min_thick, routing_grid,
                width_mm=width_mm,
                clearance_mm=resolve_clearance(model, params, net_name),
                weight=params.pour_severance_weight,
            )
        corridor = _width_cells(width_mm, params.grid_resolution_mm)
        clearance_mm = resolve_clearance(model, params, net_name)
        clearance_radius = track_corridor_radius_cells(
            width_mm, clearance_mm + margin_mm, params.grid_resolution_mm,
        )
        router.via_clearance_cells = via_halo_radius_cells(
            params.via_size_mm, clearance_mm, width_mm / 2.0,
            params.grid_resolution_mm,
        )
        # The window must admit at least a halo-width detour around both
        # terminals, or every pair pays a growth retry.
        router.window_margin_cells = max(
            params.window_margin_cells,
            router.via_clearance_cells,
            clearance_radius,
        )
        soft_mask = own_net_soft_mask(
            model, routing_grid, net_name,
            clearance_mm=clearance_mm, track_half_mm=width_mm / 2.0,
        )
        # Hard exclusion the smoother must respect: foreign-THT rings.
        # Own pads read 0 by construction, so terminals stay reachable.
        blocked_mask = secondary >= SECONDARY_BLOCKED
        success = False
        budget_hit = False
        while ripup.can_retry(net_name):
            if should_cancel is not None and should_cancel():
                raise RoutingCancelled(f"cancelled before attempt for {net_name}")

            def _pair_cb(done: int, total: int, exp: int, _net: str = net_name) -> None:
                _report(_net, done, total, expansions_total + exp)

            t_astar = time.perf_counter()
            result = router.route(
                terminals, secondary,
                progress_cb=_pair_cb if progress_cb is not None else None,
                should_cancel=should_cancel,
            )
            timings["astar_s"] = timings.get("astar_s", 0.0) + (time.perf_counter() - t_astar)
            expansions_total += result.expansions
            if (params.max_expansions_per_net > 0
                    and result.expansions >= params.max_expansions_per_net):
                budget_hit = True
            if params.debug and result.debug_attempts:
                timings[f"debug_attempts_{net_name}"] = [
                    dict(a) for a in result.debug_attempts
                ]
            _report(net_name, pairs_total, pairs_total, expansions_total)
            if result.success:
                t_smooth = time.perf_counter()
                pinned = {(c, r) for c, r, _ in terminals}
                result = _smooth_result(
                    result, cost_grid, routing_grid, corridor,
                    pinned=pinned, soft_mask=soft_mask,
                    clearance_radius=clearance_radius,
                    blocked_mask=blocked_mask,
                    angle_step_rad=math.pi / 18,
                )
                timings["smooth_s"] = timings.get("smooth_s", 0.0) + (time.perf_counter() - t_smooth)
                _apply_route_to_grid(
                    result, cost_grid, routing_grid, corridor,
                    extra_radius=clearance_radius,
                    via_clearance_cells=router.via_clearance_cells,
                )
                ripup.record_success(net_name, result.path)
                all_results[net_name] = result
                nets_routed += 1
                success = True
                break
            else:
                if budget_hit:
                    ripup.record_failure(
                        net_name,
                        f"expansion budget exhausted ({result.expansions} >= "
                        f"max_expansions_per_net={params.max_expansions_per_net}): "
                        f"no DRC-clean path found within budget — try a narrower "
                        f"track width, fewer terminals per run (piecemeal), or a "
                        f"coarser grid",
                    )
                else:
                    ripup.record_failure(net_name, "no path found")
                if budget_hit:
                    # Budget exhaustion is deterministic on an unchanged
                    # grid: the next attempt would burn the same budget for
                    # the same outcome, so stop retrying and fail the net.
                    break
        if not success:
            nets_failed += 1
        return success

    # 7-9. Route requested nets by class (power, then ground, then signal).
    # Cooperative cancel stops with partial results: already-routed nets
    # are kept, unreached nets count as failed with reason "cancelled".
    t_route = time.perf_counter()
    try:
        # 7. Route requested power nets (respects pad layers)
        for net_name in requested_power:
            if net_name not in plane_done:
                _route_one_net(net_name)

        # 8. Route requested ground nets
        for net_name in requested_ground:
            if net_name not in plane_done:
                _route_one_net(net_name)

        # 9. Route signal nets
        for net_name in requested_signal:
            if net_name not in plane_done:
                _route_one_net(net_name)
    except RoutingCancelled:
        cancelled = True
        for net_name in net_names:
            if net_name not in all_results and net_name not in ripup.get_failed_nets():
                ripup.record_failure(net_name, "cancelled")
                nets_failed += 1
    timings["route_s"] = time.perf_counter() - t_route

    # Aggregate corridor guidance stats across all routed nets.
    ctried = sum(r.corridor_tried for r in all_results.values())
    cfnd = sum(r.corridor_found for r in all_results.values())
    cused = sum(r.corridor_used for r in all_results.values())
    ccells = sum(r.corridor_cells for r in all_results.values())
    timings["corridor_tried"] = float(ctried)
    timings["corridor_found"] = float(cfnd)
    timings["corridor_used"] = float(cused)
    timings["corridor_cells"] = float(ccells)
    timings["corridor_use_rate"] = (
        round(cused / max(ctried, 1), 4) if ctried else 0.0
    )
    # 10. Convert all results to tracks/vias (per-net resolved widths;
    # own-THT hops reuse the barrel: no Via emitted there)
    tracks, vias = routes_to_tracks_vias(
        all_results,
        routing_grid,
        net_class_widths=width_map,
        default_width_mm=params.track_width_mm,
        power_width_mm=params.power_width_mm,
        via_size_mm=params.via_size_mm,
        via_drill_mm=params.via_drill_mm,
        tht_sites=_tht_sites_by_net(model, routing_grid),
        extra_power=extra_power_nets,
    )
    # 10a. Plane-tap stubs (four-layer strategy): additive, never folded
    # into MST results. Reserved in the cost grid at emission, so stitching
    # below sees them as existing copper.
    tracks.extend(plane_tracks)
    vias.extend(plane_vias)

    # 10b. Automatic GND continuity repair (stitching vias + bridge tracks).
    # Runs on the routed board: newly routed copper joins the island model,
    # so fresh fragments get repaired in the same call. Appends standard
    # vias/tracks; KiCad refills the pours around them on open. Runs
    # whenever stitching is enabled — even after a GND strip left no copper
    # behind — so evicted stitch vias are always rebuilt in the same call.
    stitch_vias_added = 0
    stitch_tracks_added = 0
    islands_before = 0
    islands_after = 0
    if params.stitch_gnd_vias:
        routed_model = replace(
            model,
            tracks=model.tracks + tuple(
                Track(start=Point(*t.start), end=Point(*t.end),
                      width_mm=t.width_mm, layer=t.layer, net_name=t.net_name)
                for t in tracks
            ),
            vias=model.vias + tuple(
                Via(position=Point(*v.position), size_mm=v.size_mm,
                    drill_mm=v.drill_mm, layers=tuple(v.layers), net_name=v.net_name)
                for v in vias
            ),
        )
        plan = plan_stitches(
            routed_model, routing_grid,
            size_mm=params.via_size_mm,
            drill_mm=params.via_drill_mm,
            clearance_mm=params.clearance_mm,
            bridge_width_mm=params.stitch_bridge_width_mm,
            max_vias=params.stitch_vias_max,
            max_bridges=params.stitch_bridges_max,
            max_bridge_mm=params.stitch_bridge_max_mm,
        )
        for sv in plan.vias:
            vias.append(OutputVia(
                position=(sv.position.x_mm, sv.position.y_mm),
                size_mm=sv.size_mm, drill_mm=sv.drill_mm,
                layers=tuple(sv.layers), net_name=sv.net_name or "",
            ))
        for st in plan.tracks:
            tracks.append(OutputTrack(
                start=(st.start.x_mm, st.start.y_mm),
                end=(st.end.x_mm, st.end.y_mm),
                width_mm=st.width_mm, layer=st.layer, net_name=st.net_name or "",
            ))
        stitch_vias_added = len(plan.vias)
        stitch_tracks_added = len(plan.tracks)
        islands_before = plan.islands_before
        islands_after = plan.islands_after

    # 11. Apply routes to PCB tree
    pcb_tree = None
    if original_pcb_tree is not None:
        pcb_tree = apply_routes_to_tree(original_pcb_tree, tracks, vias)

    # 11b. Refill zones if requested (requires kicad-cli).
    # Runs on a temp copy so the in-memory tree is only replaced when
    # kicad-cli loads, refills, and saves cleanly.
    refill_done = False
    if params.refill_zones and pcb_tree is not None:
        with tempfile.TemporaryDirectory(prefix="refill_") as tmpdir:
            tmp_path = Path(tmpdir) / "route_refill.kicad_pcb"
            tmp_path.write_text(to_sexpr(pcb_tree) + "\n", encoding="utf-8")
            refill_result = refill_zones(tmp_path)
            # loaded=True means kicad-cli wrote a DRC report (file parsed);
            # a load error leaves error set and the tree untouched.
            refill_done = refill_result.loaded and not refill_result.error
            if refill_done:
                pcb_tree = parse_file(tmp_path)

    # 12. Run DRC if requested
    drc_result = None
    if params.run_drc and pcb_tree is not None:
        drc_result = run_drc_on_tree(pcb_tree, prefix="route_")

    timings["expansions"] = float(expansions_total)
    timings["total_s"] = time.perf_counter() - t_total
    return RoutingResult(
        tracks_added=len(tracks),
        vias_added=len(vias),
        nets_routed=nets_routed,
        nets_failed=nets_failed,
        drc_clean=drc_result.clean if drc_result else True,
        drc_violations=drc_result.total_issues if drc_result else 0,
        drc_result=drc_result,
        pcb_tree=pcb_tree,
        stitch_vias_added=stitch_vias_added,
        stitch_tracks_added=stitch_tracks_added,
        gnd_islands_before=islands_before,
        gnd_islands_after=islands_after,
        gnd_stripped=gnd_stripped,
        cancelled=cancelled,
        expansions=expansions_total,
        timings=timings,
        refill_zones_done=refill_done,
        fanout_vias_added=len(plane_vias),
        fanout_tracks_added=len(plane_tracks),
        failed_reasons=dict(ripup.net_failures),
    ), cost_grid, routing_grid


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


def _build_tht_via_mask(model, grid) -> np.ndarray:
    """Build a 2D boolean mask of free layer-transition sites at THT holes.

    Each through-hole pad contributes a disc of drill radius (the copper-
    free hole the barrel already provides), not just its center cell, so
    the free hop is hittable at fine grids too. The surrounding copper
    annulus stays protected by the per-net exclusion ring in the secondary
    grid (foreign nets) — this mask only zeroes the transition price.

    Returns (H, W) boolean array where True = free transition site.
    """
    H, W = grid.height_cells, grid.width_cells
    res_mm = grid.resolution_mm
    mask = np.zeros((H, W), dtype=bool)
    rows, cols, radii = [], [], []
    for fp in model.footprints:
        for pad in fp.pads:
            if pad.is_through_hole:
                col, row = board_to_grid(
                    grid, pad.position.x_mm, pad.position.y_mm
                )
                rows.append(row)
                cols.append(col)
                radii.append(drill_radius_cells(pad.drill_mm, res_mm))
    stamp_discs(mask, rows, cols, radii, True)
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

    return replace(model, tracks=kept_tracks, vias=kept_vias)


def _strip_gnd_blockers_from_model(model: BoardModel) -> Tuple[BoardModel, int]:
    """Return (model without GND copper, count removed).

    GND-only eviction: every track/via on a ground net (``is_ground_net``)
    is a blocker candidate — pre-existing zone-pour stitch vias, old GND
    tracks, and prior stitcher bridges. Zones themselves are kept (KiCad
    refills them); ``plan_stitches()`` rebuilds inter-layer continuity
    afterwards. Signal/power copper is never touched here — that is what
    stops tracks ripping each other out. Deliberately ignores
    ``protected_nets``: GND is rebuildable, so protection still means
    "never rip signal/power" (see ``strip_gnd_blockers`` param).
    """
    kept_tracks = tuple(t for t in model.tracks if not is_ground_net(t.net_name))
    kept_vias = tuple(v for v in model.vias if not is_ground_net(v.net_name))
    removed = (len(model.tracks) - len(kept_tracks)) + (len(model.vias) - len(kept_vias))
    if removed == 0:
        return model, 0
    return replace(model, tracks=kept_tracks, vias=kept_vias), removed
