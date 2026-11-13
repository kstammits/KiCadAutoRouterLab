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
from typing import Collection, Dict, List, Optional, Set, Tuple

from .board_model import BoardModel, Point, apply_deltas, is_ground_net, is_power_net_effective, sample_circular_arc
from .drc import DEFAULT_IGNORED_TYPES
from .placement import PlacementProposal, _footprint_courtyard_polygon

# Fixed hues for power/ground nets (override the golden-angle sweep in
# net_colors so rails read instantly; mirrored in ui/index.html legend +
# nets table badges and served via /api/nets color).
POWER_NET_COLOR = "#f59e0b"  # amber
GROUND_NET_COLOR = "#38bdf8"  # sky blue
# Decoupling-cap halo (courtyard stroke when a footprint is special-treated
# as a decap; distinct from lock/pin/select/movable + force palettes).
DECAP_HALO_COLOR = "#2dd4bf"  # teal

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

# Zone-fill layer colors (fill view only): front copper reads red, back
# copper green, so overlapping pours stay distinguishable. Inner layers
# cycle the extra hues in stackup order.
FILL_FCU_COLOR = "#e74c3c"  # red
FILL_BCU_COLOR = "#2ecc71"  # green
FILL_INNER_COLORS = ("#2dd4bf", "#a78bfa", "#f5a623", "#59a7f5")  # teal, violet, amber, blue

# Inner-layer track dashes, cycled in stackup order (outer layers keep
# their historic styles: F.Cu solid, B.Cu "2 1").
_INNER_DASHES = ("5 2", "2 2", "7 2 2 2", "4 1")


def _copper_layers_of(model: BoardModel) -> Tuple[str, ...]:
    """Stackup-ordered copper layers to render (F/B fallback for old models).

    Unions the declared stackup with layers actually carrying tracks/zones
    so copper is never silently dropped (e.g. a hand-built model whose
    ``copper_layers`` predates an ``In1.Cu`` pour).
    """
    from .board_model import _sort_copper_layers

    declared = tuple(getattr(model, "copper_layers", ()) or ())
    used = [t.layer for t in model.tracks if t.layer]
    used += [a.layer for a in model.arcs if a.layer]
    for zone in model.zones:
        used.extend(zone.layers)
    if not declared and not used:
        return ("F.Cu", "B.Cu")
    return _sort_copper_layers(list(declared) + [u for u in used if u not in declared])


def _layer_track_style(layer: str, layers: Tuple[str, ...]) -> Tuple[str, str]:
    """(opacity, dash-attr) for a copper layer's track group.

    Outer layers keep their historic look (F.Cu solid/1.0, B.Cu dashed/0.7)
    so 2-layer boards render pixel-identical; inner layers get full-width
    strokes dimmed slightly with a cycling dash so stackup order reads.
    """
    if layer == "F.Cu":
        return "1.0", ""
    if layer == "B.Cu":
        return "0.7", ' stroke-dasharray="2 1"'
    inners = [name for name in layers if name not in ("F.Cu", "B.Cu")]
    pos = inners.index(layer) if layer in inners else 0
    opacity = f"{max(0.55, 0.9 - 0.05 * pos):.2f}"
    return opacity, f' stroke-dasharray="{_INNER_DASHES[pos % len(_INNER_DASHES)]}"'


def _layer_fill_color(layer: str, layers: Tuple[str, ...]) -> str:
    """Cached-fill color for a copper layer (F red, B green, inners cycle)."""
    if layer == "F.Cu":
        return FILL_FCU_COLOR
    if layer == "B.Cu":
        return FILL_BCU_COLOR
    inners = [name for name in layers if name not in ("F.Cu", "B.Cu")]
    pos = inners.index(layer) if layer in inners else 0
    return FILL_INNER_COLORS[pos % len(FILL_INNER_COLORS)]


def _layer_solid_color(layer: str, layers: Tuple[str, ...]) -> str:
    """Solid stroke/fill color for a copper layer (reuse zone-fill palette).

    F.Cu red, B.Cu green, inners cycle teal/violet/amber/blue — identical to
    the zone-fill legend so fills, tracks, vias and pads agree per layer.
    """
    return _layer_fill_color(layer, layers)


