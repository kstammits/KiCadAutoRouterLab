"""Footprints with no non-ground net hold filed positions (unit).

Nothing can pull them (ground nets carry no attraction edges), so
repulsion alone would only scatter them: mounting holes, unconnected
mechanical parts, and ground-only test points all stay put as immovable
contact obstacles. Matched structurally on nets, never on ref or
library footprint id.
"""

import pytest

from kicad_autorouter.placement.model import _collect_pad_nodes

from .helpers import make_board, make_fp


def _hole(ref="H1", uuid="uuid-h1", nets=("GND",),
          footprint_id="MountingHole:MountingHole_3.2mm_M3_Pad"):
    return make_fp(ref, uuid, 10.0, 10.0, nets, hw=1.6, hh=1.6,
                   footprint_id=footprint_id)


@pytest.mark.unit
def test_gnd_netted_mounting_hole_excluded_from_pad_nodes():
    hole = _hole()
    cap = make_fp("C1", "uuid-c1", 0.0, 0.0, ("VCC", "GND"))
    model = make_board([hole, cap])
    pads, pad_to_fp_idx, fp_uuid_to_pad_indices, _, _ = _collect_pad_nodes(model)
    assert hole.uuid not in fp_uuid_to_pad_indices
    assert cap.uuid in fp_uuid_to_pad_indices
    assert all(p.net_name is not None for p in pads)


@pytest.mark.unit
def test_custom_library_gnd_hole_excluded_by_nets_not_name():
    hole = _hole(ref="J99", uuid="uuid-j99",
                 footprint_id="MyLib:Weird_Hole_5mm")
    model = make_board([hole])
    _, _, fp_uuid_to_pad_indices, _, _ = _collect_pad_nodes(model)
    assert hole.uuid not in fp_uuid_to_pad_indices


@pytest.mark.unit
def test_signal_netted_single_pad_part_still_placed():
    # A MountingHole footprint deliberately put on a signal net has real
    # pull and stays an electrical placement subject.
    tp = _hole(ref="TP1", uuid="uuid-tp1", nets=("N1",),
               footprint_id="MountingHole:MountingHole_3.2mm_M3_Pad")
    model = make_board([tp])
    pads, _, fp_uuid_to_pad_indices, _, _ = _collect_pad_nodes(model)
    assert tp.uuid in fp_uuid_to_pad_indices
    assert len(pads) == 1
