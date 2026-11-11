"""Tier-1 signal shove: sever plus restore, no victim search.

Synthetic board: signal S sealed by full-height GND walls (both layers,
spanning past the grid) next to its left pin. A collateral V stub sits
inside the left pin box, clear of S's corridor. S fails on the intact
board; signal shove severs V plus the box GND, routes S alone, and
requires every victim piece back — V restores bit-identical while a GND
leftover stays out (fracture gate holds), so the fix adopts. Rollback
paths: victim in the seal line (S must cross it same-layer) rolls back
with the board untouched; protected/power/ground nets are never
victims; nothing severable leaves the board untouched.
"""

import pytest

from kicad_autorouter.board_model import NetConnection, board_model
from kicad_autorouter.routing.pipeline import route_nets
from kicad_autorouter.routing.retry import route_with_retry
from kicad_autorouter.routing.router import RoutingCancelled
from kicad_autorouter.routing.shove import (
    retry_failed_with_signal_shove,
    signal_blockers_for_net,
    victim_net_components,
)

from .helpers import make_fp
from ._shove_boards import shove_params as _params, wall_board as _wall_board
from .test_pour import _track


def _track_keys(tracks):
    """Dedup key: rounded endpoints + layer + net (ledger is per-layer)."""
    return [(round(t.start.x_mm, 3), round(t.start.y_mm, 3),
             round(t.end.x_mm, 3), round(t.end.y_mm, 3),
             t.layer, t.net_name)
            for t in tracks]


def _collateral_board():
    """GND-sealed S plus a V stub inside the left pin box, off-corridor."""
    v1 = make_fp("V1", "uuid-v1", 1.0, 3.4, ("V",), hw=0.25, hh=0.25)
    v2 = make_fp("V2", "uuid-v2", 3.0, 3.4, ("V",), hw=0.25, hh=0.25)
    return _wall_board(
        extra_tracks=[_track(1.0, 3.4, 3.0, 3.4, "V", layer="F.Cu",
                             width=0.5)],
        extra_nets={"V": (NetConnection("V1", "1"), NetConnection("V2", "1"))},
        extra_fps=[v1, v2],
    )


def _v_geom(model):
    return sorted(_track_keys(
        [t for t in model.tracks if t.net_name == "V"]))


