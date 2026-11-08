"""Label orbit mode: low-mass label nodes on experimental tethers.

Covers the pure helpers (ink mass, tether spring, radius clamp, slot
scoring, label contact force), param validation, the model/tree
round-trip for orbited anchors, orbit physics on a synthetic facing-label
pair (ejection within the radius cap, tether discrimination, orbit-off
parity), and the netless-board no-op.
"""

import math
from dataclasses import replace
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from kicad_autorouter.board_model import (
    SilkLabel,
    apply_deltas,
    board_model,
    label_board_pose,
)
from kicad_autorouter.board_model.labels import (
    clamp_orbit_radius,
    label_ink_mass_kg,
    label_tether_force_N,
    score_label_slot,
)
from kicad_autorouter.io import nudge_footprint_by_uuid
from kicad_autorouter.placement import PlacementParams, run_placement
from kicad_autorouter.placement.contact import label_contact_force
from kicad_autorouter.sexpr import parse_file

from .helpers import make_board, make_fp

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL_PCB = FIXTURES / "minimal.kicad_pcb"


def _lab(ref: str, lx: float, w_mm: float = 6.0) -> SilkLabel:
    return SilkLabel(
        text="REFDES",
        local_x_mm=lx,
        local_y_mm=0.0,
        local_angle_deg=0.0,
        layer="F.SilkS",
        size_mm=(w_mm / (6 * 0.6), 1.0),
        thickness_mm=0.15,
        uuid=f"{ref}-label",
    )


def _facing_pair(label_w_mm: float = 6.0, gap_mm: float = 4.0):
    fa = replace(
        make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"), hw=1.5, hh=1.0),
        label=_lab("A", 2.0, label_w_mm),
    )
    fb = replace(
        make_fp("B", "uuid-b", gap_mm, 0.0, ("N1", "N3"), hw=1.5, hh=1.0),
        label=_lab("B", -2.0, label_w_mm),
    )
    return replace(make_board([fa, fb]), nets={"N1": (), "N2": (), "N3": ()})


@pytest.mark.unit
def test_ink_mass_scales_and_floors():
    m1 = label_ink_mass_kg("AB", (1.0, 1.0))
    m2 = label_ink_mass_kg("ABCD", (1.0, 1.0))
    assert m2 == pytest.approx(2.0 * m1)
    assert m1 > 0.0
    # Empty/degenerate text still lands on a positive floor (never 0).
    assert label_ink_mass_kg("", (1.0, 1.0)) > 0.0
    assert label_ink_mass_kg("AB", (0.0, 0.0)) == pytest.approx(1e-9)


@pytest.mark.unit
def test_tether_spring_pulls_to_target():
    fx, fy = label_tether_force_N((3.0, 0.0), (0.0, 0.0), 1e-9, hz=2.0)
    assert fx < 0.0 and fy == pytest.approx(0.0)
    # Stiffness derives from mass: doubling mass doubles the force.
    fx2, _ = label_tether_force_N((3.0, 0.0), (0.0, 0.0), 2e-9, hz=2.0)
    assert fx2 == pytest.approx(2.0 * fx)


@pytest.mark.unit
def test_clamp_radius_is_backstop():
    assert clamp_orbit_radius((1.0, 1.0), (0.0, 0.0), 5.0) == (1.0, 1.0)
    cx, cy = clamp_orbit_radius((10.0, 0.0), (0.0, 0.0), 5.0)
    assert (cx, cy) == pytest.approx((5.0, 0.0))
    assert clamp_orbit_radius((3.0, 4.0), (0.0, 0.0), 5.0) == (3.0, 4.0)


@pytest.mark.unit
def test_slot_scoring_prefers_clear_side():
    bodies = {
        "own": Polygon([(-1, -1), (1, -1), (1, 1), (-1, 1)]),
        "east": Polygon([(4, -3), (8, -3), (8, 3), (4, 3)]),
    }
    crowded = score_label_slot(
        (3.5, 0.0), "AB", 0.0, (1.0, 1.0), 0.0, bodies, "own", (0.0, 0.0))
    clear = score_label_slot(
        (-2.5, 0.0), "AB", 0.0, (1.0, 1.0), 0.0, bodies, "own", (0.0, 0.0))
    assert crowded > clear


@pytest.mark.unit
def test_label_contact_pushes_out_of_overlap():
    body = {"b": Polygon([(-1, -1), (1, -1), (1, 1), (-1, 1)])}
    buried = Polygon([(-0.5, -0.5), (0.5, -0.5), (0.5, 0.5), (-0.5, 0.5)])
    fx, fy = label_contact_force(buried, body, halo_mm=6.0, wall_k_N=0.1)
    assert math.hypot(fx, fy) > 0.0
    far = Polygon([(10, 10), (11, 10), (11, 11), (10, 11)])
    assert label_contact_force(far, body, halo_mm=6.0, wall_k_N=0.1) == (0.0, 0.0)
    assert label_contact_force(
        buried, body, halo_mm=6.0, wall_k_N=0.1, solidity=0.0) == (0.0, 0.0)


@pytest.mark.params
def test_orbit_params_validate_and_round_trip():
    p = PlacementParams.from_dict(
        {"label_orbit": True, "label_tether": "slot",
         "label_orbit_radius_m": 0.01, "label_tether_hz": 3.0})
    assert p.label_orbit is True and p.label_tether == "slot"
    assert p.to_dict()["label_orbit_radius_m"] == 0.01
    for bad in ({"label_tether": "elastic"}, {"label_orbit_radius_m": 0.0},
                {"label_orbit_radius_m": -1.0}, {"label_tether_hz": 0.0},
                {"label_orbit": "yes"}):
        with pytest.raises(ValueError):
            PlacementParams.from_dict(bad)


