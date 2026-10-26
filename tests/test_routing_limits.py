"""Fail-fast guard for massive nets (max_terminals_per_net).

Single-flight Steiner routing scales ~n-1 capped A* searches per net, so
a 66-terminal plane burns ~86M expansions / ~9 min and still fails. Nets
over the cap fail immediately with an explicit reason instead of hanging
the job; planes belong to copper pours + the GND stitcher.
"""

import time
from dataclasses import replace
from pathlib import Path

import pytest

from kicad_autorouter.board_model import (
    BoardModel,
    KeepoutZone,
    NetConnection,
    Point,
    board_model,
)
from kicad_autorouter.io import parse_file
from kicad_autorouter.routing.pipeline import RoutingParams, route_nets
from tests.helpers import make_fp

FIXTURES = Path(__file__).parent / "fixtures"
DCCF_PCB = FIXTURES / "DCCF.sved.kicad_pcb"


def _ladder(n_pads: int, net: str = "BIG") -> BoardModel:
    """One single-pad footprint per terminal, spread along x."""
    fps = [
        make_fp(f"U{i}", f"uuid-big-{i:03d}", x=float(i) * 4.0, nets=(net,))
        for i in range(n_pads)
    ]
    extent = n_pads * 4.0 + 10.0
    return BoardModel(
        footprints=tuple(fps),
        keepout_zones=(),
        nets={net: tuple(NetConnection(ref=f"U{i}", pad_number="1") for i in range(n_pads))},
        by_ref={fp.ref: fp for fp in fps},
        edge_cuts=(
            (Point(0.0, 0.0), Point(extent, 0.0)),
            (Point(extent, 0.0), Point(extent, extent)),
            (Point(extent, extent), Point(0.0, extent)),
            (Point(0.0, extent), Point(0.0, 0.0)),
        ),
        footprint_region={fp.uuid: 0 for fp in fps},
    )


def test_massive_net_fails_fast():
    model = _ladder(40)
    params = RoutingParams(run_drc=False, stitch_gnd_vias=False)
    t0 = time.perf_counter()
    result, _, _ = route_nets(model, ["BIG"], set(), params=params)
    elapsed = time.perf_counter() - t0
    assert result.nets_routed == 0
    assert result.nets_failed == 1
    assert "max_terminals_per_net" in result.failed_reasons["BIG"]
    assert "40 terminals" in result.failed_reasons["BIG"]
    assert result.expansions == 0
    assert elapsed < 30, f"guard must fail fast, took {elapsed:.1f}s"


def test_cap_boundary_and_opt_out():
    small = _ladder(3)
    # At the cap: no guard failure (may route or fail on merit, but not
    # via the guard).
    res, _, _ = route_nets(
        small, ["BIG"], set(),
        params=RoutingParams(run_drc=False, stitch_gnd_vias=False,
                             max_terminals_per_net=3),
    )
    assert "max_terminals_per_net" not in res.failed_reasons.get("BIG", "")
    # Over the cap: guard fires.
    res, _, _ = route_nets(
        _ladder(4), ["BIG"], set(),
        params=RoutingParams(run_drc=False, stitch_gnd_vias=False,
                             max_terminals_per_net=3),
    )
    assert res.nets_failed == 1
    assert "max_terminals_per_net" in res.failed_reasons["BIG"]
    # 0 disables the cap: a small net routes through the normal path.
    res, _, _ = route_nets(
        small, ["BIG"], set(),
        params=RoutingParams(run_drc=False, stitch_gnd_vias=False,
                             max_terminals_per_net=0),
    )
    assert "max_terminals_per_net" not in res.failed_reasons.get("BIG", "")


def test_params_round_trip_cap():
    params = RoutingParams(max_terminals_per_net=7)
    assert RoutingParams.from_dict(params.to_dict()).max_terminals_per_net == 7
    with pytest.raises(ValueError):
        RoutingParams.from_dict({"max_terminals_per_net": -1})