def _tint_track_color(color: str, layer: str, layers: Tuple[str, ...]) -> str:
    """Shift a per-net track color by the layer tint.

    Outer layers pass through unchanged (2-layer output identical). Inner
    layers pull ``hsl()`` lightness down 10 points or blend ``#hex`` 25%
    toward the layer's fill tint, so each net still reads as itself while
    the layer reads as deeper in the stackup.
    """
    if layer in ("F.Cu", "B.Cu") or not color:
        return color
    tint = _layer_fill_color(layer, layers)
    if color.startswith("hsl("):
        try:
            inner = color[4:].rstrip(")")
            h, s, light = inner.split(",")
            l_val = float(light.strip().rstrip("%")) if "%" in light else float(light.strip())
            l_val = max(15.0, min(85.0, l_val - 10.0))
            return f"hsl({h.strip()}, {s.strip()}, {l_val:.0f}%)"
        except (ValueError, IndexError):
            return color
    if color.startswith("#") and len(color) == 7:
        try:
            r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
            tr, tg, tb = (int(tint[i:i + 2], 16) for i in (1, 3, 5))
            mix = lambda c, t: int(round(c * 0.75 + t * 0.25))
            return f"#{mix(r, tr):02x}{mix(g, tg):02x}{mix(b, tb):02x}"
        except ValueError:
            return color
    return color

# DRC violation colors
DRC_ERROR_COLOR = "#e74c3c"     # red for errors
DRC_WARNING_COLOR = "#f39c12"   # orange for warnings

# Footprint state colors
FP_LOCKED_COLOR = "#e06c75"    # KiCad locked - red
FP_PINNED_COLOR = "#e0a458"    # User pinned - orange
FP_SELECTED_COLOR = "#7ee0a0"  # Selected for move - green
FP_DEFAULT_COLOR = COURTYARD   # Default unlocked - gray-blue


def _fmt(v: float) -> str:
    return f"{v:.3f}"


def net_colors(
    model: BoardModel, extra_power: Collection[str] = ()
) -> Dict[str, str]:
    """Deterministic per-net colors; hues step by the golden angle.

    Power/ground nets get fixed hues (POWER_NET_COLOR/GROUND_NET_COLOR,
    power wins on overlap like identify_power_nets) so rails read
    instantly regardless of net-list order. User-marked power membership
    (``extra_power``) counts as power.
    """
    names = sorted(name for name in model.nets if name)
    colors = {
        name: f"hsl({(i * 137.508) % 360:.0f}, 75%, 62%)"
        for i, name in enumerate(names)
    }
    for name in names:
        if is_power_net_effective(name, extra_power):
            colors[name] = POWER_NET_COLOR
        elif is_ground_net(name):
            colors[name] = GROUND_NET_COLOR
    return colors


def decap_fp_uuids(
    model: BoardModel, extra_power: Collection[str] = ()
) -> Set[str]:
    """UUIDs of footprints special-treated as decoupling caps.

    Same rule as placement._find_decap_pad_indices: ref C*, exactly 2
    netted pads, one power + one ground side. Ghosts/padless excluded.
    User-marked power membership (``extra_power``) counts as power.
    """
    out: Set[str] = set()
    for fp in model.footprints:
        if not fp.uuid or not fp.pads:
            continue
        if not (fp.ref or "").upper().startswith("C"):
            continue
        nets = [p.net_name for p in fp.pads if p.net_name]
        if len(nets) != 2:
            continue
        sides = sorted(
            "power" if is_power_net_effective(n, extra_power) else ("ground" if is_ground_net(n) else "signal")
            for n in nets
        )
        if sides == ["ground", "power"]:
            out.add(fp.uuid)
    return out


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
        for fp in zone.filled_polygons:
            for p in fp.polygon:
                add(p)
    for fp in model.footprints:
        # Padless graphical parts are hidden from the SVG (see
        # _footprints_svg); their imputed Fab/Silk art must not inflate the
        # viewBox either.
        if not fp.pads:
            continue
        for a, b in fp.courtyard:
            add(a)
            add(b)
        for pad in fp.pads:
            add(pad.position)
    for t in model.tracks:
        add(t.start)
        add(t.end)
    for a in model.arcs:
        add(a.start)
        add(a.mid)
        add(a.end)
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
        # KiCad stores three points ON the arc; sample the true circle so
        # quarter-circle corners render round instead of shallow.
        pts = " ".join(
            f"{_fmt(x)} {_fmt(y)}"
            for x, y in sample_circular_arc(
                (arc.start.x_mm, arc.start.y_mm),
                (arc.mid.x_mm, arc.mid.y_mm),
                (arc.end.x_mm, arc.end.y_mm),
                16,
            )
        )
        out.append(f'<polyline points="{pts}"/>')
    out.append("</g>")
    return out


