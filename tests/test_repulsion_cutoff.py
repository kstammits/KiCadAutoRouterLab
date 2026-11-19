"""Kernel-vs-dense exactness for the cutoff repulsion helper (unit)."""

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
    mask = ((p2f[:, None] == p2f[None, :]) & (p2f[:, None] >= 0)
            & ~np.eye(n, dtype=bool))
    return pos, p2f, charged, mask


@pytest.mark.unit
def test_kernel_matches_dense_with_cutoff():
    pos, p2f, charged, mask = _fixture()
    for cutoff in (10.0, 20.0, 45.0):
        kernel = _pad_repulsion(pos, K, cutoff, mask, charged, p2f)
        dense = _pad_repulsion(pos, K, cutoff, mask, charged, None)
        np.testing.assert_allclose(kernel, dense, rtol=1e-9, atol=1e-12)


@pytest.mark.unit
def test_kernel_matches_dense_cutoff_disabled():
    pos, p2f, charged, mask = _fixture()
    kernel = _pad_repulsion(pos, K, 0.0, mask, charged, p2f)
    dense = _pad_repulsion(pos, K, 0.0, mask, charged, None)
    # cutoff<=0 selects the dense path in both cases: bitwise identical.
    np.testing.assert_array_equal(kernel, dense)


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
    out = _pad_repulsion(pos, K, 20.0, None, None, p2f)
    expect = K / (0.01 ** 2)  # 2e-5 N
    assert out[0, 0] == pytest.approx(-expect, rel=1e-12)
    assert out[1, 0] == pytest.approx(expect, rel=1e-12)
    assert out[0, 1] == pytest.approx(0.0, abs=1e-18)


@pytest.mark.unit
def test_far_pairs_contribute_nothing():
    pos = np.array([[0.0, 0.0], [100.0, 0.0], [0.0, 100.0]])
    p2f = np.array([0, 1, 2], dtype=np.int64)
    out = _pad_repulsion(pos, K, 20.0, None, None, p2f)
    np.testing.assert_array_equal(out, np.zeros((3, 2)))
