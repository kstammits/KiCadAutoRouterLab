"""Label-aware placement: one visible label per part, orbit always on.

Covers the sided single-label parse (SilkS over Fab, same side only,
``${REFERENCE}`` resolution), the rigid-follow + readable-snap pose
(model and ``io`` writeback agree exactly), the ``label_clearance_m``
param validation, the zero-delta tree no-op, and the clearance-0 parity
/ clearance>0 label-ejection physics on a synthetic pair. Orbit is
always on: labels are low-mass nodes on a single spring tether, no
behavioral toggles.
"""

from dataclasses import replace
from pathlib import Path

import pytest

from kicad_autorouter.board_model import (
    Footprint,
    SilkLabel,
    apply_deltas,
    board_model,
    label_board_pose,
)
from kicad_autorouter.board_model.labels import (
    label_board_angle,
    label_board_rect,
    snap_label_angle,
    snapped_local_angle,
)
from kicad_autorouter.io import (
    find_footprint,
    nudge_footprint,
    nudge_footprint_by_uuid,
)
from kicad_autorouter.placement import PlacementParams, run_placement
from kicad_autorouter.sexpr import parse_file, to_sexpr

from .helpers import make_board, make_fp

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL_PCB = FIXTURES / "minimal.kicad_pcb"


@pytest.mark.unit
def test_snap_prefers_upright_modes():
    assert snap_label_angle(0.0) == 0.0
    assert snap_label_angle(90.0) == 90.0
    assert snap_label_angle(270.0) == 270.0
    assert snap_label_angle(45.0) == 0.0  # tie breaks toward upright
    assert snap_label_angle(135.0) == 90.0  # tie avoids inverted
    assert snap_label_angle(180.0) == 90.0  # inverted refused: nearest readable


@pytest.mark.unit
def test_snapped_local_angle_round_trip():
    # Footprint rotates 0 -> 90, text filed at local 0 (board 0):
    # board becomes 90, snap keeps 90, local stays 0.
    assert snapped_local_angle(0.0, 0.0, 90.0) == 90.0 - 90.0
    # Footprint rotates 0 -> 180: board 180 refused, snaps to 90,
    # so local counter-rotates to 270.
    assert snapped_local_angle(0.0, 0.0, 180.0) == 270.0


@pytest.mark.unit
def test_label_board_angle_adds_parent():
    assert label_board_angle(0.0, 180.0) == 180.0
    assert label_board_angle(90.0, -90.0) == 0.0


@pytest.mark.unit
def test_label_rect_scales_with_text():
    rect = label_board_rect("AB", 0.0, 0.0, 0.0, (1.0, 1.0))
    xs = [p[0] for p in rect]
    ys = [p[1] for p in rect]
    assert max(xs) - min(xs) == pytest.approx(2 * 0.6 * 1.0)
    assert max(ys) - min(ys) == pytest.approx(1.0)
    halo = label_board_rect("AB", 0.0, 0.0, 0.0, (1.0, 1.0), halo_mm=0.5)
    xsh = [p[0] for p in halo]
    assert max(xsh) - min(xsh) == pytest.approx(2 * 0.6 + 1.0)


@pytest.mark.minimal
def test_minimal_labels_parsed_sided():
    model = board_model(parse_file(MINIMAL_PCB))
    labeled = [fp for fp in model.footprints if fp.label is not None]
    assert len(labeled) == len(model.footprints)
    for fp in labeled:
        assert fp.label.text == fp.ref
        assert fp.label.layer in ("F.SilkS", "F.Fab")  # all minimal parts are F.Cu


@pytest.mark.minimal
def test_zero_delta_tree_nudge_is_noop():
    tree = parse_file(MINIMAL_PCB)
    moved = nudge_footprint(tree, "MH1", 0.0, 0.0, 0.0)
    assert to_sexpr(moved) == to_sexpr(tree)


@pytest.mark.minimal
def test_model_and_tree_agree_on_rotated_label():
    tree = parse_file(MINIMAL_PCB)
    model = board_model(tree)
    fp = next(f for f in model.footprints if f.ref == "MH1")
    uuid = fp.uuid
    assert fp.label is not None

    deltas = {uuid: (1.0, 2.0, 90.0)}
    fp2 = next(f for f in apply_deltas(model, deltas).footprints if f.uuid == uuid)
    reparsed = board_model(nudge_footprint_by_uuid(tree, uuid, 1.0, 2.0, 90.0))
    fp3 = next(f for f in reparsed.footprints if f.uuid == uuid)

    assert fp2.angle_deg == pytest.approx(fp3.angle_deg)
    assert fp2.label.local_angle_deg == pytest.approx(fp3.label.local_angle_deg)
    assert label_board_pose(fp2)[2] in (0.0, 90.0, 270.0)  # readable
    # Pure translation never touches text.
    fp4 = next(
        f
        for f in apply_deltas(model, {uuid: (1.0, 2.0, 0.0)}).footprints
        if f.uuid == uuid
    )
    assert fp4.label.local_angle_deg == fp.label.local_angle_deg