def _zones_svg(
    model: BoardModel, extra_power: Collection[str] = (),
    show_fills: bool = False,
) -> List[str]:
    keepout_uuids = {z.uuid for z in model.keepout_zones}
    out: List[str] = []
    layers = _copper_layers_of(model)
    for layer in layers:
        layer_zones = [z for z in model.zones if layer in z.layers]
        if not layer_zones:
            continue
        if layer == "F.Cu":
            opacity = "0.08"
        elif layer == "B.Cu":
            opacity = "0.06"
        else:
            opacity = "0.07"
        out.append(f'<g id="layer-{layer}-zones">')
        for zone in layer_zones:
            if len(zone.polygon) < 3:
                continue
            is_keepout = zone.uuid in keepout_uuids
            if is_keepout:
                fill = KEEPOUT_FILL
            elif zone.net_name and is_power_net_effective(zone.net_name, extra_power):
                fill = POWER_NET_COLOR
            elif zone.net_name and is_ground_net(zone.net_name):
                fill = GROUND_NET_COLOR
            else:
                fill = ZONE_FILL
            dash = ' stroke-dasharray="1.5 1"' if is_keepout else ""
            net_attr = f' data-net="{html.escape(zone.net_name or "")}"' if zone.net_name else ""
            layer_fills = [
                fp for fp in zone.filled_polygons
                if fp.layer == layer and len(fp.polygon) >= 3
            ]
            if show_fills and not is_keepout and layer_fills:
                # Fill view: hide the zone outline (redundant with the
                # detailed fill) and color fills by layer so overlapping
                # pours stay distinguishable. Fill-only (no stroke): KiCad
                # keeps each island a single ring by joining clearance
                # cutouts to the outline with zero-width out-and-back slits;
                # stroking the ring draws every slit twice as a spurious
                # internal line.
                layer_fill = _layer_fill_color(layer, layers)
                fill_opacity = "0.28" if layer == "F.Cu" else "0.22"
                for fp in layer_fills:
                    fpts = " ".join(f"{_fmt(p.x_mm)} {_fmt(p.y_mm)}" for p in fp.polygon)
                    out.append(
                        f'<polygon class="zone-fill" points="{fpts}" fill="{layer_fill}" '
                        f'fill-opacity="{fill_opacity}" stroke="none"{net_attr}/>'
                    )
                continue
            pts = " ".join(f"{_fmt(p.x_mm)} {_fmt(p.y_mm)}" for p in zone.polygon)
            # Outline wash (also the fallback when a zone has no cached
            # fills on this layer, so the pour never vanishes from view).
            out.append(
                f'<polygon points="{pts}" fill="{fill}" fill-opacity="{opacity}" '
                f'stroke="{fill}" stroke-width="0.2"{dash}{net_attr}/>'
            )
        out.append("</g>")
    return out


def _courtyard_edges(fp) -> List[Tuple[Tuple[float, float], Tuple[float, float]]]:
    """Courtyard edges exactly as placement collision/legality sees them."""
    if not fp.courtyard:
        return []
    poly = _footprint_courtyard_polygon(fp)
    if poly is None or poly.is_empty:
        return []
    coords = list(poly.exterior.coords)
    return [
        (tuple(coords[i]), tuple(coords[i + 1]))
        for i in range(len(coords) - 1)
    ]


