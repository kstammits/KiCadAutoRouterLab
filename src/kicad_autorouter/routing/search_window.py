"""Search windows: bound each A* run to a sub-rectangle of the board.

A long 2-pad net (e.g. VIN across 20cm of DCCF) otherwise floods the full
``H x W x layers`` grid: every pair-search allocates a full-size ``g_score``
array and may expand far from the straight-line corridor. A window clips
both the allocation and the expansion to ``bbox(start, goal) + margin``,
grown on failure with a full-board fallback so routability never regresses.
"""

from __future__ import annotations

from dataclasses import dataclass


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
