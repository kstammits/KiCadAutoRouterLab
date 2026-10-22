"""Trajectory-frame helpers for placement dynamics.

Frame rounding, force rounding, and uniform downsampling for compact JSON
output in the SVG motion player.
"""

from __future__ import annotations

import math
from typing import Dict, List, Tuple


def _round_sig3(x: float) -> float:
    """Round to 3 significant figures."""
    if x == 0.0 or not math.isfinite(x):
        return 0.0
    return round(x, 2 - int(math.floor(math.log10(abs(x)))))


def _round_force_components(
    comps: Dict[str, Tuple[float, float]],
) -> Dict[str, Tuple[float, float]]:
    """Round force component values to 3 significant figures."""
    return {
        cause: (_round_sig3(fx), _round_sig3(fy))
        for cause, (fx, fy) in comps.items()
    }


def _thin_frames(
    frames: List[Dict[str, Tuple[float, float, float]]], max_frames: int
) -> List[Dict[str, Tuple[float, float, float]]]:
    """Uniformly downsample frames to at most max_frames, always keeping last.

    Deterministic (round-half-away via int(i * (n-1) / (m-1))); no-ops when
    already within budget.
    """
    n = len(frames)
    if max_frames < 1:
        return []
    if n <= max_frames:
        return frames
    if max_frames == 1:
        return [frames[-1]]
    return [
        frames[int(i * (n - 1) / (max_frames - 1))] for i in range(max_frames - 1)
    ] + [frames[-1]]