def _footprint_courtyard_svg(
    fp,
    is_locked: bool,
    is_pinned: bool,
    is_selected: bool,
    is_movable: bool,
    is_decap: bool = False,
) -> str:
    """Generate courtyard lines for a single footprint with state-based styling."""
    if is_decap:
        stroke = DECAP_HALO_COLOR
        extras = ' stroke-width="0.15" stroke-dasharray="0.6 0.3" opacity="0.95"'
    elif is_locked:
        stroke = FP_LOCKED_COLOR
        extras = ' stroke-width="0.1" stroke-dasharray="2 2" opacity="0.8"'
    elif is_pinned:
        stroke = FP_PINNED_COLOR
        extras = ' stroke-width="0.1" stroke-dasharray="4 2" opacity="0.8"'
    elif is_selected:
        stroke = FP_SELECTED_COLOR
        extras = ' stroke-width="0.3" opacity="0.95"'
    elif is_movable:
        stroke = FP_DEFAULT_COLOR
        extras = ' stroke-width="0.1" opacity="0.45"'
    else:
        stroke = FP_DEFAULT_COLOR
        extras = ' stroke-width="0.1" opacity="0.8"'
    edges = list(_courtyard_edges(fp))
    lines = []
    if is_selected:
        # Halo under-stroke so the selection reads at fit-zoom on dense
        # boards. Courtyard lines only — pads keep their net colors.
        for (x1, y1), (x2, y2) in edges:
            lines.append(
                f'<line class="sel-halo" x1="{_fmt(x1)}" y1="{_fmt(y1)}" '
                f'x2="{_fmt(x2)}" y2="{_fmt(y2)}" '
                f'stroke="{stroke}" stroke-width="0.8" opacity="0.3"/>'
            )
    for (x1, y1), (x2, y2) in edges:
        lines.append(
            f'<line x1="{_fmt(x1)}" y1="{_fmt(y1)}" '
            f'x2="{_fmt(x2)}" y2="{_fmt(y2)}" '
            f'stroke="{stroke}"{extras}/>'
        )
    return "\n".join(lines)


