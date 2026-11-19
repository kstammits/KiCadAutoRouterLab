"""Cutoff repulsion helper: physics and scaling (unit)."""

import numpy as np
import pytest

from kicad_autorouter.placement.force import (
    _pad_repulsion,
    _repulsion_cutoff_mm,
)

K = 2e-9


def _fixture(seed=11, n=40, n_fp=6, spread=200.0):
    rng = np.random.default_rng(seed)
    pos = rng.uniform(0, spread, size=(n, 2))
    p2f = rng.integers(0, n_fp, size=n).astype(np.int64)
    charged = rng.random(n) < 0.8
    return pos, p2f, charged


@pytest.mark.unit
def test_cutoff_drops_only_beyond_cutoff_pairs():
    # Reference: brute-force same math in plain numpy.
    pos, p2f, charged = _fixture()
    cutoff = 20.0
    diff = pos[:, None, :] - pos[None, :, :]
    dist = np.sqrt(np.maximum(np.sum(diff * diff, axis=2), 1e-6))
    fmag = K / np.maximum((dist ** 2) * 1e-6, 1e-12)
    np.fill_diagonal(fmag, 0.0)
    fmag[dist > cutoff] = 0.0
    same = (p2f[:, None] == p2f[None, :]) & (p2f[:, None] >= 0)
    fmag[same] = 0.0
    fmag[~charged, :] = 0.0
    fmag[:, ~charged] = 0.0
    expected = np.sum(diff * (fmag / dist)[:, :, None], axis=1)
    out = _pad_repulsion(pos, K, cutoff, charged, p2f)
    np.testing.assert_allclose(out, expected, rtol=1e-9, atol=1e-12)


@pytest.mark.unit
def test_zero_strength_means_zero_force():
    pos, p2f, charged = _fixture()
    out = _pad_repulsion(pos, 0.0, 0.0, charged, p2f)
    np.testing.assert_array_equal(out, np.zeros_like(pos))


@pytest.mark.unit
def test_cutoff_scales_with_strength():
    assert _repulsion_cutoff_mm(2e-9) == pytest.approx(20.0)
    assert _repulsion_cutoff_mm(1e-7) == pytest.approx(20.0 * (50.0 ** 0.5))
    assert _repulsion_cutoff_mm(0.0) == 0.0
    assert _repulsion_cutoff_mm(1e-15) == pytest.approx(
        20.0 * ((1e-15 / 2e-9) ** 0.5))


@pytest.mark.unit
def test_single_pair_coulomb_magnitude():
    pos = np.array([[0.0, 0.0], [10.0, 0.0]])
    p2f = np.array([0, 1], dtype=np.int64)
    out = _pad_repulsion(pos, K, 20.0, None, p2f)
    expect = K / (0.01 ** 2)  # 2e-5 N
    assert out[0, 0] == pytest.approx(-expect, rel=1e-12)
    assert out[1, 0] == pytest.approx(expect, rel=1e-12)
    assert out[0, 1] == pytest.approx(0.0, abs=1e-18)


@pytest.mark.unit
def test_far_pairs_contribute_nothing():
    pos = np.array([[0.0, 0.0], [100.0, 0.0], [0.0, 100.0]])
    p2f = np.array([0, 1, 2], dtype=np.int64)
    out = _pad_repulsion(pos, K, 20.0, None, p2f)
    np.testing.assert_array_equal(out, np.zeros((3, 2)))


@pytest.mark.unit
def test_uncharged_pads_carry_no_charge():
    pos = np.array([[0.0, 0.0], [5.0, 0.0]])
    p2f = np.array([0, 1], dtype=np.int64)
    out = _pad_repulsion(pos, K, 20.0,
                         np.array([True, False]), p2f)
    np.testing.assert_array_equal(out, np.zeros((2, 2)))
    both = _pad_repulsion(pos, K, 20.0, np.array([True, True]), p2f)
    assert np.linalg.norm(both, axis=1).min() > 0.0
