"""Parity tests for the incremental obstacle cache (routing/obstacles.py).

The cached path must stay bit-identical to fresh full rebuilds: base grids,
mask re-exports, rip-up/add updates, via-landing masks, and end-to-end
routing outcomes.
"""

from dataclasses import replace

import numpy as np
import pytest

from kicad_autorouter.board_model import Point, Track, board_model
from kicad_autorouter.routing.grid import create_grid_from_model
from kicad_autorouter.routing.obstacles import (
    ObstacleCache,
    _build_tht_via_mask,
    build_fanout_cost,
    build_occupancy_grid,
    build_via_block_mask,
    track_corridor_radius_cells,
)
from kicad_autorouter.routing.pipeline import (
    RoutingParams,
    _rip_up_nets_from_model,
    route_nets,
)
from kicad_autorouter.routing.router import (
    SECONDARY_BLOCKED,
    build_via_ok_cached,
    build_via_ok_mask,
)
from kicad_autorouter.sexpr import parse_file

from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name):
    path = FIXTURES / f"{name}.kicad_pcb"
    if not path.exists():
        path = FIXTURES / f"{name}.sved.kicad_pcb"
    return board_model(parse_file(path))


def _signal_nets(model, k):
    cands = [
        (len(v), name)
        for name, v in model.nets.items()
        if 2 <= len(v) <= 4
        and not name.upper().startswith(("GND", "+", "-", "V"))
    ]
    cands.sort()
    return [name for _, name in cands[:k]]


@pytest.mark.unit
def test_cache_base_matches_fresh_build():
    for name in ("minimal", "tube111"):
        model = _load(name)
        grid = create_grid_from_model(model, 0.1, 2.0)
        fresh = build_occupancy_grid(model, grid, default_clearance_mm=0.2)
        cache = ObstacleCache()
        base = cache.sync(model, grid, 0.2, 0.125)
        assert np.array_equal(fresh, base), name
        # Second sync is a warm hit with identical content.
        assert np.array_equal(fresh, cache.sync(model, grid, 0.2, 0.125))
        assert cache.last_hit
        # A fresh parse of the same board hits the warm entry (no
        # parse-time artifacts like region uuids in the fingerprint).
        model2 = _load(name)
        assert np.array_equal(
            fresh, cache.sync(model2, grid, 0.2, 0.125)
        )
        assert cache.last_hit
        tht, vbb = cache.masks()
        assert np.array_equal(tht, _build_tht_via_mask(model, grid))
        assert np.array_equal(
            vbb,
            build_via_block_mask(
                model, grid, clearance_mm=0.2, track_half_mm=0.125
            ),
        )


@pytest.mark.unit
def test_cache_rip_and_add_update_matches_fresh():
    model = _load("tube111")
    grid = create_grid_from_model(model, 0.1, 2.0)
    cache = ObstacleCache()
    cache.sync(model, grid, 0.2, 0.125)
    assert not cache.last_hit

    # Rip-up removes copper -> region-local recompute.
    ripped = _rip_up_nets_from_model(model, ["GND", "Net1"])
    assert np.array_equal(
        build_occupancy_grid(ripped, grid, default_clearance_mm=0.2),
        cache.sync(ripped, grid, 0.2, 0.125),
    )
    assert cache.last_hit

    # Re-adding everything (new Track items) stamps them back in.
    extra = Track(
        start=Point(10.0, 10.0), end=Point(20.0, 10.0),
        width_mm=0.25, layer="F.Cu", net_name="Net1",
    )
    grown = replace(ripped, tracks=ripped.tracks + (extra,))
    assert np.array_equal(
        build_occupancy_grid(grown, grid, default_clearance_mm=0.2),
        cache.sync(grown, grid, 0.2, 0.125),
    )


@pytest.mark.unit
def test_cache_via_ok_matches_full():
    model = _load("tube111")
    grid = create_grid_from_model(model, 0.1, 2.0)
    cache = ObstacleCache()
    base = cache.sync(model, grid, 0.2, 0.125)
    entry = cache.active_entry()
    tht, vbb = cache.masks()
    sec = build_fanout_cost(
        model, grid, "Net1", clearance_mm=0.2, track_half_mm=0.125
    )
    full = build_via_ok_mask(base, vbb, tht, sec, 8, 100, SECONDARY_BLOCKED)
    first = build_via_ok_cached(entry, base, sec, 8, 100, SECONDARY_BLOCKED)
    assert np.array_equal(full, first)
    # Memoized second call with identical inputs.
    assert np.array_equal(
        full, build_via_ok_cached(entry, base, sec, 8, 100, SECONDARY_BLOCKED)
    )


@pytest.mark.unit
def test_track_corridor_covers_full_width_plus_clearance():
    # Two equal-width tracks need width + clearance centre separation for a
    # clearance edge gap; the corridor radius must cover it (half-width +
    # clearance short-changes two-track separation by half a track width).
    res = 0.1
    for width, clearance in [(0.25, 0.2), (0.3, 0.2), (0.5, 0.2)]:
        r = track_corridor_radius_cells(width, clearance, res)
        assert r * res >= clearance + width - 1e-9


@pytest.mark.unit
def test_cache_end_to_end_routing_parity():
    model = _load("tube111")
    nets = _signal_nets(model, 2)
    assert len(nets) == 2
    params = RoutingParams(
        grid_resolution_mm=0.2,
        max_attempts_per_net=1,
        max_expansions=200_000,
    )
    plain, _, _ = route_nets(model, nets, set(), params=params)
    cached, _, _ = route_nets(
        model, nets, set(), params=params, obstacle_cache=ObstacleCache()
    )
    assert (cached.nets_routed, cached.nets_failed) == (
        plain.nets_routed, plain.nets_failed,
    )
    assert cached.expansions == plain.expansions
    assert cached.timings.get("occupancy_cached") == 0.0  # first call: miss
