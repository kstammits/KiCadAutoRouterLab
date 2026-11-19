"""Netted mounting holes hold filed positions (unit)."""

import pytest

from kicad_autorouter.placement.model import (
    _collect_pad_nodes,
    _is_mounting_hole,
)

from .helpers import make_board, make_fp


def _hole(ref="H1", uuid="uuid-h1", nets=("GND",),
          footprint_id="MountingHole:MountingHole_3.2mm_M3_Pad"):
    return make_fp(ref, uuid, 10.0, 10.0, nets, hw=1.6, hh=1.6,
                   footprint_id=footprint_id)


@pytest.mark.unit
def test_mounting_hole_detected_by_footprint_id_not_ref():
    assert _is_mounting_hole(_hole())
    assert _is_mounting_hole(_hole(ref="J99"))
    assert not _is_mounting_hole(
        make_fp("C1", "uuid-c1", 0.0, 0.0, ("VCC", "GND"),
                footprint_id="Test:R_0805"))


@pytest.mark.unit
def test_gnd_netted_mounting_hole_excluded_from_pad_nodes():
    hole = _hole()
    cap = make_fp("C1", "uuid-c1", 0.0, 0.0, ("VCC", "GND"))
    model = make_board([hole, cap])
    pads, pad_to_fp_idx, fp_uuid_to_pad_indices, _, _ = _collect_pad_nodes(model)
    assert hole.uuid not in fp_uuid_to_pad_indices
    assert cap.uuid in fp_uuid_to_pad_indices
    assert all(p.net_name is not None for p in pads)
