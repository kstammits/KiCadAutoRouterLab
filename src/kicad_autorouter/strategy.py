"""Board mode switch: per-board two-layer / four-layer routing presets.

Pure functions, no server imports (mirrors ``board_model.py`` helpers).
Mode lives in per-board server memory; presets apply as an in-memory
overlay via :func:`apply_mode` and are never written to disk.
"""

from __future__ import annotations

from typing import Collection, Dict, Optional

MODES = ("two-layer", "four-layer")

OUTERS = ("F.Cu", "B.Cu")

MODE_PRESETS: Dict[str, Dict[str, object]] = {
    "two-layer": {
        "stitch_gnd_vias": True,
        "strip_gnd_blockers": True,
        "pour_severance_weight": 30.0,
        "allowed_layers": ["F.Cu", "B.Cu"],
    },
    "four-layer": {
        "stitch_gnd_vias": False,
        "strip_gnd_blockers": False,
        "pour_severance_weight": 0.0,
        "allowed_layers": None,
        "refill_zones": True,
    },
}


def infer_mode(copper_layers: Collection[str] | tuple = ()) -> str:
    """``len <= 2`` (incl. empty) → ``"two-layer"``, else ``"four-layer"``."""
    return "two-layer" if len(tuple(copper_layers or ())) <= 2 else "four-layer"


def _check_mode(mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {list(MODES)}, got {mode!r}")


def apply_mode(params_dict: dict, mode: str) -> dict:
    """Return ``params_dict`` with the mode preset forced on preset-owned keys.

    Preset-owned keys always take the preset value (the mode owns them —
    the UI disables/badges those inputs); every other key passes through
    untouched.
    """
    _check_mode(mode)
    preset = MODE_PRESETS[mode]
    out = dict(params_dict)
    for key, value in preset.items():
        out[key] = list(value) if isinstance(value, list) else value
    return out


def mode_owned_keys(mode: str) -> list:
    """Preset-owned keys for ``mode`` (single source for UI gating).

    The server forces these via :func:`apply_mode` at job time; the UI
    disables/badges exactly this set when the mode is active.
    """
    _check_mode(mode)
    return list(MODE_PRESETS[mode].keys())


def _preset_equal(have: object, want: object) -> bool:
    """Preset-value equality with order-insensitive list comparison."""
    if isinstance(have, (list, tuple)) and isinstance(want, (list, tuple)):
        return sorted(str(v) for v in have) == sorted(str(v) for v in want)
    return have == want


def is_custom(effective: dict, mode: str) -> bool:
    """True when a preset-owned key differs from the mode preset.

    Non-preset keys are ignored. (Kept as a pure helper + unit-tested;
    not exposed via ``/api/state`` — the Custom badge was cut.)
    """
    _check_mode(mode)
    preset = MODE_PRESETS[mode]
    for key, value in preset.items():
        if key not in effective or not _preset_equal(effective[key], value):
            return True
    return False


def mode_warning(mode: Optional[str], copper_layers: Collection[str] | tuple) -> Optional[str]:
    """Mismatch warning when mode disagrees with the stackup; else None.

    Warns, never blocks: callers attach this to state/download/job records.
    """
    if mode is None:
        return None
    _check_mode(mode)
    layers = tuple(copper_layers or ())
    n_inner = sum(1 for l in layers if l not in OUTERS)
    if mode == "two-layer" and n_inner:
        return (
            f"two-layer mode on a {len(layers)}-copper board "
            f"({n_inner} inner layers): export collapses to genuine 2-layer"
        )
    if mode == "four-layer" and len(layers) <= 2:
        return (
            "four-layer mode on a 2-copper board: "
            "nothing to collapse, served as-is"
        )
    return None
