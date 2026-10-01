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
from typing import Dict, List, Optional, Tuple

from .board_model import BoardModel, Point
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
MOVE_ARROW = "#e0a458"  # old -> new displacement arrows


def _fmt(v: float) -> str:
    return f"{v:.3f}"


def _shift(offsets, uuid):
    """Proposed ``(dx_mm, dy_mm)`` offset for a footprint UUID (zero if none)."""
    return offsets.get(uuid, (0.0, 0.0)) if offsets else (0.0, 0.0)


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
    for zone in model.zones:
        if len(zone.polygon) < 3:
            continue
        is_keepout = zone.uuid in keepout_uuids
        fill = KEEPOUT_FILL if is_keepout else ZONE_FILL
        dash = ' stroke-dasharray="1.5 1"' if is_keepout else ""
        pts = " ".join(f"{_fmt(p.x_mm)} {_fmt(p.y_mm)}" for p in zone.polygon)
        out.append(
            f'<polygon points="{pts}" fill="{fill}" fill-opacity="0.18" '
            f'stroke="{fill}" stroke-width="0.2"{dash}/>'
        )
    return out


def _courtyards_svg(model: BoardModel, offsets=None) -> List[str]:
    out = [f'<g stroke="{COURTYARD}" stroke-width="0.1" opacity="0.6">']
    for fp in model.footprints:
        dx, dy = _shift(offsets, fp.uuid)
        for a, b in fp.courtyard:
            out.append(
                f'<line x1="{_fmt(a.x_mm + dx)}" y1="{_fmt(a.y_mm + dy)}" '
                f'x2="{_fmt(b.x_mm + dx)}" y2="{_fmt(b.y_mm + dy)}"/>'
            )
    out.append("</g>")
    return out


def _tracks_svg(model: BoardModel, colors: Dict[str, str]) -> List[str]:
    out = [f'<g fill="none" stroke-linecap="round">']
    for t in model.tracks:
        color = colors.get(t.net_name or "", NO_NET)
        out.append(
            f'<line x1="{_fmt(t.start.x_mm)}" y1="{_fmt(t.start.y_mm)}" '
            f'x2="{_fmt(t.end.x_mm)}" y2="{_fmt(t.end.y_mm)}" '
            f'stroke="{color}" stroke-width="{_fmt(t.width_mm)}"/>'
        )
    out.append("</g>")
    return out


def _vias_svg(model: BoardModel, colors: Dict[str, str]) -> List[str]:
    out = []
    for v in model.vias:
        color = colors.get(v.net_name or "", NO_NET)
        r = v.size_mm / 2.0
        x, y = _fmt(v.position.x_mm), _fmt(v.position.y_mm)
        out.append(
            f'<circle cx="{x}" cy="{y}" r="{_fmt(r)}" fill="none" '
            f'stroke="{color}" stroke-width="0.2"/>'
        )
        hr = v.drill_mm / 2.0
        if hr > 0:
            out.append(
                f'<circle cx="{x}" cy="{y}" r="{_fmt(hr)}" fill="{HOLE_BG}"/>'
            )
    return out


def _pads_svg(model: BoardModel, colors: Dict[str, str], offsets=None) -> List[str]:
    out = []
    for fp in model.footprints:
        flip = fp.layer.startswith("B")
        dx, dy = _shift(offsets, fp.uuid)
        for pad in fp.pads:
            color = colors.get(pad.net_name or "", NO_NET)
            px, py = pad.position.x_mm + dx, pad.position.y_mm + dy
            x, y = _fmt(px), _fmt(py)
            w, h = pad.size_mm
            angle = -pad.angle_deg if flip else pad.angle_deg
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
    return out


def _refs_svg(model: BoardModel, offsets=None) -> List[str]:
    out = [f'<g fill="{REF_TEXT}" font-family="monospace" font-size="1">']
    for fp in model.footprints:
        # Unnamed parts (empty ref) fall back to a short UUID so they stay
        # identifiable in the viewer; nothing is drawn when both are empty.
        label = fp.ref or (fp.uuid[:8] if fp.uuid else "")
        if not label:
            continue
        dx, dy = _shift(offsets, fp.uuid)
        out.append(
            f'<text x="{_fmt(fp.x_mm + dx)}" y="{_fmt(fp.y_mm + dy - 0.5)}">'
            f"{html.escape(label)}</text>"
        )
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
        dx, dy = d
        arrows.append(
            f'<line x1="{_fmt(fp.x_mm)}" y1="{_fmt(fp.y_mm)}" '
            f'x2="{_fmt(fp.x_mm + dx)}" y2="{_fmt(fp.y_mm + dy)}" '
            f'marker-end="url(#move-arrow)"/>'
        )
    arrows.append("</g>")
    out.extend(arrows)
    return out


def render_board_svg(
    model: BoardModel, title: str = "", proposal: Optional[PlacementProposal] = None
) -> str:
    """Build the full SVG document for ``model`` (millimeter user units).

    When ``proposal`` is given, footprints are drawn at their proposed
    positions with ghost courtyards and displacement arrows marking where
    they came from.
    """
    colors = net_colors(model)
    min_x, min_y, max_x, max_y = _bounds(model)
    moved = bool(proposal is not None and proposal.deltas)
    # Grow the margin by the largest proposed move so nothing clips.
    extra = (
        max(math.hypot(dx, dy) for dx, dy in proposal.deltas.values()) if moved else 0.0
    )
    pad = 1.0 + extra
    vb = (
        f"{_fmt(min_x - pad)} {_fmt(min_y - pad)} "
        f"{_fmt(max_x - min_x + 2 * pad)} {_fmt(max_y - min_y + 2 * pad)}"
    )
    parts: List[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" '
        f'width="100%" height="100%">'
    ]
    if title:
        parts.append(f"<title>{html.escape(title)}</title>")
    offsets = proposal.deltas if moved else None
    if moved:
        parts.append(_arrow_marker())
    parts.extend(_edge_cuts_svg(model))
    parts.extend(_zones_svg(model))
    parts.extend(_courtyards_svg(model, offsets))
    parts.extend(_tracks_svg(model, colors))
    parts.extend(_vias_svg(model, colors))
    parts.extend(_pads_svg(model, colors, offsets))
    parts.extend(_refs_svg(model, offsets))
    if moved:
        parts.extend(_proposal_overlay(model, proposal))
    parts.append("</svg>")
    return "\n".join(parts)
