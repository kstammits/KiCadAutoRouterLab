"""strategy.py: mode inference, presets, overlay, warning helper.

Markers: unit
"""

import pytest

from kicad_autorouter.strategy import (
    MODES,
    MODE_PRESETS,
    apply_mode,
    infer_mode,
    mode_owned_keys,
    mode_warning,
)


pytestmark = pytest.mark.unit


def test_infer_table():
    assert infer_mode(()) == "two-layer"
    assert infer_mode(("F.Cu",)) == "two-layer"
    assert infer_mode(("F.Cu", "B.Cu")) == "two-layer"
    assert infer_mode(("F.Cu", "In1.Cu", "B.Cu")) == "four-layer"
    assert infer_mode(("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")) == "four-layer"


def test_preset_contents_exact():
    assert MODES == ("two-layer", "four-layer")
    assert MODE_PRESETS["two-layer"] == {
        "stitch_gnd_vias": True,
        "strip_gnd_blockers": True,
        "pour_severance_weight": 30.0,
        "allowed_layers": ["F.Cu", "B.Cu"],
        "fanout_planes": False,
        "fanout_plane_vias_max": 16,
    }
    assert MODE_PRESETS["four-layer"] == {
        "stitch_gnd_vias": False,
        "strip_gnd_blockers": False,
        "pour_severance_weight": 0.0,
        "allowed_layers": None,
        "refill_zones": True,
        "fanout_planes": True,
        "fanout_plane_vias_max": 16,
    }
    assert "via_cost_mm" not in MODE_PRESETS["two-layer"]
    assert "via_cost_mm" not in MODE_PRESETS["four-layer"]


def test_apply_mode_forces_owned_keys_keeps_rest():
    base = {"via_cost_mm": 9.0, "stitch_gnd_vias": False,
            "allowed_layers": None, "grid_resolution_mm": 0.2}
    out = apply_mode(base, "two-layer")
    assert out["stitch_gnd_vias"] is True
    assert out["strip_gnd_blockers"] is True
    assert out["pour_severance_weight"] == 30.0
    assert out["allowed_layers"] == ["F.Cu", "B.Cu"]
    assert out["via_cost_mm"] == 9.0
    assert out["grid_resolution_mm"] == 0.2
    # Base dict untouched; preset list copied (no aliasing).
    assert base["allowed_layers"] is None
    out["allowed_layers"].append("In1.Cu")
    assert MODE_PRESETS["two-layer"]["allowed_layers"] == ["F.Cu", "B.Cu"]
    with pytest.raises(ValueError):
        apply_mode({}, "six-layer")


def test_mode_owned_keys_match_preset():
    assert mode_owned_keys("two-layer") == list(MODE_PRESETS["two-layer"])
    assert mode_owned_keys("four-layer") == list(MODE_PRESETS["four-layer"])
    with pytest.raises(ValueError):
        mode_owned_keys("six-layer")


def test_mode_warning():
    assert mode_warning("two-layer", ("F.Cu", "B.Cu")) is None
    assert mode_warning("four-layer", ("F.Cu", "In1.Cu", "B.Cu")) is None
    assert mode_warning(None, ("F.Cu",)) is None
    w = mode_warning("two-layer", ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))
    assert w and "collapse" in w
    w = mode_warning("four-layer", ("F.Cu", "B.Cu"))
    assert w and "as-is" in w
    with pytest.raises(ValueError):
        mode_warning("six-layer", ("F.Cu",))
