"""Ghost (pseudo-part) builders for placement forces.

Ghosts are fixed anchors that participate in the placement simulation but
are never moved: a wall (courtyard-only obstacle that pushes parts away)
or an attractor (fixed point pulling specific refs via ``ghost_attract_N``
springs). See ``board_model.Footprint(ghost, ...)`` and
``tests/fixtures/ghost_test.kicad_pcb`` for the S-expression shape.
"""

from __future__ import annotations

import uuid as uuid_module
from typing import Optional, Sequence, Tuple

from .board_model import Footprint, Point


def _new_uuid() -> str:
    return uuid_module.uuid4().hex


def _rect_courtyard(cx: float, cy: float, hw: float, hh: float):
    """Closed 4-segment rect outline centered at (cx, cy), board coords."""
    corners = [
        Point(cx - hw, cy - hh),
        Point(cx + hw, cy - hh),
        Point(cx + hw, cy + hh),
        Point(cx - hw, cy + hh),
        Point(cx - hw, cy - hh),
    ]
    return tuple((corners[i], corners[i + 1]) for i in range(4))


def _validate_common(
    ref: str, x_mm: float, y_mm: float, w_mm: float, h_mm: float
) -> tuple[float, float]:
    if not ref or not str(ref).strip():
        raise ValueError("ghost ref must be non-empty")
    for name, val in (("x_mm", x_mm), ("y_mm", y_mm)):
        if not isinstance(val, (int, float)) or not __import__("math").isfinite(val):
            raise ValueError(f"ghost {name} must be finite, got {val!r}")
    if not isinstance(w_mm, (int, float)) or not __import__("math").isfinite(w_mm) or w_mm <= 0:
        raise ValueError(f"ghost width must be > 0, got {w_mm!r}")
    if not isinstance(h_mm, (int, float)) or not __import__("math").isfinite(h_mm) or h_mm <= 0:
        raise ValueError(f"ghost height must be > 0, got {h_mm!r}")
    return float(w_mm), float(h_mm)


def _validate_attractions(
    attractions: Sequence[Tuple[str, float]],
) -> Tuple[Tuple[str, float], ...]:
    out = []
    for item in attractions:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError(f"ghost attraction must be (target_ref, ka), got {item!r}")
        target_ref, ka = item
        if not target_ref or not str(target_ref).strip():
            raise ValueError(f"ghost attraction target must be non-empty, got {item!r}")
        try:
            ka_f = float(ka)
        except (TypeError, ValueError):
            raise ValueError(f"ghost attraction ka must be numeric, got {item!r}")
        if not __import__("math").isfinite(ka_f) or ka_f < 0:
            raise ValueError(f"ghost attraction ka must be >= 0, got {item!r}")
        out.append((str(target_ref), ka_f))
    return tuple(out)


def make_ghost(
    ref: str,
    x_mm: float,
    y_mm: float,
    w_mm: float,
    h_mm: float,
    *,
    attractions: Sequence[Tuple[str, float]] = (),
    uuid: Optional[str] = None,
    angle_deg: float = 0.0,
    footprint_id: str = "Ghost:GHOST",
    layer: str = "F.Cu",
) -> Footprint:
    """Build a generic ghost footprint (board-space courtyard, no pads).

    Args:
        ref: Reference like ``"GHOST_WALL_TOP"`` (need not be unique in the
            file, but creation callers should avoid colliding with real refs).
        x_mm / y_mm: ghost anchor in board millimeters.
        w_mm / h_mm: courtyard size in millimeters (full extents).
        attractions: sequence of ``(target_ref, ka)`` springs, ``ka >= 0``.
        uuid: stable identity; generated when omitted.
        angle_deg: footprint rotation (usually 0).
        footprint_id: library id written to the ``(footprint ...)`` head.
        layer: footprint copper layer (courtyard graphics layer is chosen
            separately at serialization via ``courtyard_layer``).
    """
    w_mm, h_mm = _validate_common(ref, x_mm, y_mm, w_mm, h_mm)
    parsed = _validate_attractions(tuple(attractions or ()))
    return Footprint(
        ref=str(ref),
        footprint_id=footprint_id,
        layer=layer,
        x_mm=float(x_mm),
        y_mm=float(y_mm),
        angle_deg=float(angle_deg),
        pads=(),
        courtyard=_rect_courtyard(float(x_mm), float(y_mm), w_mm / 2.0, h_mm / 2.0),
        locked=False,
        uuid=uuid if uuid else _new_uuid(),
        ghost=True,
        ghost_attractions=parsed,
    )


def make_ghost_wall(
    ref: str,
    x_mm: float,
    y_mm: float,
    w_mm: float,
    h_mm: float,
    *,
    uuid: Optional[str] = None,
    angle_deg: float = 0.0,
    footprint_id: str = "Ghost:Wall",
    layer: str = "F.Cu",
) -> Footprint:
    """Build a courtyard-only wall ghost (pushes parts away, no attraction)."""
    return make_ghost(
        ref,
        x_mm,
        y_mm,
        w_mm,
        h_mm,
        attractions=(),
        uuid=uuid,
        angle_deg=angle_deg,
        footprint_id=footprint_id,
        layer=layer,
    )


def make_ghost_attractor(
    ref: str,
    x_mm: float,
    y_mm: float,
    target_ref: str,
    ka: float,
    *,
    size_mm: float = 4.0,
    uuid: Optional[str] = None,
    angle_deg: float = 0.0,
    footprint_id: str = "Ghost:Attractor",
    layer: str = "F.Cu",
    extra_attractions: Sequence[Tuple[str, float]] = (),
) -> Footprint:
    """Build a small attractor ghost pulling ``target_ref`` with strength ``ka``.

    Args:
        size_mm: square courtyard edge (full extent); must be > 0.
        extra_attractions: additional ``(target_ref, ka)`` springs beyond the
            primary target (for combined pull shapes).
    """
    if not isinstance(size_mm, (int, float)) or not __import__("math").isfinite(size_mm) or size_mm <= 0:
        raise ValueError(f"ghost size_mm must be > 0, got {size_mm!r}")
    return make_ghost(
        ref,
        x_mm,
        y_mm,
        float(size_mm),
        float(size_mm),
        attractions=((target_ref, ka), *tuple(extra_attractions or ())),
        uuid=uuid,
        angle_deg=angle_deg,
        footprint_id=footprint_id,
        layer=layer,
    )