@pytest.mark.unit
class TestSignalShove:
    def test_collateral_victim_restores_and_adopts(self):
        model, tree = _collateral_board()
        params = _params()
        res, _, _ = route_nets(model, ["S"], set(), params=params,
                               original_pcb_tree=tree)
        assert res.nets_failed == 1  # sealed by the GND walls
        before = _v_geom(model)
        assert len(before) == 1

        from kicad_autorouter.routing.shove import boxes_for_net
        boxes = boxes_for_net(model, "S", 4.0)
        assert signal_blockers_for_net(model, "S", boxes, set()) == ("V",)

        model2, tree2, reports = retry_failed_with_signal_shove(
            model, tree, ["S"], set(), params)
        rep = reports["S"]
        assert rep.routed, rep.reason
        assert rep.victims == ("V",)
        # GND leftover stays out (fracture-tolerant); the victim is whole.
        assert rep.evicted > rep.restored >= 1
        assert _v_geom(board_model(tree2)) == before
        assert any(t.net_name == "S" for t in board_model(tree2).tracks)

    def test_protected_seal_rolls_back_untouched(self):
        # A protected wall seals both layers end to end (never evicted):
        # S still fails alone after eviction, so the attempt rolls back
        # with the board untouched.
        v1 = make_fp("V1", "uuid-v1", 5.0, -20.0, ("V",), hw=0.25, hh=0.25)
        v2 = make_fp("V2", "uuid-v2", 5.0, 20.0, ("V",), hw=0.25, hh=0.25)
        model, tree = _wall_board(
            wall_net="V",
            extra_tracks=[
                _track(5.0, -30.0, 5.0, 30.0, "PR", layer="F.Cu", width=0.5),
                _track(5.0, -30.0, 5.0, 30.0, "PR", layer="B.Cu", width=0.5),
            ],
            extra_nets={"V": (NetConnection("V1", "1"),
                              NetConnection("V2", "1"))},
            extra_fps=[v1, v2],
        )
        params = _params()
        res, _, _ = route_nets(model, ["S"], set(), params=params,
                               original_pcb_tree=tree)
        assert res.nets_failed == 1
        model2, tree2, reports = retry_failed_with_signal_shove(
            model, tree, ["S"], {"PR"}, params)
        rep = reports["S"]
        assert not rep.routed
        assert rep.victims == ("V",)
        assert not rep.gap_routed
        assert tree2 is tree and model2 is model

    def test_seal_line_adopts_with_via_stitch(self):
        # V walls both layers, GND seals B.Cu at the same line: S takes
        # F.Cu through the V gap, and the gap flight reconnects V by
        # dipping through B.Cu — two stitch vias join the kept stubs to
        # the restored B.Cu middle.
        v1 = make_fp("V1", "uuid-v1", 5.0, -20.0, ("V",), hw=0.25, hh=0.25)
        v2 = make_fp("V2", "uuid-v2", 5.0, 20.0, ("V",), hw=0.25, hh=0.25)
        model, tree = _wall_board(
            wall_net="V",
            extra_tracks=[_track(5.0, -30.0, 5.0, 30.0, "GND", layer="B.Cu",
                                 width=0.5)],
            extra_nets={"V": (NetConnection("V1", "1"),
                              NetConnection("V2", "1"))},
            extra_fps=[v1, v2],
        )
        params = _params()
        res, _, _ = route_nets(model, ["S"], set(), params=params,
                               original_pcb_tree=tree)
        assert res.nets_failed == 1
        model2, tree2, reports = retry_failed_with_signal_shove(
            model, tree, ["S"], set(), params)
        rep = reports["S"]
        assert rep.routed, rep.reason
        assert rep.victims == ("V",)
        assert rep.gap_routed
        final = board_model(tree2)
        assert any(t.net_name == "S" for t in final.tracks)
        assert victim_net_components(final, "V") == 1
        assert sum(1 for v in final.vias if v.net_name == "V") == 2
        keys = _track_keys(final.tracks)
        assert len(set(keys)) == len(keys)

    def test_gap_reconnect_adopts_on_staggered_board(self):
        # V seals F.Cu at x=5, GND seals B.Cu at x=15: S routes F.Cu
        # through the V gap (Tier-1 victim check fails), then the gap
        # flight reconnects V around S on F.Cu while GND restores.
        from kicad_autorouter.routing.shove import (
            gnd_pad_components,
            victim_net_components,
        )
        v1 = make_fp("V1", "uuid-v1", 5.0, -20.0, ("V",), hw=0.25, hh=0.25)
        v2 = make_fp("V2", "uuid-v2", 5.0, 20.0, ("V",), hw=0.25, hh=0.25)
        model, tree = _wall_board(
            wall_net="GND",
            extra_tracks=[
                _track(5.0, -30.0, 5.0, 30.0, "V", layer="F.Cu", width=0.5),
                _track(15.0, -30.0, 15.0, 30.0, "GND", layer="B.Cu",
                       width=0.5),
            ],
            extra_nets={"V": (NetConnection("V1", "1"),
                              NetConnection("V2", "1"))},
            extra_fps=[v1, v2],
        )
        params = _params()
        res, _, _ = route_nets(model, ["S"], set(), params=params,
                               original_pcb_tree=tree)
        assert res.nets_failed == 1
        base_gnd = gnd_pad_components(model)
        v_before = _v_geom(model)
        model2, tree2, reports = retry_failed_with_signal_shove(
            model, tree, ["S"], set(), params)
        rep = reports["S"]
        assert rep.routed, rep.reason
        assert rep.victims == ("V",)
        assert rep.gap_routed
        final = board_model(tree2)
        assert any(t.net_name == "S" for t in final.tracks)
        assert victim_net_components(final, "V") == 1
        assert gnd_pad_components(final) == base_gnd
        # Kept V stubs survive verbatim; only the gap is new copper.
        stubs = {(5.0, -30.0, 5.0, 2.0, "F.Cu", "V"),
                 (5.0, 10.0, 5.0, 30.0, "F.Cu", "V")}
        assert stubs <= set(_v_geom(final))
        # No exact-duplicate segments (ledger pairing is per-layer).
        keys = _track_keys(final.tracks)
        assert len(set(keys)) == len(keys)

    def test_ledger_pairs_same_xy_walls_per_layer(self):
        # Same-coords GND walls on both layers must pair ledger nodes
        # per-layer: a stolen node restores the wrong layer's copper
        # and double-adds one node while dropping the other.
        from kicad_autorouter.routing.shove import (
            boxes_for_net,
            evict_gnd_in_boxes,
            _restore_cleared,
        )
        model, tree = _wall_board()
        params = _params()
        boxes = boxes_for_net(model, "S", 4.0)
        ev = evict_gnd_in_boxes(model, tree, boxes)
        assert ev.evicted_count > 0
        for m in ev.middles:
            assert m.node is not None
            lay = m.node.find("layer")
            assert lay is not None and lay.args[0] == m.track.layer
        model2, tree2, nrest, _kept = _restore_cleared(
            ev.model, ev.tree, ev, [], params.clearance_mm)
        assert nrest == ev.evicted_count
        final = board_model(tree2)

        def _wire(tracks):
            import math
            return sum(math.hypot(t.end.x_mm - t.start.x_mm,
                                  t.end.y_mm - t.start.y_mm)
                       for t in tracks)

        # Full restore: same copper length on both sides (eviction
        # splits tracks, so segmentation differs but copper is whole).
        assert _wire(model.tracks) == _wire(model2.tracks)
        assert _wire(model.tracks) == _wire(final.tracks)
        # ... and no double-added ledger node on either side.
        key = lambda t: (t.net_name, t.layer,
                         round(t.start.x_mm, 3), round(t.start.y_mm, 3),
                         round(t.end.x_mm, 3), round(t.end.y_mm, 3))
        assert len({key(t) for t in final.tracks}) == len(final.tracks)

    def test_isolated_pads_detection(self):
        # V2 floats with no kept copper: isolated. V1 sits on a stub:
        # excluded from the search terminal set.
        from kicad_autorouter.routing.shove import _isolated_victim_pads
        v1 = make_fp("V1", "uuid-v1", 1.0, 3.4, ("V",), hw=0.25, hh=0.25)
        v2 = make_fp("V2", "uuid-v2", 10.0, 20.0, ("V",), hw=0.25, hh=0.25)
        model, _ = _wall_board(
            extra_tracks=[_track(1.0, 3.4, 3.0, 3.4, "V", layer="F.Cu",
                                 width=0.5)],
            extra_nets={"V": (NetConnection("V1", "1"),
                              NetConnection("V2", "1"))},
            extra_fps=[v1, v2],
        )
        isolated = _isolated_victim_pads(model, "V")
        assert len(isolated) == 1
        assert isolated[0][:2] == (10.0, 20.0)

    @pytest.mark.parametrize("case", ["protected", "power", "absent"])
    def test_never_victims(self, case):
        # Protected / power / absent blockers are never severed: same
        # contract, only the victim filter differs.
        if case == "protected":
            model, tree = _collateral_board()
            protected = {"V"}
        elif case == "power":
            p1 = make_fp("P1", "uuid-p1", 1.0, 3.4, ("VCC",), hw=0.25, hh=0.25)
            p2 = make_fp("P2", "uuid-p2", 3.0, 3.4, ("VCC",), hw=0.25, hh=0.25)
            model, tree = _wall_board(
                extra_tracks=[_track(1.0, 3.4, 3.0, 3.4, "VCC", layer="F.Cu",
                                     width=0.5)],
                extra_nets={"VCC": (NetConnection("P1", "1"),
                                    NetConnection("P2", "1"))},
                extra_fps=[p1, p2],
            )
            protected = set()
        else:
            model, tree = _wall_board()
            protected = set()
        params = _params()
        model2, tree2, reports = retry_failed_with_signal_shove(
            model, tree, ["S"], protected, params)
        rep = reports["S"]
        assert not rep.routed
        assert rep.evicted == 0
        assert rep.reason == "no signal blockers in shove boxes"
        if case == "absent":
            assert tree2 is tree and model2 is model

    def test_victim_selection_smallest_first_and_capped(self):
        w1 = make_fp("W1", "uuid-w1", 14.0, 3.4, ("W",), hw=0.25, hh=0.25)
        w2 = make_fp("W2", "uuid-w2", 16.0, 3.4, ("W",), hw=0.25, hh=0.25)
        v1 = make_fp("V1", "uuid-v1", 1.0, 3.4, ("V",), hw=0.25, hh=0.25)
        v2 = make_fp("V2", "uuid-v2", 3.0, 3.4, ("V",), hw=0.25, hh=0.25)
        model, tree = _wall_board(
            extra_tracks=[
                _track(1.0, 3.4, 3.0, 3.4, "V", layer="F.Cu", width=0.5),
                _track(14.0, 3.4, 16.0, 3.4, "W", layer="F.Cu", width=0.5),
                _track(14.0, 4.4, 16.0, 4.4, "W", layer="F.Cu", width=0.5),
            ],
            extra_nets={
                "V": (NetConnection("V1", "1"), NetConnection("V2", "1")),
                "W": (NetConnection("W1", "1"), NetConnection("W2", "1")),
            },
            extra_fps=[v1, v2, w1, w2],
        )
        from kicad_autorouter.routing.shove import boxes_for_net
        boxes = boxes_for_net(model, "S", 4.0)
        assert signal_blockers_for_net(model, "S", boxes, set()) == ("V", "W")
        assert signal_blockers_for_net(
            model, "S", boxes, set(), max_victims=1) == ("V",)
        assert signal_blockers_for_net(
            model, "S", boxes, {"V"}) == ("W",)

    def test_cancel_propagates(self):
        model, tree = _collateral_board()
        with pytest.raises(RoutingCancelled):
            retry_failed_with_signal_shove(
                model, tree, ["S"], set(), _params(),
                should_cancel=lambda: True)

    def test_gnd_shove_still_wins_first_end_to_end(self):
        # Stage order: GND shove adopts on the collateral board, so the
        # signal stage is skipped (its reports stay empty).
        model, tree = _collateral_board()
        result, info = route_with_retry(
            model, tree, ["S"], set(), params=_params())
        assert result.nets_routed == 1
        assert info["shove_reports"]["S"].routed is True
        assert info["signal_shove_reports"] == {}
