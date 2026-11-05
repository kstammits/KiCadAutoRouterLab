"""Per-region force-spring simulation loop for placement.

Extracted from placement/runner.py (Step 3: split god modules). Behavior-neutral
move only — `_run_region_simulation` and its frame/force rounding helpers are
unchanged.

Supports multi-board region simulation: footprints are grouped by board
outline region (from Edge.Cuts) and each region is simulated independently
with its own bounding box and timestep state.
"""

from __future__ import annotations

import math
from typing import Collection, Dict, List, Optional, Set, Tuple

import numpy as np

from ..board_model import BoardRegion, Footprint, Pad, is_ground_net, is_power_net_effective
from ..mass import courtyard_bbox_area_m2, footprint_mass_kg
from .contact import (
    _courtyard_outline_polygon,
    _footprint_courtyard_polygon,
    courtyard_is_degenerate,
)
from .align import (
    ALIGN_RECOMPUTE_ITERS,
    build_align_ref_axes,
    compute_align_targets,
)
from .depenetrate import _depenetrate_positions
from .force import ForceContext, _compute_total_force
from .model import (
    _build_net_edges_pad_level,
    _find_decap_chip_links,
    _find_decap_pad_indices,
)
from .params import (
    FRAME_ANGLE_DECIMALS,
    FRAME_XY_DECIMALS,
    STALL_MEAN_EPS_MULT,
    PlacementParams,
)
from .rigid import (
    _clamp_rigid_step,
    _compute_footprint_pose_from_pads,
    _project_rigid_poses,
)


def _round_sig3(x: float) -> float:
    """Round to 3 significant figures."""
    if x == 0.0 or not math.isfinite(x):
        return 0.0
    return round(x, 2 - int(math.floor(math.log10(abs(x)))))


def _round_force_components(
    comps: Dict[str, Tuple[float, float]],
) -> Dict[str, Tuple[float, float]]:
    """Round force component values to 3 significant figures."""
    return {
        cause: (_round_sig3(fx), _round_sig3(fy))
        for cause, (fx, fy) in comps.items()
    }


