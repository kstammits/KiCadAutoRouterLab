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
from tests.helpers import ladder_board

DCCF_PCB = Path(__file__).parent / "fixtures" / "DCCF.sved.kicad_pcb"


def _ladder(n_pads: int, net: str = "BIG") -> BoardModel:
    return ladder_board(n_pads, net, uuid_prefix="uuid-piece")


def _terms(n: int):
    return [(i, 0, 0) for i in range(n)]


@pytest.mark.unit
def test_chunk_terminals_disjoint_and_cover():
    chunks = _chunk_terminals(_terms(21), 12)
    assert [len(c) for c in chunks] == [12, 9]
    # Disjoint, in order, full coverage.
    flat = [t for c in chunks for t in c]
    assert flat == _terms(21)


@pytest.mark.unit
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


@pytest.mark.unit
def test_merge_chunk_results_concatenates():
    r = _merge_chunk_results([_net_result([(0, 0, 0), (1, 0, 0)], 7),
                              _net_result([(1, 0, 0), (2, 0, 0)], 5)])
    assert r.success
    assert r.expansions == 12
    assert r.path == [(0, 0, 0), (1, 0, 0), (2, 0, 0)]
    assert len(r.branches) == 2


@pytest.mark.unit
def test_check_chunk_coverage():
    merged = _net_result([(0, 0, 0), (1, 0, 0)])
    assert _check_chunk_coverage([(0, 0, 0), (1, 0, 0)], merged) is None
    problem = _check_chunk_coverage([(0, 0, 0), (1, 0, 0), (9, 9, 0)], merged)
    assert problem is not None and "disconnected" in problem


@pytest.mark.unit
@pytest.mark.parametrize("n", [17, 20, 21])
def test_ladder_sizes_route_chunked(n):
    """17/20/21 terminals -> chunks; routed == 1 implies splice connectivity.

    21 (+12V) and 17 (-12V) are the previously hanging rail sizes.
    """
    model = _ladder(n)
    t0 = time.perf_counter()
    result, _, _ = route_nets(
        model, ["BIG"], set(),
        params=RoutingParams(run_drc=False, stitch_gnd_vias=False),
    )
    elapsed = time.perf_counter() - t0
    assert result.nets_routed == 1, f"{n}-terminal ladder failed"
    assert result.nets_failed == 0
    if n == 20:
        assert result.tracks_added > 0
        assert elapsed < 120, f"chunked ladder took {elapsed:.1f}s"


@pytest.mark.unit
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


@pytest.mark.slow
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


# ---------------------------------------------------------------------------
# Chunked-routing equivalence: N one-net calls == one N-net call.
# (Moved from test_routing_chunked.py: the UI routes chunked for progress
# + cancel, which holds only because each call rebuilds grids from the
# current model. DCCF-backed, ~10s, hence slow+dccf.)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def dccf_tree():
    from kicad_autorouter.io import parse_file

    return parse_file(DCCF_PCB)


def _routable_nets(model, limit=3):
    """Small multi-pad nets (deterministic order).

    NOTE: large nets (e.g. 66-pad GND, ~2k pair-searches) are deliberately
    excluded — they take minutes per call and belong in the benchmark
    script, not the unit suite.
    """
    small = [n for n, conns in model.nets.items() if 2 <= len(conns) <= 6]
    return sorted(small)[:limit]


@pytest.mark.slow
@pytest.mark.dccf
def test_chunked_sequential_matches_batched(dccf_tree):
    model = board_model(dccf_tree)
    nets = _routable_nets(model)
    if len(nets) < 2:
        pytest.skip("need >= 2 routable nets")
    params = RoutingParams(run_drc=False)

    # Batched: all nets in one call (with a tree so the final board exists
    # for the island comparison below).
    batched, _, _ = route_nets(model, nets, set(), params=params,
                               original_pcb_tree=dccf_tree)

    # Chunked: one net per call, model rebuilt from the tree each time —
    # exactly what ui/server.py does between UI chunk requests.
    # Stitching runs per call, so route copper and stitch copper are
    # compared separately: the equivalence tripwire covers routing;
    # stitching is audited via island counts below.
    tree = dccf_tree
    totals = {"tracks": 0, "vias": 0, "routed": 0, "failed": 0,
              "stitch_tracks": 0, "stitch_vias": 0}
    for net in nets:
        m = board_model(tree)
        res, _, _ = route_nets(m, [net], set(), params=params, original_pcb_tree=tree)
        totals["tracks"] += res.tracks_added - res.stitch_tracks_added
        totals["vias"] += res.vias_added - res.stitch_vias_added
        totals["routed"] += res.nets_routed
        totals["failed"] += res.nets_failed
        totals["stitch_tracks"] += res.stitch_tracks_added
        totals["stitch_vias"] += res.stitch_vias_added
        tree = res.pcb_tree

    assert totals["routed"] == batched.nets_routed
    assert totals["failed"] == batched.nets_failed
    # KNOWN DIVERGENCE (documented, not asserted exactly): a rebuilt grid
    # marks pre-existing tracks centerline-only (obstacles._mark_track has
    # no width inflation), while in-batch _apply_route_to_grid reserves the
    # full emitted-width corridor. So later nets in chunked mode may take
    # slightly tighter paths than in batched mode. Routed/failed sets must
    # match; route-track totals must be in the same ballpark. Making
    # rebuilds width-aware is follow-up work (see docs/roadmap.md, GND continuity
    # phase). Stitch tracks/vias are excluded here (each chunked call
    # repairs its intermediate state, so they accumulate differently by
    # design); island reduction is asserted on the final boards instead.
    batched_route_tracks = batched.tracks_added - batched.stitch_tracks_added
    if batched_route_tracks:
        ratio = totals["tracks"] / batched_route_tracks
        assert 0.8 <= ratio <= 1.2, f"track totals diverged: {totals['tracks']} vs {batched_route_tracks}"
    assert totals["vias"] <= (batched.vias_added - batched.stitch_vias_added) + 2

    # Both flows must not leave more GND islands than they found. (On the
    # old fragmented pours this was a strict reduction; on intact pours
    # routing carves and stitching restores, so non-regression is the
    # invariant. Chunked typically repairs more: it stitches after every net.)
    from kicad_autorouter.routing.grid import create_grid_from_model
    from kicad_autorouter.routing.pour import gnd_islands
    initial = sum(len(v) for v in gnd_islands(
        model, create_grid_from_model(model)).values())
    for final_tree, tag in ((batched.pcb_tree, "batched"), (tree, "chunked")):
        final_model = board_model(final_tree)
        final = sum(len(v) for v in gnd_islands(
            final_model, create_grid_from_model(final_model)).values())
        assert final <= initial, f"{tag}: islands {initial} -> {final}"


@pytest.mark.slow
@pytest.mark.dccf
def test_chunked_cancel_keeps_routed_copper(dccf_tree):
    """Cancelling after k nets keeps their copper (no rollback by design)."""
    model = board_model(dccf_tree)
    nets = _routable_nets(model)
    if len(nets) < 2:
        pytest.skip("need >= 2 routable nets")
    params = RoutingParams(run_drc=False)

    # Route only the first net, then "cancel".
    m = board_model(dccf_tree)
    res, _, _ = route_nets(m, [nets[0]], set(), params=params, original_pcb_tree=dccf_tree)
    kept_model = board_model(res.pcb_tree)

    # The kept net's copper survived; the unrouted net has no tracks.
    assert res.nets_routed == 1
    assert len(kept_model.tracks) + len(kept_model.vias) > 0
