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
    label_halo_mm,
    label_ink_mass_kg,
)
from kicad_autorouter.placement import PlacementParams, run_label_settle, run_placement
from kicad_autorouter.placement.contact import label_contact_force
from kicad_autorouter.sexpr import parse_file

from .helpers import (
    assert_label_near_parent as _near_parent,
    facing_label_pair as _facing_pair,
    label_cap_radii_mm as _cap,
    label_poly_of,
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
def test_clamp_ellipse_hugs_stick_flanks():
    from kicad_autorouter.board_model.labels import (
        label_disc_radii_mm,
        label_halo_mm,
        label_text_size_mm,
        point_in_label_ellipse,
    )
    # J7-shaped stick: 3.6 wide, ~21 long. The old circular floor sized
    # by the longest reach (~10.8 + text) parked labels ~13mm out; the
    # ellipse floors each axis separately.
    ex, ey = label_disc_radii_mm("J7", (1.0, 1.0), 1.8, 10.7, 3.5)
    w, h = label_text_size_mm("J7", (1.0, 1.0))
    halo = label_halo_mm((1.0, 1.0))
    text_r = 0.5 * math.hypot(w + 2.0 * halo, h + 2.0 * halo)
    assert ex == pytest.approx(1.8 + text_r + 0.5)
    assert ey == pytest.approx(10.7 + text_r + 0.5)
    assert ey > 2.0 * ex
    # Inside stays put, outside lands on the rim along its own ray.
    assert clamp_orbit_radius(
        (1.0, 1.0), (0.0, 0.0), (ex, ey)) == (1.0, 1.0)
    cx, cy = clamp_orbit_radius((13.5, 0.0), (0.0, 0.0), (ex, ey))
    assert (cx, cy) == pytest.approx((ex, 0.0))
    cx, cy = clamp_orbit_radius((0.0, 20.0), (0.0, 0.0), (ex, ey))
    assert (cx, cy) == pytest.approx((0.0, ey))
    # Rotated parent: the ellipse turns with the part — board +x is the
    # parent's local +y at 90 degrees, so (20, 0) lands on the long rim.
    cx, cy = clamp_orbit_radius((20.0, 0.0), (0.0, 0.0), (ex, ey), 90.0)
    assert point_in_label_ellipse(cx, cy, (0.0, 0.0), (ex, ey), 90.0)
    assert math.hypot(cx, cy) == pytest.approx(ey)
    # Scalar still means a disc, angle ignored.
    assert clamp_orbit_radius(
        (10.0, 0.0), (0.0, 0.0), 5.0, 37.0) == pytest.approx((5.0, 0.0))
    # Malformed bounds never raise and never move the node.
    assert clamp_orbit_radius((9.0, 9.0), (0.0, 0.0), ("x", "y")) == (9.0, 9.0)


@pytest.mark.physics
def test_settle_repeats_until_labels_stop_moving():
    # One loop rarely finishes: the end-of-loop de-penetration relocates
    # wedged labels, disturbing the equilibrium just found. Passes repeat
    # from the new state until a pass moves nothing (writeback-quiet) —
    # which must equal running single passes back-to-back by hand, since
    # frozen bodies share one frame and offsets add.
    model = _facing_pair()
    params = PlacementParams(max_iterations=300)
    auto = run_label_settle(model, params, 300, max_passes=3)
    once = run_label_settle(model, params, 300, max_passes=1)
    assert auto.iterations >= once.iterations
    if auto.iterations > once.iterations:
        moved_once = apply_deltas(model, once.deltas, label_deltas=once.label_deltas)
        twice = run_label_settle(moved_once, params, 300, max_passes=1)
        for uuid, (lx, ly) in auto.label_deltas.items():
            ox, oy = once.label_deltas.get(uuid, (0.0, 0.0))
            tx, ty = twice.label_deltas.get(uuid, (0.0, 0.0))
            assert (lx, ly) == pytest.approx((ox + tx, oy + ty))
    # A settled board settles clean: one pass moves nothing further.
    moved_auto = apply_deltas(model, auto.deltas, label_deltas=auto.label_deltas)
    again = run_label_settle(moved_auto, params, 300, max_passes=3)
    assert again.iterations == once.iterations or not again.label_deltas


@pytest.mark.physics
def test_stick_label_rests_on_flank_not_past_end():
    from .helpers import make_board, make_fp
    # Stick body (courtyard 2 x 16) with the label filed far out on the
    # flank side: the spring fetches it home and the ellipse clamp stops
    # it beside the part — never flung past the stick's ends the way the
    # old longest-reach disc allowed.
    fp = replace(
        make_fp("J7", "uuid-stick", 0.0, 0.0, ("N1", "N2"),
                hw=1.0, hh=8.0, pad_dx=1.0),
        label=_lab("J7", 15.0),
    )
    model = replace(make_board([fp]), nets={"N1": (), "N2": ()})
    prop = run_label_settle(model, PlacementParams(max_iterations=300), 300)
    moved = apply_deltas(model, prop.deltas, label_deltas=prop.label_deltas)
    d = _near_parent(moved, "uuid-stick", PlacementParams().label_orbit_m * 1000.0)
    assert d < 7.0


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
    # Rest spot next to the parent: the spring pulls every label to its
    # own part's center and the copper shoves hold it out, so it settles
    # against the parent's edge — never at a far filed offset.
    orbit_mm = PlacementParams().label_orbit_m * 1000.0
    moved = apply_deltas(model, prop.deltas, label_deltas=prop.label_deltas)
    for fp in moved.footprints:
        if fp.label is None:
            continue
        _near_parent(moved, fp.uuid, orbit_mm)


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
    # Filed ±8 mm out, the spring fetches both labels home while their
    # parents move under net pull — nothing lingers at the filed offset.
    # (Pad-center Coulomb is long-range, so the short-horizon pad course
    # below is where exact stillness is pinned — not the labels.)
    kw = dict(label_clearance_m=0.002, wall_halo_m=0.002)
    short = run_placement(model, PlacementParams(**kw, max_iterations=300))
    # Parents genuinely moved — otherwise follow-during-motion is vacuous.
    assert short.deltas
    moved = apply_deltas(model, short.deltas,
                         label_deltas=short.label_deltas)
    assert set(short.label_deltas) == {"uuid-a", "uuid-b"}
    for fp in moved.footprints:
        if fp.label is None:
            continue
        _near_parent(moved, fp.uuid, orbit_mm)
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
    # The lone label is fetched to its parent: it moves (nonempty sidecar)
    # and ends next to the part instead of out at the filed offset.
    assert set(calm.label_deltas) == {"uuid-a"}
    moved = apply_deltas(lone, calm.deltas, label_deltas=calm.label_deltas)
    d = _near_parent(moved, "uuid-a", orbit_mm)
    assert d < 8.0 - 1.0


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


@pytest.mark.physics
def test_far_label_comes_home_to_parent():
    """Labels are fetched to their parents, not parked at filed offsets.

    A label filed 8 mm from its parent in empty space feels the spring
    over its full length (small dead zone) and ends next to the part —
    the filed spot only seeds the start. This is the user-visible pin
    for far labels coming in during Legalize.
    """
    from kicad_autorouter.board_model import _to_board as _tb
    fa = replace(
        make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"), hw=1.5, hh=1.0),
        label=_lab("A", 8.0),
    )
    model = replace(_region_board([fa]), nets={"N1": (), "N2": ()})
    prop = run_placement(model, PlacementParams(max_iterations=300))
    assert set(prop.label_deltas) == {"uuid-a"}
    moved = apply_deltas(model, prop.deltas, label_deltas=prop.label_deltas)
    fp = next(f for f in moved.footprints if f.uuid == "uuid-a")
    b = _tb(fp.label.local_x_mm, fp.label.local_y_mm,
            fp.x_mm, fp.y_mm, fp.angle_deg, fp.layer)
    # Came in from 8 mm out to next to the part.
    assert math.hypot(b.x_mm - fp.x_mm, b.y_mm - fp.y_mm) < 8.0 - 1.0


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
    poly = label_poly_of(moved, "uuid-a")
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
    poly = label_poly_of(moved, "uuid-a")
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
    poly = label_poly_of(moved, "uuid-b")
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
    # …and the label still ends next to its own parent — the shove
    # deflects it, the spring keeps it from wandering off.
    moved = apply_deltas(model, prop.deltas, label_deltas=prop.label_deltas)
    _near_parent(moved, "uuid-a",
                 PlacementParams().label_orbit_m * 1000.0)


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
    ra = label_poly_of(moved, "uuid-a")
    rb = label_poly_of(moved, "uuid-b")
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
        orbit_mm = PlacementParams().label_orbit_m * 1000.0
        caps = {fp.uuid: _cap(fp, orbit_mm) for fp in model.footprints
                if fp.label is not None}
        for s in samples:
            assert set(s["labels"]) == {"uuid-a", "uuid-b"}
            for uuid, vals in s["labels"].items():
                assert len(vals) == 5
                assert all(math.isfinite(v) for v in vals)
                nx, ny, ax, ay, pa = vals
                # The clamp is the backstop: node never leaves its own
                # ellipse (per-parent sized, so sticks hug the flanks).
                from kicad_autorouter.board_model.labels import (
                    point_in_label_ellipse as _in_ell,
                )
                assert _in_ell(nx, ny, (ax, ay), caps[uuid], pa)

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

