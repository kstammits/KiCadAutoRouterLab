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
from shapely.geometry import Point as ShapelyPoint

from kicad_autorouter.board_model import (
    BoardModel,
    BoardRegion,
    Point,
    SilkLabel,
    apply_deltas,
    board_model,
)
from kicad_autorouter.board_model.labels import (
    clamp_orbit_radius,
    label_board_angle,
    label_board_rect,
    label_halo_mm,
    label_ink_mass_kg,
    snap_label_angle,
)
from kicad_autorouter.placement import PlacementParams, run_label_settle, run_placement
from kicad_autorouter.placement.contact import label_contact_force
from kicad_autorouter.sexpr import parse_file

from .helpers import (
    facing_label_pair as _facing_pair,
    make_board,
    make_fp,
    make_tht_pad,
)

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL_PCB = FIXTURES / "minimal.kicad_pcb"


def _lab(ref: str, lx: float, w_mm: float = 6.0) -> SilkLabel:
    """Narrow-label variant for single-label orbit scenarios."""
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
def test_slack_tether_floats_inside_slack():
    from kicad_autorouter.board_model.labels import (
        label_slack_tether_force_N,
    )
    # Inside the slack radius: exactly zero (free float).
    assert label_slack_tether_force_N(
        (1.0, 0.0), (0.0, 0.0), 1e-9, hz=2.0, slack_m=0.002) == (0.0, 0.0)
    # At the boundary: still zero (continuous).
    assert label_slack_tether_force_N(
        (2.0, 0.0), (0.0, 0.0), 1e-9, hz=2.0, slack_m=0.002) == (0.0, 0.0)
    # Outside: mass-proportional spring on the excess only.
    k = 1e-9 * (2.0 * math.pi * 2.0) ** 2  # N/m
    fx, fy = label_slack_tether_force_N(
        (4.0, 0.0), (0.0, 0.0), 1e-9, hz=2.0, slack_m=0.002)
    assert fx == pytest.approx(-k * 2e-3)
    assert fy == pytest.approx(0.0)
    # Stiffness derives from mass: doubling mass doubles the force.
    fx2, _ = label_slack_tether_force_N(
        (4.0, 0.0), (0.0, 0.0), 2e-9, hz=2.0, slack_m=0.002)
    assert fx2 == pytest.approx(2.0 * fx)
    # Zero slack degrades to a plain spring on the full offset.
    fx0, _ = label_slack_tether_force_N(
        (4.0, 0.0), (0.0, 0.0), 1e-9, hz=2.0, slack_m=0.0)
    assert fx0 == pytest.approx(-k * 4e-3)


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


@pytest.mark.minimal
def test_orbit_on_netless_board_is_noop():
    model = board_model(parse_file(MINIMAL_PCB))
    prop = run_placement(model, PlacementParams(max_iterations=50))
    assert prop.label_deltas == {}
    assert prop.stop_reason == "empty"