def _footprint_pads_svg(
    fp,
    colors: Dict[str, str],
    flip: bool,
    fp_angle_deg: float,
    extra_power: Collection[str] = (),
    color_mode: str = "net",
    layers: Tuple[str, ...] = (),
) -> str:
    """Generate pad elements for a single footprint."""
    out = []
    for pad in fp.pads:
        if color_mode == "layer":
            # Pads sit on the footprint's side layer (F.Cu / B.Cu): color by
            # side so front/rear copper reads distinctly in routing mode.
            color = _layer_solid_color("B.Cu" if flip else "F.Cu", layers)
        else:
            color = colors.get(pad.net_name or "", NO_NET)
        net = pad.net_name or ""
        net_attr = f' data-net="{html.escape(net)}"' if net else ""
        ident = (f' data-ref="{html.escape(fp.ref or "")}"'
                 f' data-pad="{html.escape(pad.number)}"'
                 f' data-uuid="{html.escape(fp.uuid or "")}"')
        title = f"<title>{html.escape(net)}</title>" if net else ""
        px, py = pad.position.x_mm, pad.position.y_mm
        x, y = _fmt(px), _fmt(py)
        w, h = pad.size_mm
        label = ""
        # Close-zoom net labels for rails only (bounds DOM growth):
        # hidden by default, shown via .zoom-close on the viewport.
        if net and (is_power_net_effective(net, extra_power) or is_ground_net(net)):
            label = (
                f'<text class="pad-net-label" data-net="{html.escape(net)}" '
                f'x="{x}" y="{_fmt(py - h / 2.0 - 0.4)}" text-anchor="middle" '
                f'font-family="monospace" font-size="0.8" fill="{color}" '
                f'pointer-events="none">{html.escape(net)}</text>'
            )

        # Pad angle is the file's effective board orientation (pcbnew rewrites
        # pad angles when the footprint rotates), so it is used directly
        # without combining with fp_angle (that would double-count). Board
        # geometry rotates by R(-angle) numerically (`_to_board` uses -angle:
        # local Y-DOWN to board), while SVG `rotate(a)` applies R(+a), so the
        # sign must flip or copper counter-rotates against its own pad row.
        # Normalized to [0, 360) to match the file convention.
        # `flip`/`fp_angle_deg` are retained in the signature for callers.
        angle = (-pad.angle_deg) % 360.0 if pad.angle_deg else 0.0
        rot = f' transform="rotate({_fmt(angle)} {x} {y})"' if angle else ""
        shape = (pad.shape or "").lower()
        if shape == "circle" or (shape == "oval" and w == h):
            # Round annulus (DIP pins other than pin 1): true circle in
            # pcbnew. Oval with equal w/h is circular too.
            out.append(
                f'<circle cx="{x}" cy="{y}" r="{_fmt(w / 2.0)}" '
                f'fill="{color}" stroke="{PAD_STROKE}" stroke-width="0.1"{net_attr}{ident}>'
                f"{title}</circle>"
            )
            if label:
                out.append(label)
        elif shape in ("oval", "roundrect"):
            # Capsule (oval) or soft rect (roundrect). rx=min/2 gives the
            # stadium silhouette for ovals; roundrect uses a smaller corner
            # radius (model stores no rratio, ~0.25 is KiCad's default).
            rx = _fmt(min(w, h) / 2.0 if shape == "oval" else min(w, h) / 4.0)
            out.append(
                f'<rect x="{_fmt(px - w / 2.0)}" y="{_fmt(py - h / 2.0)}" '
                f'width="{_fmt(w)}" height="{_fmt(h)}" rx="{rx}" fill="{color}" '
                f'stroke="{PAD_STROKE}" stroke-width="0.1"{rot}{net_attr}{ident}>'
                f"{title}</rect>"
            )
            if label:
                out.append(label)
        else:
            out.append(
                f'<rect x="{_fmt(px - w / 2.0)}" y="{_fmt(py - h / 2.0)}" '
                f'width="{_fmt(w)}" height="{_fmt(h)}" fill="{color}" '
                f'stroke="{PAD_STROKE}" stroke-width="0.1"{rot}{net_attr}{ident}>'
                f"{title}</rect>"
            )
            if label:
                out.append(label)
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
    """Generate reference text for a single footprint at its true label pose.

    When the footprint carries a parsed ``SilkLabel`` (silkscreen/Fab
    reference text from the file), the text is drawn at the label's board
    pose (position + rotation) at the file's font size — what you see is
    where the label actually sits. Footprints without a label keep the
    historic centroid fallback (``ref`` or short uuid).
    """
    from .board_model import label_board_pose

    label = getattr(fp, "label", None)
    if label is not None:
        pose = label_board_pose(fp)
        if pose is not None:
            cx, cy, angle = pose
            size = 1.0
            try:
                size = float(label.size_mm[1])
            except (TypeError, ValueError, IndexError):
                pass
            # Mirrored text (bottom-side ``justify mirror``) reads upright:
            # gerber-verified, a mirrored 180 plots pixel-identical to an
            # unmirrored 0 (and 90 stays 90), so fold the flip bit for
            # display. The bbox is unchanged either way, so dynamics and
            # DRC geometry (which keep the filed angle) still agree.
            ea = angle % 180.0 if getattr(label, "mirrored", False) else angle
            rot = f' transform="rotate({_fmt(ea)} {_fmt(cx)} {_fmt(cy)})"' if ea else ""
            return (
                f'<text x="{_fmt(cx)}" y="{_fmt(cy)}" '
                f'fill="{REF_TEXT}" font-family="monospace" font-size="{_fmt(size)}"{rot}>'
                f"{html.escape(label.text)}</text>"
            )
    label_text = fp.ref or (fp.uuid[:8] if fp.uuid else "")
    if not label_text:
        return ""
    cx, cy = _courtyard_centroid(fp)
    return (
        f'<text x="{_fmt(cx)}" y="{_fmt(cy - 0.5)}" '
        f'fill="{REF_TEXT}" font-family="monospace" font-size="1">'
        f"{html.escape(label_text)}</text>"
    )


