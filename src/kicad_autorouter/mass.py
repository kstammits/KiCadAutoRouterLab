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
from functools import lru_cache
from pathlib import Path
from typing import Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TABLE_PATH = REPO_ROOT / "data" / "footprint_mass.json"


def load_mass_table(path: Optional[Path] = None) -> dict:
    """Load the mass table; missing/unreadable file yields empty table."""
    table_path = Path(path) if path is not None else DEFAULT_TABLE_PATH
    try:
        return json.loads(table_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def mass_defaults(table: Optional[dict] = None) -> Tuple[float, float]:
    """Return ``(density_kg_m2, floor_kg)`` from the table ``defaults``."""
    defaults = (table or {}).get("defaults", {})
    density = float(defaults.get("areal_density_kg_m2", 3.0))
    floor_g = float(defaults.get("mass_floor_g", 0.001))
    return density, floor_g / 1000.0


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


def courtyard_bbox_area_m2(footprint) -> float:
    """Bounding-box area of a footprint's courtyard segments, in m²."""
    try:
        xs = [p.x_mm for seg in (footprint.courtyard or ()) for p in seg]
        ys = [p.y_mm for seg in (footprint.courtyard or ()) for p in seg]
    except (TypeError, AttributeError):
        return 0.0
    if not xs or not ys:
        return 0.0
    return max(max(xs) - min(xs), 0.0) * max(max(ys) - min(ys), 0.0) * 1e-6


@lru_cache(maxsize=1)
def cached_table(path_str: str = "") -> dict:
    """Process-wide cached table (path override mostly for tests)."""
    return load_mass_table(Path(path_str) if path_str else None)
