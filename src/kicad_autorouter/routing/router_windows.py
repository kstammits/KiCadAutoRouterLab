"""A* window helpers (split from router.py)."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Tuple



@dataclass(frozen=True)
class Window:
    """Inclusive board-cell rectangle ``[c0..c1] x [r0..r1]``."""

    c0: int
    r0: int
    c1: int
    r1: int


def for_pair(
    sc: int,
    sr: int,
    gc: int,
    gr: int,
    W: int,
    H: int,
    margin: int,
) -> Window:
    """Bounding box of both terminals dilated by ``margin`` and clipped."""
    m = max(0, int(margin))
    return Window(
        c0=max(0, min(sc, gc) - m),
        r0=max(0, min(sr, gr) - m),
        c1=min(W - 1, max(sc, gc) + m),
        r1=min(H - 1, max(sr, gr) + m),
    )


def grow(window: Window, W: int, H: int, factor: float = 2.0) -> Window:
    """Expand about the center by ``factor`` (each side), clipped to board."""
    f = max(1.0, float(factor))
    cw = (window.c0 + window.c1) / 2.0
    rw = (window.r0 + window.r1) / 2.0
    hw = (window.c1 - window.c0 + 1) * f / 2.0
    hh = (window.r1 - window.r0 + 1) * f / 2.0
    return Window(
        c0=max(0, int(cw - hw)),
        r0=max(0, int(rw - hh)),
        c1=min(W - 1, int(cw + hw)),
        r1=min(H - 1, int(rw + hh)),
    )


def full_board(W: int, H: int) -> Window:
    """Fallback window covering the whole board."""
    return Window(c0=0, r0=0, c1=W - 1, r1=H - 1)


def _box_mm(grid, box):
    """Board-mm edges of a window box (None when unrestricted)."""
    if box is None:
        return None
    ox, oy = grid.origin_mm
    res = grid.resolution_mm
    return [round(ox + box.c0 * res, 3), round(oy + box.r0 * res, 3),
            round(ox + (box.c1 + 1) * res, 3),
            round(oy + (box.r1 + 1) * res, 3)]
