"""Pure-Python read / modify / write for `.kicad_pcb` + `.kicad_sch` pairs.

The portable structural half of the ingest → parse → writeback stages: it
round-trips a board/schematic pair through the :class:`~kicad_autorouter.sexpr.SExpr`
tree without ``pcbnew``, so it runs in the project .venv and CI.
This module is the headless read/modify/write plumbing. The no-op edit is
:func:`nudge_footprint` with zero deltas: it rewrites one footprint's position
by +0 mm, exercising read/modify/write without moving anything. ``validate.py``
/ kicad-cli remains the authority on whether a written file loads cleanly in
KiCad itself.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Union

from .sexpr import SExpr, parse_file, to_sexpr, Symbol
from .board_model import _net_name, _to_local, _zone_layers, copper_layers


@dataclass(frozen=True)
class KicadPair:
    """A parsed board + schematic pair with their source paths."""

    pcb: SExpr
    sch: SExpr
    pcb_path: Path
    sch_path: Path


def load_pair(pcb_path: Union[str, Path], sch_path: Union[str, Path]) -> KicadPair:
    """Parse a `.kicad_pcb` / `.kicad_sch` pair and check their header tokens."""
    pcb = parse_file(Path(pcb_path))
    sch = parse_file(Path(sch_path))
    if pcb.head != "kicad_pcb":
        raise ValueError(
            f"expected (kicad_pcb ...) in {pcb_path}, got head {pcb.head!r}"
        )
    if sch.head != "kicad_sch":
        raise ValueError(
            f"expected (kicad_sch ...) in {sch_path}, got head {sch.head!r}"
        )
    return KicadPair(pcb, sch, Path(pcb_path), Path(sch_path))


def _shift_mm(value, delta_mm):
    """Add a millimeter delta to a parsed position value.

    Integer-valued results of shifting an integer stay integers so that a
    zero-millimeter nudge re-serializes exactly like the source file.
    """
    result = value + delta_mm
    if isinstance(value, int) and isinstance(result, float) and result.is_integer():
        return int(result)
    return result


def _find_footprint_where(tree: SExpr, pred, missing: str) -> SExpr:
    """Return the first top-level footprint node satisfying ``pred``."""
    for fp in tree.children("footprint"):
        if pred(fp):
            return fp
    raise KeyError(missing)


def find_footprint(tree: SExpr, ref: str) -> SExpr:
    """Return the top-level footprint node whose Reference property is `ref`."""
    def _matches(fp: SExpr) -> bool:
        return any(
            len(prop.args) >= 2 and prop.args[0] == "Reference" and prop.args[1] == ref
            for prop in fp.children("property")
        )
    return _find_footprint_where(tree, _matches, f"no footprint with reference {ref!r}")


def find_footprint_by_uuid(tree: SExpr, uuid: str) -> SExpr:
    """Return the top-level footprint node whose ``(uuid ...)`` token matches.

    Addresses footprints a reference cannot identify uniquely — duplicate or
    missing refs — since every placed footprint carries its own UUID.
    """
    def _matches(fp: SExpr) -> bool:
        u = fp.find("uuid")
        return u is not None and bool(u.args) and str(u.args[0]) == uuid
    return _find_footprint_where(tree, _matches, f"no footprint with uuid {uuid!r}")


def copper_counts_by_net(tree: SExpr) -> Dict[str, Dict[str, int]]:
    """Count ``(segment)``/``(arc)``/``(via)`` nodes per net name.

    Returns ``{net_name: {"tracks": n, "arcs": a, "vias": m}}``. Nodes
    without a net assignment are skipped.
    """
    from collections import defaultdict

    counts: Dict[str, Dict[str, int]] = defaultdict(lambda: {"tracks": 0, "arcs": 0, "vias": 0})
    for seg in tree.children("segment"):
        name = _net_name(seg.find("net"))
        if name:
            counts[name]["tracks"] += 1
    for arc in tree.children("arc"):
        name = _net_name(arc.find("net"))
        if name:
            counts[name]["arcs"] += 1
    for via in tree.children("via"):
        name = _net_name(via.find("net"))
        if name:
            counts[name]["vias"] += 1
    return dict(counts)


def rip_up_nets(
    tree: SExpr,
    affected_nets: Set[str],
    protected_nets: Optional[Set[str]] = None,
) -> SExpr:
    """Return the board tree with (segment), (arc) and (via) nodes removed for affected nets.

    Tracks/arcs/vias on protected nets are preserved.

    Args:
        tree: PCB S-expression tree
        affected_nets: Net names whose tracks/vias should be removed
        protected_nets: Net names to never rip up (e.g., power rails)

    Returns:
        New SExpr tree with affected tracks/vias filtered out
    """
    if not affected_nets:
        return tree

    protected = protected_nets or set()

    def should_keep(node: SExpr) -> bool:
        if node.head not in ("segment", "arc", "via"):
            return True
        net_node = node.find("net")
        net_name = _net_name(net_node)
        if net_name is None:
            return True  # Keep tracks without net assignment
        if net_name in protected:
            return True
        return net_name not in affected_nets

    new_children = [c for c in tree.args if should_keep(c)]
    return replace(tree, args=tuple(new_children))


COLLAPSE_OUTERS = ("F.Cu", "B.Cu")

# Top-level copper graphics that carry a ``(layer …)`` child. Inner-copper
# instances are dropped on collapse; every other layer passes through.
COLLAPSE_ANNOTATION_HEADS = frozenset({
    "gr_text",
    "gr_rect",
    "gr_circle",
    "gr_line",
    "gr_arc",
    "gr_poly",
    "gr_curve",
    "dimension",
    "leader",
    "target",
})

COLLAPSE_IMPEDANCE_NOTE = (
    "collapse keeps the first dielectric/core (thickness untouched) and "
    "restacks every via as a through via (F.Cu-B.Cu): controlled-impedance "
    "geometry changed — re-verify impedance before fabrication."
)


def _is_inner_copper(name: object) -> bool:
    """True for inner copper layer names (``InN.Cu``)."""
    return (
        isinstance(name, str)
        and name.startswith("In")
        and name.endswith(".Cu")
    )


def _collapse_zone(zone: SExpr) -> Tuple[Optional[SExpr], bool, bool, int]:
    """Restrict one ``(zone …)`` (pour or keepout) to the outer layers.

    Returns ``(new_zone_or_None, removed, restricted, fills_dropped)``:
    zones with no outer layer are deleted wholesale (cached fills go with
    them); otherwise the ``(layers …)`` / ``(layer …)`` node is rewritten
    to the outer intersection (singular form for one layer, matching
    KiCad's own convention) and inner-layer ``(filled_polygon …)`` caches
    are dropped.
    """
    names = _zone_layers(zone)
    if names and not any(layer in COLLAPSE_OUTERS for layer in names):
        return None, True, False, 0
    keep = tuple(layer for layer in names if layer in COLLAPSE_OUTERS)
    restricted = bool(names) and (len(keep) != len(names))
    layer_node = zone.find("layers") or zone.find("layer")
    new_args: List[object] = []
    changed = restricted
    for child in zone.args:
        if restricted and child is layer_node:
            if len(keep) == 1:
                new_args.append(SExpr("layer", (keep[0],)))
            else:
                new_args.append(SExpr("layers", keep))
        elif (
            isinstance(child, SExpr)
            and child.head == "filled_polygon"
        ):
            fill_layer = child.find("layer")
            fill_name = (
                str(fill_layer.args[0])
                if fill_layer is not None and fill_layer.args
                else ""
            )
            if fill_name and fill_name not in COLLAPSE_OUTERS:
                changed = True
                continue
            new_args.append(child)
        else:
            new_args.append(child)
    if not changed:
        return zone, False, False, 0
    fills_dropped = sum(
        1 for c in zone.args
        if isinstance(c, SExpr) and c.head == "filled_polygon"
    ) - sum(
        1 for c in new_args
        if isinstance(c, SExpr) and c.head == "filled_polygon"
    )
    return replace(zone, args=tuple(new_args)), False, restricted, fills_dropped


def _collapse_stackup(stackup: SExpr) -> SExpr:
    """Return ``(stackup …)`` without inner coppers or extra dielectrics.

    Keeps every other child verbatim (outer finish layers, the first
    dielectric/core with its thickness untouched, ``copper_finish``,
    constraints). Always keeps the first dielectric-class layer (``core``
    / ``prepreg`` type, or ``dielectric*`` name fallback) and drops the
    rest.
    """
    kept_dielectric = False
    new_children: List[object] = []
    for child in stackup.args:
        if (
            isinstance(child, SExpr)
            and child.head == "layer"
            and child.args
            and isinstance(child.args[0], str)
        ):
            name = child.args[0]
            if _is_inner_copper(name):
                continue
            type_node = child.find("type")
            type_name = (
                str(type_node.args[0]).lower()
                if type_node is not None and type_node.args
                else ""
            )
            if type_name in ("core", "prepreg") or name.lower().startswith("dielectric"):
                if kept_dielectric:
                    continue
                kept_dielectric = True
        new_children.append(child)
    return replace(stackup, args=tuple(new_children))


def collapse_to_two_layer(tree: SExpr) -> Tuple[SExpr, dict]:
    """Return ``(tree, report)`` with 4-layer content collapsed to 2-layer.

    Pure, same style as :func:`rip_up_nets`: drops inner-layer
    ``(segment)`` and ``(arc)`` track nodes, drops top-level
    ``(filled_polygon)`` caches and copper annotations (``gr_*`` /
    ``dimension`` / ``leader`` / ``target``) whose ``(layer …)`` is inner
    copper, restricts or deletes ``(zone)`` nodes, restacks every via to
    ``(layers "F.Cu" "B.Cu")`` (never drops — orphaned through-vias are
    harmless and DRC-clean), and prunes the stackup + ``(layers …)``
    header to the two outer coppers. Footprints, pads, netclasses and
    ``pcbplotparams`` (including the ``layerselection`` bitmask, which
    names no layers explicitly) pass through untouched, so ``grep In1.Cu``
    can still hit non-structural spots such as footprint pad wildcard
    templates. A 2-layer input returns the same tree object with a zeroed
    report (byte-identical output).
    """
    stackup_before = list(copper_layers(tree))
    tracks_removed = 0
    arcs_removed = 0
    zones_removed = 0
    zones_restricted = 0
    vias_converted = 0
    affected: Set[str] = set()
    changed = False
    new_children: List[object] = []

    def _note_net(net_node: Optional[SExpr]) -> None:
        name = _net_name(net_node)
        if name:
            affected.add(name)

    for child in tree.args:
        if not isinstance(child, SExpr):
            new_children.append(child)
            continue
        if child.head in ("segment", "arc"):
            layer_node = child.find("layer")
            layer = str(layer_node.args[0]) if layer_node is not None and layer_node.args else ""
            if layer and layer not in COLLAPSE_OUTERS:
                if child.head == "arc":
                    arcs_removed += 1
                else:
                    tracks_removed += 1
                changed = True
                _note_net(child.find("net"))
                continue
            new_children.append(child)
        elif child.head == "filled_polygon":
            layer_node = child.find("layer")
            layer = str(layer_node.args[0]) if layer_node is not None and layer_node.args else ""
            if layer and layer not in COLLAPSE_OUTERS:
                tracks_removed += 1
                changed = True
                continue
            new_children.append(child)
        elif child.head in COLLAPSE_ANNOTATION_HEADS:
            layer_node = child.find("layer")
            layer = str(layer_node.args[0]) if layer_node is not None and layer_node.args else ""
            if _is_inner_copper(layer):
                tracks_removed += 1
                changed = True
                _note_net(child.find("net"))
                continue
            new_children.append(child)
        elif child.head == "via":
            layers_node = child.find("layers")
            if layers_node is None:
                new_children.append(child)
                continue
            current = tuple(a for a in layers_node.args if not isinstance(a, SExpr))
            if tuple(str(a) for a in current) == COLLAPSE_OUTERS:
                new_children.append(child)
                continue
            restacked = replace(
                child,
                args=tuple(
                    SExpr("layers", COLLAPSE_OUTERS) if c is layers_node else c
                    for c in child.args
                ),
            )
            vias_converted += 1
            changed = True
            _note_net(child.find("net"))
            new_children.append(restacked)
        elif child.head == "zone":
            new_zone, removed, restricted, _fills = _collapse_zone(child)
            if removed:
                zones_removed += 1
                changed = True
                _note_net(child.find("net"))
                continue
            if restricted:
                zones_restricted += 1
                changed = True
                _note_net(child.find("net"))
            elif new_zone is not child:
                changed = True
                _note_net(child.find("net"))
            new_children.append(new_zone)
        elif child.head == "setup":
            stackup = child.find("stackup")
            if stackup is None:
                new_children.append(child)
                continue
            collapsed = _collapse_stackup(stackup)
            if to_sexpr(collapsed) != to_sexpr(stackup):
                changed = True
                new_children.append(
                    replace(
                        child,
                        args=tuple(
                            collapsed if c is stackup else c for c in child.args
                        ),
                    )
                )
            else:
                new_children.append(child)
        elif child.head == "layers":
            kept = tuple(
                entry for entry in child.args
                if not (
                    isinstance(entry, SExpr)
                    and any(_is_inner_copper(a) for a in entry.args)
                )
            )
            if len(kept) != len(child.args):
                changed = True
                new_children.append(replace(child, args=kept))
            else:
                new_children.append(child)
        else:
            new_children.append(child)

    if not changed:
        report = {
            "tracks_removed": 0,
            "arcs_removed": 0,
            "zones_removed": 0,
            "zones_restricted": 0,
            "vias_converted": 0,
            "vias_dropped": 0,
            "affected_nets": [],
            "stackup_before": stackup_before,
            "stackup_after": stackup_before,
            "impedance_note": None,
        }
        return tree, report
    new_tree = replace(tree, args=tuple(new_children))
    report = {
        "tracks_removed": tracks_removed,
        "arcs_removed": arcs_removed,
        "zones_removed": zones_removed,
        "zones_restricted": zones_restricted,
        "vias_converted": vias_converted,
        "vias_dropped": 0,
        "affected_nets": sorted(affected),
        "stackup_before": stackup_before,
        "stackup_after": list(copper_layers(new_tree)),
        "impedance_note": COLLAPSE_IMPEDANCE_NOTE,
    }
    return new_tree, report


def _shift_at(tree: SExpr, fp: SExpr, dx_mm: float = 0.0, dy_mm: float = 0.0, da_deg: float = 0.0) -> SExpr:
    """Return ``tree`` with footprint node ``fp``'s ``(at ...)`` position and angle shifted.

    Pad ``(at ...)`` angles store the effective board orientation (verified
    via pcbnew: tube111 Q1 fp 90 stores pads at 90 = GetOrientation 90),
    so a footprint rotation by ``da_deg`` must add ``da_deg`` to every pad
    angle too — otherwise the re-parsed pads keep their stale orientation
    and square-pad previews (``apply_deltas``) diverge from the file.
    Pad local ``x y`` positions are untouched (they live in the footprint
    frame and rotate via the footprint angle). A zero delta is still the
    exact no-op (nothing rewritten).
    """
    at = fp.find("at")
    if at is None or len(at.args) < 2:
        raise ValueError("footprint has no (at x y ...) position")
    args = list(at.args)
    args[0] = _shift_mm(args[0], dx_mm)
    args[1] = _shift_mm(args[1], dy_mm)
    if len(args) >= 3:
        args[2] = _shift_mm(args[2], da_deg)
    elif da_deg != 0.0:
        args.append(da_deg)
    new_at = SExpr("at", tuple(args))
    new_fp_args = []
    for child in fp.args:
        if child is at:
            new_fp_args.append(new_at)
        elif da_deg != 0.0 and isinstance(child, SExpr) and child.head == "pad":
            new_fp_args.append(_shift_pad_angle(child, da_deg))
        else:
            new_fp_args.append(child)
    new_fp = replace(fp, args=tuple(new_fp_args))
    return replace(tree, args=tuple(new_fp if a is fp else a for a in tree.args))


def _shift_pad_angle(pad: SExpr, da_deg: float) -> SExpr:
    """Return ``pad`` with its ``(at x y [angle])`` angle advanced by ``da_deg``.

    Normalizes to [0, 360) to match ``board_model.apply_deltas`` and KiCad's
    own serialization (which writes normalized absolute orientations).
    """
    at = pad.find("at")
    if at is None or len(at.args) < 2:
        return pad
    args = list(at.args)
    if len(args) >= 3 and isinstance(args[2], (int, float)):
        new_angle = (float(args[2]) + da_deg) % 360.0
        if isinstance(args[2], int) and float(new_angle).is_integer():
            new_angle = int(new_angle)
        args[2] = new_angle
    elif da_deg != 0.0:
        new_angle = da_deg % 360.0
        args.append(new_angle)
    else:
        return pad
    new_at = SExpr("at", tuple(args))
    return replace(pad, args=tuple(new_at if a is at else a for a in pad.args))


def nudge_footprint(
    tree: SExpr, ref: str, dx_mm: float = 0.0, dy_mm: float = 0.0, da_deg: float = 0.0
) -> SExpr:
    """Return the board tree with footprint `ref` shifted by (dx_mm, dy_mm, da_deg).

    The footprint's `(at ...)` position is rewritten and each pad's `(at ...)`
    angle is advanced by `da_deg` (pad angles store the absolute board
    orientation). Pad local `x y`, nets, ERC linkage and every other node pass
    through untouched. A zero delta is the no-op edit used to exercise
    read/modify/write without moving anything.
    """
    return _shift_at(tree, find_footprint(tree, ref), dx_mm, dy_mm, da_deg)


def nudge_footprint_by_uuid(
    tree: SExpr, uuid: str, dx_mm: float = 0.0, dy_mm: float = 0.0, da_deg: float = 0.0
) -> SExpr:
    """Return the board tree with the footprint carrying `uuid` shifted by (dx_mm, dy_mm, da_deg).

    Same guarantees as :func:`nudge_footprint`, addressed by UUID instead of
    reference so duplicate or missing refs remain addressable at writeback time.
    """
    return _shift_at(tree, find_footprint_by_uuid(tree, uuid), dx_mm, dy_mm, da_deg)


def save_pair(
    pair: KicadPair, out_pcb: Union[str, Path], out_sch: Union[str, Path]
) -> tuple[Path, Path]:
    """Serialize both trees to new files (creating parent dirs). Returns the paths."""
    pcb_path = Path(out_pcb)
    sch_path = Path(out_sch)
    for path, tree in ((pcb_path, pair.pcb), (sch_path, pair.sch)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(to_sexpr(tree) + "\n", encoding="utf-8")
    return pcb_path, sch_path


def _format_at(x_mm: float, y_mm: float, angle_deg: float = 0.0) -> SExpr:
    """Create an (at x y [angle]) SExpr."""
    if angle_deg != 0.0:
        return SExpr("at", (x_mm, y_mm, angle_deg))
    return SExpr("at", (x_mm, y_mm))


def _format_size(w: float, h: float) -> SExpr:
    """Create a (size w h) SExpr."""
    return SExpr("size", (w, h))


def _format_drill(drill_mm: float) -> SExpr:
    """Create a (drill d) SExpr."""
    return SExpr("drill", (drill_mm,))


def _format_layers(layers: Tuple[str, ...]) -> SExpr:
    """Create a (layers ...) SExpr."""
    return SExpr("layers", layers)


def _format_stroke(width: float, type_: str = "solid") -> SExpr:
    """Create a (stroke (width w) (type t)) SExpr."""
    return SExpr("stroke", (
        SExpr("width", (width,)),
        SExpr("type", (Symbol(type_),)),
    ))


def _format_effects(font_size: Tuple[float, float] = (1.0, 1.0), thickness: float = 0.15, hide: bool = False) -> SExpr:
    """Create an (effects ...) SExpr for text."""
    args = [
        SExpr("font", (
            SExpr("size", font_size),
            SExpr("thickness", (thickness,)),
        ))
    ]
    if hide:
        args.append(SExpr("hide", (Symbol("yes"),)))
    return SExpr("effects", tuple(args))


def _reference_text_to_sexpr(ref: str, x_mm: float = 0.0, y_mm: float = 0.0, layer: str = "F.Fab") -> SExpr:
    """Create fp_text for reference designator."""
    return SExpr("fp_text", (
        Symbol("user"),
        f"${{REFERENCE}}",
        _format_at(x_mm, y_mm, 0.0),
        SExpr("layer", (Symbol(layer),)),
        SExpr("uuid", (__import__("uuid").uuid4().hex,)),
        _format_effects(hide=True),
    ))


def footprint_to_sexpr(fp, footprint_library_id: str = "Test:R_0805", courtyard_layer: str = "F.CrtYd") -> SExpr:
    """Convert a Footprint dataclass to KiCad footprint S-expression.
    
    Args:
        fp: Footprint dataclass from board_model (pads/courtyard in board coords)
        footprint_library_id: Library identifier like "Resistor_SMD:R_0805"
        courtyard_layer: Graphics layer for the courtyard outline (``F.CrtYd``
            default; ``F.Fab`` keeps ghosts on a non-copper layer — the
            parser falls back crt > fab > silk, so physics still sees it)
    
    Returns:
        SExpr representing the footprint with pads/courtyard in LOCAL coordinates
    """
    args = [Symbol(footprint_library_id)]
    
    if fp.locked:
        args.append(SExpr("locked", (Symbol("yes"),)))
    
    args.append(SExpr("layer", (Symbol(fp.layer),)))
    
    if fp.uuid:
        args.append(SExpr("uuid", (fp.uuid,)))
    
    # Position in board coordinates
    args.append(_format_at(fp.x_mm, fp.y_mm, fp.angle_deg))
    
    # Description (optional)
    if hasattr(fp, 'footprint_id') and fp.footprint_id:
        args.append(SExpr("descr", (fp.footprint_id,)))
    
    # Reference property
    args.append(SExpr("property", (
        Symbol("Reference"),
        fp.ref,
        _format_at(0, -3.35, 0.0),
        SExpr("layer", (Symbol("F.SilkS"),)),
        SExpr("hide", (Symbol("yes"),)),
        SExpr("uuid", (__import__("uuid").uuid4().hex,)),
        _format_effects(),
    )))
    
    # Value property
    args.append(SExpr("property", (
        Symbol("Value"),
        fp.footprint_id,
        _format_at(0, 3.35, 0.0),
        SExpr("layer", (Symbol("F.Fab"),)),
        SExpr("hide", (Symbol("yes"),)),
        SExpr("uuid", (__import__("uuid").uuid4().hex,)),
        _format_effects(),
    )))
    
    # Datasheet property (empty)
    args.append(SExpr("property", (
        Symbol("Datasheet"),
        "",
        _format_at(0, 0, 0.0),
        SExpr("unlocked", (Symbol("yes"),)),
        SExpr("layer", (Symbol("F.Fab"),)),
        SExpr("hide", (Symbol("yes"),)),
        SExpr("uuid", (__import__("uuid").uuid4().hex,)),
        _format_effects(font_size=(1.27, 1.27)),
    )))
    
    # Description property (empty)
    args.append(SExpr("property", (
        Symbol("Description"),
        "",
        _format_at(0, 0, 0.0),
        SExpr("unlocked", (Symbol("yes"),)),
        SExpr("layer", (Symbol("F.Fab"),)),
        SExpr("hide", (Symbol("yes"),)),
        SExpr("uuid", (__import__("uuid").uuid4().hex,)),
        _format_effects(font_size=(1.27, 1.27)),
    )))
    
    # Ghost marker + attraction springs (parsed by board_model
    # _is_ghost / _parse_ghost_attractions; ignored by KiCad itself).
    if getattr(fp, 'ghost', False):
        args.append(SExpr("property", (
            "ghost",
            "yes",
            _format_at(0, 0, 0.0),
            SExpr("layer", (Symbol("F.SilkS"),)),
            SExpr("hide", (Symbol("yes"),)),
            SExpr("uuid", (__import__("uuid").uuid4().hex,)),
            _format_effects(),
        )))
        for i, (target_ref, ka) in enumerate(getattr(fp, 'ghost_attractions', ()) or (), start=1):
            args.append(SExpr("property", (
                f"ghost_attract_{i}",
                f"{target_ref},{ka}",
                _format_at(0, 0, 0.0),
                SExpr("layer", (Symbol("F.SilkS"),)),
                SExpr("hide", (Symbol("yes"),)),
                SExpr("uuid", (__import__("uuid").uuid4().hex,)),
                _format_effects(),
            )))

    # Attributes
    args.append(SExpr("attr", (Symbol("exclude_from_pos_files"), Symbol("exclude_from_bom"))))
    
    # Courtyard graphics - convert from board to local coordinates
    for start, end in fp.courtyard:
        local_start = _to_local(start.x_mm, start.y_mm, fp.x_mm, fp.y_mm, fp.angle_deg, fp.layer)
        local_end = _to_local(end.x_mm, end.y_mm, fp.x_mm, fp.y_mm, fp.angle_deg, fp.layer)
        args.append(SExpr("fp_line", (
            SExpr("start", (local_start.x_mm, local_start.y_mm)),
            SExpr("end", (local_end.x_mm, local_end.y_mm)),
            _format_stroke(0.05),
            SExpr("layer", (Symbol(courtyard_layer),)),
            SExpr("uuid", (__import__("uuid").uuid4().hex,)),
        )))
    
    # Reference text on F.Fab (local coordinates)
    args.append(_reference_text_to_sexpr(fp.ref))
    
    # Pads - convert from board to local coordinates
    for pad in fp.pads:
        local_pos = _to_local(pad.position.x_mm, pad.position.y_mm, fp.x_mm, fp.y_mm, fp.angle_deg, fp.layer)
        args.append(SExpr("pad", (
            Symbol(pad.number),
            Symbol(pad.pad_type),
            Symbol(pad.shape),
            _format_at(local_pos.x_mm, local_pos.y_mm, pad.angle_deg),
            _format_size(pad.size_mm[0], pad.size_mm[1]),
            _format_drill(pad.drill_mm) if pad.drill_mm > 0 else SExpr("drill", (0,)),
            _format_layers(pad.layers) if pad.layers else SExpr("layers", ("F.Cu",)),
            SExpr("net", (pad.net_name,)) if pad.net_name else SExpr("net", (0,)),
            SExpr("uuid", (__import__("uuid").uuid4().hex,)),
        )))
    
    # Embedded fonts
    args.append(SExpr("embedded_fonts", (Symbol("no"),)))
    
    return SExpr("footprint", tuple(args))


def add_footprint_to_tree(tree: SExpr, fp, footprint_library_id: str = "Test:R_0805", courtyard_layer: str = "F.CrtYd") -> SExpr:
    """Add a footprint to a PCB S-expression tree.
    
    Args:
        tree: PCB SExpr tree (head="kicad_pcb")
        fp: Footprint dataclass
        footprint_library_id: Library identifier
        courtyard_layer: Graphics layer for the courtyard outline
    
    Returns:
        New tree with footprint added
    """
    new_fp = footprint_to_sexpr(fp, footprint_library_id, courtyard_layer)
    new_args = list(tree.args) + [new_fp]
    return SExpr(tree.head, tuple(new_args))


def add_ghost_to_tree(tree: SExpr, fp, footprint_library_id: str = "Ghost:GHOST", courtyard_layer: str = "F.CrtYd") -> SExpr:
    """Add a ghost (pseudo-part) footprint to a PCB S-expression tree.

    Args:
        tree: PCB SExpr tree (head="kicad_pcb")
        fp: Footprint dataclass with ``ghost=True``
        footprint_library_id: Library identifier written to the footprint head
        courtyard_layer: Graphics layer for the ghost outline (``F.CrtYd``
            default; ``F.Fab`` for a non-courtyard persistence layer)

    Returns:
        New tree with the ghost footprint appended

    Raises:
        ValueError: if ``fp`` is not marked as a ghost
    """
    if not getattr(fp, 'ghost', False):
        raise ValueError("add_ghost_to_tree requires fp.ghost=True")
    return add_footprint_to_tree(tree, fp, footprint_library_id, courtyard_layer)


def remove_all_ghosts_from_tree(tree: SExpr) -> SExpr:
    """Return the board tree with every ghost footprint removed.

    Ghosts are placement-only pseudo-parts (``(property "ghost" "yes")``);
    KiCad must never see them — e.g. DRC runs on a stripped copy so ghost
    walls can't raise ``courtyards_overlap`` against real footprints.
    Real footprints are never removed. Returns the same tree object when
    it holds no ghosts.
    """
    from .board_model import _is_ghost as _is_ghost_node
    kept = tuple(
        a for a in tree.args
        if not (
            isinstance(a, SExpr)
            and a.head == "footprint"
            and _is_ghost_node(a)
        )
    )
    if len(kept) == len(tree.args):
        return tree
    return SExpr(tree.head, kept)


def remove_ghost_from_tree(tree: SExpr, uuid: str) -> SExpr:
    """Return the board tree with the ghost footprint carrying ``uuid`` removed.

    Only removes the node when it carries ``(property "ghost" "yes")``;
    real footprints are never removed by this helper.

    Raises:
        KeyError: if no footprint carries ``uuid``, or it is not a ghost
    """
    from .board_model import _is_ghost as _is_ghost_node
    target = find_footprint_by_uuid(tree, uuid)
    if not _is_ghost_node(target):
        raise KeyError(f"footprint with uuid {uuid!r} is not a ghost")
    return SExpr(tree.head, tuple(a for a in tree.args if a is not target))


def resize_ghost_in_tree(tree: SExpr, uuid: str, w_mm: float, h_mm: float) -> SExpr:
    """Return the board tree with a ghost's outline resized to ``w_mm`` x ``h_mm``.

    The outline is rewritten as a 4-segment rect centered on the footprint
    origin in *local* coordinates, so it stays correct regardless of the
    ghost's board position or angle. The existing graphics layer and stroke
    are preserved (falling back to ``F.CrtYd``). Pads and properties pass
    through untouched.

    Only ``fp_line`` outlines are supported (everything this project
    creates); ghosts with other graphic kinds are refused.

    Raises:
        KeyError: if no footprint carries ``uuid``, or it is not a ghost
        ValueError: for non-positive sizes, missing outlines, or
            non-``fp_line`` graphics
    """
    import math

    for name, val in (("w_mm", w_mm), ("h_mm", h_mm)):
        if not isinstance(val, (int, float)) or not math.isfinite(val) or val <= 0:
            raise ValueError(f"ghost {name} must be > 0, got {val!r}")
    from .board_model import _GRAPHIC_KINDS, _is_ghost as _is_ghost_node
    target = find_footprint_by_uuid(tree, uuid)
    if not _is_ghost_node(target):
        raise KeyError(f"footprint with uuid {uuid!r} is not a ghost")
    graphics = [
        c for c in target.args
        if isinstance(c, SExpr) and c.head in _GRAPHIC_KINDS
    ]
    if not graphics:
        raise ValueError(f"ghost with uuid {uuid!r} has no outline graphics")
    if any(g.head != "fp_line" for g in graphics):
        kinds = sorted({g.head for g in graphics})
        raise ValueError(f"ghost outline uses {kinds}; only fp_line can be resized")
    first_layer = graphics[0].find("layer")
    layer_name = (
        str(first_layer.args[0])
        if first_layer is not None and first_layer.args
        else "F.CrtYd"
    )
    first_stroke = graphics[0].find("stroke")
    stroke = first_stroke if first_stroke is not None else _format_stroke(0.05)
    hw, hh = float(w_mm) / 2.0, float(h_mm) / 2.0
    corners = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
    new_lines = []
    for (x0, y0), (x1, y1) in zip(corners, corners[1:] + corners[:1]):
        new_lines.append(SExpr("fp_line", (
            SExpr("start", (x0, y0)),
            SExpr("end", (x1, y1)),
            stroke,
            SExpr("layer", (Symbol(layer_name),)),
            SExpr("uuid", (__import__("uuid").uuid4().hex,)),
        )))
    graphic_ids = {id(g) for g in graphics}
    new_fp_args = tuple(
        c for c in target.args if id(c) not in graphic_ids
    ) + tuple(new_lines)
    new_fp = replace(target, args=new_fp_args)
    return replace(tree, args=tuple(new_fp if a is target else a for a in tree.args))
