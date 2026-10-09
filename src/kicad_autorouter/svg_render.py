"""Render a :class:`~kicad_autorouter.board_model.BoardModel` as an SVG string.

Pure Python (no ``pcbnew`` import): the workflow UI server calls this to build
periodic board snapshots for in-browser review while routing runs. Coordinates
are millimeters, used directly as SVG user units; the browser scales via the
``viewBox``. Draw order matches KiCad's: outline, zones, courtyards, tracks,
vias, pads, reference text.
"""

from __future__ import annotations

import html
import math
from typing import Dict, List, Optional, Set, Tuple

from .board_model import BoardModel, Point, apply_deltas
from .drc import DEFAULT_IGNORED_TYPES
from .placement import PlacementProposal

# Dark-theme palette (matches ui/index.html).
OUTLINE = "#e8edf2"       # board outline
COURTYARD = "#5b6b7a"     # footprint courtyards
REF_TEXT = "#9fb0c0"      # reference designators
NO_NET = "#8a93a0"        # unconnected copper
ZONE_FILL = "#4d7ea8"     # regular copper zones
KEEPOUT_FILL = "#e06c75"  # keepout zones
HOLE_BG = "#0b0e12"  # via holes (matches the viewer panel background)
PAD_STROKE = "#0e1216"
GHOST_COURTYARD = "#3d4a56"  # original position of moved parts (ghost overlay)
GHOST_FOOTPRINT = "#a85cf5"  # ghost/pseudo component color (purple)
MOVE_ARROW = "#e0a458"  # old -> new displacement arrows

# DRC violation colors
DRC_ERROR_COLOR = "#e74c3c"     # red for errors
DRC_WARNING_COLOR = "#f39c12"   # orange for warnings

# Footprint state colors
FP_LOCKED_COLOR = "#e06c75"    # KiCad locked - red
FP_PINNED_COLOR = "#e0a458"    # User pinned - orange
FP_SELECTED_COLOR = "#7ee0a0"  # Selected for move - green
FP_DEFAULT_COLOR = COURTYARD   # Default unlocked - gray-blue
FP_GHOST_COLOR = GHOST_FOOTPRINT  # Ghost components - purple


def _fmt(v: float) -> str:
    return f"{v:.3f}"


def net_colors(model: BoardModel) -> Dict[str, str]:
    """Deterministic per-net colors; hues step by the golden angle."""
    names = sorted(name for name in model.nets if name)
    return {
        name: f"hsl({(i * 137.508) % 360:.0f}, 75%, 62%)"
        for i, name in enumerate(names)
    }


def _bounds(model: BoardModel) -> Tuple[float, float, float, float]:
    """(minx, miny, width, height) of all geometry plus a 2 mm margin."""
    xs: List[float] = []
    ys: List[float] = []

    def add(p: Point) -> None:
        xs.append(p.x_mm)
        ys.append(p.y_mm)

    for a, b in model.edge_cuts:
        add(a)
        add(b)
    for arc in model.edge_arcs:
        add(arc.start)
        add(arc.mid)
        add(arc.end)
    for zone in model.zones:
        for p in zone.polygon:
            add(p)
    for fp in model.footprints:
        for a, b in fp.courtyard:
            add(a)
            add(b)
        for pad in fp.pads:
            add(pad.position)
    for t in model.tracks:
        add(t.start)
        add(t.end)
    for v in model.vias:
        add(v.position)
    if not xs or not ys:
        return (0.0, 0.0, 10.0, 10.0)
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    margin = 2.0
    return (minx - margin, miny - margin, (maxx - minx) + 2 * margin, (maxy - miny) + 2 * margin)


def _edge_cuts_svg(model: BoardModel) -> List[str]:
    out = [f'<g stroke="{OUTLINE}" stroke-width="0.3" fill="none">']
    for a, b in model.edge_cuts:
        out.append(
            f'<line x1="{_fmt(a.x_mm)}" y1="{_fmt(a.y_mm)}" '
            f'x2="{_fmt(b.x_mm)}" y2="{_fmt(b.y_mm)}"/>'
        )
    for arc in model.edge_arcs:
        # A quadratic Bezier through the mid point approximates the KiCad arc.
        out.append(
            f'<path d="M {_fmt(arc.start.x_mm)} {_fmt(arc.start.y_mm)} '
            f'Q {_fmt(arc.mid.x_mm)} {_fmt(arc.mid.y_mm)} '
            f'{_fmt(arc.end.x_mm)} {_fmt(arc.end.y_mm)}"/>'
        )
    out.append("</g>")
    return out


