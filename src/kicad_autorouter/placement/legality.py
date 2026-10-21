"""Legality checking: geometry-based pass/fail on final placement positions.

Pure, deterministic, no prints, no function-attribute state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np
from shapely.geometry import Polygon

from ..board_model import BoardModel, Footprint
from .contact import _footprint_courtyard_polygon


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
    exempt_pairs: Tuple[Tuple[str, str], ...]  # ghost-locked overlaps seen but exempt


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

    return polys, pos, uuid_to_idx


def _check_pairwise_overlaps(
    polys: Dict[str, Polygon],
    model: BoardModel,
    overlap_tolerance_mm2: float,
) -> Tuple[List[Tuple[str, str, float]], List[Tuple[str, str]], float]:
    """Check all pairwise courtyard overlaps within regions.

    Returns:
        overlapping_pairs: list of (uuid_a, uuid_b, area_mm2) where area > tolerance
        exempt_pairs: list of (ghost_uuid, locked_uuid) that overlap but are exempt
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

    # Build region groups: only compare within same region
    region_groups: Dict[int, List[str]] = {}
    for uuid, region_idx in model.footprint_region.items():
        if uuid in polys:
            region_groups.setdefault(region_idx, []).append(uuid)
    for uuid, region_idx in model.footprint_region.items():
        if uuid in polys and uuid not in region_groups.get(region_idx, []):
            region_groups.setdefault(region_idx, []).append(uuid)

    for region_idx, uuids in region_groups.items():
        n = len(uuids)
        for i in range(n):
            ua = uuids[i]
            pa = polys.get(ua)
            if pa is None or pa.is_empty:
                continue
            type_a = fp_types.get(ua, "movable")
            for j in range(i + 1, n):
                ub = uuids[j]
                pb = polys.get(ub)
                if pb is None or pb.is_empty:
                    continue
                type_b = fp_types.get(ub, "movable")

                # Skip pairs where both are locked (neither can move)
                if type_a == "locked" and type_b == "locked":
                    continue

                # Ghost-locked pairs are exempt by design
                if (type_a == "ghost" and type_b == "locked") or (type_a == "locked" and type_b == "ghost"):
                    # Check if they actually overlap for audit
                    if not pa.is_empty and not pb.is_empty:
                        inter = pa.intersection(pb)
                        if not inter.is_empty and inter.area > 1e-12:
                            exempt_pairs.append(tuple(sorted((ua, ub))))
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

    # Build shapely polygons for each region
    from shapely.geometry import Polygon as ShapelyPolygon
    region_polys = []
    for reg in model.board_regions:
        if reg.polygon:
            region_polys.append(ShapelyPolygon([(p.x_mm, p.y_mm) for p in reg.polygon]))
        else:
            region_polys.append(ShapelyPolygon())

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
            # Straddling: max protruding vertex distance
            depth = 0.0
            from shapely.geometry import Point
            for vertex in poly.exterior.coords:
                pt = Point(vertex)
                if not region_poly.contains(pt):
                    d = pt.distance(region_poly.exterior)
                    if d > depth:
                        depth = d

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
    polys, _, _ = _collect_posed_polygons(model, deltas)

    # Check pairwise overlaps
    overlapping_pairs, exempt_pairs, max_pair_overlap = _check_pairwise_overlaps(
        polys, model, overlap_tolerance_mm2
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