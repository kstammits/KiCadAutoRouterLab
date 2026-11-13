"""Per-region force-spring simulation loop for placement.

Supports multi-board region simulation: footprints are grouped by board
outline region (from Edge.Cuts) and each region is simulated independently
with its own bounding box and timestep state.
"""

from __future__ import annotations

import math
from typing import Collection, Dict, List, Optional, Set, Tuple

import numpy as np

from ..board_model import BoardRegion, Footprint, Pad, _to_board, _to_local, is_ground_net, is_power_net_effective
from ..mass import courtyard_bbox_area_m2, footprint_mass_kg
from .contact import (
    _courtyard_outline_polygon,
    _footprint_courtyard_polygon,
    courtyard_is_degenerate,
    label_contact_force,
)
from .align import (
    ALIGN_RECOMPUTE_ITERS,
    build_align_ref_axes,
    compute_align_targets,
    compute_row_targets,
    courtyard_half_extents,
)
from .depenetrate import _depenetrate_labels, _depenetrate_positions
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
    _rigid_motion_params,
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


# Orbit tether stiffness re-exported from board_model.labels
# (single spring strategy, fixed 40 Hz — not a param, no toggles).
from ..board_model.labels import LABEL_TETHER_HZ as _LABEL_TETHER_HZ
# Orbit radius floor (mm): the positional clamp disc never collapses
# below this even at label_clearance_m == 0, so hard shoves cannot
# fling a label across the board.
_LABEL_ORBIT_RADIUS_FLOOR_MM = 1.0


def _label_orbit_radius_mm(params) -> float:
    """Orbit clamp radius (mm) derived from ``label_orbit_m``.

    Single source of truth: the radius disc equals the orbit reach,
    floored so a zero orbit still bounds the node. Collision clearance
    (``label_clearance_m``) is a separate knob and never sizes the disc.
    """
    return max(float(params.label_orbit_m) * 1000.0, _LABEL_ORBIT_RADIUS_FLOOR_MM)


def _posed_label_anchor(
    positions: np.ndarray,
    pad_rows: np.ndarray,
    orig_pos: Optional[np.ndarray],
    anchor_home: Tuple[float, float],
    fp_angle_deg: float,
) -> Tuple[Tuple[float, float], float]:
    """Live orbit home: the parent's current pad centroid + board angle.

    The anchor follows the part as moved — translation only, never
    re-posed by rotation: the label must find free space around the
    part, not swing with it. Returns (centroid board xy, parent board
    angle now); the angle still comes from the rigid fit (readable
    label orientation), while the anchor is the plain live mean.
    Degenerate pad mappings fall back to the filed home and angle.
    """
    angle = float(fp_angle_deg)
    try:
        if orig_pos is not None and len(pad_rows):
            motion = _rigid_motion_params(orig_pos, positions[pad_rows])
            if motion is not None:
                angle = float(fp_angle_deg) + math.degrees(motion[6])
    except Exception:
        pass
    try:
        if len(pad_rows):
            centroid = np.asarray(positions[pad_rows]).mean(axis=0)
            cx, cy = float(centroid[0]), float(centroid[1])
            if math.isfinite(cx) and math.isfinite(cy):
                return (cx, cy), angle
    except (TypeError, ValueError, IndexError):
        pass
    return anchor_home, angle


def _label_body_maps(fctx) -> Tuple[Dict[str, object], Dict[str, Tuple[float, float, float]]]:
    """Live body polygons + (cx, cy, radius) per footprint from the cache."""
    polys: Dict[str, object] = {}
    stats: Dict[str, Tuple[float, float, float]] = {}
    cache = getattr(fctx, "poly_cache", {}) or {}
    for uuid, entry in cache.items():
        poly = (entry or {}).get("body_poly")
        if poly is None or getattr(poly, "is_empty", True):
            continue
        polys[uuid] = poly
        try:
            c = (entry or {}).get("centroid") or (poly.centroid.x, poly.centroid.y)
            r = float((entry or {}).get("radius", 0.0) or 0.0)
            stats[uuid] = (float(c[0]), float(c[1]), r)
        except Exception:
            continue
    return polys, stats