@pytest.mark.physics
def test_orbit_ejects_inside_radius_cap():
    model = _facing_pair()
    clearance_m = 0.005
    prop = run_placement(model, PlacementParams(
        max_iterations=300, label_clearance_m=clearance_m,
        wall_halo_m=0.002))
    assert set(prop.label_deltas) == {"uuid-a", "uuid-b"}
    # Rest spot within the orbit disc of the parent (centroid home):
    # board distance, not filed-local delta.
    orbit_mm = PlacementParams().label_orbit_m * 1000.0
    moved = apply_deltas(model, prop.deltas, label_deltas=prop.label_deltas)
    from kicad_autorouter.board_model import _to_board as _tb
    for fp in moved.footprints:
        if fp.label is None:
            continue
        b = _tb(fp.label.local_x_mm, fp.label.local_y_mm,
                fp.x_mm, fp.y_mm, fp.angle_deg, fp.layer)
        assert math.hypot(b.x_mm - fp.x_mm, b.y_mm - fp.y_mm) <= orbit_mm + 1e-3


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
    orbit_mm = PlacementParams().label_orbit_m * 1000.0
    fa = replace(
        make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"), hw=1.5, hh=1.0),
        label=_lab("A", -8.0, 2.0))
    fb = replace(
        make_fp("B", "uuid-b", 12.0, 0.0, ("N1", "N3"), hw=1.5, hh=1.0),
        label=_lab("B", 8.0, 2.0))
    model = replace(make_board([fa, fb]), nets={"N1": (), "N2": (), "N3": ()})
    # Filed ±8 mm offsets are stale: labels converge near their parents
    # (centroid home + slack float), never linger at filed anchors.
    # (Pad-center Coulomb is long-range, so the short-horizon pad course
    # below is where exact stillness is pinned — not the labels.)
    kw = dict(label_clearance_m=0.002, wall_halo_m=0.002)
    short = run_placement(model, PlacementParams(**kw, max_iterations=300))
    moved = apply_deltas(model, short.deltas, label_deltas=short.label_deltas)
    from kicad_autorouter.board_model import _to_board as _tb
    for fp in moved.footprints:
        if fp.label is None:
            continue
        b = _tb(fp.label.local_x_mm, fp.label.local_y_mm,
                fp.x_mm, fp.y_mm, fp.angle_deg, fp.layer)
        assert math.hypot(b.x_mm - fp.x_mm, b.y_mm - fp.y_mm) <= orbit_mm + 1e-3
    stripped = replace(
        model,
        footprints=tuple(replace(fp, label=None) for fp in model.footprints),
    )
    base = run_placement(stripped, PlacementParams(**kw, max_iterations=10))
    alike = run_placement(model, PlacementParams(**kw, max_iterations=10))
    assert alike.deltas.keys() == base.deltas.keys()
    for uuid in base.deltas:
        assert alike.deltas[uuid] == pytest.approx(base.deltas[uuid])

    lone = replace(make_board([fa]), nets={"N1": (), "N2": ()})
    calm = run_placement(lone, PlacementParams(**kw, max_iterations=300))
    assert calm.deltas == {}
    # Centroid home + slack float: the stale anchor is abandoned for
    # the parent's neighborhood (dynamics rest at the deadband edge;
    # the post-loop eject may then seat it off own copper, still
    # inside the orbit disc).
    moved = apply_deltas(lone, calm.deltas, label_deltas=calm.label_deltas)
    fp = next(f for f in moved.footprints if f.uuid == "uuid-a")
    from kicad_autorouter.board_model import _to_board as _tb2
    b = _tb2(fp.label.local_x_mm, fp.label.local_y_mm,
             fp.x_mm, fp.y_mm, fp.angle_deg, fp.layer)
    assert math.hypot(b.x_mm - fp.x_mm, b.y_mm - fp.y_mm) <= orbit_mm + 1e-3


def _region_board(fps, edge=20.0):
    """Board model with a real region polygon (board wall active)."""
    cuts = (
        (Point(-edge, -edge), Point(edge, -edge)),
        (Point(edge, -edge), Point(edge, edge)),
        (Point(edge, edge), Point(-edge, edge)),
        (Point(-edge, edge), Point(-edge, -edge)),
    )
    regions = (
        BoardRegion(
            uuid="region-0",
            polygon=(Point(-edge, -edge), Point(edge, -edge),
                     Point(edge, edge), Point(-edge, edge),
                     Point(-edge, -edge)),
            bbox=(-edge, edge, -edge, edge),
        ),
    )
    return BoardModel(
        footprints=tuple(fps),
        keepout_zones=(),
        nets={},
        by_ref={fp.ref: fp for fp in fps},
        edge_cuts=cuts,
        footprint_region={fp.uuid: 0 for fp in fps},
        board_regions=regions,
    )


@pytest.mark.unit
def test_label_halo_is_font_fraction():
    assert label_halo_mm((1.0, 2.0)) == pytest.approx(2.0)
    assert label_halo_mm((0.5, 0.5)) == pytest.approx(0.5)
    assert label_halo_mm((0.0, 0.0)) == 0.0


def _bare_label_poly(model, uuid):
    """Final bare-text rect polygon of a footprint's label after deltas."""
    from kicad_autorouter.board_model import _to_board
    fp = next(f for f in model.footprints if f.uuid == uuid)
    assert fp.label is not None
    b = _to_board(fp.label.local_x_mm, fp.label.local_y_mm,
                  fp.x_mm, fp.y_mm, fp.angle_deg, fp.layer)
    angle = snap_label_angle(label_board_angle(
        fp.label.local_angle_deg, fp.angle_deg))
    return Polygon(label_board_rect(
        fp.label.text, b.x_mm, b.y_mm, angle, fp.label.size_mm,
        halo_mm=0.0))


@pytest.mark.physics
def test_stale_filed_offset_does_not_hold_label_out():
    """Labels orbit their parent part, not their filed offset.

    A label filed 8mm from its parent (stale after clustering) is
    pulled home to the live centroid and floats inside the slack
    deadband — it never lingers at the filed anchor.
    """
    from kicad_autorouter.board_model import _to_board as _tb
    fa = replace(
        make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"), hw=1.5, hh=1.0),
        label=_lab("A", 8.0),
    )
    model = replace(_region_board([fa]), nets={"N1": (), "N2": ()})
    prop = run_placement(model, PlacementParams(max_iterations=300))
    moved = apply_deltas(model, prop.deltas, label_deltas=prop.label_deltas)
    fp = next(f for f in moved.footprints if f.uuid == "uuid-a")
    b = _tb(fp.label.local_x_mm, fp.label.local_y_mm,
            fp.x_mm, fp.y_mm, fp.angle_deg, fp.layer)
    # Slack deadband edge for the dynamics; the post-loop eject may
    # then seat it off own copper — still inside the orbit disc, never
    # lingering at the stale filed anchor.
    orbit_mm = PlacementParams().label_orbit_m * 1000.0
    assert math.hypot(b.x_mm - fp.x_mm, b.y_mm - fp.y_mm) <= orbit_mm + 1e-3


