"""In-dynamics Manhattan snap (Tidy regime, placement/align.py).

Test categories (pytest markers):
- unit: synthetic models, no fixtures
- minimal: Uses minimal.kicad_pcb fixture (4 footprints, no nets)
"""

from dataclasses import replace
from pathlib import Path

import math

import numpy as np
import pytest

from kicad_autorouter.board_model import (
    BoardModel,
    BoardRegion,
    Point,
    board_model,
)
from kicad_autorouter.placement import PlacementParams, run_placement
from kicad_autorouter.placement.align import apply_align_forces
from kicad_autorouter.sexpr import parse_file

from .helpers import make_fp as make_test_fp

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL_PCB = FIXTURES / "minimal.kicad_pcb"


def _model(fps, ghosts=()):
    region = BoardRegion(
        uuid="r0",
        polygon=tuple(Point(x, y) for x, y in [(-60, -60), (60, -60), (60, 60), (-60, 60)]),
        bbox=(-60.0, 60.0, -60.0, 60.0),
    )
    by_ref = {fp.ref: fp for fp in list(fps) + list(ghosts) if fp.ref}
    return BoardModel(
        footprints=tuple(fps),
        ghost_footprints=tuple(ghosts),
        keepout_zones=(),
        nets={},
        by_ref=by_ref,
        board_regions=(region,),
        footprint_region={fp.uuid: 0 for fp in list(fps) + list(ghosts) if fp.uuid},
    )


def _align_only(**kw):
    """Params with every force off except alignment: pure consensus test."""
    base = dict(
        attraction_k_N=0.0,
        repulsion_k_N_m2=0.0,
        wall_k_N=0.0,
        ghost_k_N=0.0,
        contact_tangent_gamma=0.0,
        friction_N=0.0,
        damping_Ns_m=0.12,
        align_angle_k_N=5e-3,
        align_pos_k_N_m=0.0,
        align_deadband_deg=10.0,
        align_radius_m=0.020,
        max_iterations=300,
    )
    base.update(kw)
    return PlacementParams(**base)


def _placed_fp(ref, uuid, x, angle_deg=0.0, nets=("N1", "N2"), **kw):
    """Footprint with pads physically rotated (board map R(-angle)).

    ``replace(fp, angle_deg=...)`` alone leaves pads axis-aligned, which
    reads as 0 degrees to the axis consensus. Rotate the pads for real so
    the committed pose is self-consistent.
    """
    fp = make_test_fp(ref, uuid, x, nets=nets, **kw)
    if abs(angle_deg) > 1e-12:
        rad = math.radians(angle_deg)
        c, s = math.cos(rad), math.sin(rad)
        pads = []
        for p in fp.pads:
            lx = p.position.x_mm - x
            ly = p.position.y_mm - 0.0
            pads.append(replace(
                p, position=Point(x + lx * c + ly * s, -lx * s + ly * c)))
        fp = replace(fp, angle_deg=angle_deg, pads=tuple(pads))
    return fp


def _row(step=10.0, middle_angle=30.0):
    a = _placed_fp("R1", "uuid-a", 0.0)
    b = _placed_fp("R2", "uuid-b", step, middle_angle, nets=("N3", "N4"))
    c = _placed_fp("R3", "uuid-c", 2 * step, nets=("N5", "N6"))
    return a, b, c


def _row5(step=10.0, middle_angle=30.0, base_angle=0.0):
    """Five-part row: mode 0 agreement is stable against one outlier."""
    fps = []
    nets = [("N1", "N2"), ("N3", "N4"), ("N5", "N6"), ("N7", "N8"), ("N9", "N10")]
    for i in range(5):
        ang = middle_angle if i == 2 else base_angle
        fps.append(_placed_fp(f"R{i + 1}", f"uuid-{chr(97 + i)}", i * step, ang, nets=nets[i]))
    return fps