def _orbit_label_forces(
    positions: np.ndarray,
    n_pads: int,
    lab_rows_arr: np.ndarray,
    lab_parent_li: List[int],
    lab_uuid: List[str],
    lab_text: List[str],
    lab_size: List[Tuple[float, float]],
    lab_local_angle: List[float],
    lab_anchor_home: List[Tuple[float, float]],
    lab_mass_list: List[float],
    fp_uuids: List[str],
    fp_local_to_rows: List[np.ndarray],
    fp_by_uuid: Dict[str, Footprint],
    fctx,
    params,
    iteration: int,
    lab_ref_mass_kg: float = 0.0,
) -> np.ndarray:
    """Net orbit force (newtons) per label node: slack tether + contact.

    Slack-leash tether around the live parent centroid (free float
    inside the deadband, spring beyond); contact reuses the parts
    logic: each label rect is a tiny courtyard (text bbox inflated by
    the font-fraction halo) colliding through the same wall law with
    body courtyards, the shared THT pin-extender discs, and the board
    outline as an inverted courtyard — plus pad-center Coulomb with
    the parts' k and charge mask. All new terms are one-way: labels
    feel bodies, bodies never feel labels. Always in orbit mode — no
    tether strategy toggles (spring/slot/free). Returns (L, 2), zeros
    when a label has no computable pose.

    Contact/edge/Coulomb forces scale with the label's own ink mass
    (``lab_ref_mass_kg`` is the reference pad mass, same ratio the
    damping and friction already use): at ink mass a part-scale newton
    means 1e5+ m/s^2, so unscaled walls ping-pong instead of settling.
    Scaling makes every label follow the same acceleration field as the
    reference part — the equilibrium standoff is unchanged (the tether
    is inherently mass-proportional, so mass cancels at the fixed
    point), only the bounce disappears.
    """
    from ..board_model.labels import (
        label_board_angle,
        label_board_rect,
        label_halo_mm,
        label_slack_tether_force_N,
        snap_label_angle,
    )
    from .contact import board_wall_force

    n_lab = len(lab_rows_arr)
    out = np.zeros((n_lab, 2), dtype=np.float64)
    tout = np.zeros((n_lab, 2), dtype=np.float64)
    if n_lab == 0:
        return out
    try:
        _ref = max(float(lab_ref_mass_kg), 1e-12)
    except (TypeError, ValueError):
        _ref = 1e-12
    _lab_scale = np.maximum(
        np.asarray(lab_mass_list, dtype=np.float64), 1e-12) / _ref
    cy_cache = getattr(fctx, "cy_cache", {}) or {}
    polys, stats = _label_body_maps(fctx)
    # Pre-pass: every label's clearance rect at its current node position.
    # Labels repel each other through the same wall law as bodies (their
    # own rects join each other's collision set below); body silk and
    # board notes stay out of the collision model by design.
    lab_polys: List = [None] * n_lab
    lab_cents: List[Tuple[float, float]] = [(0.0, 0.0)] * n_lab
    lab_radii: List[float] = [0.0] * n_lab
    lab_halos: List[float] = [0.0] * n_lab
    for i in range(n_lab):
        li = lab_parent_li[i]
        rows = fp_local_to_rows[li] if li < len(fp_local_to_rows) else np.array([], dtype=int)
        fp = fp_by_uuid.get(fp_uuids[li] if li < len(fp_uuids) else "")
        if fp is None or len(rows) == 0:
            continue
        entry = cy_cache.get(lab_uuid[i], {}) or {}
        orig = entry.get("orig_pos")
        anchor, parent_now = _posed_label_anchor(
            positions, np.asarray(rows), orig, lab_anchor_home[i], fp.angle_deg
        )
        board_angle = snap_label_angle(
            label_board_angle(lab_local_angle[i], parent_now),
        )
        node = positions[lab_rows_arr[i]]
        if params.label_clearance_m <= 0.0:
            continue
        # Tiny-courtyard halo: font height times the fixed fraction (not
        # label_clearance_m, which now only sizes the orbit clamp disc).
        halo_i = label_halo_mm(lab_size[i])
        lab_halos[i] = halo_i
        rect = label_board_rect(
            lab_text[i], float(node[0]), float(node[1]),
            board_angle, lab_size[i], halo_mm=halo_i,
        )
        if rect is None:
            continue
        try:
            from shapely.geometry import Polygon as _Poly
            poly = _Poly(rect)
        except Exception:
            continue
        if poly is None or poly.is_empty:
            continue
        lab_polys[i] = poly
        try:
            lab_cents[i] = (poly.centroid.x, poly.centroid.y)
            lab_radii[i] = math.hypot(
                poly.bounds[2] - poly.bounds[0],
                poly.bounds[3] - poly.bounds[1]) / 2.0
        except Exception:
            lab_cents[i] = (float(node[0]), float(node[1]))
            lab_radii[i] = 1.0
    # Pad-center Coulomb for labels (parts parity, one-way): the same k/d^2
    # law as the pad kernel, with the same charge mask and the same
    # own-footprint exemption — labels feel pads, pads never feel labels,
    # so the accumulation below only touches ``out``. This is the only SMD
    # pad protection parts have, and labels get exactly that.
    try:
        _pos = np.asarray(positions, dtype=np.float64)
        _ppos = _pos[:n_pads]
        _lpos = _pos[np.asarray(lab_rows_arr)]
        _charged = getattr(fctx, "pad_charged", None)
        _rk = float(params.repulsion_k_N_m2)
        if (
            _ppos.shape[0] > 0
            and _charged is not None
            and tuple(np.shape(_charged)) == (_ppos.shape[0],)
        ):
            _diff = _lpos[:, None, :] - _ppos[None, :, :]  # (L, P, 2), mm
            _d2 = np.maximum(np.sum(_diff * _diff, axis=2), 1e-6)
            _mag = _rk / np.maximum(_d2 * 1e-6, 1e-12)  # mm^2 -> m^2, SI
            _keep = np.broadcast_to(
                np.asarray(_charged, dtype=bool)[None, :], _mag.shape
            ).copy()
            _p2f = getattr(fctx, "pad_to_fp_local", None)
            try:
                _p2f_arr = np.asarray(_p2f)
                if _p2f_arr.shape[0] == _ppos.shape[0]:
                    _keep[_p2f_arr[None, :] == np.asarray(lab_parent_li)[:, None]] = False
            except Exception:
                pass
            _mag[~_keep] = 0.0
            with np.errstate(divide="ignore", invalid="ignore"):
                _d = np.maximum(np.sqrt(_d2)[:, :, None], 1e-9)
                _vec = _diff * (_mag[:, :, None] / _d)
            _vec[~np.isfinite(_vec)] = 0.0
            out += np.sum(_vec, axis=1)
    except Exception:
        pass
    for i in range(n_lab):
        uuid = lab_uuid[i]
        li = lab_parent_li[i]
        rows = fp_local_to_rows[li] if li < len(fp_local_to_rows) else np.array([], dtype=int)
        fp = fp_by_uuid.get(fp_uuids[li] if li < len(fp_uuids) else "")
        if fp is None or len(rows) == 0:
            continue
        entry = cy_cache.get(uuid, {}) or {}
        orig = entry.get("orig_pos")
        anchor, _ = _posed_label_anchor(
            positions, np.asarray(rows), orig, lab_anchor_home[i], fp.angle_deg
        )
        node = positions[lab_rows_arr[i]]
        # Slack-leash tether target: the live parent centroid (anchor).
        target = anchor
        # Contact through the parts wall law: the pre-pass rect is the
        # label's tiny courtyard. Other labels join the body set so labels
        # deconflict each other, not just parts; the shared THT pin discs
        # and the board outline (inverted courtyard) join below. The
        # label's own parent body is skipped (silk over its own courtyard
        # is DRC-legal, so a label may sit on its own part); its THT pin
        # copper still repels via the pin discs, like all pads.
        label_poly = lab_polys[i]
        halo_i = lab_halos[i]
        parent_uuid = fp_uuids[li] if li < len(fp_uuids) else ""
        if label_poly is not None and params.label_clearance_m > 0.0:
            try:
                lx, ly = lab_cents[i]
                lr = lab_radii[i]
            except Exception:
                lx, ly = float(node[0]), float(node[1])
                lr = 1.0
            nearby = {}
            for buid, bpoly in polys.items():
                st = stats.get(buid)
                if st is None:
                    nearby[buid] = bpoly
                    continue
                if math.hypot(lx - st[0], ly - st[1]) <= st[2] + lr + halo_i:
                    nearby[buid] = bpoly
            for j in range(n_lab):
                if j == i or lab_polys[j] is None:
                    continue
                jx, jy = lab_cents[j]
                if math.hypot(lx - jx, ly - jy) <= lab_radii[j] + lr + halo_i:
                    nearby["lab:" + lab_uuid[j]] = lab_polys[j]
            # THT pin extenders, two-level gated: only discs of bodies
            # already nearby (the parent's own body is skipped above, but
            # its pins still repel — all pads repel, including the
            # parent's own copper).
            _pins_data = getattr(fctx, "pins_data", None) or {}
            if _pins_data:
                for buid in [k for k in nearby if not k.startswith("lab:")]:
                    for k, _pin in enumerate(_pins_data.get(buid, ()) or ()):
                        try:
                            _ppoly, _pcent, _prad = _pin[0], _pin[1], float(_pin[2])
                        except Exception:
                            continue
                        if _ppoly is None or getattr(_ppoly, "is_empty", True):
                            continue
                        try:
                            _pcx, _pcy = float(_pcent[0]), float(_pcent[1])
                        except Exception:
                            continue
                        if math.hypot(lx - _pcx, ly - _pcy) <= _prad + lr + halo_i:
                            nearby["pin:%s:%d" % (buid, k)] = _ppoly
            if nearby:
                cx, cy = label_contact_force(
                    label_poly, nearby, halo_i,
                    params.wall_k_N, params.wall_exponent,
                    params.wall_cap_depth_mm,
                    solidity=1.0, tie_key="lab:" + lab_uuid[i],
                    skip_uuids=(parent_uuid,) if parent_uuid else (),
                )
                out[i, 0] += cx
                out[i, 1] += cy
            # Board outline as inverted courtyard (shared helper with the
            # part kernel): labels cannot end past EdgeCuts.
            _r2r = getattr(fctx, "fp_uuid_to_region", None) or {}
            _rpolys = getattr(fctx, "region_polygons", None) or []
            _ridx = _r2r.get(parent_uuid)
            if _ridx is not None and 0 <= _ridx < len(_rpolys):
                _rb = getattr(fctx, "region_boundaries", None) or []
                _rc = getattr(fctx, "region_centroids", None) or []
                ex, ey = board_wall_force(
                    label_poly, _rpolys[_ridx],
                    _rb[_ridx] if _ridx < len(_rb) else None,
                    _rc[_ridx] if _ridx < len(_rc) else (float(node[0]), float(node[1])),
                    params.wall_k_N, halo_i,
                    params.wall_exponent, params.wall_cap_depth_mm,
                )
                out[i, 0] += ex
                out[i, 1] += ey
        # Slack-leash tether (fixed hz; stiffness k = m*(2*pi*hz)^2).
        # Zero inside the slack radius (free float — contact owns the
        # placement), spring on the excess beyond it. Inherently
        # mass-proportional, so it accumulates separately and is NOT
        # mass-scaled at return.
        try:
            fx, fy = label_slack_tether_force_N(
                (float(node[0]), float(node[1])), target,
                lab_mass_list[i], hz=_LABEL_TETHER_HZ,
                slack_m=params.label_slack_m,
            )
        except Exception:
            fx = fy = 0.0
        tout[i, 0] += fx
        tout[i, 1] += fy
    # Mass-scale the contact/edge/Coulomb field (not the tether): every
    # label then accelerates like the reference part — same standoff,
    # no ink-mass bounce.
    return out * _lab_scale[:, None] + tout


