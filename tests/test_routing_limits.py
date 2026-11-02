"""Terminal-count tripwire (max_terminals_per_net) + piecemeal auto-split.

Single-flight Steiner routing scales ~n-1 capped A* searches per net, so
a 66-terminal plane burns ~86M expansions / ~9 min and still fails. Nets
over the cap auto-split into overlapping terminal chunks
(piecemeal_chunk_terminals) routed sequentially; with chunking disabled
(piecemeal_chunk_terminals=0) they fail immediately with an explicit
reason instead of hanging the job.
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


def test_massive_net_routes_piecemeal():
    model = _ladder(40)
    params = RoutingParams(run_drc=False, stitch_gnd_vias=False)
    assert params.max_terminals_per_net == 16
    t0 = time.perf_counter()
    result, _, _ = route_nets(model, ["BIG"], set(), params=params)
    elapsed = time.perf_counter() - t0
    assert result.nets_routed == 1
    assert result.nets_failed == 0
    assert result.expansions > 0
    assert elapsed < 120, f"chunked route took {elapsed:.1f}s"


def test_massive_net_fail_fast_when_chunking_disabled():
    model = _ladder(40)
    params = RoutingParams(run_drc=False, stitch_gnd_vias=False,
                           piecemeal_chunk_terminals=0)
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
    # Over the cap with chunking on: auto-split (a 4-terminal net fits
    # one chunk and routes whole) — no guard string.
    res, _, _ = route_nets(
        _ladder(4), ["BIG"], set(),
        params=RoutingParams(run_drc=False, stitch_gnd_vias=False,
                             max_terminals_per_net=3),
    )
    assert res.nets_routed == 1
    assert "max_terminals_per_net" not in res.failed_reasons.get("BIG", "")
    # Over the cap with chunking off: guard fires (prior behavior).
    res, _, _ = route_nets(
        _ladder(4), ["BIG"], set(),
        params=RoutingParams(run_drc=False, stitch_gnd_vias=False,
                             max_terminals_per_net=3,
                             piecemeal_chunk_terminals=0),
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


def test_params_round_trip_terminal_caps():
    params = RoutingParams(max_terminals_per_net=7, piecemeal_chunk_terminals=5)
    rt = RoutingParams.from_dict(params.to_dict())
    assert rt.max_terminals_per_net == 7
    assert rt.piecemeal_chunk_terminals == 5
    with pytest.raises(ValueError):
        RoutingParams.from_dict({"max_terminals_per_net": -1})
    with pytest.raises(ValueError):
        RoutingParams.from_dict({"piecemeal_chunk_terminals": -1})


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
    assert params.max_expansions_per_net == 4_000_000
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
def test_dccf_gnd_chunks_with_bounded_budget():
    """Over-cap GND auto-splits; per-chunk budgets bound total work."""
    import math

    model = board_model(parse_file(DCCF_PCB))
    assert len(model.nets["GND"]) == 66
    params = RoutingParams(run_drc=False, stitch_gnd_vias=False,
                           max_attempts_per_net=1,
                           max_expansions_per_net=500_000)
    size = params.piecemeal_chunk_terminals
    n_chunks = (
        1 if len(model.nets["GND"]) <= size
        else math.ceil((len(model.nets["GND"]) - size) / (size - 1)) + 1
    )
    t0 = time.perf_counter()
    result, _, _ = route_nets(model, ["GND"], set(), params=params)
    elapsed = time.perf_counter() - t0
    # Either every chunk routes, or the failure names the failed chunk.
    if result.nets_failed:
        assert "chunk" in result.failed_reasons["GND"]
    else:
        assert result.nets_routed == 1
    assert result.expansions <= n_chunks * params.max_expansions_per_net
    assert elapsed < 300, f"chunked GND took {elapsed:.1f}s"


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