@pytest.mark.physics
def test_label_pushed_inside_board_edge():
    """Labels cannot end past EdgeCuts (inverted courtyard, parts parity)."""
    fa = replace(
        make_fp("A", "uuid-a", 17.0, 0.0, ("N1", "N2"), hw=1.5, hh=1.0),
        label=_lab("A", 2.5),
    )
    model = replace(_region_board([fa]), nets={"N1": (), "N2": ()})
    # Mass-scaled walls approach gently: allow the settle horizon.
    prop = run_placement(model, PlacementParams(max_iterations=1000))
    assert prop.stop_reason in ("converged", "stalled")
    assert "uuid-a" in prop.label_deltas
    moved = apply_deltas(model, prop.deltas, label_deltas=prop.label_deltas)
    poly = _bare_label_poly(moved, "uuid-a")
    region = Polygon([(p.x_mm, p.y_mm) for p in model.board_regions[0].polygon])
    assert region.covers(poly)


@pytest.mark.physics
def test_label_ejected_from_tht_pin_disc():
    """Labels cannot sit on (own or neighbor) THT pad copper: pin discs."""
    from kicad_autorouter.board_model import Pad as _Pad
    pin = replace(make_tht_pad("1", "N1"), position=Point(4.0, 0.0))
    far = replace(make_tht_pad("2", "N2"), position=Point(-6.0, 0.0))
    fa = replace(
        replace(make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"), hw=1.5, hh=1.0),
                pads=(pin, far)),
        label=_lab("A", 4.0, 2.0),
    )
    model = replace(_region_board([fa]), nets={"N1": (), "N2": ()})
    prop = run_placement(model, PlacementParams(max_iterations=300))
    assert "uuid-a" in prop.label_deltas
    # Parent feels nothing (uncharged single pad, no neighbor): static.
    assert prop.deltas == {}
    moved = apply_deltas(model, prop.deltas, label_deltas=prop.label_deltas)
    poly = _bare_label_poly(moved, "uuid-a")
    disc = ShapelyPoint(4.0, 0.0).buffer(0.5 * math.hypot(2.0, 2.0) + 0.5)
    assert poly.disjoint(disc)


@pytest.mark.physics
def test_label_pushed_off_neighbor_smd_copper():
    """Coulomb alone clears neighbor SMD sits at settle (no wall bodies).

    B's label starts at its parent centroid overlapping A's SMD pad,
    with A's courtyard deliberately clear of the label. Wall bodies
    were removed as redundant (measured null); this pins the Coulomb
    repulsion as the clearing force. Bodies frozen, so the endpoint
    isolates label force.
    """
    from kicad_autorouter.placement.contact import pad_copper_polygon
    from shapely.ops import unary_union as _union

    fa = make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"), hw=0.6, hh=1.0)
    fb = replace(
        make_fp("B", "uuid-b", 4.0, 0.0, ("N3", "N4"), hw=1.5, hh=1.0),
        label=_lab("B", 0.0),
    )
    model = replace(
        _region_board([fa, fb]),
        nets={"N1": (), "N2": (), "N3": (), "N4": ()},
    )
    prop = run_label_settle(model, PlacementParams(), 300)
    assert "uuid-b" in prop.label_deltas
    # Coulomb 1/d^2 diverges on overlap: genuine ejection, not a nudge.
    assert prop.label_deltas["uuid-b"][0] > 0.3
    moved = apply_deltas(model, prop.deltas, label_deltas=prop.label_deltas)
    poly = _bare_label_poly(moved, "uuid-b")
    copper = _union([pad_copper_polygon(p)
                     for p in fa.pads])
    assert poly.disjoint(copper)