def _zones_svg(model: BoardModel) -> List[str]:
    keepout_uuids = {z.uuid for z in model.keepout_zones}
    out: List[str] = []
    for layer in ("F.Cu", "B.Cu"):
        layer_zones = [z for z in model.zones if layer in z.layers]
        if not layer_zones:
            continue
        opacity = "0.18" if layer == "F.Cu" else "0.12"
        out.append(f'<g id="layer-{layer}-zones">')
        for zone in layer_zones:
            if len(zone.polygon) < 3:
                continue
            is_keepout = zone.uuid in keepout_uuids
            fill = KEEPOUT_FILL if is_keepout else ZONE_FILL
            dash = ' stroke-dasharray="1.5 1"' if is_keepout else ""
            pts = " ".join(f"{_fmt(p.x_mm)} {_fmt(p.y_mm)}" for p in zone.polygon)
            out.append(
                f'<polygon points="{pts}" fill="{fill}" fill-opacity="{opacity}" '
                f'stroke="{fill}" stroke-width="0.2"{dash}/>'
            )
        out.append("</g>")
    return out


def _footprint_courtyard_svg(
    fp,
    is_locked: bool,
    is_pinned: bool,
    is_selected: bool,
    is_movable: bool,
) -> str:
    """Generate courtyard lines for a single footprint with state-based styling."""
    if is_locked:
        stroke = FP_LOCKED_COLOR
        extras = ' stroke-width="0.1" stroke-dasharray="2 2" opacity="0.8"'
    elif is_pinned:
        stroke = FP_PINNED_COLOR
        extras = ' stroke-width="0.1" stroke-dasharray="4 2" opacity="0.8"'
    elif is_selected:
        stroke = FP_SELECTED_COLOR
        extras = ' stroke-width="0.2" opacity="0.8"'
    elif is_movable:
        stroke = FP_SELECTED_COLOR
        extras = ' stroke-width="0.15" opacity="0.6"'
    else:
        stroke = FP_DEFAULT_COLOR
        extras = ' stroke-width="0.1" opacity="0.8"'
    lines = []
    for a, b in fp.courtyard:
        lines.append(
            f'<line x1="{_fmt(a.x_mm)}" y1="{_fmt(a.y_mm)}" '
            f'x2="{_fmt(b.x_mm)}" y2="{_fmt(b.y_mm)}" '
            f'stroke="{stroke}"{extras}/>'
        )
    return "\n".join(lines)


def _footprint_pads_svg(
    fp,
    colors: Dict[str, str],
    flip: bool,
    fp_angle_deg: float,
) -> str:
    """Generate pad elements for a single footprint."""
    out = []
    for pad in fp.pads:
        color = colors.get(pad.net_name or "", NO_NET)
        px, py = pad.position.x_mm, pad.position.y_mm
        x, y = _fmt(px), _fmt(py)
        w, h = pad.size_mm

        # Pad angle is used directly: the `.kicad_pcb` file stores each pad's
        # `(at ...)` angle at its effective board orientation (pcbnew rewrites
        # pad angles when the footprint rotates — verified 2026-10-08: Q1/R3
        # on B.Cu store 90 with fp angle 90 and pcbnew reports effective 90;
        # rotating the footprint to 0 rewrites the pads to 0). Combining it
        # with fp_angle again would double-count the rotation. (Symmetric
        # square/circular pads may store a stale 0, e.g. D7, which is
        # visually identical.)
        # `flip`/`fp_angle_deg` are retained in the signature for callers.
        angle = pad.angle_deg
        rot = f' transform="rotate({_fmt(angle)} {x} {y})"' if angle else ""
        if pad.shape == "circle":
            out.append(
                f'<circle cx="{x}" cy="{y}" r="{_fmt(w / 2.0)}" '
                f'fill="{color}" stroke="{PAD_STROKE}" stroke-width="0.1"/>'
            )
        else:
            out.append(
                f'<rect x="{_fmt(px - w / 2.0)}" y="{_fmt(py - h / 2.0)}" '
                f'width="{_fmt(w)}" height="{_fmt(h)}" fill="{color}" '
                f'stroke="{PAD_STROKE}" stroke-width="0.1"{rot}/>'
            )
    return "\n".join(out)