@pytest.mark.unit
class TestAlignConsensus:
    def test_outlier_rotates_toward_neighbors(self):
        fps = _row5(middle_angle=30.0)
        uuids = {fp.uuid for fp in fps}
        model = _model(fps)
        prop = run_placement(model, _align_only(), uuids)
        assert "uuid-c" in prop.deltas
        assert prop.deltas["uuid-c"][2] == pytest.approx(-30.0, abs=3.0)
        # Consensus members stay put (within deadband, no torque).
        assert prop.deltas.get("uuid-a", (0, 0, 0))[2] == pytest.approx(0.0, abs=1.0)
        assert prop.deltas.get("uuid-e", (0, 0, 0))[2] == pytest.approx(0.0, abs=1.0)

    def test_diagonal_thread_far_from_modes_untouched(self):
        # A 30-degree thread has no mode agreement: every member sees
        # neighbors far from both modes, so nobody moves.
        fps = _row5(middle_angle=30.0, base_angle=30.0)
        uuids = {fp.uuid for fp in fps}
        model = _model(fps)
        prop = run_placement(model, _align_only(), uuids)
        assert prop.deltas == {}

    def test_ic_snaps_to_nearest_90(self):
        # Multi-pad part at 105 deg: nearest mode is 90, neighbors at 90
        # agree, so it corrects by -15 (never a 180 flip). Wide
        # courtyards: a 4-pad row overhangs the default 0805 outline.
        kw = dict(nets=("N1", "N2", "N3", "N4"), hw=4.0, hh=2.0)
        fps = [
            _placed_fp("U1", "uuid-a", 0.0, 90.0, **kw),
            _placed_fp("U2", "uuid-b", 10.0, 105.0, **kw),
            _placed_fp("U3", "uuid-c", 20.0, 90.0, **kw),
        ]
        uuids = {fp.uuid for fp in fps}
        model = _model(fps)
        prop = run_placement(model, _align_only(), uuids)
        assert "uuid-b" in prop.deltas
        assert prop.deltas["uuid-b"][2] == pytest.approx(-15.0, abs=3.0)

    def test_near_mode_holds_without_flip(self):
        # Part at 170 deg is within deadband of mode 0: it holds instead
        # of swinging to the opposite side.
        fps = _row5(middle_angle=170.0, base_angle=0.0)
        uuids = {fp.uuid for fp in fps}
        model = _model(fps)
        prop = run_placement(model, _align_only(), uuids)
        assert prop.deltas.get("uuid-c", (0, 0, 0))[2] == pytest.approx(0.0, abs=1.0)

    def test_deadband_leaves_small_deviation_alone(self):
        a, _, c = _row(middle_angle=0.0)
        b = _placed_fp("R2", "uuid-b", 10.0, 5.0, nets=("N3", "N4"))
        model = _model([a, b, c])
        prop = run_placement(model, _align_only(), {"uuid-a", "uuid-b", "uuid-c"})
        assert prop.deltas == {}

    def test_off_is_noop(self):
        model = _model(list(_row(middle_angle=30.0)))
        params = _align_only(align_angle_k_N=0.0, align_pos_k_N_m=0.0)
        prop = run_placement(model, params, {"uuid-a", "uuid-b", "uuid-c"})
        assert prop.deltas == {}

    def test_deterministic(self):
        model = _model(list(_row(middle_angle=17.0)))
        p = _align_only()
        first = run_placement(model, p, {"uuid-a", "uuid-b", "uuid-c"})
        second = run_placement(model, p, {"uuid-a", "uuid-b", "uuid-c"})
        assert first.deltas == second.deltas

    def test_single_pad_gets_no_rotation(self):
        two = make_test_fp("R1", "uuid-a", 0.0, nets=("N1", "N2"))
        one = make_test_fp("R2", "uuid-b", 10.0, nets=("N3",))
        other = make_test_fp("R3", "uuid-c", 20.0, nets=("N4", "N5"))
        model = _model([two, one, other])
        prop = run_placement(model, _align_only(), {"uuid-a", "uuid-b", "uuid-c"})
        assert prop.deltas.get("uuid-b", (0.0, 0.0, 0.0))[2] == 0.0

    def test_scattered_neighborhood_no_mode_agreement_no_torque(self):
        # Parts at +-70 sit far from both modes with no agreeing
        # neighborhood, so nothing moves (the mode-0 member holds too).
        a = _placed_fp("R1", "uuid-a", 0.0, 70.0)
        b = _placed_fp("R2", "uuid-b", 10.0, 0.0, nets=("N3", "N4"))
        c = _placed_fp("R3", "uuid-c", 20.0, -70.0, nets=("N5", "N6"))
        d = _placed_fp("R4", "uuid-d", 30.0, 70.0, nets=("N7", "N8"))
        e = _placed_fp("R5", "uuid-e", 40.0, -70.0, nets=("N9", "N10"))
        model = _model([a, b, c, d, e])
        uuids = {fp.uuid for fp in (a, b, c, d, e)}
        prop = run_placement(model, _align_only(), uuids)
        assert prop.deltas == {}

    def test_frozen_target_survives_neighbor_churn(self):
        # Direct: engage against agreeing neighbors, then scatter them with
        # the previous targets carried over — the held angle must not move.
        from kicad_autorouter.placement.align import (
            build_align_ref_axes,
            compute_align_targets,
        )
        rows = [np.array([0, 1]), np.array([2, 3]), np.array([4, 5])]
        pos = np.array([
            [-1.0, 10.0], [1.0, 10.0],   # left, axis 0 deg
            [9.0, 0.0], [11.0, 0.0],     # middle, axis 0 deg
            [19.0, 10.0], [21.0, 10.0],  # right, axis 0 deg
        ])
        movable = np.array([True, True, True])
        ref_axes = build_align_ref_axes(rows, pos)
        layers = ["F.Cu"] * 3
        kw = dict(radius_mm=60.0, deadband_deg=10.0)
        # Middle rotated 30 deg off mode 0 with agreeing neighbors: engages.
        c30, s30 = math.cos(math.radians(30.0)), math.sin(math.radians(30.0))
        pos_mid = pos.copy()
        pos_mid[2:4] = np.array([[10.0 - c30, -s30], [10.0 + c30, s30]])
        first = compute_align_targets(
            pos_mid, rows, movable, layers, set(), {}, ref_axes, **kw)
        assert first[1][0] == pytest.approx(0.0)
        # Neighbors scatter to +70/-70; carried-over targets must hold 0.
        pos_churn = pos_mid.copy()
        for base, cx, deg in ((0, 0.0, 70.0), (4, 20.0, -70.0)):
            ang = math.radians(deg)
            c, s = math.cos(ang), math.sin(ang)
            for k in (0, 1):
                lx = pos_churn[base + k, 0] - cx
                ly = pos_churn[base + k, 1] - 10.0
                pos_churn[base + k, 0] = cx + lx * c + ly * s
                pos_churn[base + k, 1] = 10.0 - lx * s + ly * c
        second = compute_align_targets(
            pos_churn, rows, movable, layers, set(), {}, ref_axes,
            prev_targets=first, **kw)
        assert second[1][0] == pytest.approx(0.0)
        # Fresh read without history finds no mode agreement.
        fresh = compute_align_targets(
            pos_churn, rows, movable, layers, set(), {}, ref_axes, **kw)
        assert fresh[1][0] is None

    def test_locked_anchor_never_moves_but_votes(self):
        a = make_test_fp("R1", "uuid-a", 0.0, nets=("N1", "N2"))
        b = replace(
            _placed_fp("R2", "uuid-b", 10.0, 30.0, nets=("N3", "N4")),
            locked=True,
        )
        c = make_test_fp("R3", "uuid-c", 20.0, nets=("N5", "N6"))
        model = _model([a, b, c])
        prop = run_placement(
            model, _align_only(), {"uuid-a", "uuid-b", "uuid-c"})
        assert "uuid-b" not in prop.deltas

    def test_align_cause_present_only_when_active(self):
        model = _model(list(_row(middle_angle=30.0)))
        on = run_placement(
            model, _align_only(max_iterations=0), {"uuid-a", "uuid-b", "uuid-c"})
        assert "align" in on.force_components["uuid-b"]
        sx = sum(v[0] for v in on.force_components["uuid-b"].values())
        tx, _ty = on.forces["uuid-b"]
        assert sx == pytest.approx(tx)
        off = run_placement(
            model,
            _align_only(max_iterations=0, align_angle_k_N=0.0),
            {"uuid-a", "uuid-b", "uuid-c"},
        )
        assert "align" not in off.force_components["uuid-b"]

    def test_position_hold_pulls_back_to_anchor(self):
        # Unit-level: displaced centroid feels a restoring net force.
        rows = [np.array([0, 1]), np.array([2, 3])]
        pos = np.array([[-1.0, 0.0], [1.0, 0.0], [9.0, 0.0], [11.0, 0.0]])
        movable = np.array([True, True])
        ref_axes = {0: (0, 1, 0.0), 1: (2, 3, 0.0)}
        targets = {0: (None, 0.0, 0.0), 1: (None, 10.0, 0.0)}
        f = apply_align_forces(
            pos, rows, movable, ref_axes, targets,
            angle_k_N=0.0, pos_k_N_m=1.0,
        )
        # Part 1 centroid sits at its anchor: no hold force.
        assert f[0:2] == pytest.approx(np.zeros((2, 2)))
        # Part 2 centroid is at x=10, anchor x=10: also zero; shift it.
        pos2 = pos.copy()
        pos2[2:4, 0] += 2.0
        f2 = apply_align_forces(
            pos2, rows, movable, ref_axes, targets,
            angle_k_N=0.0, pos_k_N_m=1.0,
        )
        net = f2[2:4].sum(axis=0)
        assert net[0] < 0.0  # pulled back toward -x
        assert net == pytest.approx([-0.002, 0.0])


