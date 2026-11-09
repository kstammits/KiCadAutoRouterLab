"""Final de-penetration pass (legalize eject) for placement.

Positional projection applied once after dynamics settle. The penalty wall law
equilibrates against net attraction at nonzero penetration, so wedged pairs keep
residual overlap; this pass ejects them geometrically. Fractional relaxation per
pass with a pass cap; corrections are distributed contact-weighted so the Kabsch
rigid projection derives rotation too (full motion, not translation-only).
Rotation is clamped per pass (translation is the eject and stays uncapped):
unclamped, repeated differential pushes spin small parts tens of degrees
into their neighbors instead of settling. The loop also stops when the
worst overlap stops shrinking (ping-pong between several neighbors),
accepting the remainder for the next legalize run.

Solver-internal constants, deliberately not PlacementParams sliders.
"""

from __future__ import annotations

import logging
import math
from typing import Dict, List, Optional, Tuple

import numpy as np

from ..board_model import Footprint
from ..board_model.labels import (
    clamp_orbit_radius,
    label_board_rect,
    label_halo_mm,
)
from .contact import (
    _contact_weights,
    _footprint_courtyard_polygon,
)
from .force import _overlap_depth_mm
from .rigid import _clamp_rigid_step, _project_rigid_poses

logger = logging.getLogger(__name__)

_DEPEN_RELAX = 0.5
_DEPEN_CLEAN_AREA_MM2 = 1e-6
_DEPEN_MAX_ROT_RAD = math.radians(2.0)
# Label rounds: single nodes cannot spin, so unlike the parts pass the
# rounds iterate toward clean instead of stopping after one Jacobi
# round. Relax stays shared, so big penetrations still need ~25 rounds
# to converge geometrically; the cap only bounds obstacle fights.
_DEPEN_LABEL_MAX_ROUNDS = 30