def _courtyard_centroid(fp) -> tuple[float, float]:
    """Compute the centroid of a footprint's courtyard. Returns (x, y) in board coords.
    
    Falls back to footprint origin (fp.x_mm, fp.y_mm) if no courtyard exists.
    """
    if not fp.courtyard:
        return fp.x_mm, fp.y_mm
    
    # Collect all unique vertices from courtyard segments
    vertices = []
    for a, b in fp.courtyard:
        vertices.append((a.x_mm, a.y_mm))
        vertices.append((b.x_mm, b.y_mm))
    
    # Compute centroid
    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    return sum(xs) / len(xs), sum(ys) / len(ys)


def _footprint_ref_svg(fp) -> str:
    """Generate reference text for a single footprint at courtyard centroid."""
    label = fp.ref or (fp.uuid[:8] if fp.uuid else "")
    if not label:
        return ""
    cx, cy = _courtyard_centroid(fp)
    return (
        f'<text x="{_fmt(cx)}" y="{_fmt(cy - 0.5)}" '
        f'fill="{REF_TEXT}" font-family="monospace" font-size="1">'
        f"{html.escape(label)}</text>"
    )


def _footprints_svg(
    model: BoardModel,
    colors: Dict[str, str],
    locked_uuids: Set[str],
    pinned_uuids: Set[str],
    selected_uuids: Set[str],
) -> List[str]:
    """Generate all footprint elements (courtyards, pads, refs) grouped by footprint.

    Footprints are always drawn at their model coordinates. When a placement
    proposal is active the caller passes ``apply_deltas(model, deltas)`` as
    ``model``, so the preview shows the exact post-Accept positions (same
    pivot and rotation direction as the writeback in ``io``) instead of a
    group-transform approximation.
    """
    out = []
    for fp in model.footprints:
        is_locked = fp.uuid in locked_uuids
        is_pinned = fp.uuid in pinned_uuids
        is_selected = fp.uuid in selected_uuids
        is_movable = fp.uuid and not fp.locked and fp.uuid not in pinned_uuids

        flip = fp.layer.startswith("B")

        # Build footprint group with state classes and data attribute
        cls_parts = ["footprint"]
        if is_locked:
            cls_parts.append("fp-locked")
        if is_pinned:
            cls_parts.append("fp-pinned")
        if is_selected:
            cls_parts.append("fp-selected")
        if is_movable and not is_selected:
            cls_parts.append("fp-movable")
        cls = " ".join(cls_parts)

        fp_group = [
            f'<g class="{cls}" data-fp-uuid="{fp.uuid}">',
            _footprint_courtyard_svg(fp, is_locked, is_pinned, is_selected, is_movable),
            _footprint_pads_svg(fp, colors, flip, fp.angle_deg),
            _footprint_ref_svg(fp),
            "</g>",
        ]
        out.append("\n".join(fp_group))
    return out


