"""Silkscreen label geometry: board pose, readable snap, collision rect.

Pure helpers (no placement imports) shared by parsing pose
(``board_model.query.apply_deltas``), the dynamics collision body
(``placement.force`` via ``placement.region``'s shape cache), SVG rendering,
and ``io`` writeback. All angles in degrees, lengths in millimeters.
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

# Orbit tether stiffness (Hz, fixed): stiffness k = m*(2*pi*hz)^2 derives
# from the label's own mass so the float period is mass-independent and
# light labels stay stable at the 1 ms solver step. 40 Hz balances the
# mass-scaled wall law at ~2-3 mm displacement (walls otherwise win every
# tie and labels rim-seek to the clamp instead of holding near their
# part); k/m = (2*pi*40)^2 sits ~60x inside the explicit-Euler stability
# bound at dt=1ms. Not a param — single spring strategy.
LABEL_TETHER_HZ = 40.0

# Ink density for the derived orbiter mass (kg/m^2 of text bbox): ink is
# nearly massless, so a few mm^2 of text lands at/near the mass floor —
# physically honest and floaty by construction (see tether stiffness).
LABEL_INK_DENSITY_KG_M2 = 0.1

# Label halo as a fraction of font height: the orbit contact rect inflates
# the text bbox by font_height * this on all sides, so tiny refs get tiny
# halos (stay placeable on dense boards) and big refs get room. The label
# is a light bbox that springs out of the way of real part bodies — one
# fixed fraction, not a param; the DRC recount calibrates the value.
LABEL_HALO_FONT_FRACTION = 1.0


def label_halo_mm(size_mm: Tuple[float, float]) -> float:
    """Per-label contact halo (mm): font height times the fixed fraction.

    ``size_mm`` is the (width, height) font size; height is the second
    entry. Zero/degenerate sizes yield a zero halo (rect builders already
    skip those).
    """
    try:
        h = float(size_mm[1])
    except (TypeError, ValueError, IndexError):
        return 0.0
    return max(h, 0.0) * LABEL_HALO_FONT_FRACTION


def _norm360(angle_deg: float) -> float:
    """Normalize to [0, 360)."""
    a = angle_deg % 360.0
    if a < 0:
        a += 360.0
    return a


def label_board_angle(local_angle_deg: float, fp_angle_deg: float) -> float:
    """Board-frame orientation of a label riding on a footprint.

    KiCad renders a footprint text's stored angle literally: the parent
    rotation moves the text anchor but never the glyph orientation
    (verified by gerber export — a B-side label stored at 232.36 on a
    217.64 parent plots diagonally, and an F-side label stored at 90 on
    a 45 parent plots vertically). So the board angle is the stored
    local angle, normalized; ``fp_angle_deg`` is accepted for
    call-compatibility and ignored.
    """
    return _norm360(local_angle_deg)


def snap_label_angle(board_angle_deg: float) -> float:
    """Snap a board-frame label angle to the nearest readable orientation.

    Candidates are 0/90/180/270 — 180 is kept literally (bottom-side
    text with ``justify mirror`` reads correctly at 180, and the
    collision rect must match what DRC sees). Exact ties break toward
    0/90/270 (upright reading); a stored 180 itself always survives.
    Always on — no inverted toggle.
    """
    a = _norm360(board_angle_deg)
    ranked = sorted(
        (0.0, 90.0, 180.0, 270.0),
        key=lambda m: (
            min(abs(a - m), 360.0 - abs(a - m)),
            0 if m in (0.0, 90.0, 270.0) else 1,
            m,
        ),
    )
    return ranked[0]


def snapped_local_angle(
    label_local_angle: float,
    fp_old_angle: float,
    fp_new_angle: float,
) -> float:
    """Readable stored angle after a footprint rotation.

    Rendering is literal (the stored angle is the board angle), so a
    parent rotation never turns the glyphs — it only moves the anchor.
    Snapping therefore re-snaps the stored angle itself to the nearest
    readable board orientation (0/90/180/270; 180 survives literally for
    bottom-side mirrored text); the parent angles are accepted for
    call-compatibility and ignored. ``io`` writeback and
    ``apply_deltas`` agree exactly.
    """
    return snap_label_angle(_norm360(label_local_angle))


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


def label_slack_tether_force_N(
    node_xy_mm: Tuple[float, float],
    target_xy_mm: Tuple[float, float],
    mass_kg: float,
    hz: float = LABEL_TETHER_HZ,
    slack_m: float = 0.0,
) -> Tuple[float, float]:
    """Slack-leash tether: free float inside ``slack_m``, spring beyond.

    Within the slack radius the label feels nothing (contact owns the
    placement, friction freeze is the settle state); outside, a
    mass-proportional spring (k = m*(2*pi*hz)^2, mass-independent float
    period) acts on the excess ``(d - slack)``, so the force is
    continuous at the boundary. Pure function of board-millimeter inputs.
    """
    try:
        m = max(float(mass_kg), 1e-12)
        f = max(float(hz), 1e-6)
        slack = max(float(slack_m), 0.0)
    except (TypeError, ValueError):
        return (0.0, 0.0)
    try:
        dx = target_xy_mm[0] - node_xy_mm[0]
        dy = target_xy_mm[1] - node_xy_mm[1]
        d = math.hypot(dx, dy)
    except (TypeError, ValueError):
        return (0.0, 0.0)
    if not math.isfinite(d) or d <= slack * 1e3 or d <= 0.0:
        return (0.0, 0.0)
    k = m * (2.0 * math.pi * f) ** 2  # N/m
    excess_m = d * 1e-3 - slack
    return (k * excess_m * dx / d, k * excess_m * dy / d)


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
