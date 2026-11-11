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
    _pose_ring,
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
# Own-silk clearance for the label ejectors: ink edge to text-bbox gap in
# mm. Matches KiCad's default silk-to-silk clearance order; the text side
# already carries its halo in the label rect, so this buffers only the
# silk side (half stroke width + this margin).
_DEPEN_SILK_CLEARANCE_MM = 0.2
# Candidate ring counts for the clean-spot search: the anchor plus two
# rings of eight (half/full radius) spiral outward, nearest-first so the
# first clean hit is also the closest to home.
_CLEAN_SPOT_RING_N = 8


def _overlap_area_mm2(cand, obs_list, bboxes):
    """Total overlap area of candidate poly vs obstacles (dust included)."""
    total = 0.0
    try:
        minx, miny, maxx, maxy = cand.bounds
    except Exception:
        return total
    for obs, bb in zip(obs_list, bboxes):
        try:
            if obs is None or bb is None:
                continue
            if bb[2] < minx or bb[0] > maxx or bb[3] < miny or bb[1] > maxy:
                continue
            if cand.disjoint(obs):
                continue
            inter = cand.intersection(obs)
            if not inter.is_empty:
                total += float(inter.area)
        except Exception:
            continue
    return total


def _score_spots(rect_poly, obstacles, center_xy, radius_mm, cover_poly=None,
                 bboxes=None):
    """Yield ``(centroid, dirty_area)`` for the current pos then the spiral.

    Shared candidate engine: current position first (clean labels test
    clean immediately), then center plus two rings inside ``radius_mm``.
    ``dirty_area`` is the raw total overlap (dust included); winners are
    picked by the callers. Deterministic order. ``bboxes`` may carry
    precomputed ``obs.bounds`` aligned with ``obstacles`` to skip the
    per-scan rebuild.
    """
    try:
        if rect_poly is None or rect_poly.is_empty:
            return
        cx0, cy0 = rect_poly.centroid.x, rect_poly.centroid.y
        cenx, ceny = float(center_xy[0]), float(center_xy[1])
    except (TypeError, ValueError, IndexError):
        return
    try:
        radius = max(float(radius_mm), 0.0)
    except (TypeError, ValueError):
        radius = 0.0
    try:
        obs_list = list(obstacles)
    except TypeError:
        return
    try:
        from shapely.affinity import translate as _translate
    except Exception:
        return
    if bboxes is None:
        bboxes = []
        for obs in obs_list:
            try:
                bboxes.append(obs.bounds if obs is not None and not obs.is_empty else None)
            except Exception:
                bboxes.append(None)

    def _at(tx, ty):
        try:
            cand = _translate(rect_poly, xoff=(tx - cx0), yoff=(ty - cy0))
        except Exception:
            return None, None
        if cover_poly is not None:
            try:
                if not cover_poly.covers(cand):
                    return None, None
            except Exception:
                return None, None
        return (tx, ty), _overlap_area_mm2(cand, obs_list, bboxes)

    seen = set()
    points = [(cx0, cy0), (cenx, ceny)]
    for frac in (0.5, 1.0):
        for i in range(_CLEAN_SPOT_RING_N):
            a = 2.0 * math.pi * i / _CLEAN_SPOT_RING_N
            points.append((cenx + radius * frac * math.cos(a),
                           ceny + radius * frac * math.sin(a)))
    for tx, ty in points:
        key = (round(tx, 9), round(ty, 9))
        if key in seen:
            continue
        seen.add(key)
        spot, area = _at(tx, ty)
        if spot is not None:
            yield spot, area


