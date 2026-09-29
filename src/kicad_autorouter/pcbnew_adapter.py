"""KiCad pcbnew boundary — the ONLY module that imports `pcbnew`.

Everything else in kicad_autorouter is pure Python and importable under the
project .venv (Python 3.11). This module must run under KiCad's bundled Python
(3.9), which ships the SWIG `pcbnew` bindings:

    /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3

It wraps the small set of pcbnew calls needed for the first read-modify-write
pass: load a board, nudge unlocked footprints, add a track segment, and save.
"""

from __future__ import annotations

from pathlib import Path

try:
    import pcbnew  # type: ignore[import-not-found]
except ImportError as exc:  # pragma: no cover - only outside KiCad's Python
    raise ImportError(
        "The 'pcbnew' module is only available under KiCad's bundled Python. "
        "Run this with:\n"
        "  /Applications/KiCad/KiCad.app/Contents/Frameworks/"
        "Python.framework/Versions/3.9/bin/python3 <script>"
    ) from exc


def load_board(path: str | Path) -> "pcbnew.BOARD":
    """Load a .kicad_pcb file into an in-memory BOARD, or raise on failure."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"board file not found: {path}")
    board = pcbnew.LoadBoard(str(path))
    if board is None:
        raise ValueError(f"pcbnew failed to load board: {path}")
    return board


def unlocked_footprints(board) -> list:
    """Return footprints that are not locked (candidates for placement moves)."""
    return [fp for fp in board.GetFootprints() if not fp.IsLocked()]


def nudge_unlocked_footprints(
    board, dx_mm: float = 1.0, dy_mm: float = 0.0
) -> list[tuple[str, tuple[float, float], tuple[float, float]]]:
    """Shift every unlocked footprint by (dx_mm, dy_mm). Locked parts stay fixed.

    Returns a list of (reference, old_mm, new_mm) tuples for the parts moved.
    This is a deterministic placeholder for the future force-directed nudge.
    """
    dx = pcbnew.FromMM(dx_mm)
    dy = pcbnew.FromMM(dy_mm)
    moved: list[tuple[str, tuple[float, float], tuple[float, float]]] = []
    for fp in unlocked_footprints(board):
        old = fp.GetPosition()
        new = pcbnew.VECTOR2I(old.x + dx, old.y + dy)
        fp.SetPosition(new)
        moved.append(
            (
                fp.GetReference(),
                (pcbnew.ToMM(old.x), pcbnew.ToMM(old.y)),
                (pcbnew.ToMM(new.x), pcbnew.ToMM(new.y)),
            )
        )
    return moved


def add_track(
    board,
    x0_mm: float,
    y0_mm: float,
    x1_mm: float,
    y1_mm: float,
    width_mm: float = 0.25,
    layer=None,
) -> "pcbnew.PCB_TRACK":
    """Create and append a straight track segment on the given copper layer."""
    if layer is None:
        layer = pcbnew.F_Cu
    track = pcbnew.PCB_TRACK(board)
    track.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(x0_mm), pcbnew.FromMM(y0_mm)))
    track.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(x1_mm), pcbnew.FromMM(y1_mm)))
    track.SetWidth(pcbnew.FromMM(width_mm))
    track.SetLayer(int(layer))
    board.Add(track)
    return track


def save_board(board, path: str | Path) -> Path:
    """Save the in-memory board to `path` (creates parent dirs). Returns the path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    board.Save(str(path))
    return path