def _ghost_footprints_svg(
    model: BoardModel,
    colors: Dict[str, str],
) -> List[str]:
    """Generate ghost footprint elements (courtyards, pads, refs) for pseudo-components.
    
    Ghost footprints are rendered with a distinct purple color, dashed style,
    and semi-transparency to distinguish them from real footprints.
    """
    out = []
    if not model.ghost_footprints:
        return out
    
    out.append(
        f'<g stroke="{GHOST_FOOTPRINT}" stroke-width="0.15" '
        f'stroke-dasharray="4 3" opacity="0.7" fill="none">'
    )
    for fp in model.ghost_footprints:
        # Courtyard lines
        for a, b in fp.courtyard:
            out.append(
                f'<line x1="{_fmt(a.x_mm)}" y1="{_fmt(a.y_mm)}" '
                f'x2="{_fmt(b.x_mm)}" y2="{_fmt(b.y_mm)}"/>'
            )
        # Pads (smaller, ghost color)
        for pad in fp.pads:
            px, py = pad.position.x_mm, pad.position.y_mm
            x, y = _fmt(px), _fmt(py)
            w, h = pad.size_mm
            # Pad angle used directly (same semantics as real footprints:
            # file stores the effective board orientation).
            angle = pad.angle_deg
            rot = f' transform="rotate({_fmt(angle)} {x} {y})"' if angle else ""
            if pad.shape == "circle":
                r = w / 2.0
                out.append(
                    f'<circle cx="{x}" cy="{y}" r="{_fmt(r * 0.7)}" '
                    f'fill="{GHOST_FOOTPRINT}" fill-opacity="0.4" '
                    f'stroke="{GHOST_FOOTPRINT}" stroke-width="0.1"{rot}/>'
                )
            else:
                out.append(
                    f'<rect x="{_fmt(px - w / 2.0)}" y="{_fmt(py - h / 2.0)}" '
                    f'width="{_fmt(w * 0.7)}" height="{_fmt(h * 0.7)}" '
                    f'fill="{GHOST_FOOTPRINT}" fill-opacity="0.4" '
                    f'stroke="{GHOST_FOOTPRINT}" stroke-width="0.1"{rot}/>'
                )
        # Reference text at centroid
        if fp.ref:
            cx, cy = _courtyard_centroid(fp)
            out.append(
                f'<text x="{_fmt(cx)}" y="{_fmt(cy - 0.5)}" '
                f'fill="{GHOST_FOOTPRINT}" font-family="monospace" font-size="1" opacity="0.7">'
                f'{html.escape(fp.ref)}</text>'
            )
    out.append("</g>")
    return out


def _tracks_svg(model: BoardModel, colors: Dict[str, str]) -> List[str]:
    out = []
    for layer in ("F.Cu", "B.Cu"):
        layer_tracks = [t for t in model.tracks if t.layer == layer]
        if not layer_tracks:
            continue
        is_fcu = layer == "F.Cu"
        dash = "" if is_fcu else ' stroke-dasharray="3 2"'
        opacity = "1.0" if is_fcu else "0.7"
        out.append(f'<g id="layer-{layer}-tracks" fill="none" stroke-linecap="round" opacity="{opacity}">')
        for t in layer_tracks:
            color = colors.get(t.net_name or "", NO_NET)
            net_attr = html.escape(t.net_name or "", quote=True)
            title = html.escape(
                f"{t.net_name or '(no net)'} · {t.layer} · {t.width_mm:.2f}mm",
            )
            out.append(
                f'<line class="track" data-net="{net_attr}" data-layer="{layer}" '
                f'x1="{_fmt(t.start.x_mm)}" y1="{_fmt(t.start.y_mm)}" '
                f'x2="{_fmt(t.end.x_mm)}" y2="{_fmt(t.end.y_mm)}" '
                f'stroke="{color}" stroke-width="{_fmt(t.width_mm)}"{dash}>'
                f"<title>{title}</title></line>"
            )
        out.append("</g>")
    return out


def _vias_svg(model: BoardModel, colors: Dict[str, str]) -> List[str]:
    out = ['<g id="layer-vias">']
    for v in model.vias:
        color = colors.get(v.net_name or "", NO_NET)
        r = v.size_mm / 2.0
        x, y = _fmt(v.position.x_mm), _fmt(v.position.y_mm)
        net_attr = html.escape(v.net_name or "", quote=True)
        title = html.escape(f"{v.net_name or '(no net)'} · via · {v.size_mm:.2f}mm")
        out.append(f'<g class="via" data-net="{net_attr}"><title>{title}</title>')
        out.append(
            f'<circle cx="{x}" cy="{y}" r="{_fmt(r)}" fill="none" '
            f'stroke="{color}" stroke-width="0.2" pointer-events="none"/>'
        )
        hr = v.drill_mm / 2.0
        if hr > 0:
            out.append(
                f'<circle cx="{x}" cy="{y}" r="{_fmt(hr)}" fill="{HOLE_BG}" pointer-events="none"/>'
            )
        out.append("</g>")
    out.append("</g>")
    return out


