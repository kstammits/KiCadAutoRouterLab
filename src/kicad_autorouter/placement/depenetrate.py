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
    label_rect_poly,
)
from .contact import (
    _contact_weights,
)
from .posed import _body_poly_map
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
# Eject travel cap: the eject is local separation, not relocation. Without
# a counter-force (the spring lives in the loop) repeated Jacobi rounds
# can walk a label across its whole ellipse (measured: 17 mm on a crowded
# jack) while the search that follows is home-framed and would have kept
# it near. Longer moves belong to the search, which picks nearest-clean.
_DEPEN_EJECT_TRAVEL_CAP_MM = 2.0
# Own-silk clearance for the label ejectors: ink edge to text-bbox gap in
# mm. Matches KiCad's default silk-to-silk clearance order; the finish
# scores bare text (the halo is the loop's soft force, not finish dirt),
# so this buffers the full text-to-ink gap on the silk side
# (half stroke width + this margin).
_DEPEN_SILK_CLEARANCE_MM = 0.2
# Candidate spiral for the clean-spot search: the anchor plus four rings
# of sixteen, nearest-first; the downhill walk below finishes what the
# spiral starts.
_CLEAN_SPOT_RING_N = 16
_DESCEND_STEP_MM = 0.25
_DESCEND_MAX_MOVES = 24


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
                 bboxes=None, inside=None):
    """Yield ``(centroid, dirty_area)`` for the current pos then the spiral.

    Shared candidate engine: current position first (clean labels test
    clean immediately), then center plus four rings inside ``radius_mm``.
    ``dirty_area`` is the raw total overlap (dust included); winners are
    picked by the callers. Deterministic order. ``bboxes`` may carry
    precomputed ``obs.bounds`` aligned with ``obstacles`` to skip the
    per-scan rebuild. ``inside`` is an optional ``(x, y) -> bool``
    candidate filter (the label clamp ellipse); rejected candidates are
    never yielded.
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
    for frac in (0.25, 0.5, 0.75, 1.0):
        for i in range(_CLEAN_SPOT_RING_N):
            a = 2.0 * math.pi * i / _CLEAN_SPOT_RING_N
            points.append((cenx + radius * frac * math.cos(a),
                           ceny + radius * frac * math.sin(a)))
    for tx, ty in points:
        key = (round(tx, 9), round(ty, 9))
        if key in seen:
            continue
        seen.add(key)
        if inside is not None:
            try:
                if not inside(tx, ty):
                    continue
            except Exception:
                continue
        spot, area = _at(tx, ty)
        if spot is not None:
            yield spot, area


def _find_spot(rect_poly, obstacles, center_xy, radius_mm, cover_poly=None,
               bboxes=None, inside=None):
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
        bboxes=bboxes, inside=inside,
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
               bboxes=None, inside=None):
    """First fully-clean centroid for ``rect_poly``, spiraling out from center.

    Body-agnostic (no label/part concepts): the current position is tested
    first (clean labels never move), then the center plus four rings inside
    ``radius_mm`` — nearest-to-center wins, which re-homes drifted labels
    instead of wandering. Each candidate translates the rect so its
    centroid lands on it, and wins when disjoint-or-dust from every
    obstacle (and covered by ``cover_poly`` when given). Returns the
    winning centroid ``(x, y)`` or ``None`` when no candidate is clean.
    Deterministic: fixed candidate order, first hit wins.
    """
    first_clean, _best, _best_area, _cur = _find_spot(
        rect_poly, obstacles, center_xy, radius_mm, cover_poly,
        bboxes=bboxes, inside=inside,
    )
    return first_clean


def _best_spot(rect_poly, obstacles, center_xy, radius_mm, cover_poly=None,
               bboxes=None, inside=None):
    """Least-dirty ``(centroid, area)`` over the shared candidate spiral.

    Tested fallback companion to :func:`_clean_spot` (see
    ``test_best_spot_minimizes_when_nothing_is_clean``): minimum-overlap
    candidate from :func:`_find_spot`. Ties keep the earliest —
    current pos, then nearest-to-center. Returns ``(None, inf)`` when no
    candidate is even testable.
    """
    _first, best, best_area, _cur = _find_spot(
        rect_poly, obstacles, center_xy, radius_mm, cover_poly,
        bboxes=bboxes, inside=inside,
    )
    return best, best_area


def _descend_to_clean(rect_poly, start_xy, obstacles, bboxes, anchor_xy,
                      bound_mm, angle_deg=0.0, cover_poly=None,
                      clean_area_mm2=_DEPEN_CLEAN_AREA_MM2):
    """Downhill walk from ``start_xy`` toward a fully-clean centroid.

    Fixed ring radii can straddle a narrow clean slot, so from a spiral
    candidate this steps to the least-dirty of 8 compass neighbors
    (fixed E-first order, ``_DESCEND_STEP_MM`` stride). Flat
    neighborhoods cross too (a label straddling its own pads looks
    equally dirty until the box edge clears copper): equal-dirt moves
    over unvisited cells only. Stops at the first clean spot, on no
    improving or plateau move, or after ``_DESCEND_MAX_MOVES`` moves.
    Every candidate is translated-rect like the spiral, clipped to the
    label ellipse (``bound_mm`` + ``angle_deg``) and ``cover_poly``.
    Returns ``(spot, area)`` — the clean spot when one is reached, else
    the least-dirty visited (the start itself when stuck), or
    ``(None, inf)`` when nothing is testable. Callers fold the result
    into their least-dirty fallback. Deterministic: fixed neighbor
    order, first minimum wins ties.
    """
    from shapely.affinity import translate as _translate

    from ..board_model.labels import label_ellipse_inside as _ellin

    try:
        cx0, cy0 = rect_poly.centroid.x, rect_poly.centroid.y
        sx, sy = float(start_xy[0]), float(start_xy[1])
    except (TypeError, ValueError, IndexError, AttributeError):
        return None
    try:
        obs_list = list(obstacles)
        bbs = list(bboxes) if bboxes is not None else [None] * len(obs_list)
    except TypeError:
        return None
    inside = _ellin(anchor_xy, bound_mm, angle_deg)

    def _area_at(tx, ty):
        try:
            cand = _translate(rect_poly, xoff=(tx - cx0), yoff=(ty - cy0))
        except Exception:
            return None
        if not inside(tx, ty):
            return None
        if cover_poly is not None:
            try:
                if not cover_poly.covers(cand):
                    return None
            except Exception:
                return None
        try:
            return _overlap_area_mm2(cand, obs_list, bbs)
        except Exception:
            return None

    cur = (sx, sy)
    cur_area = _area_at(sx, sy)
    if cur_area is None:
        return None, math.inf
    if cur_area <= clean_area_mm2:
        return cur, cur_area
    step = _DESCEND_STEP_MM
    order = ((1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (-1.0, 1.0),
             (-1.0, 0.0), (-1.0, -1.0), (0.0, -1.0), (1.0, -1.0))
    norm = math.sqrt(2.0)
    # Plateaus cross too (own-pad straddles look equally dirty in every
    # direction until the box edge clears copper): moves are
    # non-increasing over unvisited cells, strict improvement first.
    seen = {(round(sx, 6), round(sy, 6))}
    for _ in range(_DESCEND_MAX_MOVES):
        best, best_area = None, cur_area
        for dx, dy in order:
            n = norm if dx != 0.0 and dy != 0.0 else 1.0
            tx, ty = cur[0] + step * dx / n, cur[1] + step * dy / n
            key = (round(tx, 6), round(ty, 6))
            if key in seen:
                continue
            a = _area_at(tx, ty)
            if a is None:
                continue
            if a < best_area - 1e-12:
                best, best_area = (tx, ty), a
            elif (best is None and a <= cur_area + 1e-12
                    and best_area >= cur_area - 1e-12):
                best, best_area = (tx, ty), a
        if best is None:
            return cur, cur_area
        seen.add((round(best[0], 6), round(best[1], 6)))
        cur, cur_area = best, best_area
        if cur_area <= clean_area_mm2:
            return cur, cur_area
    return cur, cur_area


def _nearby_silk_obstacles(center_xy, reach_mm, all_posed_silk,
                           clearance_mm=_DEPEN_SILK_CLEARANCE_MM,
                           cache=None):
    """Buffered silk polys of any parent near ``center_xy``.

    ``all_posed_silk`` maps parent uuid to posed stroke tuples from the
    shared finish geometry; only strokes whose bbox (expanded by
    ink half-width, clearance, and ``reach_mm``) meets the query disc are
    buffered, so a giant decorative outline costs one bbox reject per
    far stroke instead of a polygon buffer. When ``cache`` (a dict) is
    given, buffered polys are memoized by stroke key and reused across
    queries; without it each query buffers directly. Never raises.
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
            if cache is not None:
                key = (x1, y1, x2, y2, ink)
                poly = cache.get(key)
                if poly is not None:
                    obs.append(poly)
                    continue
            try:
                poly = _LineString([(x1, y1), (x2, y2)]).buffer(
                    ink, cap_style="round")
            except Exception:
                continue
            if poly is None or poly.is_empty:
                continue
            if cache is not None:
                cache[key] = poly
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


