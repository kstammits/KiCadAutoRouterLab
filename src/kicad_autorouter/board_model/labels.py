"""Silkscreen label geometry: board pose, readable snap, collision rect.

Pure helpers (no placement imports) shared by parsing pose
(``board_model.query.apply_deltas``), the dynamics collision body
(``placement.force`` via ``placement.region``'s shape cache), SVG rendering,
and ``io`` writeback. All angles in degrees, lengths in millimeters.
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

# Orbit tether stiffness (Hz, fixed): stiffness derives from the label's
# own mass (k = m*omega^2) so light labels get soft tethers. Explicit-Euler
# stability needs k/m << (2/dt)^2; 2 Hz at dt=1ms sits orders of
# magnitude inside that bound. Not a param — single spring strategy.
LABEL_TETHER_HZ = 2.0

# Ink density for the derived orbiter mass (kg/m^2 of text bbox): ink is
# nearly massless, so a few mm^2 of text lands at/near the mass floor —
# physically honest and floaty by construction (see tether stiffness).
LABEL_INK_DENSITY_KG_M2 = 0.1


def _norm360(angle_deg: float) -> float:
    """Normalize to [0, 360)."""
    a = angle_deg % 360.0
    if a < 0:
        a += 360.0
    return a


def label_board_angle(local_angle_deg: float, fp_angle_deg: float) -> float:
    """Board-frame orientation of a label riding on a footprint.

    Labels live in the footprint-local frame, so the board orientation is
    the footprint rotation plus the label's own local angle (same additive
    convention as pad angles, which store the absolute board orientation).
    """
    return _norm360(local_angle_deg + fp_angle_deg)


def snap_label_angle(board_angle_deg: float) -> float:
    """Snap a board-frame label angle to the nearest readable orientation.

    Candidates are 0/90/270 (180 upside-down is never chosen). Ties break
    toward 0/90 (upright reading). Keeps rotated parts' labels readable
    instead of upside-down. Always on — no inverted toggle.
    """
    a = _norm360(board_angle_deg)
    ranked = sorted(
        (0.0, 90.0, 270.0),
        key=lambda m: min(abs(a - m), 360.0 - abs(a - m)),
    )
    return ranked[0]


def snapped_local_angle(
    label_local_angle: float,
    fp_old_angle: float,
    fp_new_angle: float,
) -> float:
    """Label local angle after a footprint rotation by ``fp_new - fp_old``.

    Rigid follow first (label keeps its board orientation relative to the
    parent: local angle gains the delta), then the readable snap expressed
    back in the new local frame so ``io`` writeback and ``apply_deltas``
    agree exactly. Always snaps upright — no toggles.
    """
    # Board orientation after rotation: old board angle + delta.
    board = _norm360(label_board_angle(label_local_angle, fp_old_angle)
                      + (fp_new_angle - fp_old_angle))
    snapped_board = snap_label_angle(board)
    return _norm360(snapped_board - fp_new_angle)


def label_text_size_mm(text: str, size_mm: Tuple[float, float]) -> Tuple[float, float]:
    """Estimate rendered (width, height) of a label in millimeters.

    KiCad's stroke font averages ~0.6x the font height per glyph advance;
    height is the font height itself. Good enough for a clearance halo —
    this is a collision estimate, not typography.
    """
    w, h = size_mm
    try:
        n = len(text) if text else 1
    except TypeError:
        n = 1
    return (max(n, 1) * 0.6 * w, h)


def label_rect_corners(
    cx_mm: float,
    cy_mm: float,
    width_mm: float,
    height_mm: float,
    board_angle_deg: float,
) -> List[Tuple[float, float]]:
    """Four corners of a (w x h) rect centered at (cx, cy), rotated CCW.

    Board-frame rotation matching ``_to_board`` (local Y-down mapped with
    ``R(-angle)``); the rect just needs rigid consistency with the parent
    body, so any consistent convention works — this one matches pads.
    """
    rad = math.radians(-board_angle_deg)
    ca, sa = math.cos(rad), math.sin(rad)
    hw, hh = width_mm / 2.0, height_mm / 2.0
    return [
        (cx_mm + sx * ca - sy * sa, cy_mm + sx * sa + sy * ca)
        for sx, sy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))
    ]


def label_board_rect(
    text: str,
    cx_mm: float,
    cy_mm: float,
    board_angle_deg: float,
    size_mm: Tuple[float, float],
    halo_mm: float = 0.0,
) -> Optional[List[Tuple[float, float]]]:
    """Posed label clearance rect corners (board frame), or None if degenerate.

    ``halo_mm`` inflates the text bbox on all sides; the caller passes
    ``label_clearance_m`` (single source of truth).
    """
    w, h = label_text_size_mm(text, size_mm)
    if not (w > 0 and h > 0):
        return None
    hw = max(halo_mm, 0.0)
    return label_rect_corners(cx_mm, cy_mm, w + 2 * hw, h + 2 * hw, board_angle_deg)


def label_ink_mass_kg(
    text: str,
    size_mm: Tuple[float, float],
    density_kg_m2: float = LABEL_INK_DENSITY_KG_M2,
    floor_kg: float = 1e-9,
) -> float:
    """Derived orbiter mass: text-bbox area at ink density, floored.

    A few mm^2 of silkscreen ink at 0.1 kg/m^2 is ~1e-7..1e-6 g, so this
    lands at/near the dynamics mass floor — labels are light, and the
    mass-derived tether stiffness (k = m*omega^2) stays far inside the
    explicit-Euler stability bound. Never below ``floor_kg`` so F = ma
    can never divide by zero.
    """
    w, h = label_text_size_mm(text, size_mm)
    try:
        area_m2 = max(float(w) * float(h), 0.0) * 1e-6
    except (TypeError, ValueError):
        area_m2 = 0.0
    return max(density_kg_m2 * area_m2, floor_kg)


def label_tether_force_N(
    node_xy_mm: Tuple[float, float],
    target_xy_mm: Tuple[float, float],
    mass_kg: float,
    hz: float = LABEL_TETHER_HZ,
) -> Tuple[float, float]:
    """Spring tether force (newtons) pulling a label node toward its target.

    Stiffness derives from the label's own mass (k = m*(2*pi*hz)^2), so
    the float period is mass-independent and light labels stay stable at
    the 1 ms solver step. Pure function of board-millimeter inputs.
    """
    try:
        m = max(float(mass_kg), 1e-12)
        f = max(float(hz), 1e-6)
    except (TypeError, ValueError):
        return (0.0, 0.0)
    k = m * (2.0 * math.pi * f) ** 2  # N/m
    dx_m = (target_xy_mm[0] - node_xy_mm[0]) * 1e-3
    dy_m = (target_xy_mm[1] - node_xy_mm[1]) * 1e-3
    return (k * dx_m, k * dy_m)


def clamp_orbit_radius(
    node_xy_mm: Tuple[float, float],
    anchor_xy_mm: Tuple[float, float],
    radius_mm: float,
) -> Tuple[float, float]:
    """Hard-clamp a label node to ``radius_mm`` around its anchor (positional).

    The orbit backstop: the label can never leave the disc, so runs stay
    bounded even when contact shoves hard. Inside the disc this is the
    identity.
    """
    try:
        r = max(float(radius_mm), 0.0)
    except (TypeError, ValueError):
        return node_xy_mm
    dx = node_xy_mm[0] - anchor_xy_mm[0]
    dy = node_xy_mm[1] - anchor_xy_mm[1]
    d = math.hypot(dx, dy)
    if d <= r or d <= 0.0:
        return node_xy_mm
    s = r / d
    return (anchor_xy_mm[0] + dx * s, anchor_xy_mm[1] + dy * s)