def _arrow_marker() -> str:
    """SVG marker definition for displacement arrowheads."""
    return (
        '<defs><marker id="move-arrow" viewBox="0 0 10 10" refX="8" refY="5" '
        f'markerWidth="4" markerHeight="4" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{MOVE_ARROW}"/>'
        "</marker></defs>"
    )


# Per-cause force colors. Deliberately avoids the semantic palette (locked
# red, selected green, pinned/move orange, ghost purple, DRC red/orange) as
# far as a dark board with rainbow net tracks allows; the static HTML legend
# next to the forces toggle is the authority. Boundary/ghost additionally
# differ by dash pattern so they survive hue collisions with net tracks.
FORCE_CAUSE_COLORS = {
    "repulsion": "#22d3ee",  # cyan
    "attraction": "#a3e635",  # lime
    "courtyard": "#f472b6",  # pink
    "boundary": "#e2e8f0",  # near-white, dashed
    "ghost": "#c4b5fd",  # pale lavender, dotted
}
FORCE_CAUSE_ORDER = ("repulsion", "attraction", "courtyard", "boundary", "ghost")
FORCE_CAUSE_DASH = {
    "boundary": ' stroke-dasharray="2.5 1.5"',
    "ghost": ' stroke-dasharray="1 1.2"',
}


def _force_markers() -> str:
    """SVG marker definitions (arrowheads) for each force cause."""
    parts = ["<defs>"]
    for cause, color in FORCE_CAUSE_COLORS.items():
        parts.append(
            f'<marker id="force-{cause}" viewBox="0 0 10 10" refX="8" refY="5" '
            f'markerWidth="4" markerHeight="4" orient="auto-start-reverse">'
            f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{color}"/>'
            "</marker>"
        )
    parts.append("</defs>")
    return "".join(parts)


def _forces_overlay(model: BoardModel, proposal: PlacementProposal,
                    max_len: float = 5.0) -> List[str]:
    """Draw per-cause force vectors at footprint centroids.

    One arrow per force cause (repulsion/attraction/courtyard/boundary/
    ghost) so parameter tuning can see which knob is doing the pushing.
    Lengths use a log scale normalized to the largest visible magnitude —
    raw forces span orders of magnitude (e.g. repulsion ~3 vs courtyard
    ~3800), which pegged the old linear scale at its cap for every part.
    Causes under 1% of their footprint's strongest cause are hidden to cut
    clutter. Falls back to a single total-force arrow when the proposal
    carries no per-cause breakdown.
    """
    components = getattr(proposal, "force_components", None) or {}
    if not components:
        return _total_forces_overlay(model, proposal, max_len=max_len)

    per_fp: Dict[str, list] = {}
    max_mag = 0.0
    for fp in model.footprints:
        comps = components.get(fp.uuid)
        if not comps:
            continue
        vecs = []
        for cause in FORCE_CAUSE_ORDER:
            v = comps.get(cause)
            if not v:
                continue
            mag = math.hypot(v[0], v[1])
            if mag > 1e-9:
                vecs.append((cause, v[0], v[1], mag))
                max_mag = max(max_mag, mag)
        if vecs:
            # Hide negligible causes relative to this footprint's strongest.
            strongest = max(m for _, _, _, m in vecs)
            per_fp[fp.uuid] = [
                (cause, fx, fy, mag) for cause, fx, fy, mag in vecs
                if mag >= 0.01 * strongest
            ]
    if max_mag <= 0.0 or not per_fp:
        return []

    denom = math.log1p(max_mag)
    by_cause: Dict[str, List[str]] = {c: [] for c in FORCE_CAUSE_ORDER}
    for fp in model.footprints:
        vecs = per_fp.get(fp.uuid)
        if not vecs:
            continue
        cx, cy = _courtyard_centroid(fp)
        # Longest first so shorter same-direction arrows stay visible on top.
        for cause, fx, fy, mag in sorted(vecs, key=lambda t: -t[3]):
            length = max(max_len * math.log1p(mag) / denom, 0.5)
            ux, uy = fx / mag, fy / mag
            color = FORCE_CAUSE_COLORS[cause]
            dash = FORCE_CAUSE_DASH.get(cause, "")
            by_cause[cause].append(
                f'<line x1="{_fmt(cx)}" y1="{_fmt(cy)}" '
                f'x2="{_fmt(cx + ux * length)}" y2="{_fmt(cy + uy * length)}" '
                f'stroke="{color}" stroke-width="0.18"{dash} '
                f'marker-end="url(#force-{cause})"/>'
            )
    out = []
    for cause in FORCE_CAUSE_ORDER:
        lines = by_cause[cause]
        if lines:
            out.append(f'<g class="forces-{cause}" opacity="0.9">')
            out.extend(lines)
            out.append("</g>")
    return out