def _find_spot(rect_poly, obstacles, center_xy, radius_mm, cover_poly=None,
               bboxes=None):
    """Single scan over the shared candidate spiral.

    Returns ``(first_clean, best, best_area, cur_area)`` where
    ``first_clean`` is the nearest fully-clean centroid (or None),
    ``best``/``best_area`` the least-dirty candidate, and ``cur_area``
    the overlap at the current position (first candidate). One pass
    replaces the old clean-then-best double traversal.
    """
    first_clean, best, best_area, cur_area = None, None, math.inf, None
    for spot, area in _score_spots(
        rect_poly, obstacles, center_xy, radius_mm, cover_poly,
        bboxes=bboxes,
    ):
        if cur_area is None:
            cur_area = area
        if area < best_area - 1e-12:
            best, best_area = spot, area
        if first_clean is None and area <= _DEPEN_CLEAN_AREA_MM2:
            first_clean = spot
    if cur_area is None:
        cur_area = math.inf
    return first_clean, best, best_area, cur_area


def _clean_spot(rect_poly, obstacles, center_xy, radius_mm, cover_poly=None,
                bboxes=None):
    """First fully-clean centroid for ``rect_poly``, spiraling out from center.

    Body-agnostic (no label/part concepts): the current position is tested
    first (clean labels never move), then the center plus two rings inside
    ``radius_mm`` — nearest-to-center wins, which re-homes drifted labels
    instead of wandering. Each candidate translates the rect so its
    centroid lands on it, and wins when disjoint-or-dust from every
    obstacle (and covered by ``cover_poly`` when given). Returns the
    winning centroid ``(x, y)`` or ``None`` when no candidate is clean.
    Deterministic: fixed candidate order, first hit wins.
    """
    first_clean, _best, _best_area, _cur = _find_spot(
        rect_poly, obstacles, center_xy, radius_mm, cover_poly,
        bboxes=bboxes,
    )
    return first_clean


def _best_spot(rect_poly, obstacles, center_xy, radius_mm, cover_poly=None,
               bboxes=None):
    """Least-dirty ``(centroid, area)`` over the shared candidate spiral.

    Fallback when :func:`_clean_spot` finds nothing fully clean: the
    minimum-overlap candidate still improves on a stuck label (off pads
    onto a ring edge beats sitting on copper). Ties keep the earliest —
    current pos, then nearest-to-center. Returns ``(None, inf)`` when no
    candidate is even testable.
    """
    _first, best, best_area, _cur = _find_spot(
        rect_poly, obstacles, center_xy, radius_mm, cover_poly,
        bboxes=bboxes,
    )
    return best, best_area


def _pose_parent_silk(fp, positions, rows):
    """Posed silk strokes + centroid for one footprint.

    Returns ``([(x1, y1, x2, y2, width_mm), ...], (hx, hy))`` in board
    frame. Ghosts (and pad-less footprints) use filed geometry; the rest
    rigid-fit from the pad cloud exactly like courtyard posing. Never
    raises: malformed input yields empty strokes and the filed centroid.
    """
    try:
        home = (float(fp.x_mm), float(fp.y_mm))
    except (TypeError, ValueError, AttributeError):
        return [], (0.0, 0.0)
    segs = getattr(fp, "silk", ()) or ()
    if not segs:
        return [], home
    try:
        if getattr(fp, "ghost", False) or rows is None or len(rows) == 0:
            return [
                (s.start.x_mm, s.start.y_mm, s.end.x_mm, s.end.y_mm,
                 float(s.width_mm))
                for s in segs
            ], home
        pad_rows = [int(r) for r in rows]
        ends = []
        for s in segs:
            ends.append((s.start.x_mm, s.start.y_mm))
            ends.append((s.end.x_mm, s.end.y_mm))
        fitted = _pose_ring(ends, fp, positions, pad_rows)
        posed = [
            (fitted[i][0], fitted[i][1], fitted[i + 1][0], fitted[i + 1][1])
            for i in range(0, len(fitted) - 1, 2)
        ]
        widths = [float(s.width_mm) for s in segs]
        home = _pose_ring([(home[0], home[1])], fp, positions, pad_rows)[0]
        out = []
        for i, (x1, y1, x2, y2) in enumerate(posed):
            try:
                out.append((x1, y1, x2, y2, widths[i]))
            except (IndexError, TypeError, ValueError):
                continue
        return out, (float(home[0]), float(home[1]))
    except Exception:
        return [], home


