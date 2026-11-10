"""Staged routing service: trial -> refine relay -> local shove.

Orchestration contract (collaborators faked where noted): clean runs
cost one pass with no retry runs; refine adopts only on a strict
routed-count win; shove fixes splice into the tallies; cancellation
propagates. One end-to-end pass over a synthetic GND-walled board
exercises all three stages for real.
"""

import pytest

from kicad_autorouter.board_model import NetConnection
from kicad_autorouter.io import add_footprint_to_tree
from kicad_autorouter.routing.pipeline import RoutingParams
from kicad_autorouter.routing.retry import route_with_retry
from kicad_autorouter.routing.router import RoutingCancelled
from kicad_autorouter.sexpr import SExpr, parse_file

from .helpers import make_fp, stamp_edge_cuts
from .test_pour import _board, _track

FIXTURES = "tests/fixtures"


def _params(**kw):
    base = dict(grid_resolution_mm=0.5, run_drc=False,
                strip_gnd_blockers=False, stitch_gnd_vias=False)
    base.update(kw)
    return RoutingParams(**base)


def _tree_for(model, footprints=()):
    tree = parse_file(f"{FIXTURES}/minimal.kicad_pcb")
    for fp, lib in footprints:
        tree = add_footprint_to_tree(tree, fp, lib)
    tree = stamp_edge_cuts(tree, half=25.0)
    for t in model.tracks:
        tree = SExpr(tree.head, tree.args + (SExpr("segment", (
            SExpr("start", (t.start.x_mm, t.start.y_mm)),
            SExpr("end", (t.end.x_mm, t.end.y_mm)),
            SExpr("width", (t.width_mm,)),
            SExpr("layer", (t.layer,)),
            SExpr("net", (t.net_name,)),
        )),))
    return tree


def _easy_board():
    fa = make_fp("A", "uuid-a", 3.0, 6.0, ("S",), hw=0.25, hh=0.25)
    fb = make_fp("B", "uuid-b", 17.0, 6.0, ("S",), hw=0.25, hh=0.25)
    from dataclasses import replace
    model = _board(fps=[fa, fb], edge=25.0)
    model = replace(model, nets={
        "S": (NetConnection("A", "1"), NetConnection("B", "1")),
    })
    return model, _tree_for(model, [(fa, "Test:R_0805"), (fb, "Test:R_0805")])


def _wall_board():
    fa = make_fp("A", "uuid-a", 3.0, 6.0, ("S",), hw=0.25, hh=0.25)
    fb = make_fp("B", "uuid-b", 17.0, 6.0, ("S",), hw=0.25, hh=0.25)
    from dataclasses import replace
    model = _board(
        fps=[fa, fb],
        tracks=[
            _track(5.0, -30.0, 5.0, 30.0, "GND", layer="F.Cu", width=0.5),
            _track(5.0, -30.0, 5.0, 30.0, "GND", layer="B.Cu", width=0.5),
        ],
        edge=25.0,
    )
    model = replace(model, nets={
        "S": (NetConnection("A", "1"), NetConnection("B", "1")),
    })
    return model, _tree_for(model, [(fa, "Test:R_0805"), (fb, "Test:R_0805")])


def _fake_result(routed, failed, reasons=None, tree=None):
    from kicad_autorouter.routing.pipeline import RoutingResult
    return RoutingResult(
        tracks_added=0, vias_added=0, nets_routed=routed, nets_failed=failed,
        drc_clean=False, drc_violations=0, pcb_tree=tree,
        failed_reasons=dict(reasons or {}),
    )


