"""In-dynamics orientation consensus (Tidy regime, placement/align.py).

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
    """Five-part row: the neighbor median is stable against one outlier."""
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

    def test_diagonal_consensus_preserved(self):
        # All parts share a 30-degree thread; one outlier joins it.
        fps = _row5(middle_angle=85.0, base_angle=30.0)
        uuids = {fp.uuid for fp in fps}
        model = _model(fps)
        prop = run_placement(model, _align_only(), uuids)
        assert "uuid-c" in prop.deltas
        # 85 -> 30 is a -55 step under 180-degree symmetry.
        assert prop.deltas["uuid-c"][2] == pytest.approx(-55.0, abs=5.0)
        # Consensus members stay put (within deadband, no torque).
        assert prop.deltas.get("uuid-a", (0, 0, 0))[2] == pytest.approx(0.0, abs=1.0)
        assert prop.deltas.get("uuid-e", (0, 0, 0))[2] == pytest.approx(0.0, abs=1.0)

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


@pytest.mark.minimal
class TestAlignMinimalFixture:
    def test_minimal_runs_without_crash(self):
        model = board_model(parse_file(MINIMAL_PCB))
        prop = run_placement(model, _align_only(), None)
        assert isinstance(prop.deltas, dict)