def _nearby_silk_obstacles(center_xy, reach_mm, all_posed_silk,
                           clearance_mm=_DEPEN_SILK_CLEARANCE_MM):
    """Buffered silk polys of any parent near ``center_xy``.

    ``all_posed_silk`` maps parent uuid to posed stroke tuples (as built
    by :func:`_pose_parent_silk`); only strokes whose bbox (expanded by
    ink half-width, clearance, and ``reach_mm``) meets the query disc are
    buffered, so a giant decorative outline costs one bbox reject per
    far stroke instead of a polygon buffer. Never raises.
    """
    from shapely.geometry import LineString as _LineString

    try:
        cx, cy = float(center_xy[0]), float(center_xy[1])
        reach = max(float(reach_mm), 0.0)
    except (TypeError, ValueError, IndexError):
        return []
    obs = []
    for strokes in all_posed_silk.values():
        for x1, y1, x2, y2, width in strokes:
            try:
                ink = float(width) / 2.0 + float(clearance_mm)
            except (TypeError, ValueError):
                continue
            if (max(x1, x2) + ink < cx - reach
                    or min(x1, x2) - ink > cx + reach
                    or max(y1, y2) + ink < cy - reach
                    or min(y1, y2) - ink > cy + reach):
                continue
            try:
                poly = _LineString([(x1, y1), (x2, y2)]).buffer(ink, cap_style="round")
            except Exception:
                continue
            if poly is None or poly.is_empty:
                continue
            obs.append(poly)
    return obs


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

    For each intersecting same-layer body pair (including movable-vs-wall-
    ghost courtyard walls; attractor ghosts are pull-only and never
    bodies), translates bodies apart along the MTV by
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
    positions. Attractor ghosts (pull-only) are not bodies — only wall
    ghosts join the obstacle set. Skips degenerates, never raises.
    """
    polys: Dict[str, object] = {}
    for li, uuid in enumerate(fp_uuids):
        fp = fp_by_uuid.get(uuid)
        if fp is None:
            continue
        if getattr(fp, "ghost", False) and getattr(fp, "ghost_attractions", ()):
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
    A label's own parent body is excluded (sitting inside its own
    courtyard is DRC-legal), but the parent's own silk strokes still eject
    (ink touching the text trips silk clearance, so only the courtyard —
    never the artwork — is sit-able ground). Its copper still repels via
    ``copper_polys``. Every move is clipped to the orbit disc around the
    filed anchor. Iterated Jacobi eject rounds first (single nodes cannot
    spin, so the ping-pong that caps the parts pass at one round does not
    apply); then a clean-spot search relocates whatever is still dirty —
    spiral candidates from the anchor against the full obstacle set
    (own + nearby-neighbor silk included), nearest-first. Candidate
    testing cannot cancel the way opposing pushes can, so ring traps and
    straddles resolve here. Returns True when any label moved.
    """
    from shapely.geometry import LineString as _LineString
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
    silk_items: Dict[str, list] = {}
    all_posed_silk: Dict[str, list] = {}
    _uuid_to_li = {uuid: li for li, uuid in enumerate(fp_uuids)}
    for uuid in fp_uuids:
        fp = fp_by_uuid.get(uuid)
        if fp is None or not fp.silk:
            all_posed_silk[uuid] = []
            continue
        try:
            li = _uuid_to_li.get(uuid)
            rows = fp_local_to_rows[li] if li is not None else None
            strokes, _home = _pose_parent_silk(fp, positions, rows)
            all_posed_silk[uuid] = strokes
        except Exception:
            all_posed_silk[uuid] = []
    for k in order:
        parent = lab_specs[k][5]
        if parent in silk_items:
            continue
        silk_items[parent] = []
        try:
            for x1, y1, x2, y2, width in all_posed_silk.get(parent, ()):
                dx, dy = x2 - x1, y2 - y1
                seg_len = math.hypot(dx, dy)
                if seg_len <= 1e-12:
                    nx, ny = None, None
                else:
                    nx, ny = -dy / seg_len, dx / seg_len
                try:
                    obs = _LineString([(x1, y1), (x2, y2)]).buffer(
                        float(width) / 2.0 + _DEPEN_SILK_CLEARANCE_MM,
                        cap_style="round",
                    )
                except Exception:
                    continue
                if obs is None or obs.is_empty:
                    continue
                silk_items[parent].append(
                    (obs, nx, ny, (x1 + x2) / 2.0, (y1 + y2) / 2.0)
                )
        except Exception:
            silk_items[parent] = []

    def _rects():
        rects = {}
        for k in order:
            uuid, text, size, angle, _anchor, _parent = lab_specs[k]
            halo = label_halo_mm(size)
            rects[uuid] = _label_rect_poly(
                text, positions[lab_rows[k]], angle, size, halo)
        return rects

    def _bounds_of(poly):
        try:
            return poly.bounds if poly is not None and not poly.is_empty else None
        except Exception:
            return None

    # Static per call: body/copper geometry never moves during the label
    # pass (labels are one-way), so measure bounds once instead of per
    # label per sweep. Silk strokes buffer on first touch and reuse the
    # cached poly for every later label/sweep in this call.
    _body_bboxes = [(bu, obs, _bounds_of(obs)) for bu, obs in body_items]
    _copper_bboxes = [(_bounds_of(obs), obs) for obs in copper_polys]
    _silk_cache: Dict[tuple, object] = {}

    def _nearby_silk_cached(center_xy, reach_mm):
        from shapely.geometry import LineString as _LineString

        try:
            cx, cy = float(center_xy[0]), float(center_xy[1])
            reach = max(float(reach_mm), 0.0)
        except (TypeError, ValueError, IndexError):
            return []
        obs = []
        for strokes in all_posed_silk.values():
            for x1, y1, x2, y2, width in strokes:
                try:
                    ink = float(width) / 2.0 + float(_DEPEN_SILK_CLEARANCE_MM)
                except (TypeError, ValueError):
                    continue
                if (max(x1, x2) + ink < cx - reach
                        or min(x1, x2) - ink > cx + reach
                        or max(y1, y2) + ink < cy - reach
                        or min(y1, y2) - ink > cy + reach):
                    continue
                key = (x1, y1, x2, y2, ink)
                poly = _silk_cache.get(key)
                if poly is None:
                    try:
                        poly = _LineString([(x1, y1), (x2, y2)]).buffer(
                            ink, cap_style="round")
                    except Exception:
                        continue
                    if poly is None or poly.is_empty:
                        continue
                    _silk_cache[key] = poly
                obs.append(poly)
        return obs

    def _eject_from(poly, obs, share=1.0, ux=None, uy=None):
        """Relaxed eject of poly from blocker obs (or None).

        Shared gate (intersects, dust area, depth) for bodies, copper,
        and silk. With no axis the MTV comes from nearest points (with
        centroid fallback); silk passes its stroke normal so a label
        centered on long ink lifts off instead of sliding along it.
        """
        try:
            if poly is None or poly.is_empty or obs is None or obs.is_empty:
                return None
            if not poly.intersects(obs):
                return None
            inter = poly.intersection(obs)
            if inter.is_empty or float(inter.area) <= _DEPEN_CLEAN_AREA_MM2:
                return None
            if ux is None or uy is None:
                qa, qb = _nearest_points(poly, obs)
                dx, dy = float(qa.x - qb.x), float(qa.y - qb.y)
            else:
                dx, dy = ux, uy
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

    def _push_from(poly, obs, share):
        """Relaxed MTV eject of poly from blocker obs (or None)."""
        return _eject_from(poly, obs, share)

    def _silk_push(poly, obs, ux, uy):
        """Relaxed eject of poly from a silk-stroke blocker along unit (ux, uy)."""
        return _eject_from(poly, obs, 1.0, ux, uy)

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
            # Own-silk eject: side-tested stroke normals. Opposing sides of
            # an enclosing outline can still cancel here — that trap is
            # the search phase's job below (candidate testing cannot
            # cancel), so the eject stays dumb and cheap.
            for obs, nx, ny, sx, sy in silk_items.get(parent, ()):
                if nx is None or ny is None:
                    push = _push_from(poly, obs, 1.0)
                else:
                    try:
                        cx, cy = poly.centroid.x, poly.centroid.y
                    except Exception:
                        continue
                    side = 1.0 if (cx - sx) * nx + (cy - sy) * ny >= 0.0 else -1.0
                    push = _silk_push(poly, obs, side * nx, side * ny)
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

    # Space search: eject rounds only resolve overlap they already touch,
    # so opposing pushes (ring traps, straddles, neighbor ink) can stall
    # dirty. Candidate testing cannot cancel: for each still-dirty label,
    # spiral outward from the anchor and take the first fully-clean spot
    # (nearest-first, so D2-style drift re-homes instead of wandering).
    # The obstacle set here is the full one — own + nearby-neighbor silk
    # included — unlike the eject above. Two sweeps max: the second
    # catches touches the first sweep's moves introduced. Labels with no
    # clean spot in-disc stay put and are logged (leash-limited, not an
    # ejector stall).
    for _ in range(2):
        rects = _rects()
        swept = False
        for k in order:
            uuid = lab_specs[k][0]
            parent = lab_specs[k][5]
            anchor = lab_specs[k][4]
            poly = rects.get(uuid)
            if poly is None or poly.is_empty:
                continue
            try:
                minx, _miny, maxx, _maxy = poly.bounds
                half_diag = 0.5 * math.hypot(maxx - minx, _maxy - _miny)
            except Exception:
                continue
            obstacles = [obs for bu, obs, _bb in _body_bboxes if bu != parent]
            oboxes = [bb for bu, obs, bb in _body_bboxes if bu != parent]
            for bb, obs in _copper_bboxes:
                obstacles.append(obs)
                oboxes.append(bb)
            try:
                reach = radius + half_diag + 0.5
                for poly in _nearby_silk_cached(anchor, reach):
                    obstacles.append(poly)
                    oboxes.append(_bounds_of(poly))
            except Exception:
                pass
            for ou, op in rects.items():
                if ou != uuid and op is not None and not op.is_empty:
                    obstacles.append(op)
                    oboxes.append(_bounds_of(op))
            try:
                first_clean, best, best_area, cur_area = _find_spot(
                    poly, obstacles, anchor, radius, cover_poly=edge_poly,
                    bboxes=oboxes)
                spot = first_clean
                if spot is None:
                    if best is None or not (
                            best_area < cur_area - 1e-9):
                        logger.debug(
                            "label %s leash-limited (%.4f mm2)", uuid, cur_area)
                        continue
                    logger.debug(
                        "label %s improves %.4f -> %.4f mm2",
                        uuid, cur_area, best_area)
                    spot = best
            except Exception:
                continue
            try:
                cx, cy = poly.centroid.x, poly.centroid.y
            except Exception:
                continue
            dx, dy = spot[0] - cx, spot[1] - cy
            if abs(dx) <= 1e-9 and abs(dy) <= 1e-9:
                continue
            node = positions[lab_rows[k]]
            try:
                nx, ny = clamp_orbit_radius(
                    (float(node[0]) + dx, float(node[1]) + dy), anchor, radius,
                )
            except Exception:
                continue
            if abs(nx - float(node[0])) > 1e-9 or abs(ny - float(node[1])) > 1e-9:
                positions[lab_rows[k], 0] = nx
                positions[lab_rows[k], 1] = ny
                rects[uuid] = _label_rect_poly(
                    lab_specs[k][1], (nx, ny), lab_specs[k][3],
                    lab_specs[k][2], label_halo_mm(lab_specs[k][2]))
                swept = True
        if swept:
            moved_any = True
        else:
            break
    return moved_any