def _total_forces_overlay(model: BoardModel, proposal: PlacementProposal,
                          max_len: float = 5.0) -> List[str]:
    """Legacy single-arrow fallback when no per-cause breakdown is present."""
    if not proposal.forces:
        return []
    out = [f'<g stroke="{MOVE_ARROW}" stroke-width="0.15" fill="{MOVE_ARROW}" opacity="0.8">']
    for fp in model.footprints:
        f = proposal.forces.get(fp.uuid)
        if not f:
            continue
        fx, fy = f
        mag = math.hypot(fx, fy)
        if mag < 1e-6:
            continue
        # Scale and cap
        length = min(mag * 0.1, max_len)
        ux, uy = fx / mag, fy / mag
        cx, cy = _courtyard_centroid(fp)
        # Arrow from centroid in force direction
        out.append(
            f'<line x1="{_fmt(cx)}" y1="{_fmt(cy)}" '
            f'x2="{_fmt(cx + ux * length)}" y2="{_fmt(cy + uy * length)}" '
            f'marker-end="url(#move-arrow)"/>'
        )
    out.append("</g>")
    return out


def _drc_violations_overlay(
    violations: List[dict],
    ignored_types: Optional[Set[str]] = None,
    stale: bool = False,
) -> List[str]:
    """Draw DRC violations as colored circles at their positions.

    Args:
        violations: List of violation dicts from DRC report
        ignored_types: Set of violation types to ignore (default: silkscreen/library issues)
        stale: If True, the result is for an older board version; draw dimmed.

    Returns:
        List of SVG strings for the violation markers
    """
    if ignored_types is None:
        ignored_types = DEFAULT_IGNORED_TYPES
    
    # Filter violations
    filtered = [v for v in violations if v.get("type") not in ignored_types]
    if not filtered:
        return []
    
    group_attrs = ' opacity="0.4"' if stale else ""
    out = [f'<g id="drc-violations"{group_attrs}>']
    for v in filtered:
        vtype = v.get("type", "unknown")
        severity = v.get("severity", "warning")
        description = v.get("description", "")
        
        # Color by severity
        color = DRC_ERROR_COLOR if severity == "error" else DRC_WARNING_COLOR
        # Size by severity
        radius = 1.2 if severity == "error" else 0.8
        
        for item in v.get("items", []):
            pos = item.get("pos")
            if not pos:
                continue
            x = pos.get("x")
            y = pos.get("y")
            if x is None or y is None:
                continue
            
            item_desc = item.get("description", "")
            title = html.escape(f"{vtype}: {description} | {item_desc}")
            
            out.append(
                f'<circle cx="{_fmt(x)}" cy="{_fmt(y)}" r="{_fmt(radius)}" '
                f'fill="{color}" opacity="0.7" stroke="{color}" stroke-width="0.1">'
                f'<title>{title}</title>'
                f'</circle>'
            )
    out.append("</g>")
    return out


def _proposal_overlay(model: BoardModel, proposal: PlacementProposal) -> List[str]:
    """Ghost courtyards at original positions + old→new arrows for moved parts."""
    out = [
        f'<g stroke="{GHOST_COURTYARD}" stroke-width="0.1" '
        f'stroke-dasharray="0.6 0.4" opacity="0.9">'
    ]
    for fp in model.footprints:
        if fp.uuid not in proposal.deltas:
            continue
        for a, b in fp.courtyard:
            out.append(
                f'<line x1="{_fmt(a.x_mm)}" y1="{_fmt(a.y_mm)}" '
                f'x2="{_fmt(b.x_mm)}" y2="{_fmt(b.y_mm)}"/>'
            )
    out.append("</g>")
    arrows = [f'<g stroke="{MOVE_ARROW}" stroke-width="0.2">']
    for fp in model.footprints:
        d = proposal.deltas.get(fp.uuid)
        if not d:
            continue
        dx, dy = d[0], d[1]
        arrows.append(
            f'<line x1="{_fmt(fp.x_mm)}" y1="{_fmt(fp.y_mm)}" '
            f'x2="{_fmt(fp.x_mm + dx)}" y2="{_fmt(fp.y_mm + dy)}" '
            f'marker-end="url(#move-arrow)"/>'
        )
    arrows.append("</g>")
    out.extend(arrows)
    return out