def _footprints_svg(
    model: BoardModel,
    colors: Dict[str, str],
    locked_uuids: Set[str],
    pinned_uuids: Set[str],
    selected_uuids: Set[str],
    extra_power: Collection[str] = (),
    color_mode: str = "net",
) -> List[str]:
    """Generate all footprint elements (courtyards, pads, refs) grouped by footprint.

    Footprints are always drawn at their model coordinates. When a placement
    proposal is active the caller passes ``apply_deltas(model, deltas)`` as
    ``model``, so the preview shows the exact post-Accept positions (same
    pivot and rotation direction as the writeback in ``io``) instead of a
    group-transform approximation.
    """
    decaps = decap_fp_uuids(model, extra_power)
    layers = _copper_layers_of(model)
    out = []
    for fp in model.footprints:
        # Graphical-only parts (no pads) are hidden: their imputed Fab/Silk
        # courtyard art floods the DOM with decorative segments and slows
        # rendering. They stay in the model (physics skips them, writeback
        # is tree-based), they are just not drawn. Ghosts are unaffected.
        if not fp.pads:
            continue
        is_locked = fp.uuid in locked_uuids
        is_pinned = fp.uuid in pinned_uuids
        is_selected = fp.uuid in selected_uuids
        is_movable = fp.uuid and not fp.locked and fp.uuid not in pinned_uuids

        flip = fp.layer.startswith("B")

        # Build footprint group with state classes and data attribute
        cls_parts = ["footprint"]
        is_decap = fp.uuid in decaps
        if is_locked:
            cls_parts.append("fp-locked")
        if is_pinned:
            cls_parts.append("fp-pinned")
        if is_selected:
            cls_parts.append("fp-selected")
        if is_movable and not is_selected:
            cls_parts.append("fp-movable")
        if is_decap:
            cls_parts.append("fp-decap")
        cls = " ".join(cls_parts)

        # Ref text rides in its own inner group so the motion player can
        # apply the per-frame label slide (ldx, ldy) without touching the
        # part transform. Pointer events stay on the footprint group.
        fp_group = [
            f'<g class="{cls}" data-fp-uuid="{fp.uuid}">',
            _footprint_courtyard_svg(fp, is_locked, is_pinned, is_selected, is_movable, is_decap),
            _footprint_pads_svg(fp, colors, flip, fp.angle_deg, extra_power,
                                color_mode=color_mode, layers=layers),
            f'<g class="label" data-label-for="{fp.uuid}" pointer-events="none">'
            f'{_footprint_ref_svg(fp)}</g>',
            "</g>",
        ]
        out.append("\n".join(fp_group))
    return out


