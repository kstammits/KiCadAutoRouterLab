"""Pure-Python read / modify / write for `.kicad_pcb` + `.kicad_sch` pairs.

The portable structural half of the ingest → parse → writeback stages: it
round-trips a board/schematic pair through the :class:`~kicad_autorouter.sexpr.SExpr`
tree without ``pcbnew``, so it runs in the project .venv and CI. Per the 2026-09-29
decision, routing-accurate semantics still come from ``pcbnew`` via
``pcbnew_adapter.py``; this module is the headless plumbing those edits will be
written against (and a test double for them). The no-op edit is
:func:`nudge_footprint` with zero deltas: it rewrites one footprint's position
by +0 mm, exercising read/modify/write without moving anything. ``validate.py``
/ kicad-cli remains the authority on whether a written file loads cleanly in
KiCad itself.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional, Set, Union

from .sexpr import SExpr, parse_file, to_sexpr, Symbol
from .board_model import _to_local


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


def find_footprint(tree: SExpr, ref: str) -> SExpr:
    """Return the top-level footprint node whose Reference property is `ref`."""
    for fp in tree.children("footprint"):
        for prop in fp.children("property"):
            if len(prop.args) >= 2 and prop.args[0] == "Reference" and prop.args[1] == ref:
                return fp
    raise KeyError(f"no footprint with reference {ref!r}")


def find_footprint_by_uuid(tree: SExpr, uuid: str) -> SExpr:
    """Return the top-level footprint node whose ``(uuid ...)`` token matches.

    Addresses footprints a reference cannot identify uniquely — duplicate or
    missing refs — since every placed footprint carries its own UUID.
    """
    for fp in tree.children("footprint"):
        u = fp.find("uuid")
        if u is not None and u.args and str(u.args[0]) == uuid:
            return fp
    raise KeyError(f"no footprint with uuid {uuid!r}")


def _net_name_of(node: Optional[SExpr]) -> Optional[str]:
    """Extract net name from a (net <name>) node."""
    if node is None or not node.args:
        return None
    value = node.args[0]
    name = str(value) if isinstance(value, str) else None
    return name or None


def rip_up_nets(
    tree: SExpr,
    affected_nets: Set[str],
    protected_nets: Optional[Set[str]] = None,
) -> SExpr:
    """Return the board tree with (segment) and (via) nodes removed for affected nets.

    Tracks/vias on protected nets are preserved.

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
        if node.head not in ("segment", "via"):
            return True
        net_node = node.find("net")
        net_name = _net_name_of(net_node)
        if net_name is None:
            return True  # Keep tracks without net assignment
        if net_name in protected:
            return True
        return net_name not in affected_nets

    new_children = [c for c in tree.args if should_keep(c)]
    return replace(tree, args=tuple(new_children))


def _shift_at(tree: SExpr, fp: SExpr, dx_mm: float = 0.0, dy_mm: float = 0.0, da_deg: float = 0.0) -> SExpr:
    """Return ``tree`` with footprint node ``fp``'s ``(at ...)`` position and angle shifted."""
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
    new_fp = replace(fp, args=tuple(new_at if a is at else a for a in fp.args))
    return replace(tree, args=tuple(new_fp if a is fp else a for a in tree.args))


def nudge_footprint(
    tree: SExpr, ref: str, dx_mm: float = 0.0, dy_mm: float = 0.0, da_deg: float = 0.0
) -> SExpr:
    """Return the board tree with footprint `ref` shifted by (dx_mm, dy_mm, da_deg).

    Only the footprint's `(at ...)` position is rewritten; pads, nets, ERC
    linkage and every other node pass through untouched. A zero delta is the
    no-op edit used to exercise read/modify/write without moving anything.
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


def _pad_to_sexpr(pad) -> SExpr:
    """Convert a Pad dataclass to KiCad pad S-expression."""
    # Pad position is in board coordinates, need to convert to local
    # For now, assume pads are at local positions relative to footprint center
    # The board_model pads have absolute positions; we need to compute local
    
    args = [
        Symbol(pad.number),
        Symbol(pad.pad_type),
        Symbol(pad.shape),
    ]
    
    # at position (local coordinates)
    args.append(_format_at(pad.position.x_mm, pad.position.y_mm, pad.angle_deg))
    
    # size
    args.append(_format_size(pad.size_mm[0], pad.size_mm[1]))
    
    # drill
    if pad.drill_mm > 0:
        args.append(_format_drill(pad.drill_mm))
    
    # layers
    if pad.layers:
        args.append(_format_layers(pad.layers))
    
    # net
    if pad.net_name:
        args.append(SExpr("net", (pad.net_name,)))
    
    # pinfunction/pintype if available
    if hasattr(pad, 'pinfunction') and pad.pinfunction:
        args.append(SExpr("pinfunction", (pad.pinfunction,)))
    if hasattr(pad, 'pintype') and pad.pintype:
        args.append(SExpr("pintype", (pad.pintype,)))
    
    # uuid
    import uuid as uuid_module
    args.append(SExpr("uuid", (uuid_module.uuid4().hex,)))
    
    return SExpr("pad", tuple(args))


def _courtyard_to_sexpr(courtyard_segments) -> list[SExpr]:
    """Convert courtyard segments to fp_line/fp_circle S-expressions."""
    sexprs = []
    for start, end in courtyard_segments:
        # Check if it's a circle approximation (many small segments)
        # For simplicity, emit as fp_line
        sexprs.append(SExpr("fp_line", (
            SExpr("start", (start.x_mm, start.y_mm)),
            SExpr("end", (end.x_mm, end.y_mm)),
            _format_stroke(0.05),
            SExpr("layer", (Symbol("F.CrtYd"),)),
            SExpr("uuid", (__import__("uuid").uuid4().hex,)),
        )))
    return sexprs


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


def footprint_to_sexpr(fp, footprint_library_id: str = "Test:R_0805") -> SExpr:
    """Convert a Footprint dataclass to KiCad footprint S-expression.
    
    Args:
        fp: Footprint dataclass from board_model (pads/courtyard in board coords)
        footprint_library_id: Library identifier like "Resistor_SMD:R_0805"
    
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
            SExpr("layer", (Symbol("F.CrtYd"),)),
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


def add_footprint_to_tree(tree: SExpr, fp, footprint_library_id: str = "Test:R_0805") -> SExpr:
    """Add a footprint to a PCB S-expression tree.
    
    Args:
        tree: PCB SExpr tree (head="kicad_pcb")
        fp: Footprint dataclass
        footprint_library_id: Library identifier
    
    Returns:
        New tree with footprint added
    """
    new_fp = footprint_to_sexpr(fp, footprint_library_id)
    new_args = list(tree.args) + [new_fp]
    return SExpr(tree.head, tuple(new_args))