def _run_region_simulation(
    region_idx: int,
    region: BoardRegion,
    region_pad_indices: List[int],
    region_pads: List[Pad],
    region_pad_to_fp_idx: List[int],
    region_fp_uuid_to_pad_indices: Dict[str, List[int]],
    region_global_pad_idx_to_local: Dict[int, int],
    region_pad_is_ghost: List[bool],
    model,
    params: PlacementParams,
    movable_uuids: Optional[Set[str]],
    record_frames: bool = False,
    frame_stride: int = 1,
    extra_power_nets: Collection[str] = (),
) -> Tuple[
    Dict[str, Tuple[float, float, float]],
    Dict[str, Tuple[float, float]],
    int,
    Dict[str, Dict[str, Tuple[float, float]]],
    List[Dict[str, Tuple[float, float, float]]],
    List[Dict[str, Dict[str, Tuple[float, float]]]],
    str,
    float,
    dict,
    bool,
]:
    """Run force-spring simulation for a single board region."""
    # Extract region bounds
    min_x, max_x, min_y, max_y = region.bbox

    # Filter pads for this region
    n_pads = len(region_pads)
    if n_pads == 0:
        return {}, {}, 0, {}, [], [], "empty", 0.0, {}, False

    # Build pad positions array
    pad_positions = np.zeros((n_pads, 2), dtype=np.float64)
    for i, pad in enumerate(region_pads):
        pad_positions[i, 0] = pad.position.x_mm
        pad_positions[i, 1] = pad.position.y_mm

    # Determine movable pads (real footprints only; ghosts are always fixed)
    fp_movable: Dict[str, bool] = {}
    for fp in model.footprints:
        if not fp.uuid or fp.locked:
            fp_movable[fp.uuid] = False
            continue
        if model.footprint_region.get(fp.uuid) != region_idx:
            fp_movable[fp.uuid] = False
            continue
        if movable_uuids is not None and fp.uuid not in movable_uuids:
            fp_movable[fp.uuid] = False
        else:
            fp_movable[fp.uuid] = True

    # Ghost footprints are never movable
    for fp in model.ghost_footprints:
        if fp.uuid and model.footprint_region.get(fp.uuid) == region_idx:
            fp_movable[fp.uuid] = False

    pad_movable = np.array([
        (not region_pad_is_ghost[i] and fp_movable.get(
            model.footprints[region_pad_to_fp_idx[i]].uuid, False
        )) if region_pad_to_fp_idx[i] >= 0 else False
        for i in range(n_pads)
    ], dtype=bool)

    # SI masses: per-footprint kg broadcast to pads (static for the run).
    _uuid_to_mass_kg: Dict[str, float] = {}
    for _fp in list(model.footprints) + list(model.ghost_footprints):
        if _fp.uuid and _fp.uuid not in _uuid_to_mass_kg:
            _uuid_to_mass_kg[_fp.uuid] = footprint_mass_kg(
                _fp.footprint_id,
                courtyard_bbox_area_m2(_fp),
                params.mass_density_kg_m2,
                params.mass_floor_kg,
            )
    _fallback_mass_kg = max(params.mass_floor_kg, 1e-12)

    def _pad_footprint(_i: int):
        _idx = region_pad_to_fp_idx[_i]
        try:
            if _idx >= 0:
                return model.footprints[_idx]
            return model.ghost_footprints[-_idx - 1]
        except IndexError:
            return None

    pad_mass_kg = np.array([
        _uuid_to_mass_kg.get(
            _pad_footprint(i).uuid if _pad_footprint(i) is not None else "", _fallback_mass_kg)
        for i in range(n_pads)
    ], dtype=np.float64)
    pad_mass_kg = np.maximum(pad_mass_kg, _fallback_mass_kg)

    if not pad_movable.any():
        return {}, {}, 0, {}, [], [], "empty", 0.0, {}, False

    # Net attraction edges for this region.
    _ref_by_idx: Dict[int, str] = {}
    for _i, _fp_idx in enumerate(region_pad_to_fp_idx):
        if _fp_idx >= 0 and _fp_idx not in _ref_by_idx:
            _ref_by_idx[_fp_idx] = model.footprints[_fp_idx].ref
    _decap_pads = _find_decap_pad_indices(
        region_pads, region_pad_to_fp_idx, _ref_by_idx, extra_power_nets
    )
    src_idx, dst_idx, edge_weights = _build_net_edges_pad_level(
        region_pads, region_pad_to_fp_idx,
        decap_pad_indices=_decap_pads,
        power_scale=params.power_attraction_scale,
        decap_link=params.decap_link,
        include_power=params.include_power_nets,
        extra_power=extra_power_nets,
    )

    # Supplemental decap-chip links: small caps gain one symmetric edge
    # per shared net to their nearest chip (global rail clique above is
    # kept for rail balance and chip rotation). Unassigned/distant caps
    # and bulk electrolytics get nothing here (global-only).
    if params.decap_link > 0.0:
        _fp_area: Dict[int, float] = {}
        for _i, _fp_idx in enumerate(region_pad_to_fp_idx):
            if _fp_idx >= 0 and _fp_idx not in _fp_area:
                try:
                    _fp_area[_fp_idx] = courtyard_bbox_area_m2(
                        model.footprints[_fp_idx]
                    )
                except (AttributeError, TypeError, ValueError):
                    continue
        _local_edges, _ = _find_decap_chip_links(
            region_pads, region_pad_to_fp_idx, _ref_by_idx,
            fp_area_m2_by_idx=_fp_area,
            extra_power=extra_power_nets,
        )
        _kept: List[Tuple[int, int]] = []
        for _a, _b in _local_edges:
            try:
                _net = region_pads[_a].net_name
            except (IndexError, AttributeError):
                continue
            if not _net or _net != getattr(region_pads[_b], "net_name", None):
                continue
            if is_power_net_effective(_net, extra_power_nets) and not params.include_power_nets:
                continue
            _kept.append((_a, _b) if _a < _b else (_b, _a))
        _n_global = len(src_idx)
        if _kept:
            _lsrc = np.array([k[0] for k in _kept], dtype=int)
            _ldst = np.array([k[1] for k in _kept], dtype=int)
            _lw = np.full(len(_kept), float(params.decap_link), dtype=float)
            src_idx = np.concatenate([src_idx, _lsrc])
            dst_idx = np.concatenate([dst_idx, _ldst])
            edge_weights = np.concatenate([edge_weights, _lw])
            _n_global = len(edge_weights) - len(_kept)
    else:
        _n_global = len(edge_weights)
        _kept = []
    has_net_edges = len(src_idx) > 0

    # Decap protection: global decap-incident edges pull the cap toward
    # its target but do not drag the target (often a large, anchored
    # part) into the cap. Decap-decap edges and supplemental local
    # decap-chip links stay symmetric (the chip rotates toward its cap).
    edge_sides = np.zeros(len(src_idx), dtype=np.int8)
    if _decap_pads and len(src_idx):
        _n_global = min(int(_n_global), len(src_idx))
        _gsrc, _gdst = src_idx[:_n_global], dst_idx[:_n_global]
        _src_decap = np.isin(_gsrc, list(_decap_pads))
        _dst_decap = np.isin(_gdst, list(_decap_pads))
        edge_sides[:_n_global][_src_decap & ~_dst_decap] = 1
        edge_sides[:_n_global][_dst_decap & ~_src_decap] = 2

    # Build mapping from footprint UUID to local pad indices for wall checks
    region_fp_to_pad_indices = {}
    for fp_uuid, pad_indices in region_fp_uuid_to_pad_indices.items():
        region_fp_to_pad_indices[fp_uuid] = pad_indices

    iteration = 0

    # Build list of footprints in this region with courtyards (real + ghost)
    fp_uuids: List[str] = []
    fp_movable_list: List[bool] = []
    fp_pad_indices: Dict[str, List[int]] = {}
    pad_to_fp_local: List[int] = []

    for fp in model.footprints:
        if not fp.uuid or model.footprint_region.get(fp.uuid) != region_idx:
            continue
        if not fp.pads:
            continue
        fp_uuids.append(fp.uuid)
        fp_movable_list.append(fp_movable.get(fp.uuid, False))
        if fp.uuid in region_fp_uuid_to_pad_indices:
            fp_pad_indices[fp.uuid] = region_fp_uuid_to_pad_indices[fp.uuid]

    for fp in model.ghost_footprints:
        if not fp.uuid or model.footprint_region.get(fp.uuid) != region_idx:
            continue
        fp_uuids.append(fp.uuid)
        fp_movable_list.append(False)
        if fp.uuid in region_fp_uuid_to_pad_indices:
            fp_pad_indices[fp.uuid] = region_fp_uuid_to_pad_indices[fp.uuid]

    fp_uuid_to_local_idx = {uuid: i for i, uuid in enumerate(fp_uuids)}

    for i in range(len(region_pads)):
        fp_idx = region_pad_to_fp_idx[i]
        if fp_idx >= 0:
            fp_uuid = model.footprints[fp_idx].uuid
        else:
            ghost_idx = -fp_idx - 1
            if ghost_idx < len(model.ghost_footprints):
                fp_uuid = model.ghost_footprints[ghost_idx].uuid
            else:
                fp_uuid = ""
        pad_to_fp_local.append(fp_uuid_to_local_idx.get(fp_uuid, -1))

    fp_movable_arr = np.array(fp_movable_list, dtype=bool)

    _pad_fp_idx = np.array(pad_to_fp_local, dtype=int)
    fp_local_to_rows: List[np.ndarray] = [
        np.where(_pad_fp_idx == _li)[0] for _li in range(len(fp_uuids))
    ]

    # ---- Per-run courtyard shape cache ----
    fp_by_uuid: Dict[str, Footprint] = {}
    for _fp in model.footprints:
        if _fp.uuid:
            fp_by_uuid[_fp.uuid] = _fp
    for _fp in model.ghost_footprints:
        if _fp.uuid:
            fp_by_uuid[_fp.uuid] = _fp

    # ---- Debug instrumentation (zero overhead when params.debug is False) ----
    _dbg_enabled = bool(params.debug)
    _dbg_iters: List[dict] = [] if _dbg_enabled else None
    _dbg_depen: Optional[List[dict]] = [] if _dbg_enabled else None

    def _local_shape(_fp: Optional[Footprint]) -> dict:
        if _fp is None:
            return {"body": {"kind": "empty", "empty": True}, "pins": [],
                    "pad_idx": np.array([], dtype=int),
                    "orig_pos": np.array([], dtype=np.float64)}
        outline = _courtyard_outline_polygon(_fp.courtyard)
        if outline is not None and courtyard_is_degenerate(_fp):
            # Drafting debris, not a body (pin-marker dots): fall through
            # to the pad-copper hull so dynamics, legality, and the viewer
            # agree on the same body.
            outline = None
        if outline is not None:
            body = {
                "kind": "courtyard",
                "empty": False,
                "verts": np.array(outline.exterior.coords, dtype=np.float64),
                "layer": _fp.layer,
            }
        elif _fp.courtyard:
            fallback = _footprint_courtyard_polygon(_fp)
            body = {
                "kind": "courtyard",
                "empty": False,
                "verts": np.array(fallback.exterior.coords, dtype=np.float64),
                "layer": _fp.layer,
            }
        elif _fp.pads:
            # No outline at all: pad-copper hull (rotated with the part),
            # not an axis-aligned center box.
            hull = _footprint_courtyard_polygon(_fp)
            try:
                verts = np.array(hull.exterior.coords, dtype=np.float64)
            except Exception:
                verts = np.empty((0, 2), dtype=np.float64)
            if len(verts) >= 3:
                body = {
                    "kind": "courtyard",
                    "empty": False,
                    "verts": verts,
                    "layer": _fp.layer,
                }
            else:
                body = {"kind": "bbox", "empty": False, "layer": _fp.layer}
        else:
            _cx, _cy = _fp.x_mm, _fp.y_mm
            body = {
                "kind": "point",
                "empty": False,
                "verts": np.array([
                    [_cx - 0.5, _cy - 0.5], [_cx + 0.5, _cy - 0.5],
                    [_cx + 0.5, _cy + 0.5], [_cx - 0.5, _cy + 0.5],
                ], dtype=np.float64),
                "layer": _fp.layer,
            }
        pins = []
        # For footprints with courtyards: add all THT pins, but mark which
        # extend significantly beyond the courtyard bounds (threshold 0.5mm).
        # Pins fully inside or barely extending (<0.5mm) are redundant with
        # courtyard collision (same-layer); pins extending significantly beyond
        # are needed for mounting holes etc. Cross-layer always needs pins.
        EXTENDS_THRESHOLD_MM = 0.5
        if body["kind"] == "courtyard":
            body_poly = body.get("verts")  # np.array of courtyard vertices
            if body_poly is not None and len(body_poly) > 0:
                minx = float(body_poly[:, 0].min())
                maxx = float(body_poly[:, 0].max())
                miny = float(body_poly[:, 1].min())
                maxy = float(body_poly[:, 1].max())
            else:
                minx = maxx = miny = maxy = 0.0
            for pad in _fp.pads:
                if pad.is_through_hole or pad.pad_type == "np_thru_hole":
                    pad_r = 0.5 * math.hypot(pad.size_mm[0], pad.size_mm[1]) + 0.5
                    px, py = pad.position.x_mm, pad.position.y_mm
                    # Does this pin extend significantly beyond the courtyard?
                    extends_x = (px - pad_r < minx - EXTENDS_THRESHOLD_MM or
                                 px + pad_r > maxx + EXTENDS_THRESHOLD_MM)
                    extends_y = (py - pad_r < miny - EXTENDS_THRESHOLD_MM or
                                 py + pad_r > maxy + EXTENDS_THRESHOLD_MM)
                    extends = extends_x or extends_y
                    pins.append({
                        "center": (px, py),
                        "radius": pad_r,
                        "layer": _fp.layer,
                        "extends_beyond_courtyard": extends,
                    })
        else:
            # No courtyard: add all THT pins as collision shapes
            for pad in _fp.pads:
                if pad.is_through_hole or pad.pad_type == "np_thru_hole":
                    pad_r = 0.5 * math.hypot(pad.size_mm[0], pad.size_mm[1]) + 0.5
                    pins.append({
                        "center": (pad.position.x_mm, pad.position.y_mm),
                        "radius": pad_r,
                        "layer": _fp.layer,
                        "extends_beyond_courtyard": True,  # no courtyard = always needed
                    })
        _pad_idx = np.array(fp_pad_indices.get(_fp.uuid, []), dtype=int)
        _orig_pos = np.array(
            [(p.position.x_mm, p.position.y_mm) for p in _fp.pads],
            dtype=np.float64,
        ) if _fp.pads else np.array([], dtype=np.float64)
        return {"body": body, "pins": pins, "pad_idx": _pad_idx, "orig_pos": _orig_pos}

    cy_cache: Dict[str, dict] = {}
    for _uuid in fp_uuids:
        _fp = fp_by_uuid.get(_uuid)
        cy_cache[_uuid] = _local_shape(_fp)

    # Same-footprint mask: no repulsion between pads of one footprint.
    _fp_col = np.array(pad_to_fp_local, dtype=int)
    same_fp_mask = (
        (_fp_col[:, None] == _fp_col[None, :])
        & (_fp_col[:, None] >= 0)
        & ~np.eye(len(region_pads), dtype=bool)
    )

    # Build ghost attraction edges: (ghost_anchor, target_pad_indices, ka)
    ghost_attractions = []
    for fp in model.ghost_footprints:
        if not fp.uuid or model.footprint_region.get(fp.uuid) != region_idx:
            continue
        if not fp.ghost_attractions:
            continue
        ghost_anchor = np.array([fp.x_mm, fp.y_mm], dtype=np.float64)
        for target_ref, ka_attr in fp.ghost_attractions:
            target_fp = model.by_ref.get(target_ref)
            if target_fp is None:
                continue
            if not target_fp.uuid:
                continue
            if model.footprint_region.get(target_fp.uuid) != region_idx:
                continue
            target_pad_indices = fp_pad_indices.get(target_fp.uuid, [])
            if not target_pad_indices:
                continue
            ghost_attractions.append((ghost_anchor, target_pad_indices, ka_attr))

    positions = pad_positions.copy()
    prev_positions = pad_positions.copy()

    # Orientation-consensus regime (Tidy): reference axes from the
    # committed pose, targets refreshed every N iterations. Off unless a
    # stiffness is set, so cluster/legalize trajectories are untouched.
    align_active = (
        params.align_angle_k_N > 0.0 or params.align_pos_k_N_m > 0.0
    )
    _ghost_uuids = {
        fp.uuid for fp in model.ghost_footprints if fp.uuid
    }
    _ghost_local = {
        fp_uuid_to_local_idx[u] for u in _ghost_uuids
        if u in fp_uuid_to_local_idx
    }
    _fp_layers = [
        getattr(fp_by_uuid.get(u), "layer", "") or "" for u in fp_uuids
    ]
    _committed_angles = {
        fp_uuid_to_local_idx[fp.uuid]: float(fp.angle_deg)
        for fp in list(model.footprints) + list(model.ghost_footprints)
        if fp.uuid and fp.uuid in fp_uuid_to_local_idx
    }
    _align_ref_axes = (
        build_align_ref_axes(fp_local_to_rows, pad_positions)
        if align_active else {}
    )

    def _refresh_align_targets(
        prev: Optional[dict] = None,
    ) -> dict:
        if not align_active:
            return {}
        return compute_align_targets(
            positions, fp_local_to_rows, fp_movable_arr,
            _fp_layers, _ghost_local, _committed_angles, _align_ref_axes,
            radius_mm=params.align_radius_m * 1000.0,
            deadband_deg=params.align_deadband_deg,
            prev_targets=prev,
        )

    _align_targets = _refresh_align_targets()

    # Polygon cache for courtyard collision - persists across iterations.
    # Rebuild thresholds are TIGHT (20µm/0.02°): a stale body polygon snaps
    # the contact winner discretely when it refreshes, which reads as force
    # flip-flop at the sample rate. Small thresholds keep the refresh jumps
    # below what the normal blending slews out invisibly.
    poly_cache: Dict[str, dict] = {}
    cache_rebuild_threshold_mm = 0.02
    cache_rebuild_threshold_rad = math.radians(0.02)

    # Shared force-evaluation context (static caches + region bounds).
    # Build region polygons list and fp_uuid -> region mapping for the board wall
    region_polygons = []
    fp_uuid_to_region = {}
    if model.board_regions:
        for ridx, reg in enumerate(model.board_regions):
            # Convert BoardRegion polygon to shapely Polygon
            from shapely.geometry import Polygon as ShapelyPolygon
            if reg.polygon:
                region_polygons.append(ShapelyPolygon([(p.x_mm, p.y_mm) for p in reg.polygon]))
            else:
                region_polygons.append(ShapelyPolygon())
    # Map each footprint in this region to its region index
    for fp_uuid, ridx in model.footprint_region.items():
        if ridx == region_idx:
            fp_uuid_to_region[fp_uuid] = ridx

    fctx = ForceContext(
        fp_uuids=fp_uuids,
        fp_local_to_rows=fp_local_to_rows,
        fp_movable_arr=fp_movable_arr,
        pad_to_fp_local=pad_to_fp_local,
        poly_cache=poly_cache,
        cache_rebuild_threshold_mm=cache_rebuild_threshold_mm,
        cache_rebuild_threshold_rad=cache_rebuild_threshold_rad,
        pad_movable=pad_movable,
        fp_pad_indices=fp_pad_indices,
        fp_uuid_to_local_idx=fp_uuid_to_local_idx,
        region_polygons=region_polygons,
        fp_uuid_to_region=fp_uuid_to_region,
        cy_cache=cy_cache,
        pad_charged=np.array(
            [p.net_name is not None for p in region_pads], dtype=bool
        ),
    )

    # SI integrator state: per-pad velocities (m/s) and adaptive timestep (s).
    _vel = np.zeros((len(region_pads), 2), dtype=np.float64)
    _dt = float(params.dt_s)
    _prev_ke: Optional[float] = None

    # Initial forces predict the NEXT step from the committed board positions.
    initial_force, initial_components = _compute_total_force(
        positions, len(region_pads), pad_movable,
        params, has_net_edges, src_idx, dst_idx, edge_weights,
        ghost_attractions,
        same_fp_mask,
        edge_sides=edge_sides,
        return_components=True,
        ctx=fctx,
        pad_velocities=_vel,
        align_targets=_align_targets,
        align_ref_axes=_align_ref_axes,
    )

    # ---- Trajectory frames (opt-in motion viz) ----
    _frame_stride = max(1, int(frame_stride or 1))
    _frame_fps: List[Tuple[Footprint, list]] = []
    if record_frames:
        for _fp in model.footprints:
            if not _fp.uuid or _fp.locked:
                continue
            if model.footprint_region.get(_fp.uuid) != region_idx:
                continue
            if movable_uuids is not None and _fp.uuid not in movable_uuids:
                continue
            _pi = region_fp_uuid_to_pad_indices.get(_fp.uuid)
            if not _pi:
                continue
            _frame_fps.append((_fp, list(_pi)))

    def _capture_frame(pos: np.ndarray) -> Dict[str, Tuple[float, float, float]]:
        frame: Dict[str, Tuple[float, float, float]] = {}
        for _fp, _pi in _frame_fps:
            try:
                _nx, _ny, _na = _compute_footprint_pose_from_pads(
                    _fp, pos, _pi, region_global_pad_idx_to_local
                )
            except Exception:
                continue
            _dx = _nx - _fp.x_mm
            _dy = _ny - _fp.y_mm
            _da = (_na - _fp.angle_deg + 180) % 360 - 180
            frame[_fp.uuid] = (
                round(float(_dx), FRAME_XY_DECIMALS),
                round(float(_dy), FRAME_XY_DECIMALS),
                round(float(_da), FRAME_ANGLE_DECIMALS),
            )
        return frame

    def _capture_force_frame(pos: np.ndarray) -> Dict[str, Dict[str, Tuple[float, float]]]:
        force, components = _compute_total_force(
            pos, len(region_pads), pad_movable,
            params, has_net_edges, src_idx, dst_idx, edge_weights,
            ghost_attractions,
            same_fp_mask,
            edge_sides=edge_sides,
            return_components=True,
            ctx=fctx,
            pad_velocities=_vel,
            align_targets=_align_targets,
            align_ref_axes=_align_ref_axes,
        )
        force_frame: Dict[str, Dict[str, Tuple[float, float]]] = {}
        for _fp, _pi in _frame_fps:
            if _fp.uuid not in region_fp_uuid_to_pad_indices:
                continue
            pad_indices = region_fp_uuid_to_pad_indices[_fp.uuid]
            if not pad_indices:
                continue
            comp: Dict[str, Tuple[float, float]] = {}
            for cause, arr in components.items():
                s = arr[pad_indices].sum(axis=0)
                if bool(np.all(np.isfinite(s))):
                    comp[cause] = (float(s[0]), float(s[1]))
            if comp:
                force_frame[_fp.uuid] = _round_force_components(comp)
        return force_frame

    _region_frames: List[Dict[str, Tuple[float, float, float]]] = []
    _region_force_frames: List[Dict[str, Dict[str, Tuple[float, float]]]] = []
    if record_frames and _frame_fps:
        _region_frames.append(_capture_frame(positions))
        _region_force_frames.append(_capture_force_frame(positions))

    # Rigid-body projection data (PBD): per movable multi-pad footprint.
    _proj_bodies: List[Tuple[np.ndarray, np.ndarray]] = []
    for _uuid, _movable in zip(fp_uuids, fp_movable_list):
        if not _movable:
            continue
        _brows = fp_pad_indices.get(_uuid)
        if _brows is None or len(_brows) < 2:
            continue
        _barr = np.asarray(_brows, dtype=int)
        _proj_bodies.append((_barr, positions[_barr].copy()))

    # Settle threshold for the convergence gate (newtons).
    force_settle_N = max(
        20.0 * params.friction_N,
        4.0 * params.ghost_k_N,
        1e-3 * max(params.wall_k_N, params.attraction_k_N),
    )
    # Energy-guard absolute floor: a fraction of the characteristic single-pad
    # kinetic energy (0.5*m*vmax^2). KE jumps below this are legitimate force
    # work or contact noise in stiff configurations, not instability — only
    # larger jumps justify halving the timestep.
    _ke_abs_increase_threshold = (
        0.3 * 0.5 * float(np.max(pad_mass_kg)) * float(params.vmax_m_s) ** 2
    )
    stop_reason = "budget"
    residual_max_force_N = 0.0
    iteration = 0  # defined when max_iterations=0 (static probe: loop never runs)
    # Stall detection: window-averaged pose progress. Per-step displacement
    # never goes quiet under contact chatter — a part jittering in place
    # against a locked neighbor moves every step, so a consecutive-quiet
    # gate burns budget forever. Instead the mean pose over each stall
    # window is compared against the previous window's mean: a limit cycle
    # averages itself out (its center sits still) while real transit moves
    # the mean orders of magnitude more. A mean shift below
    # STALL_MEAN_EPS_MULT * convergence_eps_m stops the run as "stalled".
    # Slow creep still moves the mean, so only true standstills stop here.
    # stall_window=0 disables. The initial pose seeds the reference, so a
    # run that starts wedged stops after exactly one window.
    stall_mean_ref = positions[pad_movable].copy()
    stall_acc = np.zeros_like(stall_mean_ref)
    stall_n = 0

    for iteration in range(params.max_iterations):
        # Consensus refresh: quasi-static targets that track drift.
        if align_active and iteration > 0 and iteration % ALIGN_RECOMPUTE_ITERS == 0:
            _align_targets = _refresh_align_targets(_align_targets)
        # Update context with current velocities for tangential assist
        fctx.pad_velocities = _vel
        force, _ = _compute_total_force(
            positions, len(region_pads), pad_movable,
            params, has_net_edges, src_idx, dst_idx, edge_weights,
            ghost_attractions,
            same_fp_mask,
            edge_sides=edge_sides,
            ctx=fctx,
            pad_velocities=_vel,
            align_targets=_align_targets,
            align_ref_axes=_align_ref_axes,
        )

        # --- debug: per-iteration snapshot of positions + raw forces ---
        if _dbg_iters is not None:
            _snap = {"iteration": iteration, "footprints": {}}
            for fi, uuid in enumerate(fp_uuids):
                if not fp_movable_list[fi]:
                    continue
                _rows = fp_local_to_rows[fi]
                if _rows is None or len(_rows) == 0:
                    continue
                _pos = positions[_rows]
                _frc = force[_rows]
                _snap["footprints"][uuid] = {
                    "pad_positions": [[float(x), float(y)] for x, y in _pos],
                    "pad_forces": [[float(fx), float(fy)] for fx, fy in _frc],
                    "net_force": [float(_frc.sum(axis=0)[0]), float(_frc.sum(axis=0)[1])],
                }
            _dbg_iters.append(_snap)

        # Dry friction: freeze a whole footprint whose largest pad-force
        # norm is below friction_N. Frozen bodies also lose velocity.
        _frozen = np.zeros(len(region_pads), dtype=bool)
        if params.friction_N > 0:
            _fp_force_norm = np.linalg.norm(force, axis=1)
            for _rows in fp_local_to_rows:
                if len(_rows) and np.max(_fp_force_norm[_rows]) < params.friction_N:
                    force[_rows] = 0.0
                    _frozen[_rows] = True
        force[~pad_movable] = 0.0
        _frozen[~pad_movable] = True

        if pad_movable.any():
            # Rigid-aware residual: net force per movable FOOTPRINT (pads
            # summed), not per-pad max. Pads of one rigid body routinely
            # carry large canceling contact loads (wall on the contact pads
            # vs attraction spread over all pads); per-pad max then never
            # settles even at a true rigid equilibrium and every
            # contact rest runs to budget. Net force vanishes exactly when
            # the body has nothing left to obey. (Persistent torque still
            # moves pads, so the displacement gate covers rotation.)
            _res = 0.0
            for _rows, _mov in zip(fp_local_to_rows, fp_movable_list):
                if not _mov or len(_rows) == 0:
                    continue
                _res = max(
                    _res,
                    float(np.linalg.norm(force[_rows].sum(axis=0))),
                )
            residual_max_force_N = _res
        else:
            residual_max_force_N = 0.0

        # Semi-implicit Euler: v += (F/m) dt, with implicit-Euler viscous
        # decay v *= 1/(1 + c*dt/m), then a speed ceiling.
        _accel = force / pad_mass_kg[:, None]
        _vel[pad_movable] += _accel[pad_movable] * _dt
        _decay = 1.0 / (1.0 + params.damping_Ns_m * _dt / pad_mass_kg)
        _vel *= _decay[:, None]
        _vel[_frozen] = 0.0
        _speed = np.linalg.norm(_vel, axis=1, keepdims=True)
        _vcap = params.vmax_m_s / np.maximum(_speed, 1e-12)
        np.minimum(_vcap, 1.0, out=_vcap)
        _vel *= _vcap
        _vel[~pad_movable] = 0.0

        # Drift in meters, applied to the millimeter-native positions.
        _old_positions = positions.copy()
        positions[pad_movable] += _vel[pad_movable] * _dt * 1000.0
        positions = np.nan_to_num(positions, nan=0.0, posinf=0.0, neginf=0.0)

        # Rigid projection + rigid-step clamp.
        _project_rigid_poses(positions, _proj_bodies)
        _clamp_rigid_step(
            positions, _old_positions, _proj_bodies, pad_movable,
            cap_mm=params.vmax_m_s * _dt * 1000.0,
            max_rot_rad=math.radians(10.0),
        )
        # Re-derive velocity from the actual projected displacement so the
        # projection can't inject phantom energy into the guard below.
        # Blend with old velocity (momentum_alpha) for momentum persistence.
        if _dt > 0:
            vel_from_disp = (
                (positions[pad_movable] - _old_positions[pad_movable]) * 1e-3 / _dt)
            alpha = float(params.momentum_alpha)
            _vel[pad_movable] = alpha * _vel[pad_movable] + (1.0 - alpha) * vel_from_disp
        _vel[_frozen] = 0.0
        _vel[~pad_movable] = 0.0

        # Adaptive-dt energy guard: bleed and slow down on significant
        # kinetic-energy jumps, regrow otherwise.
        _ke = 0.5 * float(np.sum(
            pad_mass_kg[pad_movable, None]
            * _vel[pad_movable] * _vel[pad_movable]))
        # Only trigger the energy guard on significant jumps: above the
        # numerical noise floor AND relatively large (1.25x) AND absolutely
        # large (see _ke_abs_increase_threshold above).
        if (_prev_ke is not None
                and _ke > 1e-12
                and _ke > 1.25 * _prev_ke + 1e-15
                and _ke - _prev_ke > _ke_abs_increase_threshold):
            _dt = max(float(params.dt_min_s), _dt * 0.5)
            _vel *= 0.5
        else:
            _dt = min(float(params.dt_max_s), _dt * 1.1)
        _prev_ke = _ke

        if record_frames and _frame_fps and ((iteration + 1) % _frame_stride == 0):
            _frame = _capture_frame(positions)
            _region_frames.append(_frame)
            _region_force_frames.append(_capture_force_frame(positions))

        # Convergence check: max pad displacement AND residual forces below threshold.
        max_disp = float(np.max(np.linalg.norm(positions - prev_positions, axis=1))) * 1e-3
        if max_disp < params.convergence_eps_m and residual_max_force_N < force_settle_N:
            stop_reason = "converged"
            break

        # Stall check: no NET progress of the window-averaged pose over a
        # full stall window means the run sits in place with unserved
        # forces (wedged, or chattering with no resolvable drift) —
        # converged can't fire (forces remain), but burning budget changes
        # nothing. The mean (not endpoints, not per-step quiet) is what
        # oscillates out: endpoint comparison is phase-luck on a limit
        # cycle, and per-step quiet never arrives under chatter. Slow creep
        # still moves the mean, so only true standstills stop here.
        # stall_window=0 disables.
        if params.stall_window > 0:
            stall_acc += positions[pad_movable]
            stall_n += 1
            if stall_n >= params.stall_window:
                stall_mean = stall_acc / stall_n
                if stall_mean.shape[0]:
                    stall_shift_m = float(np.max(np.linalg.norm(
                        stall_mean - stall_mean_ref, axis=1))) * 1e-3
                else:
                    stall_shift_m = 0.0
                if stall_shift_m < STALL_MEAN_EPS_MULT * params.convergence_eps_m:
                    stop_reason = "stalled"
                    break
                stall_mean_ref = stall_mean
                stall_acc[:] = 0.0
                stall_n = 0

        prev_positions = positions.copy()

    # Final de-penetration pass: eject residual overlaps the penalty wall
    # law equilibrated against (force balance, not zero overlap). Skipped
    # for static probes (max_iterations=0 must report, never move) and
    # when the wall response itself is disabled (wall_k_N/halo == 0):
    # geometric ejection is the wall law made positional, so walls-off
    # configurations must stay untouched.
    _depen_moved = False
    if (params.max_iterations > 0 and params.wall_k_N > 0.0
            and params.wall_halo_m > 0.0):
        _depen_moved = _depenetrate_positions(
            positions, pad_movable, fp_uuids, fp_movable_list,
            fp_local_to_rows, fp_by_uuid, _proj_bodies,
            debug_sink=_dbg_depen,
        )
        if _depen_moved:
            _vel[:] = 0.0

    # Compute footprint deltas from final pad positions
    deltas: Dict[str, Tuple[float, float, float]] = {}
    final_max_disp = 0.0
    footprint_forces: Dict[str, Tuple[float, float]] = {}
    footprint_components: Dict[str, Dict[str, Tuple[float, float]]] = {}

    final_force, final_components = _compute_total_force(
        positions, len(region_pads), pad_movable,
        params, has_net_edges, src_idx, dst_idx, edge_weights,
        ghost_attractions,
        same_fp_mask,
        edge_sides=edge_sides,
        return_components=True,
        ctx=fctx,
        pad_velocities=_vel,
        align_targets=_align_targets,
        align_ref_axes=_align_ref_axes,
    )

    for fp in model.footprints:
        if model.footprint_region.get(fp.uuid) != region_idx:
            continue
        if not fp.uuid or fp.locked:
            continue
        if movable_uuids is not None and fp.uuid not in movable_uuids:
            continue
        if fp.uuid not in region_fp_uuid_to_pad_indices:
            continue

        pad_indices = region_fp_uuid_to_pad_indices[fp.uuid]
        new_x, new_y, new_angle = _compute_footprint_pose_from_pads(
            fp, positions, pad_indices, region_global_pad_idx_to_local
        )

        dx = new_x - fp.x_mm
        dy = new_y - fp.y_mm
        da = new_angle - fp.angle_deg
        da = (da + 180) % 360 - 180

        if math.hypot(dx, dy) > 1e-9 or abs(da) > 1e-9:
            deltas[fp.uuid] = (dx, dy, da)
            final_max_disp = max(final_max_disp, math.hypot(dx, dy))

        if pad_indices:
            fp_force = final_force[pad_indices].sum(axis=0)
            footprint_forces[fp.uuid] = (float(fp_force[0]), float(fp_force[1]))
            comp: Dict[str, Tuple[float, float]] = {}
            for cause, arr in final_components.items():
                s = arr[pad_indices].sum(axis=0)
                if bool(np.all(np.isfinite(s))):
                    comp[cause] = (float(s[0]), float(s[1]))
            footprint_components[fp.uuid] = comp

    if _depen_moved:
        # Positions changed geometrically after the dynamics loop stopped,
        # so the loop's residual is stale: re-read net per-footprint force
        # at the ejected pose (same rigid-aware definition as the loop).
        _res = 0.0
        for _rows, _mov in zip(fp_local_to_rows, fp_movable_list):
            if not _mov or len(_rows) == 0:
                continue
            _res = max(
                _res,
                float(np.linalg.norm(final_force[_rows].sum(axis=0))),
            )
        residual_max_force_N = _res

    if record_frames and _frame_fps:
        _final_frame = _capture_frame(positions)
        _final_force_frame = _capture_force_frame(positions)
        if not _region_frames or _region_frames[-1] != _final_frame:
            _region_frames.append(_final_frame)
            _region_force_frames.append(_final_force_frame)

    return (
        deltas, footprint_forces, iteration + 1, footprint_components,
        _region_frames, _region_force_frames, stop_reason, residual_max_force_N,
        {"per_iteration": _dbg_iters or [], "depen": _dbg_depen or []},
        _depen_moved,
    )