@pytest.mark.unit
class TestAlignParams:
    def test_defaults_are_off(self):
        p = PlacementParams()
        assert p.align_angle_k_N == 0.0
        assert p.align_pos_k_N_m == 0.0
        assert p.align_deadband_deg == pytest.approx(10.0)
        assert p.align_radius_m == pytest.approx(0.020)

    def test_round_trip(self):
        p = PlacementParams.from_dict({
            "align_angle_k_N": 5e-3, "align_pos_k_N_m": 3.0,
            "align_deadband_deg": 12.0, "align_radius_m": 0.03,
        })
        assert p.align_angle_k_N == pytest.approx(5e-3)
        assert p.to_dict()["align_angle_k_N"] == pytest.approx(5e-3)

    def test_unknown_keys_still_dropped(self):
        p = PlacementParams.from_dict({"nope": 1.0})
        assert p.align_angle_k_N == 0.0

    @pytest.mark.parametrize("bad", [-1e-4, float("nan")])
    def test_bad_stiffness_rejected(self, bad):
        with pytest.raises(ValueError):
            PlacementParams.from_dict({"align_angle_k_N": bad})
        with pytest.raises(ValueError):
            PlacementParams.from_dict({"align_pos_k_N_m": bad})

    @pytest.mark.parametrize("bad", [-1.0, 91.0, float("nan")])
    def test_bad_deadband_rejected(self, bad):
        with pytest.raises(ValueError):
            PlacementParams.from_dict({"align_deadband_deg": bad})

    @pytest.mark.parametrize("bad", [0.0, -0.02])
    def test_bad_radius_rejected(self, bad):
        with pytest.raises(ValueError):
            PlacementParams.from_dict({"align_radius_m": bad})