def _depenetrate_positions(
    positions: np.ndarray,
    pad_movable: np.ndarray,
    fp_uuids: List[str],
    fp_movable_list: List[bool],
    fp_local_to_rows: List[np.ndarray],
    fp_by_uuid: Dict[str, Footprint],
    proj_bodies: List[Tuple[np.ndarray, np.ndarray]],
    debug_sink: Optional[List[dict]] = None,
) -> bool:
    """Eject residual courtyard overlaps left by the force dynamics.

    For each intersecting same-layer body pair (including movable-vs-ghost
    courtyard walls), translates bodies apart along the MTV by
    ``_DEPEN_RELAX`` of the penetration depth (locked / ghost sides never
    move; the movable side takes the full correction, split evenly when
    both are movable). Only true penetration (``s < 0``) is projected,
    never halo proximity. The board outline itself is deliberately out of
    scope: the wall force owns it in production, and geometric wall pulls
    would corrupt walls-off isolation tests. Returns True when anything
    moved.
    """
    from shapely.ops import nearest_points as _nearest_points

    def _posed():
        polys = _body_poly_map(positions, fp_uuids, fp_local_to_rows, fp_by_uuid)
        cents: Dict[str, Tuple[float, float]] = {}
        radii: Dict[str, float] = {}
        layers: Dict[str, str] = {}
        for uuid, poly in polys.items():
            fp = fp_by_uuid.get(uuid)
            if fp is None:
                continue
            try:
                c = poly.centroid
                coords = np.asarray(poly.exterior.coords, dtype=np.float64)
                r = float(np.max(np.linalg.norm(coords - [c.x, c.y], axis=1)))
            except Exception:
                logger.debug("courtyard centroid failed for %s", uuid)
                continue
            if not math.isfinite(r) or r < 1e-9:
                r = 1.0
            cents[uuid] = (float(c.x), float(c.y))
            radii[uuid] = r
            layers[uuid] = fp.layer
        return polys, cents, radii, layers

    def _push_rows(delta, li, ux, uy, dist_mm):
        rows = fp_local_to_rows[li]
        if rows is None or len(rows) == 0 or dist_mm <= 0:
            return
        uuid = fp_uuids[li]
        cx, cy = cents[uuid]
        proj = ((positions[rows, 0] - cx) * ux + (positions[rows, 1] - cy) * uy) / radii[uuid]
        w = _contact_weights(proj)
        delta[rows, 0] += w * (ux * dist_mm)
        delta[rows, 1] += w * (uy * dist_mm)

    # Single Jacobi round (deliberately not iterated): one relaxed eject
    # per overlapping pair from frozen geometry. Iterating re-measures
    # after each shove and ping-pongs wedged parts between neighbors
    # (measured: R13 walked 12-20mm while worst overlap grew); one round
    # halves every overlap and leaves the remainder — honestly reported
    # via legality — for the next legalize run.
    moved_any = False
    _pass_old = positions.copy()
    polys, cents, radii, layers = _posed()
    delta = np.zeros_like(positions)
    # Part-part (and part-ghost-wall): deepest MTV per pair.
    for a in range(len(fp_uuids)):
        ua = fp_uuids[a]
        if ua not in polys:
            continue
        for b in range(a + 1, len(fp_uuids)):
            ub = fp_uuids[b]
            if ub not in polys:
                continue
            ma = bool(fp_movable_list[a])
            mb = bool(fp_movable_list[b])
            if not ma and not mb:
                continue
            if layers[ua] != layers[ub]:
                continue
            pa, pb = polys[ua], polys[ub]
            try:
                if not pa.intersects(pb):
                    continue
                inter = pa.intersection(pb)
                area = float(inter.area) if not inter.is_empty else 0.0
            except Exception:
                continue
            if area <= _DEPEN_CLEAN_AREA_MM2:
                continue
            try:
                qa, qb = _nearest_points(pa, pb)
                dx, dy = float(qa.x - qb.x), float(qa.y - qb.y)
            except Exception:
                dx = dy = 0.0
            dist = math.hypot(dx, dy)
            if dist <= 1e-8:
                dx = cents[ua][0] - cents[ub][0]
                dy = cents[ua][1] - cents[ub][1]
                dist = math.hypot(dx, dy)
            if dist <= 1e-9:
                dx, dy, dist = 1.0, 0.0, 1.0
            ux, uy = dx / dist, dy / dist
            depth = _overlap_depth_mm(pa, pb, ux, uy)
            if depth <= 1e-9:
                continue
            step = depth * _DEPEN_RELAX
            sa = step * (0.5 if (ma and mb) else 1.0) if ma else 0.0
            sb = step * (0.5 if (ma and mb) else 1.0) if mb else 0.0
            if debug_sink is not None:
                debug_sink.append({
                    "pair": [ua, ub],
                    "area_mm2": float(area),
                    "depth_mm": float(depth),
                    "step_mm": float(step),
                    "sa_mm": float(sa),
                    "sb_mm": float(sb),
                    "a_movable": bool(ma),
                    "b_movable": bool(mb),
                })
            if sa > 0:
                _push_rows(delta, a, ux, uy, sa)
                moved_any = True
            if sb > 0:
                _push_rows(delta, b, -ux, -uy, sb)
                moved_any = True
    if np.any(delta != 0.0):
        positions += delta
    _project_rigid_poses(positions, proj_bodies)
    _clamp_rigid_step(
        positions, _pass_old, proj_bodies, pad_movable,
        cap_mm=1e9, max_rot_rad=_DEPEN_MAX_ROT_RAD,
    )
    return moved_any


def _label_rect_poly(text, node_xy, board_angle_deg, size_mm, halo_mm):
    """Halo-inflated label collision rect at a node, or None if degenerate."""
    try:
        rect = label_board_rect(
            text, float(node_xy[0]), float(node_xy[1]),
            float(board_angle_deg), size_mm, halo_mm=halo_mm,
        )
    except (TypeError, ValueError):
        return None
    if not rect:
        return None
    try:
        from shapely.geometry import Polygon as _Polygon
        poly = _Polygon(rect)
    except Exception:
        return None
    if poly is None or poly.is_empty:
        return None
    return poly


