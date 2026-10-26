"""allowed_layers prerequisite: subset routing + explicit terminal failures.

Markers: minimal
"""

from pathlib import Path

import pytest

from kicad_autorouter.board_model import board_model
from kicad_autorouter.io import add_footprint_to_tree
from kicad_autorouter.routing.pipeline import (
    RoutingParams,
    _resolve_grid_layers,
    route_nets,
)
from kicad_autorouter.sexpr import parse_file

from .helpers import make_fp

FIXTURES = Path("tests/fixtures")


def _two_pad_net(layer="F.Cu", net="AL1"):
    """Minimal board + two 1-pad footprints sharing one net (fast, <0.1s)."""
    pcb_tree = parse_file(FIXTURES / "minimal.kicad_pcb")
    for ref, uuid, x in (("ALA", "allowed-a-uuid-0001", 80.0),
                         ("ALB", "allowed-b-uuid-0002", 120.0)):
        fp = make_fp(ref, uuid, x, 95.0, (net,), layer=layer,
                     footprint_id="Test:R_0805_AL")
        pcb_tree = add_footprint_to_tree(pcb_tree, fp, "Test:R_0805_AL")
    return pcb_tree, board_model(pcb_tree)


def test_allowed_layers_round_trip():
    assert RoutingParams().allowed_layers is None
    p = RoutingParams(allowed_layers=["F.Cu", "B.Cu"])
    assert RoutingParams.from_dict(p.to_dict()).allowed_layers == ["F.Cu", "B.Cu"]
    # Unknown keys still dropped; explicit None survives.
    assert RoutingParams.from_dict(
        {"allowed_layers": None, "bogus": 1}).allowed_layers is None
    # from_dict copies the list (no caller aliasing).
    src = {"allowed_layers": ["F.Cu"]}
    assert RoutingParams.from_dict(src).allowed_layers == ["F.Cu"]
    src["allowed_layers"].append("B.Cu")
    assert RoutingParams.from_dict(src).allowed_layers == ["F.Cu", "B.Cu"]


def test_allowed_layers_subset_respected():
    pcb_tree, model = _two_pad_net()
    res, _, grid = route_nets(
        model, ["AL1"], set(),
        params=RoutingParams(run_drc=False, grid_resolution_mm=0.5,
                             allowed_layers=["F.Cu", "B.Cu"]),
        original_pcb_tree=pcb_tree,
    )
    assert res.nets_routed == 1, f"outer subset must route: {res.failed_reasons}"
    assert tuple(grid.layers) == ("F.Cu", "B.Cu")

    # Single-layer restriction: routes with zero vias, all copper on F.Cu.
    pcb_tree, model = _two_pad_net()
    res, _, grid = route_nets(
        model, ["AL1"], set(),
        params=RoutingParams(run_drc=False, grid_resolution_mm=0.5,
                             stitch_gnd_vias=False,
                             allowed_layers=["F.Cu"]),
        original_pcb_tree=pcb_tree,
    )
    assert res.nets_routed == 1, f"single-layer must route: {res.failed_reasons}"
    assert res.vias_added == 0
    assert tuple(grid.layers) == ("F.Cu",)
    from kicad_autorouter.board_model import tracks, vias

    assert all(t.layer == "F.Cu" for t in tracks(res.pcb_tree)
               if t.net_name == "AL1")
    assert not [v for v in vias(res.pcb_tree) if v.net_name == "AL1"]


def test_disallowed_terminal_fails_explicitly():
    pcb_tree, model = _two_pad_net(layer="B.Cu", net="BL1")
    res, _, _ = route_nets(
        model, ["BL1"], set(),
        params=RoutingParams(run_drc=False, grid_resolution_mm=0.5,
                             allowed_layers=["F.Cu"]),
        original_pcb_tree=pcb_tree,
    )
    assert res.nets_routed == 0
    assert res.nets_failed == 1
    reason = res.failed_reasons.get("BL1", "")
    assert "disallowed" in reason and "B.Cu" in reason, f"need explicit reason: {reason}"


def test_unknown_or_empty_allowed_layers_rejected():
    _, model = _two_pad_net()
    with pytest.raises(ValueError):
        _resolve_grid_layers(model, RoutingParams(allowed_layers=["In9.Cu"]))
    with pytest.raises(ValueError):
        _resolve_grid_layers(model, RoutingParams(allowed_layers=[]))
    with pytest.raises(ValueError):
        route_nets(model, ["AL1"], set(),
                   params=RoutingParams(allowed_layers=["Nope.Cu"]),
                   original_pcb_tree=None)


def test_grid_rebuilds_on_layer_set_change():
    pcb_tree, model = _two_pad_net()
    _, _, grid_all = route_nets(
        model, ["AL1"], set(),
        params=RoutingParams(run_drc=False, grid_resolution_mm=0.5),
        original_pcb_tree=pcb_tree)
    assert tuple(grid_all.layers) == ("F.Cu", "B.Cu")
    # Reusing the 2-layer grid with a 1-layer request must rebuild, not reuse.
    _, _, grid_one = route_nets(
        model, ["AL1"], set(), routing_grid=grid_all,
        params=RoutingParams(run_drc=False, grid_resolution_mm=0.5,
                             stitch_gnd_vias=False,
                             allowed_layers=["F.Cu"]),
        original_pcb_tree=pcb_tree)
    assert tuple(grid_one.layers) == ("F.Cu",)