def _posed_label_anchors(
    positions: np.ndarray,
    lab_parent_li: List[int],
    lab_anchor_home: List[Tuple[float, float]],
    fp_uuids: List[str],
    fp_local_to_rows: List[np.ndarray],
    fp_by_uuid: Dict[str, Footprint],
    fctx,
) -> np.ndarray:
    """Live centroid per orbiting label from the parent's current pads.

    The exact-carry basis: the anchor is the live pad centroid, so
    labels ride translating parents with zero lag and the tether
    governs only the orbit offset. Rotation never re-poses the home.
    Degenerate mappings fall back to the filed home (carry 0).
    """
    out = np.zeros((len(lab_parent_li), 2), dtype=np.float64)
    cy_cache = getattr(fctx, "cy_cache", {}) or {}
    for k, li in enumerate(lab_parent_li):
        rows = fp_local_to_rows[li] if li < len(fp_local_to_rows) else np.array([], dtype=int)
        uuid = fp_uuids[li] if li < len(fp_uuids) else ""
        fp = fp_by_uuid.get(uuid)
        if fp is None or len(rows) == 0:
            out[k] = lab_anchor_home[k]
            continue
        entry = cy_cache.get(uuid, {}) or {}
        anchor, _ = _posed_label_anchor(
            positions, np.asarray(rows), entry.get("orig_pos"),
            lab_anchor_home[k], fp.angle_deg,
        )
        out[k] = anchor
    return out