def _body_poly_map(
    positions: np.ndarray,
    fp_uuids: List[str],
    fp_local_to_rows: List[np.ndarray],
    fp_by_uuid: Dict[str, Footprint],
) -> Dict[str, object]:
    """Posed courtyard polygon per footprint uuid (ghosts filed, rest posed).

    Shared body set for both depenetrate passes: ghosts use their filed
    courtyards (static anchors), real footprints rebuild from current pad
    positions. Skips degenerates, never raises.
    """
    polys: Dict[str, object] = {}
    for li, uuid in enumerate(fp_uuids):
        fp = fp_by_uuid.get(uuid)
        if fp is None:
            continue
        try:
            rows = fp_local_to_rows[li]
        except (IndexError, TypeError):
            continue
        if fp.ghost or len(rows) == 0:
            if not fp.courtyard:
                continue
            poly = _footprint_courtyard_polygon(fp)
        else:
            try:
                poly = _footprint_courtyard_polygon(
                    fp, pad_positions=positions, pad_indices=[int(r) for r in rows]
                )
            except Exception:
                logger.debug("courtyard polygon failed for %s", uuid)
                continue
        if poly is None or poly.is_empty:
            continue
        polys[uuid] = poly
    return polys


def _depenetrate_labels(
    positions: np.ndarray,
    lab_rows: List[int],
    lab_specs: List[Tuple[str, str, Tuple[float, float], float, Tuple[float, float], str]],
    fp_uuids: List[str],
    fp_local_to_rows: List[np.ndarray],
    fp_by_uuid: Dict[str, Footprint],
    copper_polys: List[object],
    edge_poly: Optional[object],
    radius_mm: float,
    max_rounds: int = _DEPEN_LABEL_MAX_ROUNDS,
) -> bool:
    """Eject settled labels out of true body/copper/edge penetration.

    ``lab_specs`` parallels ``lab_rows`` as (uuid, text, size_mm,
    board_angle_deg, anchor_xy_mm, parent_uuid). Bodies, copper, and the
    region edge are static — the label always takes the full correction
    (one-way invariant); label-vs-label splits evenly like movable parts.
    A label's own parent body is excluded (silk over its own courtyard
    is DRC-legal); its copper still repels via ``copper_polys``. Every
    move is clipped to the orbit disc around the filed anchor. Iterated
    Jacobi rounds toward clean (capped): single nodes cannot spin, so
    the ping-pong that caps the parts pass at one round does not apply.
    Returns True when any label moved.
    """
    from shapely.ops import nearest_points as _nearest_points

    order = sorted(range(len(lab_rows)), key=lambda k: lab_specs[k][0])
    if not order:
        return False
    try:
        radius = max(float(radius_mm), 0.0)
    except (TypeError, ValueError):
        radius = 0.0
    body_items = list(
        _body_poly_map(positions, fp_uuids, fp_local_to_rows, fp_by_uuid).items()
    )

    def _rects():
        rects = {}
        for k in order:
            uuid, text, size, angle, _anchor, _parent = lab_specs[k]
            halo = label_halo_mm(size)
            rects[uuid] = _label_rect_poly(
                text, positions[lab_rows[k]], angle, size, halo)
        return rects

    def _push_from(poly, obs, share):
        """Relaxed MTV eject of poly from blocker obs (or None)."""
        try:
            if poly is None or poly.is_empty or obs is None or obs.is_empty:
                return None
            if not poly.intersects(obs):
                return None
            inter = poly.intersection(obs)
            if inter.is_empty or float(inter.area) <= _DEPEN_CLEAN_AREA_MM2:
                return None
            qa, qb = _nearest_points(poly, obs)
            dx, dy = float(qa.x - qb.x), float(qa.y - qb.y)
        except Exception:
            return None
        dist = math.hypot(dx, dy)
        if dist <= 1e-9:
            try:
                dx = poly.centroid.x - obs.centroid.x
                dy = poly.centroid.y - obs.centroid.y
                dist = math.hypot(dx, dy)
            except Exception:
                dist = 0.0
            if dist <= 1e-9:
                dx, dy, dist = 1.0, 0.0, 1.0
        depth = _overlap_depth_mm(poly, obs, dx / dist, dy / dist)
        if depth <= 1e-9:
            return None
        step = depth * _DEPEN_RELAX * share
        return (dx / dist * step, dy / dist * step)

    def _edge_push(poly):
        """Containment push for a rect outside the region polygon."""
        if edge_poly is None or edge_poly.is_empty:
            return None
        try:
            if edge_poly.covers(poly):
                return None
            boundary = edge_poly.boundary
            worst = None
            best_d = -1.0
            for v in poly.exterior.coords:
                from shapely.geometry import Point as _Point
                pt = _Point(v)
                if edge_poly.covers(pt):
                    continue
                d = pt.distance(boundary)
                if d > best_d:
                    best_d = float(d)
                    worst = (float(v[0]), float(v[1]))
            if worst is None:
                return None
            q = _nearest_points(
                _Point(worst), boundary)[1]
            dx, dy = worst[0] - float(q.x), worst[1] - float(q.y)
            d = math.hypot(dx, dy)
            if d <= 1e-12:
                return None
            s = (best_d + 1e-6) / d
            return (-dx * s, -dy * s)
        except Exception:
            return None

    moved_any = False
    for _ in range(max(1, int(max_rounds))):
        rects = _rects()
        moves: Dict[str, Tuple[float, float]] = {}
        for k in order:
            uuid = lab_specs[k][0]
            parent = lab_specs[k][5]
            poly = rects.get(uuid)
            if poly is None or poly.is_empty:
                continue
            mx, my = 0.0, 0.0
            for bu, obs in body_items:
                if bu == parent:
                    continue
                push = _push_from(poly, obs, 1.0)
                if push is not None:
                    mx, my = mx + push[0], my + push[1]
            for obs in copper_polys:
                push = _push_from(poly, obs, 1.0)
                if push is not None:
                    mx, my = mx + push[0], my + push[1]
            push = _edge_push(poly)
            if push is not None:
                mx, my = mx + push[0], my + push[1]
            moves[uuid] = (mx, my)
        # Label-vs-label, split evenly, from the same frozen geometry.
        for ia in range(len(order)):
            for ib in range(ia + 1, len(order)):
                ua, ub = lab_specs[order[ia]][0], lab_specs[order[ib]][0]
                pa, pb = rects.get(ua), rects.get(ub)
                if pa is None or pb is None or pa.is_empty or pb.is_empty:
                    continue
                push = _push_from(pa, pb, 0.5)
                if push is None:
                    continue
                moves[ua] = (moves[ua][0] + push[0], moves[ua][1] + push[1])
                moves[ub] = (moves[ub][0] - push[0], moves[ub][1] - push[1])
        changed = False
        for k in order:
            uuid = lab_specs[k][0]
            mx, my = moves[uuid]
            if abs(mx) <= 1e-12 and abs(my) <= 1e-12:
                continue
            node = positions[lab_rows[k]]
            try:
                nx, ny = clamp_orbit_radius(
                    (float(node[0]) + mx, float(node[1]) + my),
                    lab_specs[k][4], radius,
                )
            except Exception:
                continue
            if abs(nx - float(node[0])) > 1e-9 or abs(ny - float(node[1])) > 1e-9:
                positions[lab_rows[k], 0] = nx
                positions[lab_rows[k], 1] = ny
                changed = True
        if changed:
            moved_any = True
        else:
            break
    return moved_any
