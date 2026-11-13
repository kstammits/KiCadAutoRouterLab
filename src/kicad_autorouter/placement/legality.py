"""Legality checking: geometry-based pass/fail on final placement positions.

Pure, deterministic, no prints, no function-attribute state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
from shapely import STRtree
from shapely import contains as _shapely_contains
from shapely import distance as _shapely_distance
from shapely import points as _shapely_points
from shapely.geometry import Polygon
from shapely.ops import unary_union

from ..board_model import BoardModel, Footprint
from .contact import (
    _footprint_courtyard_polygon,
    pad_copper_polygon,
    pad_present_on_layer,
)


@dataclass(frozen=True)
class LegalityReport:
    """Result of a legality check on final placement positions.

    All measurements in board millimeters (mm / mm^2).
    """

    is_legal: bool
    max_pair_overlap_mm2: float
    overlapping_pairs: Tuple[Tuple[str, str, float], ...]  # (uuid_a, uuid_b, area_mm2), sorted
    max_wall_penetration_mm: float
    wall_violations: Tuple[Tuple[str, float], ...]  # (uuid, depth_mm), sorted
    exempt_pairs: Tuple[Tuple[str, str], ...]  # ghost-involved overlaps seen but exempt


def _get_footprint_type(fp: Footprint) -> str:
    """Classify footprint type for legality purposes."""
    if fp.ghost:
        return "ghost"
    if fp.locked:
        return "locked"
    return "movable"


def _build_posed_courtyard(
    fp: Footprint,
    pad_positions: np.ndarray,
    pad_indices: List[int],
) -> Polygon:
    """Build the posed courtyard polygon for a footprint."""
    if fp.ghost:
        # Ghosts have no pads; their courtyard is already in board coords.
        if not fp.courtyard:
            return Polygon()
        poly = _footprint_courtyard_polygon(fp)
        return poly if poly is not None and not poly.is_empty else Polygon()
    if not pad_indices:
        return Polygon()
    return _footprint_courtyard_polygon(fp, pad_positions=pad_positions, pad_indices=pad_indices)


def _collect_posed_polygons(
    model: BoardModel,
    deltas: Dict[str, Tuple[float, float, float]],
) -> Tuple[Dict[str, Polygon], np.ndarray, Dict[str, List[int]]]:
    """Build courtyard polygons at final positions after applying deltas.

    Returns:
        uuid -> polygon (empty polygon if no courtyard)
        pad_positions array (n_pads, 2)
        uuid -> pad_indices list
        moved model (deltas applied) for pose-correct pad geometry
    """
    from ..board_model import apply_deltas
    from .model import _collect_pad_nodes

    moved = apply_deltas(model, deltas)
    pads, _, uuid_to_idx, _, _ = _collect_pad_nodes(moved)
    pos = np.array([(p.position.x_mm, p.position.y_mm) for p in pads], dtype=np.float64)

    polys: Dict[str, Polygon] = {}
    for fp in moved.footprints:
        if not fp.uuid:
            continue
        pad_idx = uuid_to_idx.get(fp.uuid, [])
        polys[fp.uuid] = _build_posed_courtyard(fp, pos, pad_idx)
    for fp in moved.ghost_footprints:
        if not fp.uuid:
            continue
        pad_idx = uuid_to_idx.get(fp.uuid, [])
        polys[fp.uuid] = _build_posed_courtyard(fp, pos, pad_idx)

    return polys, pos, uuid_to_idx, moved


def _tht_pin_union(fp: Footprint):
    """Union of a footprint's through-hole pin copper (posed), or None.

    Built once per legality check instead of once per cross-layer pair:
    for THT pins, layer presence depends only on whether the far side is
    a copper layer, so one union serves every pair (same construction as
    the per-pair build it replaces).
    """
    pins = [
        pad_copper_polygon(p)
        for p in fp.pads
        if p.is_through_hole
    ]
    if not pins:
        return None
    union = unary_union(pins)
    return union if not union.is_empty else None


def _cross_layer_pin_overlap(
    fa: Footprint,
    fb: Footprint,
    polys: Dict[str, Polygon],
    copper_layers,
    pin_unions: Optional[Dict[str, object]] = None,
) -> float:
    """Worst THT-pin-vs-opposite-courtyard overlap area for a cross-layer pair.

    Bodies on opposite sides stack freely; only through-hole copper
    protruding onto the shared physical layer counts (true pad shape via
    :func:`pad_copper_polygon`, not a disc). SMD-only pairs have no pins on
    the far side and always return 0.0.
    """
    best = 0.0
    cu = set(copper_layers or ())
    for fp_self, other_uuid, other_layer in (
        (fa, fb.uuid, fb.layer),
        (fb, fa.uuid, fa.layer),
    ):
        other_poly = polys.get(other_uuid)
        if other_poly is None or other_poly.is_empty:
            continue
        if other_layer not in cu:
            # No THT pin is present on a non-copper far side: same empty
            # result as filtering every pin out per pair.
            continue
        if pin_unions is not None and fp_self.uuid in pin_unions:
            pins = pin_unions[fp_self.uuid]
            if pins is None or pins.is_empty:
                continue
            inter = pins.intersection(other_poly)
        else:
            pins = [
                pad_copper_polygon(p)
                for p in fp_self.pads
                if p.is_through_hole
                and pad_present_on_layer(p, fp_self.layer, other_layer, copper_layers)
            ]
            if not pins:
                continue
            inter = unary_union(pins).intersection(other_poly)
        if not inter.is_empty:
            best = max(best, float(inter.area))
    return best


def _check_pairwise_overlaps(
    polys: Dict[str, Polygon],
    model: BoardModel,
    overlap_tolerance_mm2: float,
    moved: BoardModel,
) -> Tuple[List[Tuple[str, str, float]], List[Tuple[str, str]], float]:
    """Check all pairwise courtyard overlaps within regions.

    Same-layer pairs use courtyard-vs-courtyard as before. Cross-layer pairs
    of real footprints use pin-vs-opposite-courtyard (bodies stack, THT pins
    don't); pairs involving ghosts are exempt (audit only). Layers come from
    the posed (moved) model.

    Returns:
        overlapping_pairs: list of (uuid_a, uuid_b, area_mm2) where area > tolerance
        exempt_pairs: list of (uuid_a, uuid_b) ghost-involved overlaps seen but exempt
        max_overlap: maximum overlap area found
    """
    overlapping_pairs = []
    exempt_pairs = []
    max_overlap = 0.0

    # Build fp uuid -> type map
    fp_types: Dict[str, str] = {}
    for fp in model.footprints:
        if fp.uuid:
            fp_types[fp.uuid] = _get_footprint_type(fp)
    for fp in model.ghost_footprints:
        if fp.uuid:
            fp_types[fp.uuid] = _get_footprint_type(fp)

    # Posed footprints + layers (deltas applied; uuids/regions unchanged).
    posed: Dict[str, Footprint] = {}
    layers: Dict[str, str] = {}
    for fp in moved.footprints:
        if fp.uuid:
            posed[fp.uuid] = fp
            layers[fp.uuid] = fp.layer or ""
    for fp in moved.ghost_footprints:
        if fp.uuid:
            posed[fp.uuid] = fp
            layers[fp.uuid] = getattr(fp, "layer", "") or ""

    # Build region groups: only compare within same region
    region_groups: Dict[int, List[str]] = {}
    for uuid, region_idx in model.footprint_region.items():
        if uuid in polys:
            region_groups.setdefault(region_idx, []).append(uuid)
    for uuid, region_idx in model.footprint_region.items():
        if uuid in polys and uuid not in region_groups.get(region_idx, []):
            region_groups.setdefault(region_idx, []).append(uuid)

    # Per-footprint THT pin unions, built once (not once per pair).
    pin_unions: Dict[str, object] = {}
    for fp in list(moved.footprints) + list(moved.ghost_footprints):
        if fp.uuid and fp.uuid not in pin_unions:
            pin_unions[fp.uuid] = _tht_pin_union(fp)

    for region_idx, uuids in region_groups.items():
        # Spatial index over courtyard polygons: only envelope-overlapping
        # pairs can intersect, so O(n^2) shapely intersections drop to
        # O(candidate pairs). Pruned pairs provably have empty
        # intersections (area 0, never above a non-negative tolerance),
        # and the per-pair tests below are unchanged. THT pins may extend
        # beyond their own courtyard, so cross-layer candidates also query
        # with the pin union envelope.
        geoms = [polys[u] for u in uuids]
        tree = STRtree(geoms)
        for i, ua in enumerate(uuids):
            pa = polys.get(ua)
            if pa is None or pa.is_empty:
                continue
            type_a = fp_types.get(ua, "movable")
            cand = set(tree.query(pa).tolist())
            pu = pin_unions.get(ua)
            if pu is not None and not pu.is_empty:
                cand.update(tree.query(pu).tolist())
            for j in sorted(cand):
                if j <= i:
                    continue
                ub = uuids[j]
                pb = polys.get(ub)
                if pb is None or pb.is_empty:
                    continue
                type_b = fp_types.get(ub, "movable")

                # Skip pairs where both are locked (neither can move)
                if type_a == "locked" and type_b == "locked":
                    continue

                # Ghost-involved pairs are exempt by design: ghosts are
                # placement-only pseudo-parts (KiCad DRC strips them), so
                # an attractor square sitting on its own target is not a
                # real collision. Recorded for audit, never illegal.
                if type_a == "ghost" or type_b == "ghost":
                    if not pa.is_empty and not pb.is_empty:
                        inter = pa.intersection(pb)
                        if not inter.is_empty and inter.area > 1e-12:
                            exempt_pairs.append(tuple(sorted((ua, ub))))
                    continue

                # Cross-layer real footprints: bodies stack freely, so the
                # courtyard test is replaced by THT-pin-vs-opposite-body.
                # Ghosts never reach this branch (exempt above).
                la = layers.get(ua, "")
                lb = layers.get(ub, "")
                if la and lb and la != lb and type_a != "ghost" and type_b != "ghost":
                    fa = posed.get(ua)
                    fb = posed.get(ub)
                    area = _cross_layer_pin_overlap(
                        fa, fb, polys, model.copper_layers, pin_unions,
                    ) if fa is not None and fb is not None else 0.0
                    if area > overlap_tolerance_mm2:
                        overlapping_pairs.append((ua, ub, round(area, 6)))
                        if area > max_overlap:
                            max_overlap = area
                    continue

                # Check overlap
                if not pa.is_empty and not pb.is_empty:
                    inter = pa.intersection(pb)
                    if not inter.is_empty:
                        area = inter.area
                        if area > overlap_tolerance_mm2:
                            overlapping_pairs.append((ua, ub, round(area, 6)))
                            if area > max_overlap:
                                max_overlap = area

    # Deterministic sorting
    overlapping_pairs.sort(key=lambda x: (x[0], x[1]))
    exempt_pairs.sort()

    return overlapping_pairs, exempt_pairs, max_overlap


def _check_wall_violations(
    polys: Dict[str, Polygon],
    model: BoardModel,
    wall_tolerance_mm: float,
) -> Tuple[List[Tuple[str, float]], float]:
    """Check board-wall penetration for movable footprints.

    Returns:
        wall_violations: list of (uuid, depth_mm) where depth > tolerance
        max_penetration: maximum penetration depth found
    """
    wall_violations = []
    max_penetration = 0.0

    if not model.board_regions:
        return wall_violations, max_penetration

    # Build shapely polygons for each region, plus the static outer ring
    # built once instead of per footprint (identical values).
    from shapely.geometry import Polygon as ShapelyPolygon
    region_polys = []
    for reg in model.board_regions:
        if reg.polygon:
            region_polys.append(ShapelyPolygon([(p.x_mm, p.y_mm) for p in reg.polygon]))
        else:
            region_polys.append(ShapelyPolygon())
    region_rings = [
        (p.exterior if not p.is_empty else None) for p in region_polys
    ]

    # Check each movable footprint
    for fp in model.footprints:
        if not fp.uuid or fp.locked or fp.ghost:
            continue
        if fp.uuid not in polys:
            continue
        poly = polys[fp.uuid]
        if poly.is_empty:
            continue

        region_idx = model.footprint_region.get(fp.uuid)
        if region_idx is None or region_idx < 0 or region_idx >= len(region_polys):
            continue  # cannot judge

        region_poly = region_polys[region_idx]
        if region_poly.is_empty:
            continue

        # Check coverage
        if region_poly.covers(poly):
            continue  # fully inside

        if poly.disjoint(region_poly):
            # Completely outside board
            # Distance to region as violation depth
            depth = poly.distance(region_poly)
        else:
            # Straddling: max protruding vertex distance, measured to the
            # outer ring over the vertices lying outside the region. Two
            # vectorized GEOS calls instead of one Point + contains +
            # distance per vertex (same per-point ops).
            depth = 0.0
            try:
                _coords = np.asarray(poly.exterior.coords, dtype=np.float64)
                _pts = _shapely_points(_coords)
                _outside = ~np.asarray(
                    _shapely_contains(region_poly, _pts), dtype=bool
                )
                if _outside.any():
                    _d = np.asarray(
                        _shapely_distance(
                            _pts[_outside], region_rings[region_idx]
                        ),
                        dtype=np.float64,
                    )
                    _finite = _d[np.isfinite(_d)]
                    if _finite.size:
                        depth = float(_finite.max())
            except (ValueError, TypeError, AttributeError, IndexError):
                pass

        if depth > wall_tolerance_mm:
            wall_violations.append((fp.uuid, round(depth, 6)))
            if depth > max_penetration:
                max_penetration = depth

    wall_violations.sort(key=lambda x: x[0])
    return wall_violations, max_penetration


def check_legality(
    model: BoardModel,
    deltas: Dict[str, Tuple[float, float, float]],
    *,
    overlap_tolerance_mm2: float = 0.1,
    wall_tolerance_mm: float = 0.1,
) -> LegalityReport:
    """Check legality of final placement positions.

    Args:
        model: Board model with footprints, regions, and locked/ghost info
        deltas: Final deltas {uuid: (dx_mm, dy_mm, dangle_deg)}
        overlap_tolerance_mm2: Courtyard overlap area (mm^2) above which a pair is illegal
        wall_tolerance_mm: Board-wall protrusion depth (mm) above which a part is illegal

    Returns:
        LegalityReport with all findings
    """
    # Build posed courtyard polygons
    polys, _, _, moved = _collect_posed_polygons(model, deltas)

    # Check pairwise overlaps
    overlapping_pairs, exempt_pairs, max_pair_overlap = _check_pairwise_overlaps(
        polys, model, overlap_tolerance_mm2, moved
    )

    # Check wall violations
    wall_violations, max_wall_penetration = _check_wall_violations(
        polys, model, wall_tolerance_mm
    )

    # Overall legality
    is_legal = len(overlapping_pairs) == 0 and len(wall_violations) == 0

    return LegalityReport(
        is_legal=is_legal,
        max_pair_overlap_mm2=max_pair_overlap,
        overlapping_pairs=tuple(overlapping_pairs),
        max_wall_penetration_mm=max_wall_penetration,
        wall_violations=tuple(wall_violations),
        exempt_pairs=tuple(exempt_pairs),
    )
