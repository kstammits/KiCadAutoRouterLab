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
    """Identity edit; the hook that real autorouter edits will replace."""
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


def noop_roundtrip(
    pcb_path: Union[str, Path],
    sch_path: Union[str, Path],
    out_dir: Union[str, Path, None] = None,
) -> tuple[Path, Path]:
    """Load a pair, apply the no-op edit, and write new files.

    A pure read→write round trip that proves the pipeline plumbing before real
    routing edits exist. Output names are ``<stem>_no_op.kicad_pcb`` /
    ``<stem>_no_op.kicad_sch`` in ``out_dir`` (default: the input's directory).
    """
    pair = load_pair(pcb_path, sch_path)
    edited = replace(pair, pcb=no_op_edit(pair.pcb), sch=no_op_edit(pair.sch))
    base = Path(out_dir) if out_dir is not None else Path(pcb_path).parent
    out_pcb = base / (Path(pcb_path).stem + "_no_op.kicad_pcb")
    out_sch = base / (Path(sch_path).stem + "_no_op.kicad_sch")
    return save_pair(edited, out_pcb, out_sch)


class RoundtripError(ValueError):
    """Raised when a written file does not re-parse to the expected tree."""


def verify_noop(
    pcb_path: Union[str, Path],
    sch_path: Union[str, Path],
    out_pcb: Union[str, Path],
    out_sch: Union[str, Path],
) -> None:
    """Re-parse written files and require structural equality with the originals.

    Raises :class:`RoundtripError` if either tree changed; also re-checks the
    header tokens of the written files via :func:`load_pair`.
    """
    orig = load_pair(pcb_path, sch_path)
    new = load_pair(out_pcb, out_sch)
    if new.pcb != orig.pcb or new.sch != orig.sch:
        raise RoundtripError(
            f"round trip changed content: {out_pcb} / {out_sch} no longer match "
            f"{pcb_path} / {sch_path}"
        )
