"""Unit tests for ui.debug_grids payload builders (pure numpy->dict).

Markers: unit (synthetic arrays only, no fixtures, no I/O).
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ui"))

from debug_grids import (  # noqa: E402
    _cost_grid_to_debug,
    _secondary_to_debug,
)

pytestmark = pytest.mark.unit


def _grid(layers=("F.Cu", "B.Cu"), h=32, w=48, res=0.1):
    return SimpleNamespace(layers=list(layers), origin_mm=[0.0, 0.0],
                           resolution_mm=res)


def test_cost_grid_small_passthrough_min_max():
    grid = _grid()
    cost = np.zeros((2, 32, 48), dtype=np.int16)
    cost[0, 3, 4] = 100
    cost[0, 5, 6] = 50
    out = _cost_grid_to_debug(cost, grid)
    assert out["layers"][0]["scale"] == 1
    assert out["layers"][0]["height"] == 32
    assert out["layers"][0]["width"] == 48
    assert out["layers"][0]["data"][3][4] == 100
    assert out["layers"][0]["min_cost"] == 50
    assert out["layers"][0]["max_cost"] == 100
    # Empty layer reports zeros, not a crash on min().
    assert out["layers"][1]["min_cost"] == 0
    assert out["layers"][1]["max_cost"] == 0
    assert out["resolution_mm"] == pytest.approx(0.1)


def test_cost_grid_downsample_preserves_max():
    grid = _grid(h=400, w=400)
    cost = np.zeros((2, 400, 400), dtype=np.int16)
    cost[1, 201, 203] = 100  # lone blocked cell inside a block
    out = _cost_grid_to_debug(cost, grid)
    assert out["layers"][1]["scale"] == 2
    assert out["layers"][1]["height"] == 200
    assert out["layers"][1]["width"] == 200
    assert max(v for row in out["layers"][1]["data"] for v in row) == 100
    assert out["resolution_mm"] == pytest.approx(0.2)


def test_secondary_banding_boundaries():
    grid = _grid(layers=("F.Cu",), h=8, w=8)
    sec = np.zeros((1, 8, 8), dtype=np.int16)
    vals = [0, 1, 100, 399, 500, 999, 1000, 10000]
    for c, v in enumerate(vals):
        sec[0, 0, c] = v
    out = _secondary_to_debug(sec, grid, [])
    row = out["layers"][0]["data"][0][:8]
    assert row == [0, 1, 1, 1, 2, 2, 3, 3]
    assert out["bands"] == {0: "free", 1: "soft", 2: "inner", 3: "hard"}
    assert out["blocked_at"] == 1000


def test_secondary_terminals_seal_flags_and_ds_coords():
    grid = _grid(layers=("F.Cu",), h=400, w=400)
    sec = np.zeros((1, 400, 400), dtype=np.int16)
    sec[0, 100, 200] = 10000  # sealed terminal
    out = _secondary_to_debug(sec, grid, [(200, 100, 0), (10, 10, 0),
                                          (9999, 9999, 0), (5, 5, 9)])
    terms = out["terminals"]
    assert terms[0]["sealed"] is True
    assert terms[0]["own_cell"] == 10000
    assert (terms[0]["dc"], terms[0]["dr"]) == (100, 50)  # scale 2
    assert terms[1]["sealed"] is False
    assert terms[1]["own_cell"] == 0
    # Out-of-bounds cell / layer: -1, never sealed.
    assert terms[2]["own_cell"] == -1
    assert terms[2]["sealed"] is False
    assert terms[3]["own_cell"] == -1
    assert terms[3]["sealed"] is False
