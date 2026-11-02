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
    _encode_paletted_png,
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


def _decode_png_size(blob: bytes):
    import struct
    assert blob[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", blob[16:24])
    return w, h


def test_cost_png_full_res_and_palette_exact():
    import base64
    grid = _grid()
    cost = np.zeros((2, 32, 48), dtype=np.int16)
    cost[0, 3, 4] = 25
    cost[0, 5, 6] = 100
    out = _cost_grid_to_debug(cost, grid)
    layer = out["layers"][0]
    assert layer["png_scale"] == 1
    blob = base64.b64decode(layer["png_b64"])
    assert _decode_png_size(blob) == (48, 32)
    assert out["full_resolution_mm"] == pytest.approx(0.1)


def test_cost_png_unknown_value_rounds_hotter():
    import base64
    import struct
    import zlib
    grid = _grid()
    cost = np.zeros((1, 4, 4), dtype=np.int16)
    cost[0, 0, 0] = 45  # not in the engine's value set
    out = _cost_grid_to_debug(cost, grid)
    blob = base64.b64decode(out["layers"][0]["png_b64"])
    # Walk chunks to IDAT, decompress, read the palette index at (0,0):
    # 45 must round up to the high-cost slot (index 3), never hide cost.
    pos = 8
    raw = b""
    while pos < len(blob):
        (length,) = struct.unpack(">I", blob[pos:pos + 4])
        tag = blob[pos + 4:pos + 8]
        if tag == b"IDAT":
            raw += blob[pos + 8:pos + 8 + length]
        pos += 12 + length
    rows = zlib.decompress(raw)
    assert rows[1] == 3  # filter byte 0, then index 3 at (0,0)


def test_secondary_terminals_carry_full_res_coords():
    grid = _grid(layers=("F.Cu",), h=400, w=400)
    sec = np.zeros((1, 400, 400), dtype=np.int16)
    out = _secondary_to_debug(sec, grid, [(200, 100, 0)])
    assert (out["terminals"][0]["fc"], out["terminals"][0]["fr"]) == (200, 100)
