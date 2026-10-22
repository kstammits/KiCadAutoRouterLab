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

    # Batched: all nets in one call (with a tree so the final board exists
    # for the island comparison below).
    batched, _, _ = route_nets(model, nets, set(), params=params,
                               original_pcb_tree=minimal_tree)

    # Chunked: one net per call, model rebuilt from the tree each time —
    # exactly what ui/server.py does between UI chunk requests.
    # Stitching runs per call, so route copper and stitch copper are
    # compared separately: the equivalence tripwire covers routing;
    # stitching is audited via island counts below.
    tree = minimal_tree
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

    # Both flows must leave fewer GND islands than they found. (Chunked
    # typically repairs more: it stitches after every net.)
    from kicad_autorouter.routing.grid import create_grid_from_model
    from kicad_autorouter.routing.pour import gnd_islands
    initial = sum(len(v) for v in gnd_islands(
        model, create_grid_from_model(model)).values())
    for final_tree, tag in ((batched.pcb_tree, "batched"), (tree, "chunked")):
        final_model = board_model(final_tree)
        final = sum(len(v) for v in gnd_islands(
            final_model, create_grid_from_model(final_model)).values())
        assert final < initial, f"{tag}: islands {initial} -> {final}"
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