def _ghost_footprints_svg(
    model: BoardModel,
    colors: Dict[str, str],
    selected_ghost_uuids: Collection[str] = (),
) -> List[str]:
    """Generate ghost footprint elements (courtyards, pads, refs) for pseudo-components.

    Ghost footprints are rendered with a distinct purple color, dashed style,
    and semi-transparency to distinguish them from real footprints. Each
    ghost gets its own ``g.footprint.fp-ghost[data-fp-uuid]`` group (mirroring
    real footprints) so click-to-select, motion transforms, and per-footprint
    force vectors reuse the same client code paths.
    """
    out = []
    if not model.ghost_footprints:
        return out

    selected = set(selected_ghost_uuids or ())
    out.append(
        f'<g stroke="{GHOST_FOOTPRINT}" stroke-width="0.15" '
        f'stroke-dasharray="4 3" opacity="0.7" fill="none">'
    )
    for fp in model.ghost_footprints:
        is_selected = fp.uuid and fp.uuid in selected
        cls = "footprint fp-ghost" + (" fp-selected" if is_selected else "")
        # data-fp-uuid reuses the real-footprint client paths
        # (click-select, motion transforms, force vectors).
        out.append(f'<g class="{cls}" data-fp-uuid="{fp.uuid or ""}">')
        # Courtyard lines
        for (x1, y1), (x2, y2) in _courtyard_edges(fp):
            out.append(
                f'<line x1="{_fmt(x1)}" y1="{_fmt(y1)}" '
                f'x2="{_fmt(x2)}" y2="{_fmt(y2)}"/>'
            )
        # Pads (smaller, ghost color)
        for pad in fp.pads:
            px, py = pad.position.x_mm, pad.position.y_mm
            x, y = _fmt(px), _fmt(py)
            w, h = pad.size_mm
            # Pad angle: same negated convention as real footprints (file
            # stores effective board orientation; SVG rotate is R(+a) while
            # board geometry is R(-a)). Ghost rects are 0.7-scale and stay
            # centred on the pad position.
            angle = (-pad.angle_deg) % 360.0 if pad.angle_deg else 0.0
            rot = f' transform="rotate({_fmt(angle)} {x} {y})"' if angle else ""
            gshape = (pad.shape or "").lower()
            if gshape == "circle" or (gshape == "oval" and w == h):
                r = w / 2.0
                out.append(
                    f'<circle cx="{x}" cy="{y}" r="{_fmt(r * 0.7)}" '
                    f'fill="{GHOST_FOOTPRINT}" fill-opacity="0.4" '
                    f'stroke="{GHOST_FOOTPRINT}" stroke-width="0.1"/>'
                )
            else:
                gw, gh = w * 0.7, h * 0.7
                rx_attr = ""
                if gshape in ("oval", "roundrect"):
                    rx_attr = f' rx="{_fmt(min(gw, gh) / 2.0 if gshape == "oval" else min(gw, gh) / 4.0)}"'
                out.append(
                    f'<rect x="{_fmt(px - gw / 2.0)}" y="{_fmt(py - gh / 2.0)}" '
                    f'width="{_fmt(gw)}" height="{_fmt(gh)}"{rx_attr} '
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
    out.append("</g>")
    return out


def _tracks_svg(model: BoardModel, colors: Dict[str, str],
                color_mode: str = "net") -> List[str]:
    out = []
    layers = _copper_layers_of(model)
    # Emit back-to-front (reverse stackup): SVG paints in document order,
    # so the front group lands last and covers back copper at crossings.
    for layer in reversed(layers):
        layer_tracks = [t for t in model.tracks if t.layer == layer]
        layer_arcs = [a for a in model.arcs if a.layer == layer]
        if not layer_tracks and not layer_arcs:
            continue
        opacity, dash = _layer_track_style(layer, layers)
        if color_mode == "layer":
            # Color alone distinguishes front/rear/inner: no hatching, full
            # opacity on every layer (net mode keeps the historic dash cues).
            opacity = "1.0"
            dash = ""
        out.append(f'<g id="layer-{layer}-tracks" fill="none" stroke-linecap="round" opacity="{opacity}">')
        for t in layer_tracks:
            color = _layer_solid_color(layer, layers) if color_mode == "layer" \
                else _tint_track_color(colors.get(t.net_name or "", NO_NET), layer, layers)
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
        for a in layer_arcs:
            color = _layer_solid_color(layer, layers) if color_mode == "layer" \
                else _tint_track_color(colors.get(a.net_name or "", NO_NET), layer, layers)
            net_attr = html.escape(a.net_name or "", quote=True)
            title = html.escape(
                f"{a.net_name or '(no net)'} · {a.layer} · {a.width_mm:.2f}mm · arc",
            )
            pts = list(sample_circular_arc(
                (a.start.x_mm, a.start.y_mm),
                (a.mid.x_mm, a.mid.y_mm),
                (a.end.x_mm, a.end.y_mm),
                24,
            ))
            d = "M" + " L".join(f"{_fmt(x)} {_fmt(y)}" for x, y in pts)
            out.append(
                f'<path class="track arc-track" data-net="{net_attr}" data-layer="{layer}" '
                f'd="{d}" fill="none" '
                f'stroke="{color}" stroke-width="{_fmt(a.width_mm)}"{dash}>'
                f"<title>{title}</title></path>"
            )
        out.append("</g>")
    return out


def _vias_svg(model: BoardModel, colors: Dict[str, str],
              color_mode: str = "net") -> List[str]:
    out = ['<g id="layer-vias">']
    layers = _copper_layers_of(model)
    for v in model.vias:
        span = list(getattr(v, "layers", ()) or ("F.Cu", "B.Cu"))
        if color_mode == "layer":
            if set(span) == {"F.Cu", "B.Cu"}:
                # Through via spans both outers: neutral ring so it reads as a
                # connector between sides, not as owned by one net's color.
                color = "#e8edf2"
            elif len(span) == 1:
                color = _layer_solid_color(span[0], layers)
            else:
                # Blind/bury across inners or mixed span: ambiguous ownership.
                color = "#e8edf2"
        else:
            color = colors.get(v.net_name or "", NO_NET)
        r = v.size_mm / 2.0
        x, y = _fmt(v.position.x_mm), _fmt(v.position.y_mm)
        net_attr = html.escape(v.net_name or "", quote=True)
        span = " ".join(v.layers) if getattr(v, "layers", ()) else "F.Cu B.Cu"
        layers_attr = f' data-layers="{html.escape(span, quote=True)}"'
        title = html.escape(f"{v.net_name or '(no net)'} · via · {v.size_mm:.2f}mm")
        out.append(f'<g class="via" data-net="{net_attr}"{layers_attr}><title>{title}</title>')
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
# next to the forces toggle is the authority. Ghost additionally differs by
# dash pattern so it survives hue collisions with net tracks.
FORCE_CAUSE_COLORS = {
    "repulsion": "#22d3ee",  # cyan
    "attraction": "#a3e635",  # lime
    "courtyard": "#f472b6",  # pink (part contact + board wall)
    "ghost": "#c4b5fd",  # pale lavender, dotted
    "tangential": "#eab308",  # yellow (sliding assist along contact)
    "align": "#fb923c",  # orange (orientation-consensus tidy regime)
}
FORCE_CAUSE_ORDER = ("repulsion", "attraction", "courtyard", "ghost", "tangential", "align")
FORCE_CAUSE_DASH = {
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

    One arrow per force cause (repulsion/attraction/courtyard/
    ghost/tangential) so parameter tuning can see which knob is doing
    the pushing.
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
        f'stroke-dasharray="0.3 0.2" opacity="0.9">'
    ]
    for fp in model.footprints:
        if fp.uuid not in proposal.deltas:
            continue
        for (x1, y1), (x2, y2) in _courtyard_edges(fp):
            out.append(
                f'<line x1="{_fmt(x1)}" y1="{_fmt(y1)}" '
                f'x2="{_fmt(x2)}" y2="{_fmt(y2)}"/>'
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
    include_force_markers: bool = False,
    extra_power_nets: Collection[str] = (),
    show_fills: bool = False,
    color_mode: str = "net",
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
        include_force_markers: If True, include force marker definitions (arrowheads)
            even when show_forces=False. Needed when motion frames exist so the
            client can draw force vectors client-side with proper markers.
        extra_power_nets: user-marked power membership (rail colors, rail
            pad labels, decap halos follow it).
        show_fills: draw KiCad's cached ``filled_polygon`` islands inside
            each zone (stale after edits; use --refill-zones to refresh).
        color_mode: ``"net"`` (default) colors copper per net; ``"layer"``
            colors tracks/arcs/vias/pads solid by copper layer for front/rear
            (and inner) distinction in routing mode. Net identity is preserved
            via ``data-net`` / ``<title>`` in both modes.
    """
    colors = net_colors(model, extra_power_nets)
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
        view = apply_deltas(
            model, deltas,
            label_deltas=getattr(proposal, "label_deltas", None) or None,
        )
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
    if show_forces or include_force_markers:
        parts.append(_force_markers())

    locked_uuids = {fp.uuid for fp in model.footprints if fp.locked and fp.uuid}
    pinned = pinned_uuids or set()
    selected = selected_uuids or set()

    parts.extend(_edge_cuts_svg(model))
    parts.extend(_zones_svg(model, extra_power_nets, show_fills=show_fills))
    parts.extend(_footprints_svg(view, colors, locked_uuids, pinned, selected, extra_power_nets,
                                 color_mode=color_mode))
    parts.extend(_ghost_footprints_svg(view, colors))
    parts.extend(_tracks_svg(model, colors, color_mode=color_mode))
    parts.extend(_vias_svg(model, colors, color_mode=color_mode))
    if moved:
        parts.extend(_proposal_overlay(model, proposal))
    if proposal is not None and show_forces:
        # Forces are evaluated at the committed board positions (they predict
        # the next step from where the board is), so anchor them on `model`,
        # not the preview-shifted `view`. Displacement ghosts/arrows above
        # already show where the proposal would move to.
        parts.extend(_forces_overlay(model, proposal))
    if drc_violations:
        parts.extend(_drc_violations_overlay(drc_violations, drc_ignored_types, stale=drc_stale))
    parts.append("</svg>")
    return "\n".join(parts)