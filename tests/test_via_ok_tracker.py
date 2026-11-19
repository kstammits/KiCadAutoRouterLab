"""ViaOkTracker: incremental via mask must match the one-shot build exactly."""

import numpy as np
import pytest

from kicad_autorouter.routing.router_via import (
    ViaOkTracker,
    build_via_ok_mask,
)


def _oneshot(cost, vb, tht, sec, r):
    return build_via_ok_mask(cost, vb, tht, sec, r, 100, 1000)


def _rand(rng, shape):
    return (rng.random(shape) < 0.08)


@pytest.mark.unit
def test_tracker_matches_oneshot_through_mutations():
    rng = np.random.default_rng(7)
    H, W = 60, 50
    cost = (rng.random((2, H, W)) * 120).astype(np.int16)
    vb = _rand(rng, (H, W))
    tht = _rand(rng, (H, W))
    sec = (rng.random((2, H, W)) * 1200).astype(np.int16)
    tracker = ViaOkTracker()
    # Sequence: growth, removals, edge-touching change, secondary swap.
    muts = [
        (slice(5, 15), slice(5, 15), True),
        (slice(8, 12), slice(8, 12), False),  # removal inside prior growth
        (slice(0, 4), slice(0, W), True),  # board-edge touch
        (slice(50, 60), slice(40, 50), True),
    ]
    for i, (rs, cs, val) in enumerate(muts):
        cost[:, rs, cs] = 150 if val else 0
        if i == 2:
            sec = (rng.random((2, H, W)) * 1200).astype(np.int16)
        got = tracker.build(cost, vb, tht, sec, 3, 100, 1000)
        want = _oneshot(cost, vb, tht, sec, 3)
        assert got.shape == want.shape == (H, W)
        assert np.array_equal(got, want), f"mismatch at mutation {i}"


@pytest.mark.unit
def test_tracker_radius_change_zero_and_none_secondary():
    rng = np.random.default_rng(11)
    H, W = 40, 37
    cost = (rng.random((2, H, W)) * 120).astype(np.int16)
    vb = _rand(rng, (H, W))
    tht = _rand(rng, (H, W))
    tracker = ViaOkTracker()
    sec = (rng.random((2, H, W)) * 1200).astype(np.int16)
    assert np.array_equal(
        tracker.build(cost, vb, tht, sec, 3, 100, 1000),
        _oneshot(cost, vb, tht, sec, 3),
    )
    # Radius change forces a fresh full build, still exact.
    assert np.array_equal(
        tracker.build(cost, vb, tht, sec, 5, 100, 1000),
        _oneshot(cost, vb, tht, sec, 5),
    )
    # Back to the first radius with mutated input: still exact.
    cost[:, 10:20, 10:20] = 150
    assert np.array_equal(
        tracker.build(cost, vb, tht, sec, 3, 100, 1000),
        _oneshot(cost, vb, tht, sec, 3),
    )
    # Degenerate radius: all-True mask like the one-shot path.
    assert tracker.build(cost, vb, tht, sec, 0, 100, 1000).all()
    # Secondary None.
    assert np.array_equal(
        tracker.build(cost, vb, tht, None, 3, 100, 1000),
        _oneshot(cost, vb, tht, None, 3),
    )


@pytest.mark.unit
def test_tracker_empty_diff_reuses_mask():
    rng = np.random.default_rng(13)
    H, W = 30, 30
    cost = (rng.random((2, H, W)) * 120).astype(np.int16)
    vb = _rand(rng, (H, W))
    tht = _rand(rng, (H, W))
    tracker = ViaOkTracker()
    a = tracker.build(cost, vb, tht, None, 2, 100, 1000)
    b = tracker.build(cost, vb, tht, None, 2, 100, 1000)
    assert np.array_equal(a, b)
