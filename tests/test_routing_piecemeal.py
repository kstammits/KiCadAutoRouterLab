"""Piecemeal routing: over-cap nets auto-split into spatial chunks.

Later chunks route against earlier chunks as obstacle they may also join
(kept copper stays walkable + a valid A* goal, plus a trunk connector).
``routed == 1`` on a chunked net implies splice connectivity — the engine
fails the net via ``_check_chunk_coverage`` otherwise. Failed nets keep
their routed chunks as display-only partial copper (never written back).
"""

import time
from dataclasses import replace
from pathlib import Path

import pytest

from kicad_autorouter.board_model import (
    BoardModel,
    NetConnection,
    Point,
    board_model,
)
from kicad_autorouter.board_model.model import KeepoutZone
from kicad_autorouter.sexpr import parse_file
from kicad_autorouter.routing.pipeline import (
    RoutingParams,
    _check_chunk_coverage,
    _chunk_terminals,
    _merge_chunk_results,
    route_nets,
)
from kicad_autorouter.routing.router import RouteResult as NetRouteResult
from tests.helpers import make_fp

DCCF_PCB = Path(__file__).parent / "fixtures" / "DCCF.sved.kicad_pcb"


def _ladder(n_pads: int, net: str = "BIG") -> BoardModel:
    """One single-pad footprint per terminal, spread along x."""
    fps = [
        make_fp(f"U{i}", f"uuid-piece-{i:03d}", x=float(i) * 4.0, nets=(net,))
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


def _terms(n: int):
    return [(i, 0, 0) for i in range(n)]


def test_chunk_terminals_disjoint_and_cover():
    chunks = _chunk_terminals(_terms(21), 12)
    assert [len(c) for c in chunks] == [12, 9]
    # Disjoint, in order, full coverage.
    flat = [t for c in chunks for t in c]
    assert flat == _terms(21)


def test_chunk_terminals_small_sizes():
    assert _chunk_terminals(_terms(12), 12) == [_terms(12)]
    # Trailing singleton merges into the previous chunk.
    assert [len(c) for c in _chunk_terminals(_terms(13), 12)] == [13]
    assert [len(c) for c in _chunk_terminals(_terms(25), 12)] == [12, 13]
    # Size < 2 routes whole.
    assert _chunk_terminals(_terms(40), 1) == [_terms(40)]
    assert _chunk_terminals(_terms(40), 0) == [_terms(40)]


def _net_result(cells, expansions=10):
    branch = tuple(cells)
    return NetRouteResult(
        path=list(branch), wire_mm=float(len(branch)), via_count=0,
        cost=float(len(branch)), success=True, expansions=expansions,
        branches=(branch,),
    )


def test_merge_chunk_results_concatenates():
    r = _merge_chunk_results([_net_result([(0, 0, 0), (1, 0, 0)], 7),
                              _net_result([(1, 0, 0), (2, 0, 0)], 5)])
    assert r.success
    assert r.expansions == 12
    assert r.path == [(0, 0, 0), (1, 0, 0), (2, 0, 0)]
    assert len(r.branches) == 2


def test_check_chunk_coverage():
    merged = _net_result([(0, 0, 0), (1, 0, 0)])
    assert _check_chunk_coverage([(0, 0, 0), (1, 0, 0)], merged) is None
    problem = _check_chunk_coverage([(0, 0, 0), (1, 0, 0), (9, 9, 0)], merged)
    assert problem is not None and "disconnected" in problem


def test_chunked_ladder_routes_and_covers():
    """20 terminals -> 2 chunks; routed == 1 implies splice connectivity."""
    model = _ladder(20)
    t0 = time.perf_counter()
    result, _, _ = route_nets(
        model, ["BIG"], set(),
        params=RoutingParams(run_drc=False, stitch_gnd_vias=False),
    )
    elapsed = time.perf_counter() - t0
    assert result.nets_routed == 1
    assert result.nets_failed == 0
    assert result.tracks_added > 0
    assert elapsed < 120, f"chunked ladder took {elapsed:.1f}s"


def test_power_rail_sizes_route_chunked():
    """21 (+12V) and 17 (-12V) terminals: the previously hanging sizes."""
    for n in (21, 17):
        result, _, _ = route_nets(
            _ladder(n), ["BIG"], set(),
            params=RoutingParams(run_drc=False, stitch_gnd_vias=False),
        )
        assert result.nets_routed == 1, f"{n}-terminal ladder failed"
        assert result.nets_failed == 0


def test_forced_chunks_on_small_net():
    """Chunking engages below the tripwire when the cap is lowered."""
    params = RoutingParams(run_drc=False, stitch_gnd_vias=False,
                           max_terminals_per_net=10,
                           piecemeal_chunk_terminals=6)
    result, _, _ = route_nets(_ladder(12), ["BIG"], set(), params=params)
    assert result.nets_routed == 1
    assert result.nets_failed == 0


@pytest.mark.dccf
@pytest.mark.slow
def test_dccf_gnd_routes():
    """DCCF GND spans two board regions and routes fully per region.

    A region-1 chunk used to fail here: its pair-searches terminated on
    kept copper instead of each other, stranding one terminal, and the
    seed join (which would have fixed it) was skipped on the incomplete
    tree. The per-component join rescues it.
    """
    model = board_model(parse_file(DCCF_PCB))
    params = RoutingParams(run_drc=False, stitch_gnd_vias=False,
                           max_attempts_per_net=1,
                           max_expansions_per_net=2_000_000)
    result, _, _ = route_nets(model, ["GND"], set(), params=params)
    assert result.nets_routed == 1
    assert result.nets_failed == 0
    assert result.tracks_added > 0


def test_failed_chunk_keeps_partial_copper():
    """A failed chunked net keeps its routed chunks as display-only copper.

    The keepout seals the later chunks' pads off the board: chunk 1
    routes, chunk 2 finds no path. Partial tracks carry the failed net's
    name for the viewer overlay and nothing is committed (tracks_added
    stays 0 — partials are excluded from writeback).
    """
    model = _ladder(20)
    keepout = KeepoutZone(
        uuid="kz-sealed",
        layers=("F.Cu", "B.Cu"),
        polygon=(Point(40.0, -5.0), Point(90.0, -5.0),
                 Point(90.0, 15.0), Point(40.0, 15.0)),
        tracks_allowed=False, vias_allowed=False, pads_allowed=False,
    )
    model = replace(model, keepout_zones=(keepout,))
    params = RoutingParams(run_drc=False, stitch_gnd_vias=False,
                           max_attempts_per_net=1,
                           max_terminals_per_net=10,
                           piecemeal_chunk_terminals=6)
    result, _, _ = route_nets(model, ["BIG"], set(), params=params)
    assert result.nets_failed == 1
    assert "no path found" in result.failed_reasons["BIG"]
    assert len(result.partial_tracks) > 0
    assert {t.net_name for t in result.partial_tracks} == {"BIG"}
    assert all(t.width_mm > 0 for t in result.partial_tracks)
    assert result.tracks_added == 0
    assert all(v.net_name == "BIG" for v in result.partial_vias)