def _shifted_fp(fp, dx=0.0, dy=0.0):
    """Shift pads for real (centroids come from pads, not x_mm/y_mm)."""
    from dataclasses import replace

    pads = tuple(
        replace(p, position=Point(p.position.x_mm + dx, p.position.y_mm + dy))
        for p in fp.pads
    )
    return replace(fp, x_mm=fp.x_mm + dx, y_mm=fp.y_mm + dy, pads=pads)


def _row4(offset_idx=2, dx=0.0, dy=0.0):
    """Four-part mode-0 row; one member shifted by (dx, dy)."""
    fps = [
        _placed_fp(f"R{i + 1}", f"uuid-{c}", i * 10.0, 0.0,
                   nets=(f"N{2 * i + 1}", f"N{2 * i + 2}"))
        for i, c in enumerate("abcd")
    ]
    if dx or dy:
        fps[offset_idx] = _shifted_fp(fps[offset_idx], dx, dy)
    return fps


def _row_only(**kw):
    """Params with every force off except the position hold (row reader)."""
    base = dict(
        attraction_k_N=0.0,
        wall_k_N=0.0,
        align_angle_k_N=0.0,
        align_pos_k_N_m=0.5,
        align_deadband_deg=10.0,
        align_radius_m=0.020,
        max_iterations=300,
    )
    base.update(kw)
    return _align_only(**base)


