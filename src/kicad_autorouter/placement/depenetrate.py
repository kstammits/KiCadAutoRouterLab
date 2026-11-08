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
        polys: Dict[str, object] = {}
        cents: Dict[str, Tuple[float, float]] = {}
        radii: Dict[str, float] = {}
        layers: Dict[str, str] = {}
        for li, uuid in enumerate(fp_uuids):
            fp = fp_by_uuid.get(uuid)
            if fp is None:
                continue
            rows = fp_local_to_rows[li]
            if fp.ghost or len(rows) == 0:
                if not fp.courtyard:
                    continue
                poly = _footprint_courtyard_polygon(fp)
                if poly is None or poly.is_empty:
                    continue
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
            try:
                c = poly.centroid
                coords = np.asarray(poly.exterior.coords, dtype=np.float64)
                r = float(np.max(np.linalg.norm(coords - [c.x, c.y], axis=1)))
            except Exception:
                logger.debug("courtyard centroid failed for %s", uuid)
                continue
            if not math.isfinite(r) or r < 1e-9:
                r = 1.0
            polys[uuid] = poly
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