@pytest.mark.unit
class TestRouteWithRetry:
    def test_clean_board_single_pass_no_retries(self):
        model, tree = _easy_board()
        params = _params()
        result, info = route_with_retry(model, tree, ["S"], set(), params=params)
        assert result.nets_routed == 1 and result.nets_failed == 0
        assert info["refine_result"] is None
        assert info["refine_adopted"] is False
        assert info["shove_reports"] == {}
        assert info["trial_result"] is result

    def test_refine_gated_by_seeds(self):
        import kicad_autorouter.routing.retry as retry_mod

        model, tree = _wall_board()
        params = _params(refine_seeds=1)
        calls = []
        real_route = retry_mod.route_nets

        def spy_route(**kw):
            calls.append(kw["net_names"])
            return real_route(**kw)

        import unittest.mock as mock
        with mock.patch.object(retry_mod, "route_nets", spy_route):
            with mock.patch.object(
                    retry_mod, "refine_routing",
                    side_effect=AssertionError("must not run")):
                with mock.patch.object(
                        retry_mod, "retry_failed_with_shove",
                        return_value=(model, tree, {})):
                    result, info = route_with_retry(
                        model, tree, ["S"], set(), params=params)
        assert result.nets_failed == 1
        assert info["refine_result"] is None
        assert info["shove_reports"] == {}
        assert calls == [["S"]]

    def test_refine_adopted_only_on_strict_win(self):
        import unittest.mock as mock

        import kicad_autorouter.routing.retry as retry_mod

        model, tree = _easy_board()
        params = _params()
        trial = _fake_result(0, 1, {"S": "no path"}, tree)
        tie = _fake_result(0, 1, {"S": "still no path"}, tree)
        win = _fake_result(1, 0, {}, tree)
        with mock.patch.object(retry_mod, "route_nets",
                               return_value=(trial, None, None)):
            with mock.patch.object(retry_mod, "refine_routing",
                                   return_value=(tie, None, None)):
                with mock.patch.object(retry_mod, "retry_failed_with_shove",
                                       return_value=(model, tree, {})):
                    result, info = route_with_retry(
                        model, tree, ["S"], set(), params=params)
                    assert result is trial
                    assert info["refine_adopted"] is False
            with mock.patch.object(retry_mod, "refine_routing",
                                   return_value=(win, None, None)):
                with mock.patch.object(retry_mod, "retry_failed_with_shove",
                                       return_value=(model, tree, {})):
                    result, info = route_with_retry(
                        model, tree, ["S"], set(), params=params)
                    assert result is win
                    assert info["refine_adopted"] is True

    def test_shove_fix_spliced_into_tallies(self):
        import unittest.mock as mock

        import kicad_autorouter.routing.retry as retry_mod
        from kicad_autorouter.routing.shove import ShoveReport

        model, tree = _easy_board()
        params = _params()
        trial = _fake_result(0, 1, {"S": "no path"}, tree)
        trial.failed_diagnostics = {"S": {"attempts": []}}
        fixed_tree = tree
        with mock.patch.object(retry_mod, "route_nets",
                               return_value=(trial, None, None)):
            with mock.patch.object(retry_mod, "refine_routing",
                                   return_value=(trial, None, None)):
                rep = ShoveReport(net="S", routed=True, evicted=3, restored=2)
                with mock.patch.object(
                        retry_mod, "retry_failed_with_shove",
                        return_value=(model, fixed_tree, {"S": rep})):
                    result, info = route_with_retry(
                        model, tree, ["S"], set(), params=params)
        assert result.nets_routed == 1
        assert result.nets_failed == 0
        assert result.failed_reasons == {}
        assert result.failed_diagnostics == {}
        assert result.pcb_tree is fixed_tree
        assert info["shove_reports"]["S"].routed is True

    def test_repair_adopted_for_refine_casualty(self):
        # Trial routes A,B; the adopted refine winner routes A,C,D but
        # breaks B. One solo repair pass on the winner board fixes B.
        import unittest.mock as mock

        import kicad_autorouter.routing.retry as retry_mod

        model, tree = _easy_board()
        params = _params()
        trial = _fake_result(
            2, 2, {"C": "no path", "D": "no path"}, tree)
        winner = _fake_result(3, 1, {"B": "walled"}, tree)
        fixed = _fake_result(4, 0, {}, tree)

        def fake_route(**kw):
            if kw["net_names"] == ["A", "B", "C", "D"]:
                return (trial, None, None)
            assert kw["net_names"] == ["B"]
            return (fixed, None, None)

        with mock.patch.object(retry_mod, "route_nets", fake_route):
            with mock.patch.object(retry_mod, "refine_routing",
                                    return_value=(winner, None, None)):
                with mock.patch.object(
                        retry_mod, "retry_failed_with_shove",
                        side_effect=AssertionError("no failures left")):
                    with mock.patch.object(
                            retry_mod, "retry_failed_with_signal_shove",
                            side_effect=AssertionError("no failures left")):
                        result, info = route_with_retry(
                            model, tree, ["A", "B", "C", "D"], set(),
                            params=params)
        assert info["refine_adopted"] is True
        assert info["repair_reports"]["B"].routed is True
        assert result.nets_routed == 4
        assert result.nets_failed == 0
        assert result.failed_reasons == {}
        assert result.pcb_tree is tree

    def test_repair_failure_keeps_winner(self):
        # A casualty whose solo repair also fails stays failed; the
        # adopted winner is untouched and flows on to the shove stages.
        import unittest.mock as mock

        import kicad_autorouter.routing.retry as retry_mod

        model, tree = _easy_board()
        params = _params()
        trial = _fake_result(
            2, 2, {"C": "no path", "D": "no path"}, tree)
        winner = _fake_result(3, 1, {"B": "walled"}, tree)
        still_bad = _fake_result(0, 1, {"B": "still walled"}, tree)

        def fake_route(**kw):
            if kw["net_names"] == ["A", "B", "C", "D"]:
                return (trial, None, None)
            assert kw["net_names"] == ["B"]
            return (still_bad, None, None)

        with mock.patch.object(retry_mod, "route_nets", fake_route):
            with mock.patch.object(retry_mod, "refine_routing",
                                    return_value=(winner, None, None)):
                with mock.patch.object(retry_mod, "retry_failed_with_shove",
                                        return_value=(model, tree, {})):
                    with mock.patch.object(
                            retry_mod, "retry_failed_with_signal_shove",
                            return_value=(model, tree, {})):
                        result, info = route_with_retry(
                            model, tree, ["A", "B", "C", "D"], set(),
                            params=params)
        assert info["repair_reports"]["B"].routed is False
        assert result is winner
        assert result.nets_routed == 3
        assert result.nets_failed == 1
        assert result.failed_reasons == {"B": "walled"}

    def test_no_casualties_no_extra_pass(self):
        # Clean trial: no refine, no repair, exactly one route_nets call.
        import unittest.mock as mock

        import kicad_autorouter.routing.retry as retry_mod

        model, tree = _easy_board()
        params = _params()
        trial = _fake_result(2, 0, {}, tree)
        calls = []

        def spy_route(**kw):
            calls.append(kw["net_names"])
            return (trial, None, None)

        with mock.patch.object(retry_mod, "route_nets", spy_route):
            with mock.patch.object(
                    retry_mod, "refine_routing",
                    side_effect=AssertionError("must not run")):
                with mock.patch.object(
                        retry_mod, "retry_failed_with_shove",
                        side_effect=AssertionError("must not run")):
                    with mock.patch.object(
                            retry_mod, "retry_failed_with_signal_shove",
                            side_effect=AssertionError("must not run")):
                        result, info = route_with_retry(
                            model, tree, ["A", "B"], set(), params=params)
        assert calls == [["A", "B"]]
        assert info["refine_adopted"] is False
        assert info["repair_reports"] == {}
        assert result is trial

    def test_cancel_propagates(self):
        model, tree = _easy_board()
        with pytest.raises(RoutingCancelled):
            route_with_retry(model, tree, ["S"], set(), params=_params(),
                             should_cancel=lambda: True)

    def test_walled_board_end_to_end(self):
        # Trial 0 fails, refine ties (same single-net order), shove adopts.
        model, tree = _wall_board()
        result, info = route_with_retry(
            model, tree, ["S"], set(), params=_params(refine_seeds=2))
        assert info["trial_result"].nets_failed == 1
        assert info["refine_result"] is not None
        assert info["refine_adopted"] is False
        assert info["shove_reports"]["S"].routed is True
        assert result.nets_routed == 1
        assert result.nets_failed == 0
        assert result.failed_reasons == {}