@pytest.mark.unit
class TestAlignRows:
    def test_baseline_pulls_outlier_back(self):
        fps = _row4(dy=2.5)
        uuids = {fp.uuid for fp in fps}
        prop = run_placement(_model(fps), _row_only(), uuids)
        assert "uuid-c" in prop.deltas
        assert prop.deltas["uuid-c"][1] == pytest.approx(-1.78, abs=0.5)
        assert prop.deltas["uuid-c"][0] == pytest.approx(0.0, abs=0.2)
        for u in ("uuid-a", "uuid-b", "uuid-d"):
            assert prop.deltas.get(u, (0, 0, 0))[1] == pytest.approx(0.0, abs=0.3)

    def test_pitch_evens_gap(self):
        fps = _row4(offset_idx=3, dx=5.0)
        uuids = {fp.uuid for fp in fps}
        prop = run_placement(_model(fps), _row_only(), uuids)
        assert "uuid-d" in prop.deltas
        assert prop.deltas["uuid-d"][0] < -1.0
        assert prop.deltas["uuid-d"][1] == pytest.approx(0.0, abs=0.3)

    def test_neat_row_untouched(self):
        fps = _row4()
        prop = run_placement(
            _model(fps), _row_only(), {fp.uuid for fp in fps})
        assert prop.deltas == {}

    def test_two_part_band_ignored(self):
        fps = _row4()[:2]
        prop = run_placement(
            _model(fps), _row_only(), {fp.uuid for fp in fps})
        assert prop.deltas == {}

    def test_mixed_mode_band_ignored(self):
        # Middle part reads mode 90: excluded, leaving two — no row.
        fps = _row4(dy=0.0)
        fps[1] = _placed_fp("R2", "uuid-b", 10.0, 90.0, nets=("N3", "N4"))
        fps[2] = _shifted_fp(fps[2], 0.0, 2.5)
        prop = run_placement(
            _model(fps), _row_only(), {fp.uuid for fp in fps})
        assert prop.deltas == {}

    def test_diagonal_band_ignored(self):
        # 30-degree thread: no member sits near a mode, so no row forms
        # (and no angle agreement either).
        fps = [
            _placed_fp(f"R{i + 1}", f"uuid-{c}", i * 10.0, 30.0,
                       nets=(f"N{2 * i + 1}", f"N{2 * i + 2}"))
            for i, c in enumerate("abc")
        ]
        prop = run_placement(
            _model(fps), _row_only(), {fp.uuid for fp in fps})
        assert prop.deltas == {}

    def test_row_hold_needs_leash(self):
        # Row targets ride the leash anchors: pos k off means no-op even
        # with a ragged row (angle-only params).
        fps = _row4(dy=2.5)
        prop = run_placement(
            _model(fps), _align_only(), {fp.uuid for fp in fps})
        assert prop.deltas == {}

    def test_frozen_row_frame_survives_churn(self):
        from kicad_autorouter.placement.align import (
            build_align_ref_axes,
            compute_row_targets,
        )
        rows = [np.array([0, 1]), np.array([2, 3]),
                np.array([4, 5]), np.array([6, 7])]
        pos = np.array([
            [-1.0, 0.0], [1.0, 0.0],
            [9.0, 0.0], [11.0, 0.0],
            [19.0, 2.5], [21.0, 2.5],
            [29.0, 0.0], [31.0, 0.0],
        ])
        movable = np.array([True] * 4)
        ref_axes = build_align_ref_axes(rows, pos)
        layers = ["F.Cu"] * 4
        extents = {i: (1.5, 1.0) for i in range(4)}
        kw = dict(radius_mm=60.0, deadband_deg=10.0)
        first = compute_row_targets(
            pos, rows, movable, layers, set(), {}, ref_axes, extents, **kw)
        assert first[2][0] == pytest.approx(20.0)
        assert first[2][1] == pytest.approx(0.0, abs=0.2)
        # Churn the neighbors +5 in y with history: frozen frame holds.
        churn = pos.copy()
        churn[[0, 1, 2, 3, 6, 7], 1] += 5.0
        second = compute_row_targets(
            churn, rows, movable, layers, set(), {}, ref_axes, extents,
            prev_targets=first, **kw)
        assert second[2] == pytest.approx(first[2])
        # Fresh read follows the churned baseline instead.
        fresh = compute_row_targets(
            churn, rows, movable, layers, set(), {}, ref_axes, extents, **kw)
        assert fresh[2][1] == pytest.approx(5.0, abs=0.5)

    def test_ejected_part_releases_row(self):
        from kicad_autorouter.placement.align import compute_row_targets
        rows = [np.array([0, 1]), np.array([2, 3]), np.array([4, 5])]
        pos = np.array([
            [-1.0, 0.0], [1.0, 0.0],
            [9.0, 2.5], [11.0, 2.5],
            [19.0, 0.0], [21.0, 0.0],
        ])
        movable = np.array([True] * 3)
        layers = ["F.Cu"] * 3
        extents = {0: (1.5, 1.0), 1: (1.5, 1.0), 2: (1.5, 1.0)}
        kw = dict(radius_mm=60.0, deadband_deg=10.0)
        first = compute_row_targets(
            pos, rows, movable, layers, set(), {}, {}, extents, **kw)
        assert 1 in first
        # Ejected clear of the neighborhood: the frozen target drops.
        gone = pos.copy()
        gone[2:4, 0] += 100.0
        second = compute_row_targets(
            gone, rows, movable, layers, set(), {}, {}, extents,
            prev_targets=first, **kw)
        assert 1 not in second


@pytest.mark.minimal
class TestAlignMinimalFixture:
    def test_minimal_runs_without_crash(self):
        model = board_model(parse_file(MINIMAL_PCB))
        prop = run_placement(model, _align_only(), None)
        assert isinstance(prop.deltas, dict)
