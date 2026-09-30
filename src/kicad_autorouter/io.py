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
from typing import Union

from .sexpr import SExpr, parse_file, to_sexpr


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


def nudge_footprint(
    tree: SExpr, ref: str, dx_mm: float = 0.0, dy_mm: float = 0.0
) -> SExpr:
    """Return the board tree with footprint `ref` shifted by (dx_mm, dy_mm).

    Only the footprint's `(at ...)` position is rewritten; pads, nets, ERC
    linkage and every other node pass through untouched. A zero delta is the
    no-op edit used to exercise read/modify/write without moving anything.
    """
    fp = find_footprint(tree, ref)
    at = fp.find("at")
    if at is None or len(at.args) < 2:
        raise ValueError(f"footprint {ref!r} has no (at x y ...) position")
    args = list(at.args)
    args[0] = _shift_mm(args[0], dx_mm)
    args[1] = _shift_mm(args[1], dy_mm)
    new_at = SExpr("at", tuple(args))
    new_fp = replace(fp, args=tuple(new_at if a is at else a for a in fp.args))
    return replace(tree, args=tuple(new_fp if a is fp else a for a in tree.args))


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
