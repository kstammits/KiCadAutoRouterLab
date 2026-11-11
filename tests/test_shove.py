"""Local GND shove: evict-split-restore retry for GND-walled nets.

Synthetic board: signal S must cross a full-height GND wall (both
layers, spanning past the grid) next to its left pin. S fails on the
intact board, routes after per-pin GND eviction, GND pad-components
stay at 1, and only the conflicting middle piece stays evicted (the
rest restores). Rollback paths: nothing evicted -> board untouched;
signal-owned walls are never victims.
"""

import pytest

from kicad_autorouter.board_model import board_model
from kicad_autorouter.routing.pipeline import route_nets
from kicad_autorouter.routing.shove import (
    boxes_for_net,
    evict_gnd_in_boxes,
    gnd_pad_components,
    retry_failed_with_shove,
)

from ._shove_boards import (
    rail_board as _rail_board,
    shove_params as _params,
    wall_board as _wall_board,
)


@pytest.mark.unit
class TestShoveRetry:
    def test_walled_signal_fails_then_routes_with_shove(self):
        model, tree = _wall_board()
        params = _params()
        res, _, _ = route_nets(model, ["S"], set(), params=params,
                               original_pcb_tree=tree)
        assert res.nets_failed == 1  # sealed by the GND wall
        assert gnd_pad_components(model) == 1

        model2, tree2, reports = retry_failed_with_shove(
            model, tree, ["S"], set(), params, radius_mm=4.0)
        rep = reports["S"]
        assert rep.routed, rep.reason
        assert rep.evicted > 0
        # The hole piece conflicts with the new path and stays out;
        # everything else restores.
        assert 0 <= rep.restored < rep.evicted
        assert gnd_pad_components(model2) == 1
        final = board_model(tree2)
        assert any(t.net_name == "S" for t in final.tracks)
        # GND link far from the pins survives untouched.
        assert any(t.net_name == "GND" and t.start.y_mm == -15.0
                   for t in final.tracks)

    @pytest.mark.parametrize("wall_net,radius", [
        ("GND", 0.05),  # boxes too small to hold any GND
        ("K", 4.0),  # signal-owned walls are never victims
    ])
    def test_no_gnd_victim_leaves_board_untouched(self, wall_net, radius):
        model, tree = _wall_board(wall_net=wall_net)
        params = _params()
        if wall_net == "K":
            res, _, _ = route_nets(model, ["S"], set(), params=params,
                                   original_pcb_tree=tree)
            assert res.nets_failed == 1
        model2, tree2, reports = retry_failed_with_shove(
            model, tree, ["S"], set(), params, radius_mm=radius)
        rep = reports["S"]
        assert not rep.routed
        assert rep.evicted == 0
        if wall_net == "GND":
            assert rep.reason == "no GND in shove boxes"
            assert tree2 is tree and model2 is model

    def test_evict_splits_tracks_not_amputates(self):
        model, tree = _wall_board()
        boxes = boxes_for_net(model, "S", 4.0)
        assert len(boxes) == 2
        ev = evict_gnd_in_boxes(model, tree, boxes)
        assert ev.evicted_count > 0
        # Outside-box wall pieces survive on both layers.
        assert any(t.net_name == "GND" and t.layer == "F.Cu" for t in ev.model.tracks)
        assert any(t.net_name == "GND" and t.layer == "B.Cu" for t in ev.model.tracks)
        # The untouched GND link survives whole.
        assert any(t.net_name == "GND" and t.start.y_mm == -15.0
                   for t in ev.model.tracks)

    def test_gnd_gap_healed_adopt(self):
        # The crossing middle cannot restore (S routes through it), so
        # the fracture gate fails — but a go-around exists and the gap
        # flight bridges it: adopted with gap_routed set.
        model, tree = _rail_board()
        params = _params()
        res, _, _ = route_nets(model, ["S"], set(), params=params,
                               original_pcb_tree=tree)
        assert res.nets_failed == 1  # sealed by the full-width GND rail
        assert gnd_pad_components(model) == 1

        model2, tree2, reports = retry_failed_with_shove(
            model, tree, ["S"], set(), params, radius_mm=4.0)
        rep = reports["S"]
        assert rep.routed, rep.reason
        assert rep.gap_routed is True
        # Partial restore: the crossing middle stays evicted.
        assert rep.evicted > rep.restored
        assert gnd_pad_components(model2) == 1
        final = board_model(tree2)
        assert any(t.net_name == "S" for t in final.tracks)
        # Bridge copper joins the split stubs on GND (stubs + bridge
        # outnumber the two kept stubs).
        assert sum(1 for t in final.tracks if t.net_name == "GND") >= 3

    def test_gnd_gap_failure_rolls_back(self):
        # A failed gap flight keeps the fracture rollback: the board is
        # untouched and the reason names the flight failure.
        import unittest.mock as mock

        model, tree = _rail_board()
        params = _params()
        with mock.patch(
                "kicad_autorouter.routing.shove.route_gap_terminals",
                return_value=(None, "boom", None, 0.25, None)):
            model2, tree2, reports = retry_failed_with_shove(
                model, tree, ["S"], set(), params, radius_mm=4.0)
        rep = reports["S"]
        assert not rep.routed
        assert rep.gap_routed is False
        assert "boom" in rep.reason
        assert tree2 is tree and model2 is model
        assert not any(t.net_name == "S" for t in model2.tracks)
