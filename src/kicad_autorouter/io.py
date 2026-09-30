"""Pure-Python read / modify / write for `.kicad_pcb` + `.kicad_sch` pairs.

The portable structural half of the ingest → parse → writeback stages: it
round-trips a board/schematic pair through the :class:`~kicad_autorouter.sexpr.SExpr`
tree without ``pcbnew``, so it runs in the project .venv and CI. Per the 2026-09-29
decision, routing-accurate semantics still come from ``pcbnew`` via
``pcbnew_adapter.py``; this module is the headless plumbing those edits will be
written against (and a test double for them). ``validate.py`` / kicad-cli remains
the authority on whether a written file loads cleanly in KiCad itself.
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


def no_op_edit(tree: SExpr) -> SExpr:
    return tree

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