def _depenetrate_labels(
    positions: np.ndarray,
    lab_rows: List[int],
    lab_specs: List[Tuple[str, str, Tuple[float, float], float, Tuple[float, float], str]],
    posed,
    edge_poly: Optional[object],
    radius_mm: object,
    max_rounds: int = _DEPEN_LABEL_MAX_ROUNDS,
) -> bool:
    """Eject settled labels out of true body/copper/edge penetration.

    ``lab_specs`` parallels ``lab_rows`` as (uuid, text, size_mm,
    board_angle_deg, anchor_xy_mm, parent_uuid[, parent_angle_deg[,
    search_seed_xy]]). The anchor is always the parent centroid (the
    clamp/search frame never moves); the seed is a starting candidate
    only.
    ``radius_mm`` is one disc radius for all labels or a per-label list
    parallel to ``lab_rows`` — scalars mean discs, ``(ex, ey)`` tuples
    mean parent-local clamp ellipses (body half-extents + text extent,
    so sticks grow room only along their length). The finish scores bare
    text rects — the font halo is the loop's soft settlement force, not
    finish dirt — against the shared posed geometry (static bodies,
    copper, and the region edge from one frozen-phase build);
    the label always takes the full correction (one-way invariant);
    label-vs-label splits evenly like movable parts.
    A label's own parent body is excluded (sitting inside its own
    courtyard is DRC-legal), but the parent's own silk strokes still eject
    (ink touching the text trips silk clearance, so only the courtyard —
    never the artwork — is sit-able ground). All copper repels,
    including the parent's own: a label buried in its
    own pads is not visible after assembly, so the eject pushes it out
    and the search below relocates it to nearest-clean instead of
    parking it. Every move is clipped to the per-label ellipse
    around the parent centroid. Iterated Jacobi eject rounds first (single nodes cannot
    spin, so the ping-pong that caps the parts pass at one round does not
    apply); then a clean-spot search relocates whatever is still dirty —
    spiral candidates from the anchor against the full obstacle set
    (own + nearby-neighbor silk included), nearest-first, ellipse-bound.
    Candidate testing cannot cancel the way opposing pushes can, so ring
    traps and straddles resolve here. Returns True when any label moved.
    """
    from shapely.geometry import LineString as _LineString
    from shapely.ops import nearest_points as _nearest_points

    from ..board_model.labels import label_ellipse_inside as _ellin

    def _bound(k: int):
        if radii is not None and 0 <= k < len(radii):
            return radii[k]
        return radius

    def _pang(k: int) -> float:
        try:
            return float(lab_specs[k][6])
        except (TypeError, ValueError, IndexError):
            return 0.0

    def _seed_pt(k: int):
        """Hygiene seed: nearest-to-home clean spot, or None.

        A starting candidate only — the clamp/search frame stays on the
        parent centroid, so a deeply-buried home cannot drag the ellipse
        off the part. Malformed or missing seeds read as None.
        """
        try:
            spec = lab_specs[k]
            if len(spec) <= 7 or spec[7] is None:
                return None
            return (float(spec[7][0]), float(spec[7][1]))
        except (TypeError, ValueError, IndexError):
            return None

    order = sorted(range(len(lab_rows)), key=lambda k: lab_specs[k][0])
    if not order:
        return False
    try:
        radius = max(float(radius_mm), 0.0)
    except (TypeError, ValueError):
        radius = 0.0
    radii = list(radius_mm) if isinstance(radius_mm, (list, tuple)) else None

    def _rk(k: int) -> float:
        b = _bound(k)
        try:
            return max(float(b), 0.0)
        except (TypeError, ValueError):
            pass
        try:
            ex, ey = b
            return max(float(ex), float(ey), 0.0)
        except (TypeError, ValueError):
            pass
        return radius
    body_items = list(posed.bodies.items())
    silk_items: Dict[str, list] = {}
    for k in order:
        parent = lab_specs[k][5]
        if parent in silk_items:
            continue
        silk_items[parent] = []
        try:
            for x1, y1, x2, y2, width in posed.silk.get(parent, ()):
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
        # Bare text rects: the halo is the loop's soft settlement force,
        # not finish dirt — the finish scores true ink overlap only.
        rects = {}
        for k in order:
            uuid, text, size, angle, _anchor, _parent = lab_specs[k][:6]
            node = positions[lab_rows[k]]
            rects[uuid] = label_rect_poly(
                text, float(node[0]), float(node[1]), angle, size, 0.0)
        return rects

    def _bounds_of(poly):
        try:
            return poly.bounds if poly is not None and not poly.is_empty else None
        except Exception:
            return None

    # Static per call: body/copper geometry never moves during the label
    # pass (labels are one-way), so measure bounds once instead of per
    # label per sweep. Silk strokes buffer on first touch into the shared
    # memo and reuse the cached poly for every later label/sweep in this call.
    _body_bboxes = [(bu, obs, _bounds_of(obs)) for bu, obs in body_items]
    _copper_bboxes = [(_bounds_of(obs), obs) for obs in posed.copper]

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
    try:
        _eject_start = {
            lab_specs[k][0]: (
                float(positions[lab_rows[k]][0]),
                float(positions[lab_rows[k]][1]))
            for k in order
        }
    except Exception:
        _eject_start = {}
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
                push = _eject_from(poly, obs, 1.0)
                if push is not None:
                    mx, my = mx + push[0], my + push[1]
            for obs in posed.copper:
                push = _eject_from(poly, obs, 1.0)
                if push is not None:
                    mx, my = mx + push[0], my + push[1]
            # Own-silk eject: side-tested stroke normals. Opposing sides of
            # an enclosing outline can still cancel here — that trap is
            # the search phase's job below (candidate testing cannot
            # cancel), so the eject stays dumb and cheap.
            for obs, nx, ny, sx, sy in silk_items.get(parent, ()):
                if nx is None or ny is None:
                    push = _eject_from(poly, obs, 1.0)
                else:
                    try:
                        cx, cy = poly.centroid.x, poly.centroid.y
                    except Exception:
                        continue
                    side = 1.0 if (cx - sx) * nx + (cy - sy) * ny >= 0.0 else -1.0
                    push = _eject_from(poly, obs, 1.0, side * nx, side * ny)
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
                push = _eject_from(pa, pb, 0.5)
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
                    lab_specs[k][4], _bound(k), _pang(k),
                )
            except Exception:
                continue
            try:
                _sx, _sy = _eject_start.get(uuid, (nx, ny))
                _tx, _ty = nx - _sx, ny - _sy
                _td = math.hypot(_tx, _ty)
                if _td > _DEPEN_EJECT_TRAVEL_CAP_MM > 0.0:
                    _f = _DEPEN_EJECT_TRAVEL_CAP_MM / _td
                    nx, ny = _sx + _tx * _f, _sy + _ty * _f
            except Exception:
                pass
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
    # (nearest-first, so labels settle instead of wandering).
    # The obstacle set here matches the eject above — own body excluded,
    # all copper (including the parent's own) and nearby-neighbor silk
    # included — so the two phases cannot disagree about what counts as
    # dirt. Both score bare text (halo is loop-only); the spiral evaluates
    # the label's own rect at each candidate; the silk loop must not rebind
    # that rect variable. Two sweeps max: the second
    # catches touches the first sweep's moves introduced. Labels with no
    # clean spot in-ellipse stay put and are logged (bound-limited, not an
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
            for _bb, _obs in _copper_bboxes:
                obstacles.append(_obs)
                oboxes.append(_bb)
            try:
                reach = _rk(k) + half_diag + 0.5
                for _silk_poly in _nearby_silk_obstacles(
                        anchor, reach, posed.silk, cache=posed.silk_memo):
                    obstacles.append(_silk_poly)
                    oboxes.append(_bounds_of(_silk_poly))
            except (ValueError, TypeError, AttributeError, IndexError):
                pass
            for ou, op in rects.items():
                if ou != uuid and op is not None and not op.is_empty:
                    obstacles.append(op)
                    oboxes.append(_bounds_of(op))
            try:
                _inside = _ellin(anchor, _bound(k), _pang(k))
                _cands = [
                    (s, a) for s, a in _score_spots(
                        poly, obstacles, anchor, _rk(k),
                        cover_poly=edge_poly, bboxes=oboxes,
                        inside=_inside)
                ]
                first_clean, best, best_area, cur_area = (
                    None, None, math.inf, None)
                for _s, _a in _cands:
                    if cur_area is None:
                        cur_area = _a
                    if _a < best_area - 1e-12:
                        best, best_area = _s, _a
                    if first_clean is None and _a <= _DEPEN_CLEAN_AREA_MM2:
                        first_clean = _s
                if cur_area is None:
                    cur_area = math.inf
                spot = first_clean
                if spot is None:
                    # Multi-start descent: the single best can sit in a
                    # strict local minimum (home straddling its own
                    # pads, every step worse), so one walk from it goes
                    # nowhere. Walk from several least-dirty spiral
                    # points instead — a walk starting adjacent to the
                    # slot descends into it. Distinct starts only
                    # (1 mm apart), cheapest first, current pos included.
                    _starts = []
                    _seed = _seed_pt(k)
                    if _seed is not None:
                        _starts.append(_seed)
                    for _s, _a in sorted(
                            _cands, key=lambda t: t[1]):
                        if all(math.hypot(_s[0] - _p[0], _s[1] - _p[1])
                               >= 1.0 for _p in _starts):
                            _starts.append(_s)
                        if len(_starts) >= 4:
                            break
                    # Nearest clean wins, not first found: a far pristine
                    # start must not beat a near clean one (labels sit
                    # next to their parts). Least-dirty still folds into
                    # the fallback below when nothing is clean.
                    _clean_spots = []
                    for _st in _starts:
                        try:
                            _ds, _da = _descend_to_clean(
                                poly, _st, obstacles, oboxes, anchor,
                                _bound(k), _pang(k), cover_poly=edge_poly)
                        except Exception:
                            continue
                        if _da < best_area - 1e-12:
                            best, best_area = _ds, _da
                        if _da <= _DEPEN_CLEAN_AREA_MM2:
                            _clean_spots.append(_ds)
                    if _clean_spots:
                        spot = min(
                            _clean_spots,
                            key=lambda _s: math.hypot(
                                _s[0] - anchor[0], _s[1] - anchor[1]))
                if spot is None:
                    if best is None or not (
                            best_area < cur_area - 1e-9):
                        logger.debug(
                            "label %s bound-limited (%.4f mm2)", uuid, cur_area)
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
                    (float(node[0]) + dx, float(node[1]) + dy), anchor,
                    _bound(k), _pang(k),
                )
            except Exception:
                continue
            if abs(nx - float(node[0])) > 1e-9 or abs(ny - float(node[1])) > 1e-9:
                positions[lab_rows[k], 0] = nx
                positions[lab_rows[k], 1] = ny
                rects[uuid] = label_rect_poly(
                    lab_specs[k][1], nx, ny, lab_specs[k][3],
                    lab_specs[k][2], 0.0)
                swept = True
        if swept:
            moved_any = True
        else:
            break
    return moved_any
