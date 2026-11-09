"""Label orbit (always on): low-mass label nodes on a single spring tether.

Covers the pure helpers (ink mass, tether spring, radius clamp, label
contact force), param validation, the model/tree round-trip for orbited
anchors, orbit physics on a synthetic facing-label pair (ejection within
the derived radius cap), and the netless-board no-op. No behavioral
toggles: orbit is always on, one spring strategy, upright snap only.
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
    p = PlacementParams.from_dict({"label_clearance_m": 0.01})
    assert p.label_clearance_m == 0.01
    assert p.to_dict()["label_clearance_m"] == 0.01
    for bad in ({"label_clearance_m": -1.0}, {"label_clearance_m": 0.06},
                {"label_clearance_m": "wide"}):
        with pytest.raises(ValueError):
            PlacementParams.from_dict(bad)


@pytest.mark.minimal
def test_orbit_on_netless_board_is_noop():
    model = board_model(parse_file(MINIMAL_PCB))
    prop = run_placement(model, PlacementParams(max_iterations=50))
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
def test_orbit_ejects_inside_radius_cap():
    model = _facing_pair()
    clearance_m = 0.005
    prop = run_placement(model, PlacementParams(
        max_iterations=300, label_clearance_m=clearance_m,
        wall_halo_m=0.002))
    assert set(prop.label_deltas) == {"uuid-a", "uuid-b"}
    for dx, dy in prop.label_deltas.values():
        # Local-frame millimeters: the orbit cap derives from clearance.
        assert math.hypot(dx, dy) <= clearance_m * 1000.0 + 1e-3
        assert math.hypot(dx, dy) > 0.5  # ejected, not parked at anchor


@pytest.mark.physics
def test_zero_clearance_leaves_labels_at_anchor():
    # Lone labeled part: no pair forces, nothing moves, the spring holds
    # the label exactly at its filed anchor.
    fa = replace(
        make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"), hw=1.5, hh=1.0),
        label=_lab("A", 2.0, 2.0))
    model = replace(make_board([fa]), nets={"N1": (), "N2": ()})
    prop = run_placement(model, PlacementParams(
        max_iterations=300, label_clearance_m=0.0, wall_halo_m=0.002))
    assert prop.label_deltas == {}
    assert prop.deltas == {}


@pytest.mark.physics
def test_clear_labels_do_not_perturb_pads():
    """Labels parked clear of contact must not perturb pad dynamics.

    Exact pad-delta match on a short deterministic horizon (long chaotic
    runs amplify even 1e-9-level KE-guard perturbations, so parity is
    pinned where it is meaningful), plus a converged single-part run
    that leaves the sidecar empty.
    """
    fa = replace(
        make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"), hw=1.5, hh=1.0),
        label=_lab("A", -8.0, 2.0))
    fb = replace(
        make_fp("B", "uuid-b", 12.0, 0.0, ("N1", "N3"), hw=1.5, hh=1.0),
        label=_lab("B", 8.0, 2.0))
    model = replace(make_board([fa, fb]), nets={"N1": (), "N2": (), "N3": ()})
    # Clearance 2 mm: anchors parked ±8 mm are truly clear of contact
    # (text edge ~7 mm out, inflated reach ~2 mm, courtyard edge 1.5 mm).
    kw = dict(label_clearance_m=0.002, wall_halo_m=0.002)
    short = run_placement(model, PlacementParams(**kw, max_iterations=10))
    assert short.label_deltas == {}
    stripped = replace(
        model,
        footprints=tuple(replace(fp, label=None) for fp in model.footprints),
    )
    base = run_placement(stripped, PlacementParams(**kw, max_iterations=10))
    assert short.deltas.keys() == base.deltas.keys()
    for uuid in base.deltas:
        assert short.deltas[uuid] == pytest.approx(base.deltas[uuid])

    lone = replace(make_board([fa]), nets={"N1": (), "N2": ()})
    calm = run_placement(lone, PlacementParams(**kw, max_iterations=300))
    assert calm.deltas == {} and calm.label_deltas == {}