def _walled_board() -> BoardModel:
    """Two terminals split by a full-height keepout wall (no clean path).

    Each side floods hundreds of cells, so a tiny budget trips on the
    first attempt while an unbounded run would burn window × retries.
    """
    fps = [
        make_fp("U0", "uuid-wall-a", x=10.0, y=20.0, nets=("BIG",)),
        make_fp("U1", "uuid-wall-b", x=50.0, y=20.0, nets=("BIG",)),
    ]
    wall = KeepoutZone(
        uuid="uuid-wall",
        layers=("F.Cu", "B.Cu"),
        polygon=(Point(28.0, 0.0), Point(32.0, 0.0),
                 Point(32.0, 40.0), Point(28.0, 40.0)),
        tracks_allowed=False,
        vias_allowed=False,
        pads_allowed=False,
    )
    return BoardModel(
        footprints=tuple(fps),
        keepout_zones=(wall,),
        nets={"BIG": (NetConnection(ref="U0", pad_number="1"),
                      NetConnection(ref="U1", pad_number="1"))},
        by_ref={fp.ref: fp for fp in fps},
        edge_cuts=(
            (Point(0.0, 0.0), Point(60.0, 0.0)),
            (Point(60.0, 0.0), Point(60.0, 40.0)),
            (Point(60.0, 40.0), Point(0.0, 40.0)),
            (Point(0.0, 40.0), Point(0.0, 0.0)),
        ),
        footprint_region={fp.uuid: 0 for fp in fps},
    )


def test_per_net_budget_fails_fast_with_reason():
    model = _walled_board()
    params = RoutingParams(run_drc=False, stitch_gnd_vias=False,
                           max_expansions_per_net=100)
    t0 = time.perf_counter()
    result, _, _ = route_nets(model, ["BIG"], set(), params=params)
    elapsed = time.perf_counter() - t0
    assert result.nets_routed == 0
    assert result.nets_failed == 1
    assert "max_expansions_per_net" in result.failed_reasons["BIG"]
    # Early stop inside the first search: far below what one capped
    # search alone would burn (each side floods ~100k+ cells).
    assert result.expansions < 1000
    assert elapsed < 30


def test_per_net_budget_zero_disables():
    model = _ladder(2)
    params = RoutingParams(run_drc=False, stitch_gnd_vias=False,
                           max_expansions_per_net=0)
    result, _, _ = route_nets(model, ["BIG"], set(), params=params)
    assert result.nets_routed == 1
    assert "max_expansions_per_net" not in result.failed_reasons.get("BIG", "")


def test_per_net_budget_generous_leaves_normal_nets_alone():
    model = _ladder(2)
    params = RoutingParams(run_drc=False, stitch_gnd_vias=False)
    assert params.max_expansions_per_net == 8_000_000
    result, _, _ = route_nets(model, ["BIG"], set(), params=params)
    assert result.nets_routed == 1


def test_per_net_budget_round_trip():
    params = RoutingParams(max_expansions_per_net=123)
    assert RoutingParams.from_dict(params.to_dict()).max_expansions_per_net == 123
    with pytest.raises(ValueError):
        RoutingParams.from_dict({"max_expansions_per_net": -1})
    with pytest.raises(ValueError):
        RoutingParams.from_dict({"max_expansions_per_net": 2_000_000_000})


@pytest.mark.dccf
def test_dccf_gnd_fails_fast():
    model = board_model(parse_file(DCCF_PCB))
    assert len(model.nets["GND"]) == 66
    t0 = time.perf_counter()
    result, _, _ = route_nets(
        model, ["GND"], set(),
        params=RoutingParams(run_drc=False, stitch_gnd_vias=False,
                             max_attempts_per_net=1),
    )
    elapsed = time.perf_counter() - t0
    assert result.nets_routed == 0
    assert result.nets_failed == 1
    assert "max_terminals_per_net" in result.failed_reasons["GND"]
    assert elapsed < 60, f"GND guard must fail fast, took {elapsed:.1f}s"


@pytest.mark.dccf
def test_dccf_gnd_per_net_budget_stops_search():
    """Net-level budget bounds the pair-search flood (terminal guard off)."""
    model = board_model(parse_file(DCCF_PCB))
    t0 = time.perf_counter()
    result, _, _ = route_nets(
        model, ["GND"], set(),
        params=RoutingParams(run_drc=False, stitch_gnd_vias=False,
                             max_attempts_per_net=1,
                             max_terminals_per_net=0,
                             max_expansions_per_net=1_000_000),
    )
    elapsed = time.perf_counter() - t0
    assert result.nets_routed == 0
    assert result.nets_failed == 1
    assert "max_expansions_per_net" in result.failed_reasons["GND"]
    assert result.expansions < 1_500_000
    assert elapsed < 60, f"budget guard must fail fast, took {elapsed:.1f}s"
