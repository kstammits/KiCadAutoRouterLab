"""Nets-table enrichment: lengths, cost, region/bridge + failure flags.

Markers: unit, server
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ui.handlers import nets as nets_h  # noqa: E402
from ui.server_state import STATE  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def _load(fixture):
    STATE.load((FIXTURES / fixture).read_bytes(), fixture, "pcb")


def _rows():
    code, body, _ = nets_h._handle_nets_get()
    assert code == 200
    return json.loads(body)["nets"]


@pytest.mark.unit
@pytest.mark.server
def test_mst_helper():
    assert nets_h._mst_length_mm([]) == 0.0
    assert nets_h._mst_length_mm([(0, 0)]) == 0.0
    assert nets_h._mst_length_mm([(0, 0), (3, 4)]) == pytest.approx(5.0)
    assert nets_h._mst_length_mm([(0, 0), (1, 0), (2, 0)]) == pytest.approx(2.0)


@pytest.mark.unit
@pytest.mark.server
def test_lengths_cost_invariant_and_failed_roundtrip():
    _load("tube111.kicad_pcb")
    for n in _rows():
        assert n["airwire_len_mm"] >= 0.0
        assert n["copper_len_mm"] >= 0.0
        assert n["copper_cost_mm"] == pytest.approx(
            n["copper_len_mm"] + n["via_count"] * n["via_cost_mm"], abs=0.011
        )
        assert n["failed_reason"] is None
    STATE.set_last_failed({"GND": "no route found"})
    gnd = next(n for n in _rows() if n["name"] == "GND")
    assert gnd["failed_reason"] == "no route found"
    assert gnd["failed_version"] == STATE.version


@pytest.mark.unit
@pytest.mark.server
def test_multi_region_bridge_flags():
    _load("tube111.kicad_pcb")
    gnd = next(n for n in _rows() if n["name"] == "GND")
    assert gnd["region_count"] >= 2
    assert gnd["multi_region"] is True
    assert gnd["needs_bridge"] is True and gnd["bridged"] is False
    model = STATE.snapshot[0]
    fp = next(f for f in model.footprints
              if any(p.net_name == "GND" for p in f.pads))
    pad = next(p for p in fp.pads if p.net_name == "GND")
    STATE.set_bridges([{"a": {"ref": fp.ref, "pad": pad.number},
                        "b": {"ref": fp.ref, "pad": pad.number},
                        "r_mohm": 20.0}])
    gnd2 = next(n for n in _rows() if n["name"] == "GND")
    assert gnd2["bridged"] is True and gnd2["needs_bridge"] is False