@pytest.mark.minimal
def test_orbit_on_netless_board_is_noop():
    model = board_model(parse_file(MINIMAL_PCB))
    prop = run_placement(
        model, PlacementParams(max_iterations=50, label_orbit=True))
    assert prop.label_deltas == {}
    assert prop.stop_reason == "empty"


@pytest.mark.minimal
def test_orbited_anchor_round_trips_model_tree():
    tree = parse_file(MINIMAL_PCB)
    model = board_model(tree)
    fp = next(f for f in model.footprints if f.ref == "MH1")
    uuid = fp.uuid
    deltas = {uuid: (1.0, 2.0, 90.0)}
    label_deltas = {uuid: (2.5, -1.0)}
    fp2 = next(
        f for f in apply_deltas(model, deltas, label_deltas=label_deltas).footprints
        if f.uuid == uuid)
    assert (fp2.label.local_x_mm, fp2.label.local_y_mm) == pytest.approx(
        (fp.label.local_x_mm + 2.5, fp.label.local_y_mm - 1.0))
    reparsed = board_model(nudge_footprint_by_uuid(
        tree, uuid, 1.0, 2.0, 90.0, label_dx_mm=2.5, label_dy_mm=-1.0))
    fp3 = next(f for f in reparsed.footprints if f.uuid == uuid)
    assert (fp3.label.local_x_mm, fp3.label.local_y_mm) == pytest.approx(
        (fp2.label.local_x_mm, fp2.label.local_y_mm))
    assert fp3.label.local_angle_deg == pytest.approx(fp2.label.local_angle_deg)
    # True board pose agrees too (anchor + snap through both paths).
    assert label_board_pose(fp3) == pytest.approx(label_board_pose(fp2))


@pytest.mark.physics
def test_orbit_ejects_inside_anchor_within_cap():
    model = _facing_pair()
    prop = run_placement(model, PlacementParams(
        max_iterations=300, label_orbit=True, label_tether="spring",
        label_solidity=0.3, wall_halo_m=0.002, label_halo_m=0.0003,
        label_orbit_radius_m=0.005))
    assert set(prop.label_deltas) == {"uuid-a", "uuid-b"}
    for dx, dy in prop.label_deltas.values():
        # Local-frame millimeters: the 0.005 m orbit cap is 5 mm here.
        assert math.hypot(dx, dy) <= 5.0 + 1e-3
        assert math.hypot(dx, dy) > 0.5  # ejected, not parked at anchor


@pytest.mark.physics
def test_free_mode_respects_radius_cap_under_hard_shove():
    model = _facing_pair()
    prop = run_placement(model, PlacementParams(
        max_iterations=120, label_orbit=True, label_tether="free",
        label_solidity=1.0, wall_halo_m=0.006,
        label_orbit_radius_m=0.005))
    assert prop.label_deltas
    for dx, dy in prop.label_deltas.values():
        assert math.hypot(dx, dy) <= 5.0 + 1e-3


@pytest.mark.physics
def test_slot_settles_differently_than_spring():
    model = _facing_pair()
    kw = dict(max_iterations=300, label_orbit=True, label_solidity=0.3,
              wall_halo_m=0.002, label_halo_m=0.0003,
              label_orbit_radius_m=0.005)
    spring = run_placement(model, PlacementParams(**kw, label_tether="spring"))
    slot = run_placement(model, PlacementParams(**kw, label_tether="slot"))
    assert spring.label_deltas != slot.label_deltas


@pytest.mark.physics
def test_orbit_off_parity_with_clear_anchors():
    """Labels parked clear of contact must not perturb pad dynamics.

    Exact pad-delta match on a short deterministic horizon (long chaotic
    runs amplify even 1e-9-level KE-guard perturbations, so parity is
    pinned where it is meaningful), plus a converged single-part orbit
    run that leaves both sidecars empty.
    """
    fa = replace(
        make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"), hw=1.5, hh=1.0),
        label=_lab("A", -8.0, 2.0))
    fb = replace(
        make_fp("B", "uuid-b", 12.0, 0.0, ("N1", "N3"), hw=1.5, hh=1.0),
        label=_lab("B", 8.0, 2.0))
    model = replace(make_board([fa, fb]), nets={"N1": (), "N2": (), "N3": ()})
    kw = dict(label_solidity=0.3, wall_halo_m=0.002, label_halo_m=0.0003)
    base = run_placement(model, PlacementParams(
        **kw, label_orbit=False, max_iterations=10))
    orb = run_placement(model, PlacementParams(
        **kw, label_orbit=True, label_tether="spring", max_iterations=10))
    assert orb.label_deltas == {}
    assert orb.deltas.keys() == base.deltas.keys()
    for uuid in base.deltas:
        assert orb.deltas[uuid] == pytest.approx(base.deltas[uuid])

    lone = replace(make_board([fa]), nets={"N1": (), "N2": ()})
    calm = run_placement(lone, PlacementParams(
        **kw, label_orbit=True, label_tether="spring",
        max_iterations=300))
    assert calm.deltas == {} and calm.label_deltas == {}