def render_board_svg(
    model: BoardModel,
    title: str = "",
    proposal: Optional[PlacementProposal] = None,
    pinned_uuids: Optional[Set[str]] = None,
    selected_uuids: Optional[Set[str]] = None,
    show_forces: bool = False,
    drc_violations: Optional[List[dict]] = None,
    drc_ignored_types: Optional[Set[str]] = None,
    drc_stale: bool = False,
) -> str:
    """Build the full SVG document for ``model`` (millimeter user units).

    When ``proposal`` is given, footprints are drawn at their proposed
    positions with ghost courtyards and displacement arrows marking where
    they came from.

    Board coordinates (Y-up) are used directly as SVG coordinates (Y-down),
    matching pcbnew's display orientation (Y+ down on screen).

    Args:
        model: Board model to render.
        title: Optional title for the SVG.
        proposal: Optional placement proposal for overlay.
        pinned_uuids: Set of footprint UUIDs pinned by user (excluded from movement).
        selected_uuids: Set of footprint UUIDs currently selected (for move).
        show_forces: If True, draw force vectors on footprints.
        drc_violations: Optional list of DRC violation dicts for overlay.
        drc_ignored_types: Optional set of violation types to ignore in overlay.
        drc_stale: If True, the violations are from an older board version; draw dimmed.
    """
    colors = net_colors(model)
    moved = bool(proposal is not None and proposal.deltas)
    # Preview footprints at their exact post-Accept positions: apply_deltas is
    # the same code path commit_placement uses, and nudge_footprint_by_uuid
    # writes the same transform to the file (verified to fp noise), so what
    # you see is what Accept writes. Ghost courtyards below still mark where
    # each part came from.
    if moved:
        deltas = {
            u: (d[0], d[1], d[2] if len(d) > 2 else 0.0)
            for u, d in proposal.deltas.items()
        }
        view = apply_deltas(model, deltas)
    else:
        view = model
    min_x, min_y, width, height = _bounds(view)
    # Grow the margin by the largest proposed move so ghosts don't clip.
    extra = 0.0
    if moved:
        for delta in proposal.deltas.values():
            dx = delta[0]
            dy = delta[1]
            extra = max(extra, math.hypot(dx, dy))
    pad = 1.0 + extra
    vb = (
        f"{_fmt(min_x - pad)} {_fmt(min_y - pad)} "
        f"{_fmt(width + 2 * pad)} {_fmt(height + 2 * pad)}"
    )
    parts: List[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" '
        f'width="100%" height="100%">'
    ]
    if title:
        parts.append(f"<title>{html.escape(title)}</title>")
    if moved or (proposal is not None and show_forces):
        parts.append(_arrow_marker())
    if proposal is not None and show_forces:
        parts.append(_force_markers())

    locked_uuids = {fp.uuid for fp in model.footprints if fp.locked and fp.uuid}
    pinned = pinned_uuids or set()
    selected = selected_uuids or set()

    parts.extend(_edge_cuts_svg(model))
    parts.extend(_zones_svg(model))
    parts.extend(_footprints_svg(view, colors, locked_uuids, pinned, selected))
    parts.extend(_ghost_footprints_svg(view, colors))
    parts.extend(_tracks_svg(model, colors))
    parts.extend(_vias_svg(model, colors))
    if moved:
        parts.extend(_proposal_overlay(model, proposal))
    if proposal is not None and show_forces:
        parts.extend(_forces_overlay(view, proposal))
    if drc_violations:
        parts.extend(_drc_violations_overlay(drc_violations, drc_ignored_types, stale=drc_stale))
    parts.append("</svg>")
    return "\n".join(parts)