@pytest.mark.params
def test_label_params_validate_and_round_trip():
    p = PlacementParams.from_dict({"label_clearance_m": 0.005})
    assert p.label_clearance_m == 0.005
    assert p.to_dict()["label_clearance_m"] == 0.005
    for bad in ({"label_clearance_m": -0.001}, {"label_clearance_m": 0.06},
                {"label_clearance_m": "5mm"}):
        with pytest.raises(ValueError):
            PlacementParams.from_dict(bad)
    # Partial POST compat: unknown keys dropped, label keys default.
    # Legacy label keys are unknown now (removed toggles) — dropped too.
    q = PlacementParams.from_dict({"unknown_future_key": 1})
    assert q.label_clearance_m == 0.005
    legacy = PlacementParams.from_dict(
        {"label_solidity": 0.5, "label_orbit": True, "label_tether": "slot"})
    assert legacy.label_clearance_m == 0.005


def _labeled_pair(label_w_mm=6.0):
    """Two netted parts side by side with big facing labels.

    Courtyards (hw=1.5) start 4mm apart center-to-center: inside the 6mm
    wall halo so the pair feels contact; the labels sit on the facing
    edges to force label-body overlap only when solidity unions them in.
    """

    def _lab(ref, lx):
        return SilkLabel(
            text="REFDES",
            local_x_mm=lx,
            local_y_mm=0.0,
            local_angle_deg=0.0,
            layer="F.SilkS",
            size_mm=(label_w_mm / (6 * 0.6), 1.0),
            thickness_mm=0.15,
            uuid=f"{ref}-label",
        )

    fa = replace(
        make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"), hw=1.5, hh=1.0),
        label=_lab("A", 2.0),
    )
    fb = replace(
        make_fp("B", "uuid-b", 4.0, 0.0, ("N1", "N3"), hw=1.5, hh=1.0),
        label=_lab("B", -2.0),
    )
    nets = {"N1": (), "N2": (), "N3": ()}
    model = make_board([fa, fb])
    return replace(model, nets=nets)


@pytest.mark.physics
def test_clearance_zero_matches_no_label_parity():
    """Clearance 0 must reproduce the label-unaware run exactly.

    Pad dynamics never feel labels (orbit nodes own all label contact),
    so a labeled board with parked-clear anchors matches the stripped
    board pad-for-pad. Asserts the orbit integration cannot leak into
    the pad kernel.
    """
    base = _labeled_pair()
    stripped = replace(
        base,
        footprints=tuple(replace(fp, label=None) for fp in base.footprints),
    )
    params = PlacementParams(max_iterations=50, label_clearance_m=0.0)
    a = run_placement(base, params)
    b = run_placement(stripped, params)
    assert a.deltas.keys() == b.deltas.keys()
    for uuid in a.deltas:
        assert a.deltas[uuid] == pytest.approx(b.deltas[uuid])


@pytest.mark.physics
def test_clearance_ejects_labels_not_parents():
    """Clearance>0 ejects the facing labels (label_deltas grow) within
    the derived cap, while the short-horizon pad course is
    label-independent — labels find homes nearby via their own nodes
    instead of shoving footprints around. (Long chaotic runs diverge
    pad-for-pad through the shared KE guard, so pad parity is pinned
    on a short deterministic horizon.)"""
    import math

    model = _labeled_pair(label_w_mm=2.0)
    # Long run: real clearance ejects the facing labels within the cap.
    b = run_placement(
        model, PlacementParams(max_iterations=300, label_clearance_m=0.001))
    assert b.label_deltas
    for dx, dy in b.label_deltas.values():
        assert math.hypot(dx, dy) <= 1.0 + 1e-3  # cap derives from clearance
        assert math.hypot(dx, dy) > 0.1  # ejected, not parked at anchor
    # Short horizon: pad course holds station either way.
    a = run_placement(
        model, PlacementParams(max_iterations=10, label_clearance_m=0.0))
    c = run_placement(
        model, PlacementParams(max_iterations=10, label_clearance_m=0.001))
    assert a.deltas.keys() == c.deltas.keys()
    for uuid in a.deltas:
        assert a.deltas[uuid] == pytest.approx(c.deltas[uuid])
