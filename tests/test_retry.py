"""Staged routing service: trial -> refine relay -> local shove.

Orchestration contract (collaborators faked where noted): clean runs
cost one pass with no retry runs; refine adopts only on a strict
routed-count win; shove fixes splice into the tallies; cancellation
propagates. One end-to-end pass over a synthetic GND-walled board
exercises all three stages for real.
"""

import pytest

from kicad_autorouter.routing.retry import route_with_retry
from kicad_autorouter.routing.router import RoutingCancelled

from ._shove_boards import (
    easy_board as _easy_board,
    retry_wall_board as _wall_board,
    shove_params as _params,
)


def _fake_result(routed, failed, reasons=None, tree=None):
    from kicad_autorouter.routing.pipeline import RoutingResult
    return RoutingResult(
        tracks_added=0, vias_added=0, nets_routed=routed, nets_failed=failed,
        drc_clean=False, drc_violations=0, pcb_tree=tree,
        failed_reasons=dict(reasons or {}),
    )


@pytest.mark.unit
class TestRouteWithRetry:
    @pytest.mark.parametrize("case", [
        # Real route, clean board: one pass, trial stands.
        dict(board="easy", nets=["S"], trial=None, params={}),
        # Real route, walled board, refine seeds on: refine gated off,
        # shove finds nothing, signal stage still runs for real.
        dict(board="wall", nets=["S"], trial=None,
             params={"refine_seeds": 1}, expect_failed=1,
             shove_runs=True, signal_runs=True),
        # Fake failing trial, stages off: trial returns as-is.
        dict(board="easy", nets=["S"], trial=(0, 1, {"S": "no path"}),
             params={"retry_stages": False}),
        # Fake clean trial: no refine, no repair, exactly one call.
        dict(board="easy", nets=["A", "B"], trial=(2, 0, {}), params={}),
    ])
    def test_no_retry_pass(self, case):
        import unittest.mock as mock

        import kicad_autorouter.routing.retry as retry_mod

        board = _easy_board() if case["board"] == "easy" else _wall_board()
        model, tree = board
        params = _params(**case["params"])
        trial = None
        if case["trial"] is not None:
            trial = _fake_result(*case["trial"][:2],
                                 dict(case["trial"][2]), tree)
        calls = []
        real_route = retry_mod.route_nets

        def spy_route(**kw):
            calls.append(kw["net_names"])
            if trial is None:
                return real_route(**kw)
            return (trial, None, None)

        if case.get("shove_runs"):
            shove_patch = mock.patch.object(
                retry_mod, "retry_failed_with_shove",
                return_value=(model, tree, {}))
        else:
            shove_patch = mock.patch.object(
                retry_mod, "retry_failed_with_shove",
                side_effect=AssertionError("must not run"))
        # The gated case exercises the real signal stage; the rest forbid it.
        signal_patch = mock.patch.object(
            retry_mod, "retry_failed_with_signal_shove",
            side_effect=AssertionError("must not run"))
        if case.get("signal_runs"):
            signal_patch = mock.patch.object(
                retry_mod, "retry_failed_with_signal_shove",
                wraps=retry_mod.retry_failed_with_signal_shove)
        with mock.patch.object(retry_mod, "route_nets", spy_route):
            with mock.patch.object(
                    retry_mod, "refine_routing",
                    side_effect=AssertionError("must not run")):
                with shove_patch:
                    with signal_patch:
                        result, info = route_with_retry(
                            model, tree, case["nets"], set(), params=params)
        assert calls == [case["nets"]]
        assert info["refine_result"] is None
        assert info["refine_adopted"] is False
        assert info["repair_reports"] == {}
        assert info["shove_reports"] == {}
        if not case.get("signal_runs"):
            assert info["signal_shove_reports"] == {}
        if trial is None:
            assert result is info["trial_result"]
            assert result.nets_failed == case.get("expect_failed", 0)
        else:
            assert result is trial

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

    def test_recovery_boundary_no_core_imports_recovery(self):
        # One-way dependency, enforced: importing engine core must not
        # pull in the recovery layer (same package by history, not by
        # coupling). Subprocess: immune to this session's sys.modules.
        import subprocess
        import sys
        from pathlib import Path

        repo = Path(__file__).resolve().parent.parent
        code = (
            f"import sys; sys.path.insert(0, {str(repo / 'src')!r}); "
            "import kicad_autorouter.routing.pipeline, "
            "kicad_autorouter.routing.route_raw, "
            "kicad_autorouter.routing.router, "
            "kicad_autorouter.routing.refine, "
            "kicad_autorouter.routing.finish_routes, "
            "kicad_autorouter.routing.output; "
            "bad = [m for m in sys.modules if m.endswith('.retry') "
            "or m.endswith('.shove')]; "
            "assert not bad, bad"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr

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
