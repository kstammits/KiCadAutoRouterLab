"""Params validation: non-finite / mistyped values fail fast.

Covers PlacementParams (placement dynamics) and RoutingParams (routing
grid/budget configuration): corrupt dicts raise ValueError, round-trips
preserve defaults.

Markers: unit, params
"""

import math

import pytest

from kicad_autorouter.placement.params import PlacementParams
from kicad_autorouter.routing.pipeline import RoutingParams


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


@pytest.mark.unit
@pytest.mark.params
def test_from_dict_round_trip_defaults():
    assert RoutingParams.from_dict(RoutingParams().to_dict()).to_dict() == RoutingParams().to_dict()


@pytest.mark.unit
@pytest.mark.params
def test_from_dict_rejects_bad_numerics():
    for key, bad in (
        ("grid_resolution_mm", 0),
        ("grid_resolution_mm", -0.1),
        ("grid_resolution_mm", float("nan")),
        ("grid_resolution_mm", "0.1"),
        ("via_cost_mm", -1.0),
        ("track_width_mm", float("inf")),
        ("clearance_mm", "wide"),
        ("heuristic_weight", -3.0),
        ("window_growth", 0.0),
    ):
        with pytest.raises(ValueError, match=key.split("_")[0]):
            RoutingParams.from_dict({key: bad})


@pytest.mark.unit
@pytest.mark.params
def test_from_dict_rejects_bad_ints_and_caps():
    for key, bad in (
        ("max_expansions", "many"),
        ("max_expansions", True),
        ("max_expansions", -1),
        ("max_expansions", 100_000_001),
        ("stitch_vias_max", 100_001),
        ("window_retries", 1.5),
    ):
        with pytest.raises(ValueError):
            RoutingParams.from_dict({key: bad})


@pytest.mark.unit
@pytest.mark.params
def test_from_dict_rejects_bad_structures():
    with pytest.raises(ValueError):
        RoutingParams.from_dict({"run_drc": "yes"})
    with pytest.raises(ValueError):
        RoutingParams.from_dict({"allowed_layers": "F.Cu"})
    with pytest.raises(ValueError):
        RoutingParams.from_dict({"allowed_layers": ["F.Cu", 3]})
    assert RoutingParams.from_dict({"allowed_layers": ["B.Cu", "F.Cu"]}).allowed_layers == ["B.Cu", "F.Cu"]
    assert RoutingParams.from_dict({"allowed_layers": None}).allowed_layers is None
    with pytest.raises(ValueError):
        RoutingParams.from_dict({"net_widths": {"VIN": float("nan")}})
    assert RoutingParams.from_dict({"net_widths": {"VIN": 0.5}}).net_widths == {"VIN": 0.5}
    assert math.isfinite(RoutingParams.from_dict({}).grid_resolution_mm)
