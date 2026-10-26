"""PlacementParams validation: non-finite / mistyped values fail fast.

Markers: unit, params
"""

import math

import pytest

from kicad_autorouter.placement.params import PlacementParams


@pytest.mark.unit
@pytest.mark.params
def test_contact_tangent_gamma_rejects_nonfinite():
    for bad in (float("nan"), float("inf"), float("-inf"), "0.15", True):
        with pytest.raises(ValueError):
            PlacementParams.from_dict({"contact_tangent_gamma": bad})
    assert PlacementParams.from_dict({"contact_tangent_gamma": 0.0}).contact_tangent_gamma == 0.0


@pytest.mark.unit
@pytest.mark.params
def test_numeric_fields_reject_nonfinite():
    for key in ("wall_exponent", "power_attraction_scale", "decap_boost", "wall_k_N"):
        with pytest.raises(ValueError):
            PlacementParams.from_dict({key: float("nan")})
    assert math.isfinite(PlacementParams.from_dict({}).wall_exponent)
