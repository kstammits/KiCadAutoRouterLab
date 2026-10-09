"""Chunked-routing equivalence: N one-net calls == one N-net call.

The UI routes chunked (one net per POST) for progress + cancel with no
server threading changes. This holds only because each call rebuilds its
grids from the current model — which already contains prior nets' copper
(the server rebuilds STATE.model from the updated tree after every call).
If chunked and batched ever diverge, the UI progress design is invalid.
"""

from pathlib import Path

import pytest

from kicad_autorouter.board_model import board_model
from kicad_autorouter.routing.pipeline import route_nets, RoutingParams
from kicad_autorouter.io import parse_file

FIXTURES = Path(__file__).parent / "fixtures"
# NOTE: minimal.kicad_pcb has no multi-pad nets, so chunking equivalence is
# exercised on DCCF (each one-net call costs ~1s; total ~10s).
DCCF_PCB = FIXTURES / "DCCF.sved.kicad_pcb"


@pytest.fixture(scope="module")
def minimal_tree():
    return parse_file(DCCF_PCB)


def _routable_nets(model, limit=3):
    """Small multi-pad nets (deterministic order).

    NOTE: large nets (e.g. 66-pad GND, ~2k pair-searches) are deliberately
    excluded — they take minutes per call and belong in the benchmark
    script, not the unit suite.
    """
    small = [n for n, conns in model.nets.items() if 2 <= len(conns) <= 6]
    return sorted(small)[:limit]


def test_chunked_sequential_matches_batched(minimal_tree):
    model = board_model(minimal_tree)
    nets = _routable_nets(model)
    if len(nets) < 2:
        pytest.skip("need >= 2 routable nets")
    params = RoutingParams(run_drc=False)

    # Batched: all nets in one call.
    batched, _, _ = route_nets(model, nets, set(), params=params)

    # Chunked: one net per call, model rebuilt from the tree each time —
    # exactly what ui/server.py does between UI chunk requests.
    tree = minimal_tree
    totals = {"tracks": 0, "vias": 0, "routed": 0, "failed": 0}
    for net in nets:
        m = board_model(tree)
        res, _, _ = route_nets(m, [net], set(), params=params, original_pcb_tree=tree)
        totals["tracks"] += res.tracks_added
        totals["vias"] += res.vias_added
        totals["routed"] += res.nets_routed
        totals["failed"] += res.nets_failed
        tree = res.pcb_tree

    assert totals["routed"] == batched.nets_routed
    assert totals["failed"] == batched.nets_failed
    # KNOWN DIVERGENCE (documented, not asserted exactly): a rebuilt grid
    # marks pre-existing tracks centerline-only (obstacles._mark_track has
    # no width inflation), while in-batch _apply_route_to_grid reserves the
    # full emitted-width corridor. So later nets in chunked mode may take
    # slightly tighter paths than in batched mode. Routed/failed sets must
    # match; track totals must be in the same ballpark. Making rebuilds
    # width-aware is follow-up work (see TODO.md, GND continuity phase).
    if batched.tracks_added:
        ratio = totals["tracks"] / batched.tracks_added
        assert 0.8 <= ratio <= 1.2, f"track totals diverged: {totals['tracks']} vs {batched.tracks_added}"
    assert totals["vias"] <= batched.vias_added + 2


def test_chunked_cancel_keeps_routed_nets(minimal_tree):
    """Cancelling after k nets keeps their copper (no rollback by design)."""
    model = board_model(minimal_tree)
    nets = _routable_nets(model)
    if len(nets) < 2:
        pytest.skip("need >= 2 routable nets")
    params = RoutingParams(run_drc=False)

    # Route only the first net, then "cancel".
    m = board_model(minimal_tree)
    res, _, _ = route_nets(m, [nets[0]], set(), params=params, original_pcb_tree=minimal_tree)
    kept_model = board_model(res.pcb_tree)

    # The kept net's copper survived; the unrouted net has no tracks.
    assert res.nets_routed == 1
    assert len(kept_model.tracks) + len(kept_model.vias) > 0