@pytest.mark.physics
def test_label_feels_smd_pad_coulomb():
    """SMD pads repel labels at range (parts' Coulomb, one-way)."""
    fa = replace(
        make_fp("A", "uuid-a", 0.0, 0.0, ("N8", "N8"), hw=1.5, hh=1.0),
        label=_lab("A", 8.0, 2.0),
    )
    fb = make_fp("B", "uuid-b", 14.0, 0.0, ("N9", "N9"), hw=1.5, hh=1.0)
    model = replace(_region_board([fa, fb]), nets={"N8": (), "N9": ()})
    prop = run_placement(model, PlacementParams(max_iterations=100))
    # One-way: B's part trajectory matches the stripped run exactly.
    stripped = replace(
        model,
        footprints=tuple(replace(fp, label=None) for fp in model.footprints),
    )
    base = run_placement(stripped, PlacementParams(max_iterations=100))
    assert prop.deltas.keys() == base.deltas.keys()
    for uuid in base.deltas:
        assert prop.deltas[uuid] == pytest.approx(base.deltas[uuid])
    dx, dy = prop.label_deltas["uuid-a"]
    # B's charged pads sit +x of the label: repulsion pushes it back.
    assert dx < -0.1
    # …and the label ends near its parent (centroid home + slack),
    # not out at the stale filed offset.
    moved = apply_deltas(model, prop.deltas, label_deltas=prop.label_deltas)
    fp = next(f for f in moved.footprints if f.uuid == "uuid-a")
    from kicad_autorouter.board_model import _to_board as _tb
    b = _tb(fp.label.local_x_mm, fp.label.local_y_mm,
            fp.x_mm, fp.y_mm, fp.angle_deg, fp.layer)
    orbit_mm = PlacementParams().label_orbit_m * 1000.0
    assert math.hypot(b.x_mm - fp.x_mm, b.y_mm - fp.y_mm) <= orbit_mm + 1e-3


@pytest.mark.physics
def test_overlapping_labels_leave_part_trajectories_untouched():
    """One-way invariant under overlap: parts match the stripped run."""
    fa = replace(
        make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"), hw=1.5, hh=1.0),
        label=_lab("A", 4.5),
    )
    fb = replace(
        make_fp("B", "uuid-b", 8.0, 0.0, ("N1", "N3"), hw=1.5, hh=1.0),
        label=_lab("B", -4.5),
    )
    model = replace(make_board([fa, fb]), nets={"N1": (), "N2": (), "N3": ()})
    kw = dict(max_iterations=10)
    lab = run_placement(model, PlacementParams(**kw))
    stripped = replace(
        model,
        footprints=tuple(replace(fp, label=None) for fp in model.footprints),
    )
    base = run_placement(stripped, PlacementParams(**kw))
    assert lab.deltas.keys() == base.deltas.keys()
    for uuid in base.deltas:
        assert lab.deltas[uuid] == pytest.approx(base.deltas[uuid])


@pytest.mark.physics
def test_label_settle_clears_overlap_with_frozen_bodies():
    """Frozen-body settle: labels de-overlap, footprints never move."""
    model = _facing_pair()
    prop = run_label_settle(model, PlacementParams(), 300)
    assert prop.deltas == {}
    assert set(prop.label_deltas) == {"uuid-a", "uuid-b"}
    assert prop.stop_reason == "converged"
    moved = apply_deltas(model, prop.deltas, label_deltas=prop.label_deltas)
    ra = _bare_label_poly(moved, "uuid-a")
    rb = _bare_label_poly(moved, "uuid-b")
    assert ra.disjoint(rb)


@pytest.mark.unit
class TestLabelSamples:
    """Stride-sampled label trajectory: double-gated telemetry only.

    No geometry in the loop — raw node + parent-carried anchor points
    per orbiting label for offline overlap/rim analysis. Populated
    only when debug AND record_frames are both on; either flag alone
    (or neither) yields an empty list.
    """

    def test_samples_recorded_when_debug_and_frames(self):
        model = _facing_pair()
        prop = run_placement(
            model,
            PlacementParams(max_iterations=10, debug=True),
            record_frames=True, frame_stride=2,
        )
        samples = prop.debug_trace.get("label_samples", [])
        assert len(samples) >= 2
        iters = [s["iteration"] for s in samples]
        assert iters[0] == 0
        assert iters == sorted(iters)
        assert iters[-1] == prop.iterations
        for s in samples:
            assert set(s["labels"]) == {"uuid-a", "uuid-b"}
            for vals in s["labels"].values():
                assert len(vals) == 5
                assert all(math.isfinite(v) for v in vals)
                nx, ny, ax, ay, _pa = vals
                # Orbit clamp is the backstop: node never leaves the disc.
                orbit_mm = PlacementParams().label_orbit_m * 1000.0
                assert math.hypot(nx - ax, ny - ay) <= orbit_mm + 1e-3

    def test_samples_need_both_debug_and_frames(self):
        frames_only = run_placement(
            _facing_pair(),
            PlacementParams(max_iterations=10),
            record_frames=True, frame_stride=2,
        )
        assert frames_only.debug_trace.get("label_samples", []) == []
        debug_only = run_placement(
            _facing_pair(),
            PlacementParams(max_iterations=10, debug=True),
        )
        assert debug_only.debug_trace.get("label_samples", []) == []

