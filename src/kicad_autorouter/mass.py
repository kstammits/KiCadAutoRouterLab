"""Footprint mass estimates for SI placement dynamics.

Resolution order for a footprint: exact ``footprint_id`` entry, then the
first case-insensitive keyword substring match (specific entries must precede
general ones in the file), then the area fallback::

    m = density_kg_m2 * courtyard_area_m2 + floor_kg

clamped to ``>= floor`` so ``F = ma`` can never divide by zero. The table at
``data/footprint_mass.json`` is user-editable; all ``mass_g`` values there
are rough guesses, good enough to give inertia the right order of magnitude.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TABLE_PATH = REPO_ROOT / "data" / "footprint_mass.json"


def load_mass_table(path: Optional[Path] = None) -> dict:
    """Load the mass table; missing/unreadable file yields empty table."""
    table_path = Path(path) if path is not None else DEFAULT_TABLE_PATH
    try:
        return json.loads(table_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def resolve_table_mass_g(
    footprint_id: str, table: Optional[dict] = None
) -> Optional[float]:
    """Look up a tabulated mass in grams; None when no entry matches."""
    table = table if table is not None else load_mass_table()
    fp_id = footprint_id or ""
    exact = (table.get("by_footprint_id") or {}).get(fp_id)
    if isinstance(exact, dict) and "mass_g" in exact:
        try:
            return float(exact["mass_g"])
        except (TypeError, ValueError):
            pass
    lowered = fp_id.lower()
    for entry in table.get("by_keyword") or ():
        match = str(entry.get("match", ""))
        if match and match.lower() in lowered:
            try:
                return float(entry.get("mass_g"))
            except (TypeError, ValueError):
                continue
    return None


def footprint_mass_kg(
    footprint_id: str,
    courtyard_area_m2: float,
    density_kg_m2: float = 3.0,
    floor_kg: float = 1e-9,
) -> float:
    """Effective footprint mass in kg (table hit or area fallback, floored)."""
    table_g = resolve_table_mass_g(footprint_id)
    if table_g is not None:
        return max(table_g / 1000.0, floor_kg)
    try:
        area = float(courtyard_area_m2)
    except (TypeError, ValueError):
        area = 0.0
    if not (area > 0.0):
        area = 0.0
    return max(density_kg_m2 * area + floor_kg, floor_kg)


def _pad_copper_corners(pad) -> List[Tuple[float, float]]:
    """Four corners of a pad's copper rect in board coordinates.

    Same orientation convention as the rest of board geometry: the size box
    rotated by R(-angle) (local Y-DOWN to board, as in ``_to_board`` and the
    SVG pad paint). Covers oval capsules along their axis (end discs fit
    inside the corner span) and over-covers roundrect corners slightly —
    fine for a mass fallback.
    """
    try:
        w, h = float(pad.size_mm[0]), float(pad.size_mm[1])
        cx, cy = float(pad.position.x_mm), float(pad.position.y_mm)
        ang = math.radians(-float(getattr(pad, "angle_deg", 0.0) or 0.0))
    except (TypeError, ValueError, AttributeError):
        return []
    ca, sa = math.cos(ang), math.sin(ang)
    hw, hh = w / 2.0, h / 2.0
    return [
        (cx + sx * ca - sy * sa, cy + sx * sa + sy * ca)
        for sx, sy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))
    ]


def _convex_hull_area(points: List[Tuple[float, float]]) -> float:
    """Shoelace area of the convex hull (monotone chain, no shapely needed)."""
    pts = sorted(set(points))
    if len(pts) < 3:
        return 0.0

    def _cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and _cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and _cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = lower[:-1] + upper[:-1]
    return abs(sum(
        hull[i][0] * hull[(i + 1) % len(hull)][1]
        - hull[(i + 1) % len(hull)][0] * hull[i][1]
        for i in range(len(hull))
    )) / 2.0


def courtyard_bbox_area_m2(footprint) -> float:
    """Bounding-box area of a footprint's courtyard segments, in m².

    Floored by the pad-copper convex-hull area: files whose courtyard is
    drafting debris (e.g. pin-marker dots with no body outline) report a
    smaller box than the pads themselves, which would drop the area mass
    fallback to the floor and leave the part hyper-mobile. The hull is
    computed in file coordinates from rotated pad corners, so — unlike an
    axis-aligned box — it is rotation-invariant: mass depends on copper
    size, never on placement angle, and stays fixed for the run. A real
    courtyard always encloses the pads, so the max() is exact for honest
    files and a sane body-scale fallback for degenerate ones.
    """
    try:
        xs = [p.x_mm for seg in (footprint.courtyard or ()) for p in seg]
        ys = [p.y_mm for seg in (footprint.courtyard or ()) for p in seg]
    except (TypeError, AttributeError):
        xs, ys = [], []
    seg_area = 0.0
    if xs and ys:
        seg_area = max(max(xs) - min(xs), 0.0) * max(max(ys) - min(ys), 0.0)
    corners: List[Tuple[float, float]] = []
    try:
        pads = list(getattr(footprint, "pads", None) or ())
    except (TypeError, AttributeError):
        pads = []
    for pad in pads:
        corners.extend(_pad_copper_corners(pad))
    hull_area = _convex_hull_area(corners)
    return max(seg_area, hull_area) * 1e-6