def _clamp_labels_to_radius(
    positions: np.ndarray,
    lab_rows_arr: np.ndarray,
    lab_parent_li: List[int],
    lab_anchor_home: List[Tuple[float, float]],
    fp_uuids: List[str],
    fp_local_to_rows: List[np.ndarray],
    fp_by_uuid: Dict[str, Footprint],
    fctx,
    params,
) -> None:
    """Positional backstop: pin every label node inside the orbit disc.

    The anchor follows the parent's current rigid motion (same helper as
    the force path), so a moved parent carries its disc along. Hard
    backstop after the drift so hard shoves can never fling a label
    across the board. In-place.
    """
    from ..board_model.labels import clamp_orbit_radius

    if len(lab_rows_arr) == 0:
        return
    radius_mm = _label_orbit_radius_mm(params)
    # Anchors from the same helper as the force path (parent-tracking, so
    # a moved parent carries its disc along). Unmapped parents fall back
    # to the filed home there; those nodes initialize exactly at it, so
    # clamping around it is a no-op and the skip below is equivalent.
    anchors = _posed_label_anchors(
        positions, lab_parent_li, lab_anchor_home, fp_uuids,
        fp_local_to_rows, fp_by_uuid, fctx,
    )
    for k, row in enumerate(lab_rows_arr):
        li = lab_parent_li[k]
        rows = fp_local_to_rows[li] if li < len(fp_local_to_rows) else np.array([], dtype=int)
        uuid = fp_uuids[li] if li < len(fp_uuids) else ""
        if fp_by_uuid.get(uuid) is None or len(rows) == 0:
            continue
        node = positions[int(row)]
        cx, cy = clamp_orbit_radius(
            (float(node[0]), float(node[1])), tuple(anchors[k]), radius_mm
        )
        positions[int(row), 0] = cx
        positions[int(row), 1] = cy
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
    freeze_bodies: bool = False,
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
    Dict[str, Tuple[float, float]],
]:
    """Run force-spring simulation for a single board region.

    With ``freeze_bodies``, pads never integrate (all footprints forced
    immovable) while labels still orbit to a local de-overlap equilibrium
    — a label-settle pass. Convergence then keys on label quiet alone
    (pad residual is trivially zero), so a settled pass reports
    ``converged`` even when the frozen board carries part overlaps.
    """
    # Extract region bounds
    min_x, max_x, min_y, max_y = region.bbox

    # Filter pads for this region
    n_pads = len(region_pads)
    if n_pads == 0:
        return {}, {}, 0, {}, [], [], "empty", 0.0, {}, False, {}

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

    if freeze_bodies:
        # Label-settle pass: bodies are anchors, not dynamics. Force every
        # footprint immovable (part kernel gates on fp_movable/pad_movable,
        # so contacts still compute but nothing integrates); the label
        # section below orbits anyway via the freeze override.
        for fp in model.footprints:
            if fp.uuid:
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

    if not pad_movable.any() and not freeze_bodies:
        return {}, {}, 0, {}, [], [], "empty", 0.0, {}, False, {}

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
        # Attractor ghosts are pure pull: their anchor square must not
        # act as a wall (it would shove the target off its own anchor —
        # the spring and the square reach equilibrium ~1mm apart). Only
        # wall ghosts (no attractions) join the body set. Attraction
        # edges are built separately below from model.ghost_footprints.
        if getattr(fp, "ghost_attractions", ()):
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
    # Sampled label trajectory: per stride-sampled iteration, the board
    # node + parent-carried anchor per orbiting label. Raw points only
    # (offline analysis rebuilds rects/overlaps) so the loop pays no
    # geometry. Populated only when debug AND record_frames are both on
    # — production (debug off) pays a single `is not None` gate.
    _lab_samples: Optional[List[dict]] = [] if _dbg_enabled else None

    def _local_shape(_fp: Optional[Footprint]) -> dict:
        if _fp is None:
            return {"body": {"kind": "empty", "empty": True}, "pins": [],
                    "pad_idx": np.array([], dtype=int),
                    "orig_pos": np.array([], dtype=np.float64),
                    "label": None}
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
        # Label rect in filed board coordinates (bare text bbox; the orbit
        # forces inflate it by label_clearance_m at contact time).
        _label = None
        if _fp.label is not None:
            from ..board_model import label_board_pose
            from ..board_model.labels import label_board_rect

            _pose = label_board_pose(_fp)
            if _pose is not None:
                _corners = label_board_rect(
                    _fp.label.text, _pose[0], _pose[1], _pose[2],
                    _fp.label.size_mm, halo_mm=0.0,
                )
                if _corners is not None:
                    _label = {"corners": _corners}
        return {"body": body, "pins": pins, "pad_idx": _pad_idx, "orig_pos": _orig_pos,
                "label": _label}

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

    # Manhattan-snap + row-hold regime (Tidy): reference axes from the
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
    _part_extents = (
        {
            li: courtyard_half_extents(fp_by_uuid[uuid])
            for li, uuid in enumerate(fp_uuids)
            if uuid in fp_by_uuid
        }
        if align_active else {}
    )

    def _refresh_align_targets(
        prev: Optional[dict] = None,
        prev_rows: Optional[dict] = None,
    ) -> tuple:
        if not align_active:
            return {}, {}
        targets = compute_align_targets(
            positions, fp_local_to_rows, fp_movable_arr,
            _fp_layers, _ghost_local, _committed_angles, _align_ref_axes,
            radius_mm=params.align_radius_m * 1000.0,
            deadband_deg=params.align_deadband_deg,
            prev_targets=prev,
        )
        row_targets: dict = {}
        if params.align_pos_k_N_m > 0.0:
            # Row hold overrides the leash anchor (not the angle): the same
            # weak spring pulls toward the frozen row frame instead of the
            # last recompute pose. Parts outside any row keep the leash.
            row_targets = compute_row_targets(
                positions, fp_local_to_rows, fp_movable_arr,
                _fp_layers, _ghost_local, _committed_angles,
                _align_ref_axes, _part_extents,
                radius_mm=params.align_radius_m * 1000.0,
                deadband_deg=params.align_deadband_deg,
                prev_targets=prev_rows,
            )
            for li, (tx, ty) in row_targets.items():
                if li in targets:
                    angle, _ax, _ay = targets[li]
                    targets[li] = (angle, tx, ty)
                else:
                    targets[li] = (None, tx, ty)
        return targets, row_targets

    _align_targets, _row_targets = _refresh_align_targets()

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

    # ---- Orbit-mode label nodes (always on): each label is one extra
    # integrated node in the tail rows of positions/_vel (indices >= n_pads)
    # with its derived ink mass. The pad-indexed force kernel never sees
    # them (called on [:n_pads] slices); label tether + contact forces are
    # computed separately in the loop below. Only movable, in-region,
    # labeled real footprints orbit — locked parents keep welded labels.
    # Home is the parent's live pad centroid (translation only — the
    # part's rotation never re-poses it), and the slack tether leaves
    # the label free inside the deadband so contact, not the leash,
    # decides the rest spot. The disc, eject, and find-spare-room
    # search all center on the live home.
    # Clearance 0 welds labels at filed spots: no rows, so the whole
    # label path below (forces, eject, commit) is a no-op.

    from ..board_model import label_board_pose
    from ..board_model.labels import label_ink_mass_kg

    lab_rows: List[int] = []
    lab_parent_li: List[int] = []
    lab_uuid: List[str] = []
    lab_text: List[str] = []
    lab_size: List[Tuple[float, float]] = []
    lab_local_angle: List[float] = []
    lab_home_local: List[Tuple[float, float]] = []
    lab_anchor_home: List[Tuple[float, float]] = []
    lab_mass_list: List[float] = []
    _lab_vel = np.zeros((0, 2), dtype=np.float64)
    _lab_damp_c = np.zeros(0, dtype=np.float64)
    _lab_fric_N = np.zeros(0, dtype=np.float64)
    lab_anchor_prev: Optional[np.ndarray] = None
    _labels_live = params.label_clearance_m > 0.0
    for _fp in model.footprints:
        if not _labels_live:
            break  # welded: filed spots stand, no rows built below
        if _fp.label is None or not _fp.uuid:
            continue
        if not freeze_bodies and not fp_movable.get(_fp.uuid, False):
            continue
        if model.footprint_region.get(_fp.uuid) != region_idx:
            continue
        if _fp.uuid not in region_fp_uuid_to_pad_indices:
            continue
        if movable_uuids is not None and _fp.uuid not in movable_uuids:
            continue
        _pose = label_board_pose(_fp)
        if _pose is None:
            continue
        _li = fp_uuid_to_local_idx.get(_fp.uuid)
        if _li is None:
            continue
        # Orbit home: the parent's pad centroid in rigid-invariant
        # parent-local coords. Translation-following only — the part's
        # rotation never swings the home (the label must find free space
        # around the part, not re-pose with it). The slack tether leaves
        # the label free inside the deadband; contact owns the placement.
        try:
            _lrows = fp_local_to_rows[_li] if _li < len(fp_local_to_rows) else []
            _centroid = np.asarray(positions[_lrows]).mean(axis=0)
            _cx, _cy = float(_centroid[0]), float(_centroid[1])
            if not (math.isfinite(_cx) and math.isfinite(_cy)):
                raise ValueError("non-finite parent centroid")
            _home_local = _to_local(
                _cx, _cy,
                float(_fp.x_mm), float(_fp.y_mm),
                float(_fp.angle_deg), _fp.layer)
        except (TypeError, ValueError, IndexError):
            continue
        lab_rows.append(n_pads + len(lab_rows))
        lab_parent_li.append(_li)
        lab_uuid.append(_fp.uuid)
        lab_text.append(_fp.label.text)
        lab_size.append(_fp.label.size_mm)
        lab_local_angle.append(_fp.label.local_angle_deg)
        lab_home_local.append(
            (float(_home_local.x_mm), float(_home_local.y_mm))
        )
        lab_anchor_home.append((_cx, _cy))
        lab_mass_list.append(
            label_ink_mass_kg(
                _fp.label.text, _fp.label.size_mm,
                floor_kg=params.mass_floor_kg,
            )
        )

    if lab_rows:
        _lab_init = np.array(lab_anchor_home, dtype=np.float64)
        positions = np.vstack([positions, _lab_init])
        prev_positions = positions.copy()
        _vel = np.vstack([_vel, np.zeros((len(lab_rows), 2))])
        pad_mass_kg = np.concatenate([
            pad_mass_kg,
            np.maximum(
                np.array(lab_mass_list, dtype=np.float64),
                _fallback_mass_kg,
            ),
        ])
        lab_anchor_prev = _lab_init.copy()
        _lab_vel = np.zeros((len(lab_rows), 2), dtype=np.float64)
        # Mass-consistent damping/friction: the shared viscous decay
        # 1/(1+c*dt/m) and the friction deadband assume part-scale
        # masses — at ink mass they would pin labels absolutely
        # (decay ~1e-5/step, tether forces far below friction_N).
        # Scale both by m_label/m_ref (heaviest pad) so labels share
        # the parts' decay timescale and freeze threshold relatively.
        # Contact/edge/Coulomb forces get the same ratio inside
        # _orbit_label_forces (same acceleration field as parts, no
        # ink-mass bounce); the tether is inherently mass-proportional.
        _ref_mass = max(float(np.max(pad_mass_kg[:n_pads])), 1e-12)
        _lab_scale = np.maximum(
            np.array(lab_mass_list, dtype=np.float64),
            _fallback_mass_kg,
        ) / _ref_mass
        _lab_damp_c = np.full(len(lab_rows), params.damping_Ns_m) * _lab_scale
        _lab_fric_N = params.friction_N * _lab_scale
    lab_rows_arr = np.array(lab_rows, dtype=int)
    lab_movable = np.ones(len(lab_rows), dtype=bool)
    all_movable = np.concatenate([pad_movable, lab_movable])

    # Initial forces predict the NEXT step from the committed board positions.
    # (Tail label rows are invisible to the pad-indexed kernel.)
    initial_force, initial_components = _compute_total_force(
        positions[:n_pads], len(region_pads), pad_movable,
        params, has_net_edges, src_idx, dst_idx, edge_weights,
        ghost_attractions,
        same_fp_mask,
        edge_sides=edge_sides,
        return_components=True,
        ctx=fctx,
        pad_velocities=_vel[:n_pads],
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

    # Label slide lookup: uuid -> index into the lab_* tail-row lists.
    # Built once; empty when labels are welded (clearance 0) or absent.
    _lab_idx_by_uuid: Dict[str, int] = {
        _u: _k for _k, _u in enumerate(lab_uuid)
    }

    def _capture_frame(pos: np.ndarray) -> Dict[str, Tuple[float, ...]]:
        frame: Dict[str, Tuple[float, ...]] = {}
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
            # Label slide around its home point (board mm): label node
            # minus the parent-carried anchor. Zero when the part has no
            # orbiting label row. Same stride/merge/thin as the part move —
            # no second frame list.
            _ldx, _ldy = 0.0, 0.0
            try:
                _lk = _lab_idx_by_uuid.get(_fp.uuid)
                if _lk is not None:
                    _lrow = lab_rows[_lk]
                    _anchor, _ = _posed_label_anchor(
                        pos, np.asarray(_pi),
                        (cy_cache.get(_fp.uuid, {}) or {}).get("orig_pos"),
                        lab_anchor_home[_lk], _fp.angle_deg,
                    )
                    _node = pos[_lrow]
                    _ldx = float(_node[0]) - float(_anchor[0])
                    _ldy = float(_node[1]) - float(_anchor[1])
                    if not (math.isfinite(_ldx) and math.isfinite(_ldy)):
                        _ldx, _ldy = 0.0, 0.0
            except Exception:
                _ldx, _ldy = 0.0, 0.0
            frame[_fp.uuid] = (
                round(float(_dx), FRAME_XY_DECIMALS),
                round(float(_dy), FRAME_XY_DECIMALS),
                round(float(_da), FRAME_ANGLE_DECIMALS),
                round(float(_ldx), FRAME_XY_DECIMALS),
                round(float(_ldy), FRAME_XY_DECIMALS),
            )
        return frame

    def _capture_force_frame(pos: np.ndarray) -> Dict[str, Dict[str, Tuple[float, float]]]:
        force, components = _compute_total_force(
            pos[:n_pads], len(region_pads), pad_movable,
            params, has_net_edges, src_idx, dst_idx, edge_weights,
            ghost_attractions,
            same_fp_mask,
            edge_sides=edge_sides,
            return_components=True,
            ctx=fctx,
            pad_velocities=_vel[:n_pads],
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

    def _capture_label_sample(pos: np.ndarray, iteration: int) -> dict:
        """Stride-sampled label node + anchor snapshot for offline analysis.

        One entry per orbiting label uuid: ``(node_x, node_y, anchor_x,
        anchor_y, parent_angle_deg)`` in board mm/degrees, rounded like
        motion frames. The anchor is the parent-carried orbit home and the
        angle the parent's current board angle (same helpers as the force
        path), so offline code can derive home_dist, rim fraction, and —
        with the filed board — exact rigid-motion posing of copper for
        rect overlaps without re-fitting. Returns the sample dict
        (empty labels when no labels orbit).
        """
        _sample: Dict[str, list] = {}
        _cy = getattr(fctx, "cy_cache", {}) or {}
        for _k, _uuid in enumerate(lab_uuid):
            try:
                _li = lab_parent_li[_k]
                _rows = fp_local_to_rows[_li] \
                    if _li < len(fp_local_to_rows) else np.array([], dtype=int)
                _fp = fp_by_uuid.get(fp_uuids[_li] if _li < len(fp_uuids) else "")
                if _fp is None or len(_rows) == 0:
                    continue
                _entry = _cy.get(_uuid, {}) or {}
                _anchor, _parent_now = _posed_label_anchor(
                    pos, np.asarray(_rows), _entry.get("orig_pos"),
                    lab_anchor_home[_k], _fp.angle_deg,
                )
                _node = positions[lab_rows[_k]]
                _sample[_uuid] = [
                    round(float(_node[0]), FRAME_XY_DECIMALS),
                    round(float(_node[1]), FRAME_XY_DECIMALS),
                    round(float(_anchor[0]), FRAME_XY_DECIMALS),
                    round(float(_anchor[1]), FRAME_XY_DECIMALS),
                    round(float(_parent_now), FRAME_ANGLE_DECIMALS),
                ]
            except Exception:
                continue
        return {"iteration": iteration, "region": region_idx,
                "labels": _sample}

    _region_frames: List[Dict[str, Tuple[float, ...]]] = []
    _region_force_frames: List[Dict[str, Dict[str, Tuple[float, float]]]] = []
    if record_frames and _frame_fps:
        _region_frames.append(_capture_frame(positions))
        _region_force_frames.append(_capture_force_frame(positions))
        if _lab_samples is not None:
            _lab_samples.append(_capture_label_sample(positions, 0))

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
    stall_mean_ref = positions[all_movable].copy()
    stall_acc = np.zeros_like(stall_mean_ref)
    stall_n = 0

    for iteration in range(params.max_iterations):
        # Consensus refresh: quasi-static targets that track drift.
        if align_active and iteration > 0 and iteration % ALIGN_RECOMPUTE_ITERS == 0:
            _align_targets, _row_targets = _refresh_align_targets(
                _align_targets, _row_targets)
        # Update context with current velocities for tangential assist
        # (pad-indexed kernel: tail label rows stay invisible).
        fctx.pad_velocities = _vel[:n_pads]
        force, _ = _compute_total_force(
            positions[:n_pads], len(region_pads), pad_movable,
            params, has_net_edges, src_idx, dst_idx, edge_weights,
            ghost_attractions,
            same_fp_mask,
            edge_sides=edge_sides,
            ctx=fctx,
            pad_velocities=_vel[:n_pads],
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

        # Rigid-aware net force per footprint (pads summed, not per-pad
        # max). Pads of one rigid body routinely carry large canceling
        # contact loads (wall on the contact pads vs attraction spread
        # over all pads); per-pad max then never settles even at a true
        # rigid equilibrium and every contact rest runs to budget. Net
        # force vanishes exactly when the body has nothing left to obey.
        # (Persistent torque still moves pads — the freeze below checks
        # rim-equivalent torque too, and the displacement gate covers
        # rotation.)
        _fp_nets: List[float] = []
        _fp_rim: List[float] = []
        for _rows, _mov in zip(fp_local_to_rows, fp_movable_list):
            if len(_rows) == 0:
                _fp_nets.append(0.0)
                _fp_rim.append(0.0)
                continue
            _f = force[_rows]
            _net = float(np.linalg.norm(_f.sum(axis=0)))
            _fp_nets.append(_net)
            _rel = positions[_rows] - positions[_rows].mean(axis=0)
            _lever = float(np.max(np.linalg.norm(_rel, axis=1)))
            if _lever > 0:
                _torque = abs(float((_rel[:, 0] * _f[:, 1]
                                     - _rel[:, 1] * _f[:, 0]).sum()))
                _fp_rim.append(_torque / _lever)
            else:
                _fp_rim.append(0.0)

        # Dry friction: freeze a whole footprint whose net force AND
        # rim-equivalent torque sit below the freeze bar — a body
        # pressed into a wall (big canceling per-pad loads, ~zero net)
        # rests instead of chattering. The bar is 20*friction_N: the
        # ghost/wall legs of force_settle_N stay out, so a part still
        # under real load (wall shove, ghost tension) never freezes.
        # friction_N=0 disables (bar 0, net is never below it).
        # Re-evaluated from live forces every step, so a shoved part
        # unfreezes on its own. Frozen bodies also lose velocity.
        _freeze_bar_N = 20.0 * params.friction_N
        _frozen = np.zeros(len(region_pads), dtype=bool)
        for _rows, _net, _rim in zip(fp_local_to_rows, _fp_nets, _fp_rim):
            if (len(_rows) and _net < _freeze_bar_N
                    and _rim < _freeze_bar_N):
                force[_rows] = 0.0
                _frozen[_rows] = True
        force[~pad_movable] = 0.0
        _frozen[~pad_movable] = True

        # ---- Orbit-mode label state: exact carry + relative orbit velocity.
        # Labels ride their parent's anchor displacement exactly (zero
        # lag by construction); only the orbit OFFSET integrates forces.
        # _lab_vel is the offset velocity (labels never join the shared
        # momentum blend — offset dynamics stay overdamped and settle).
        _lab_frozen = np.zeros(len(lab_rows), dtype=bool)
        # (Forces integrate after the carry below, at carried positions.)

        if pad_movable.any():
            # Residual for the convergence gate: max net force over movable
            # footprints, computed above (post-freeze, so frozen bodies
            # read exactly zero).
            _res = 0.0
            for _net, _mov in zip(_fp_nets, fp_movable_list):
                if not _mov:
                    continue
                _res = max(_res, _net)
            residual_max_force_N = _res
        else:
            residual_max_force_N = 0.0

        # Semi-implicit Euler for pads: v += (F/m) dt, with implicit-Euler
        # viscous decay v *= 1/(1 + c*dt/m), then a speed ceiling. Pads
        # only — labels integrate their offset velocity separately below,
        # after the exact carry (carrying by velocity would double-count
        # the anchor motion: once via _vel drift, once via the carry).
        _pvel = _vel[:n_pads]
        _accel = force / pad_mass_kg[:n_pads, None]
        _pvel[pad_movable] += _accel[pad_movable] * _dt
        _pdecay = 1.0 / (1.0 + params.damping_Ns_m * _dt / pad_mass_kg[:n_pads])
        _pvel *= _pdecay[:, None]
        _pvel[_frozen] = 0.0
        _speed = np.linalg.norm(_pvel, axis=1, keepdims=True)
        _vcap = params.vmax_m_s / np.maximum(_speed, 1e-12)
        np.minimum(_vcap, 1.0, out=_vcap)
        _pvel *= _vcap
        _pvel[~pad_movable] = 0.0

        # Drift in meters, applied to the millimeter-native positions.
        _old_positions = positions.copy()
        positions[:n_pads][pad_movable] += _pvel[pad_movable] * _dt * 1000.0
        positions = np.nan_to_num(positions, nan=0.0, posinf=0.0, neginf=0.0)
        if lab_rows:
            _clamp_labels_to_radius(
                positions, lab_rows_arr, lab_parent_li,
                lab_anchor_home, fp_uuids, fp_local_to_rows, fp_by_uuid,
                fctx, params,
            )

        # Rigid projection + rigid-step clamp (pads only; label rows are
        # carried exactly below instead of projected).
        _project_rigid_poses(positions, _proj_bodies)
        _clamp_rigid_step(
            positions, _old_positions, _proj_bodies, pad_movable,
            cap_mm=params.vmax_m_s * _dt * 1000.0,
            max_rot_rad=math.radians(10.0),
        )
        # Exact carry: the posed anchor moved with the parent's
        # projection, so translate each label node by the anchor's
        # own displacement. The tether then governs only the orbit
        # offset — labels can never lag behind a moving parent.
        if lab_rows:
            _anchor_now = _posed_label_anchors(
                positions, lab_parent_li, lab_anchor_home,
                fp_uuids, fp_local_to_rows, fp_by_uuid, fctx,
            )
            positions[lab_rows_arr] += _anchor_now - lab_anchor_prev
            lab_anchor_prev = _anchor_now
            # Orbit forces at the carried positions: tether spring +
            # label-vs-body contact, integrated into the offset velocity
            # with mass-consistent decay (same timescale as parts) and
            # the shared speed ceiling.
            _lab_force = _orbit_label_forces(
                positions, n_pads, lab_rows_arr,
                lab_parent_li, lab_uuid, lab_text, lab_size,
                lab_local_angle, lab_anchor_home, lab_mass_list,
                fp_uuids, fp_local_to_rows, fp_by_uuid,
                fctx, params, iteration,
                lab_ref_mass_kg=_ref_mass,
            )
            _lab_frozen = np.zeros(len(lab_rows), dtype=bool)
            if params.friction_N > 0:
                _lab_frozen = (
                    np.linalg.norm(_lab_force, axis=1) < _lab_fric_N
                )
                _lab_force[_lab_frozen] = 0.0
            _lab_accel = _lab_force / np.maximum(
                np.array(lab_mass_list, dtype=np.float64), 1e-12
            )[:, None]
            _lab_vel += _lab_accel * _dt
            _lab_vel *= (1.0 / (1.0 + _lab_damp_c * _dt / np.maximum(
                np.array(lab_mass_list, dtype=np.float64), 1e-12
            )))[:, None]
            _lab_vel[_lab_frozen] = 0.0
            _lab_speed = np.linalg.norm(_lab_vel, axis=1, keepdims=True)
            _lab_vcap = params.vmax_m_s / np.maximum(_lab_speed, 1e-12)
            np.minimum(_lab_vcap, 1.0, out=_lab_vcap)
            _lab_vel *= _lab_vcap
            positions[lab_rows_arr] += _lab_vel * _dt * 1000.0
            positions = np.nan_to_num(positions, nan=0.0, posinf=0.0, neginf=0.0)
            _clamp_labels_to_radius(
                positions, lab_rows_arr, lab_parent_li,
                lab_anchor_home, fp_uuids, fp_local_to_rows, fp_by_uuid,
                fctx, params,
            )
        # Re-derive velocity from the actual projected displacement so the
        # projection can't inject phantom energy into the guard below.
        # Blend with old velocity (momentum_alpha) for momentum persistence.
        # Labels are diagnostic here (their dynamics live in _lab_vel):
        # total displacement velocity, no momentum blend, so the offset
        # dynamics stay overdamped and settle onto the tether.
        if _dt > 0:
            vel_from_disp = (
                (positions[:n_pads] - _old_positions[:n_pads]) * 1e-3 / _dt)
            alpha = float(params.momentum_alpha)
            _pvel[pad_movable] = alpha * _pvel[pad_movable] + (1.0 - alpha) * vel_from_disp[pad_movable]
            _vel[lab_rows_arr] = (
                (positions[lab_rows_arr] - _old_positions[lab_rows_arr]) * 1e-3 / _dt)
        _vel[:n_pads][_frozen] = 0.0
        _vel[~all_movable] = 0.0

        # Adaptive-dt energy guard: bleed and slow down on significant
        # kinetic-energy jumps, regrow otherwise. Orbit labels join the
        # budget through all_movable (their ink mass is tiny, so only a
        # true instability trips the guard).
        _ke = 0.5 * float(np.sum(
            pad_mass_kg[all_movable, None]
            * _vel[all_movable] * _vel[all_movable]))
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
            if _lab_samples is not None:
                _lab_samples.append(
                    _capture_label_sample(positions, iteration + 1))

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
            stall_acc += positions[all_movable]
            stall_n += 1
            if stall_n >= params.stall_window:
                stall_mean = stall_acc / stall_n
                if stall_mean.shape[0]:
                    stall_shift_m = float(np.max(np.linalg.norm(
                        stall_mean - stall_mean_ref, axis=1))) * 1e-3
                else:
                    stall_shift_m = 0.0
                if stall_shift_m < STALL_MEAN_EPS_MULT * params.convergence_eps_m:
                    # A dead window mean with quiet residual is a settle,
                    # not a wedge: limit-cycle chatter averages itself out
                    # while real tension would keep forces up. Report the
                    # honest reason — downstream callers treat stalled
                    # like budget, never like converged.
                    if (stall_shift_m < params.convergence_eps_m
                            and residual_max_force_N < force_settle_N):
                        stop_reason = "converged"
                    else:
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
        positions[:n_pads], len(region_pads), pad_movable,
        params, has_net_edges, src_idx, dst_idx, edge_weights,
        ghost_attractions,
        same_fp_mask,
        edge_sides=edge_sides,
        return_components=True,
        ctx=fctx,
        pad_velocities=_vel[:n_pads],
        align_targets=_align_targets,
        align_ref_axes=_align_ref_axes,
    )

    # (The final exact re-seat at the Kabsch-fitted parent pose happens
    # in label extraction below, covering de-penetration ejects too.)
    label_deltas: Dict[str, Tuple[float, float]] = {}
    new_poses: Dict[str, Tuple[float, float, float]] = {}

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
        new_poses[fp.uuid] = (new_x, new_y, new_angle)

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

    # Label de-penetration: the same geometric eject the parts pass gives
    # wedged courtyards, applied to the labels' tiny-courtyard bodies
    # (text bbox plus the shared halo — the exact bodies the wall law
    # separates). Walls-off configurations skip it exactly like the parts
    # pass, and zero orbit clearance welds labels exactly like the orbit
    # forces; bodies are static (one-way invariant holds here too).
    if (lab_rows and params.max_iterations > 0 and params.wall_k_N > 0.0
            and params.wall_halo_m > 0.0 and params.label_clearance_m > 0.0):
        from dataclasses import replace as _replace
        from shapely.geometry import Polygon as _ShapelyPolygon
        from ..board_model import Point as _Point
        from ..board_model.labels import label_board_angle as _lba
        from ..board_model.labels import label_halo_mm as _lhalo
        from ..board_model.labels import snap_label_angle as _sla
        from .contact import pad_copper_polygon as _pad_cu
        from .contact import pad_present_on_layer as _pad_on_layer
        from .depenetrate import _body_poly_map as _bodymap
        from .depenetrate import _depenetrate_labels as _depen_labels
        from .depenetrate import _find_spot as _findspot
        from .depenetrate import _label_rect_poly as _labrect
        from .depenetrate import _nearby_silk_obstacles as _silknear
        from .depenetrate import _pose_parent_silk as _posesilk
        try:
            _edge_poly = _ShapelyPolygon(
                [(p.x_mm, p.y_mm) for p in region.polygon]
            ) if region.polygon else None
            if _edge_poly is not None and _edge_poly.is_empty:
                _edge_poly = None
        except Exception:
            _edge_poly = None
        _cu_layers = tuple(getattr(model, "copper_layers", ()) or ())
        # Same visibility the orbit walls feel: any pad with copper on
        # either side (THT spans sides, so it is covered once), all bodies
        # regardless of layer — the depenetrate finishes what the shared
        # wall law equilibrated on, with no label-only obstacle set.
        _copper_all: List = []
        for _i in range(n_pads):
            try:
                _li = region_pad_to_fp_idx[_i]
                if _li < 0:
                    continue
                _fp = model.footprints[_li]
                if not _fp.uuid:
                    continue
                _pose = new_poses.get(_fp.uuid)
                _da = (_pose[2] - _fp.angle_deg) if _pose is not None else 0.0
                _pad = region_pads[_i]
                _posed = _replace(
                    _pad,
                    position=_Point(float(positions[_i, 0]), float(positions[_i, 1])),
                    angle_deg=_pad.angle_deg + _da,
                )
                if (_pad_on_layer(_posed, _fp.layer, "F.Cu", _cu_layers)
                        or _pad_on_layer(_posed, _fp.layer, "B.Cu", _cu_layers)):
                    _poly = _pad_cu(_posed)
                    if _poly is not None and not _poly.is_empty:
                        _copper_all.append(_poly)
            except Exception:
                continue
        _lab_spec_rows: List[int] = []
        _lab_specs: List = []
        # Anchor hygiene: the filed-carried anchor can sit in ink/copper
        # (filed dirty, or ridden there by the parent), and the tether
        # then holds the label there all run. Nudge dirty anchors to the
        # nearest clean in-disc spot via the shared clean-spot search
        # before depenetrating, so tether, clamp, eject, and search share
        # one sane center. Obstacles are posed at final positions (bodies
        # + copper already built above; silk posed per parent). Best
        # effort: any failure keeps the filed-carried anchor.
        _hy_radius = _label_orbit_radius_mm(params)
        try:
            _hy_bodies = _bodymap(
                positions, fp_uuids, fp_local_to_rows, fp_by_uuid)
            _hy_uuid_to_li = {u: i for i, u in enumerate(fp_uuids)}
            _hy_silk: Dict[str, list] = {}
            for _u in fp_uuids:
                _fp = fp_by_uuid.get(_u)
                if _fp is None or not _fp.silk:
                    _hy_silk[_u] = []
                    continue
                _li = _hy_uuid_to_li.get(_u)
                _rows = fp_local_to_rows[_li] if _li is not None else None
                _hy_silk[_u], _ = _posesilk(_fp, positions, _rows)
        except Exception:
            _hy_bodies, _hy_silk = {}, {}
        for _k, _uuid in enumerate(lab_uuid):
            try:
                _pose = new_poses.get(_uuid)
                _lfp = fp_by_uuid.get(_uuid)
                if _pose is None or _lfp is None:
                    continue
                _fx0, _fy0 = lab_home_local[_k]
                _anchor_b = _to_board(
                    _fx0, _fy0, _pose[0], _pose[1], _pose[2], _lfp.layer)
                _anchor = (float(_anchor_b.x_mm), float(_anchor_b.y_mm))
                _ang = _sla(_lba(lab_local_angle[_k], _pose[2]))
                if params.label_clearance_m > 0.0:
                    try:
                        _rect0 = _labrect(
                            lab_text[_k], _anchor, _ang, lab_size[_k],
                            _lhalo(lab_size[_k]))
                        _obs: List = [
                            _o for _bu, _o in _hy_bodies.items()
                            if _bu != _uuid
                        ]
                        _obs.extend(_copper_all)
                        try:
                            _minx, _miny, _maxx, _maxy = _rect0.bounds
                            _hd = 0.5 * math.hypot(_maxx - _minx, _maxy - _miny)
                        except Exception:
                            _hd = 2.0
                        _obs.extend(_silknear(
                            _anchor, _hy_radius + _hd + 0.5, _hy_silk))
                        _first, _best, _best_area, _cur = _findspot(
                            _rect0, _obs, _anchor, _hy_radius,
                            cover_poly=_edge_poly)
                        _spot = _first
                        if _spot is None and _best is not None \
                                and _best_area < _cur - 1e-9:
                            _spot = _best
                        if _spot is not None and (
                                abs(_spot[0] - _anchor[0]) > 1e-9
                                or abs(_spot[1] - _anchor[1]) > 1e-9):
                            _anchor = (float(_spot[0]), float(_spot[1]))
                    except Exception:
                        pass
                _lab_spec_rows.append(lab_rows[_k])
                _lab_specs.append((
                    _uuid, lab_text[_k], lab_size[_k], _ang, _anchor, _uuid,
                ))
            except (TypeError, ValueError, IndexError):
                continue
        if _lab_specs:
            try:
                if _depen_labels(
                    positions, _lab_spec_rows, _lab_specs,
                    fp_uuids, fp_local_to_rows, fp_by_uuid,
                    _copper_all,
                    _edge_poly, _label_orbit_radius_mm(params),
                ):
                    _vel[:] = 0.0
            except Exception:
                pass

    # Orbit-mode label offsets: final node board position expressed in the
    # parent's FINAL local frame, minus the filed home local (the stage
    # input's frame of reference). Empty when no label moved (follow
    # behavior, zero sidecar payload). Nodes are
    # re-seated exactly at the Kabsch-fitted parent pose first: the loop
    # clamp used the incremental rigid motion, whose angle estimate can
    # differ by microns from the final fit — without this the sidecar
    # could exceed the radius cap by that sliver.
    from ..board_model.labels import clamp_orbit_radius as _clamp_r

    radius_mm = _label_orbit_radius_mm(params)
    for k, uuid in enumerate(lab_uuid):
        pose = new_poses.get(uuid)
        fp = fp_by_uuid.get(uuid)
        if pose is None or fp is None:
            continue
        fx0, fy0 = lab_home_local[k]
        try:
            anchor_b = _to_board(
                fx0, fy0, pose[0], pose[1], pose[2], fp.layer
            )
        except (TypeError, ValueError, IndexError):
            continue
        node = positions[lab_rows[k]]
        cx, cy = _clamp_r(
            (float(node[0]), float(node[1])),
            (anchor_b.x_mm, anchor_b.y_mm), radius_mm,
        )
        positions[lab_rows[k], 0] = cx
        positions[lab_rows[k], 1] = cy
        try:
            local = _to_local(cx, cy, pose[0], pose[1], pose[2], fp.layer)
        except (TypeError, ValueError, IndexError):
            continue
        # Sidecar delta is measured against the FILED local (the file's
        # frame of reference), not the orbit home: lab_home_local carries
        # the parent centroid, while the file holds the original offset.
        try:
            ffx, ffy = float(fp.label.local_x_mm), float(fp.label.local_y_mm)
        except (TypeError, ValueError, AttributeError):
            continue
        if math.hypot(local.x_mm - ffx, local.y_mm - ffy) > 1e-9:
            label_deltas[uuid] = (local.x_mm - ffx, local.y_mm - ffy)

    if record_frames and _frame_fps:
        _final_frame = _capture_frame(positions)
        _final_force_frame = _capture_force_frame(positions)
        if not _region_frames or _region_frames[-1] != _final_frame:
            _region_frames.append(_final_frame)
            _region_force_frames.append(_final_force_frame)
        if _lab_samples is not None:
            _lab_samples.append(
                _capture_label_sample(positions, iteration + 1))

    return (
        deltas, footprint_forces, iteration + 1, footprint_components,
        _region_frames, _region_force_frames, stop_reason, residual_max_force_N,
        {"per_iteration": _dbg_iters or [], "depen": _dbg_depen or [],
         "label_samples": _lab_samples or []},
        _depen_moved, label_deltas,
    )
