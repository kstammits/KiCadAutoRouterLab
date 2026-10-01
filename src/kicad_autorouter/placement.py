"""Force-spring placement domain: parameters, proposals, and the runner interface.

The v0 runner is a stub — no physics yet (PLAN item 2 replaces its body with the
vectorized numpy force-spring simulation). It exists so the UI preview pipeline
(params → run → proposal overlay) can be built and exercised before that lands.
Deltas are keyed by footprint UUID because refs may be duplicated or missing;
locked footprints never appear in ``deltas`` (they are fixed anchors).
"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import asdict, dataclass, fields
from typing import Dict, Tuple

from .board_model import BoardModel


@dataclass(frozen=True)
class PlacementParams:
    """Tunable force-spring parameters (v1 set; grows with the simulation)."""

    repulsion_kr: float = 100.0
    attraction_ka: float = 0.05
    ideal_length_mm: float = 25.0
    max_iterations: int = 1000
    convergence_eps_mm: float = 0.01
    # Preview-only knob for the v0 stub: deterministic per-UUID jitter so the
    # proposal overlay is visibly testable before real physics exists.
    demo_jitter_mm: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PlacementParams":
        """Build params from a JSON-style dict; missing keys take defaults and
        unknown keys are ignored (forward compatibility with newer param sets)."""
        known = {f.name for f in fields(cls)}
        params = cls(**{name: data[name] for name in known if name in data})
        _validate(params)
        return params


def _validate(params: PlacementParams) -> None:
    numeric = (
        "repulsion_kr",
        "attraction_ka",
        "ideal_length_mm",
        "convergence_eps_mm",
        "demo_jitter_mm",
    )
    for name in numeric:
        value = getattr(params, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a number")
    for name in (
        "repulsion_kr",
        "attraction_ka",
        "ideal_length_mm",
        "convergence_eps_mm",
    ):
        if getattr(params, name) <= 0:
            raise ValueError(f"{name} must be > 0")
    if params.demo_jitter_mm < 0:
        raise ValueError("demo_jitter_mm must be >= 0")
    if isinstance(params.max_iterations, bool) or not isinstance(
        params.max_iterations, int
    ):
        raise ValueError("max_iterations must be an integer")
    if params.max_iterations < 1:
        raise ValueError("max_iterations must be >= 1")


@dataclass(frozen=True)
class PlacementProposal:
    """Result of a placement run: per-footprint deltas keyed by UUID + diagnostics."""

    deltas: Dict[str, Tuple[float, float]]
    iterations: int
    final_max_disp_mm: float
    elapsed_s: float
    params: dict


def _jitter_for(uuid: str, max_mm: float) -> Tuple[float, float]:
    """Deterministic (angle, magnitude) jitter derived from the footprint UUID."""
    digest = hashlib.sha256(uuid.encode("utf-8")).digest()
    h = int.from_bytes(digest[:4], "big")
    angle = (h % 360) * math.pi / 180.0
    magnitude = max_mm * ((h >> 16) % 1000) / 1000.0
    return (magnitude * math.cos(angle), magnitude * math.sin(angle))


def run_placement(model: BoardModel, params: PlacementParams) -> PlacementProposal:
    """Run placement for ``model`` under ``params`` and return a proposal.

    v0 stub (PLAN item 2 replaces the body with numpy force-spring physics):
    identity deltas unless ``demo_jitter_mm > 0``, in which case every unlocked,
    UUID-bearing footprint receives a deterministic jitter so the preview
    pipeline is visibly exercisable before real physics lands.
    """
    start = time.perf_counter()
    deltas: Dict[str, Tuple[float, float]] = {}
    if params.demo_jitter_mm > 0:
        for fp in model.footprints:
            if fp.locked or not fp.uuid:
                continue
            deltas[fp.uuid] = _jitter_for(fp.uuid, params.demo_jitter_mm)
    max_disp = max((math.hypot(dx, dy) for dx, dy in deltas.values()), default=0.0)
    return PlacementProposal(
        deltas=deltas,
        iterations=0,
        final_max_disp_mm=max_disp,
        elapsed_s=time.perf_counter() - start,
        params=params.to_dict(),
    )
