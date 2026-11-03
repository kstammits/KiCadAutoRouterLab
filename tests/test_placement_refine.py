"""Tests for the Tidy refine pass (placement/refine.py).

Test categories (pytest markers):
- unit: synthetic models, no fixtures
- minimal: Uses minimal.kicad_pcb fixture (4 footprints, no nets)
"""

from dataclasses import replace
from pathlib import Path

import pytest

from kicad_autorouter.board_model import (
    BoardModel,
    BoardRegion,
    Point,
    apply_deltas,
    board_model,
)
from kicad_autorouter.placement import (
    PlacementParams,
    refine_placement,
    refine_placement_with_report,
)
from kicad_autorouter.placement.legality import check_legality
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


def _row(step=10.0, middle_angle=30.0):
    a = make_test_fp("R1", "uuid-a", 0.0, nets=("N1", "N2"))
    b = replace(make_test_fp("R2", "uuid-b", step, nets=("N3", "N4")), angle_deg=middle_angle)
    c = make_test_fp("R3", "uuid-c", 2 * step, nets=("N5", "N6"))
    return a, b, c


@pytest.mark.unit
class TestRefineBasics:
    def test_strength_zero_returns_empty(self):
        model = _model(list(_row()))
        assert refine_placement(model, {}, strength=0.0, radius_mm=25.0) == {}

    def test_angle_blends_to_neighbor_median(self):
        model = _model(list(_row(middle_angle=30.0)))
        out = refine_placement(model, {}, strength=1.0, radius_mm=25.0)
        assert out["uuid-b"][2] == pytest.approx(-30.0, abs=1e-6)

    def test_partial_strength_blends_proportionally(self):
        model = _model(list(_row(middle_angle=30.0)))
        out = refine_placement(model, {}, strength=0.5, radius_mm=25.0)
        assert out["uuid-b"][2] == pytest.approx(-15.0, abs=1e-6)

    def test_out_of_radius_ignores_far_neighbors(self):
        model = _model(list(_row(middle_angle=30.0)))
        out = refine_placement(model, {}, strength=1.0, radius_mm=1.0)
        # Neighbors beyond 1mm are invisible; the middle keeps its angle.
        assert all(abs(da) < 1e-9 for _, _, da in out.values())

    def test_deterministic(self):
        model = _model(list(_row(step=12.0, middle_angle=17.0)))
        first = refine_placement(model, {}, strength=0.7, radius_mm=25.0)
        second = refine_placement(model, {}, strength=0.7, radius_mm=25.0)
        assert first == second

    def test_single_pad_gets_no_rotation(self):
        two = make_test_fp("R1", "uuid-a", 0.0, nets=("N1", "N2"))
        one = make_test_fp("R2", "uuid-b", 10.0, nets=("N3",))
        other = make_test_fp("R3", "uuid-c", 20.0, nets=("N4", "N5"))
        model = _model([two, one, other])
        out = refine_placement(model, {}, strength=1.0, radius_mm=25.0)
        assert out.get("uuid-b", (0.0, 0.0, 0.0))[2] == 0.0

    def test_locked_anchor_never_moves(self):
        a = make_test_fp("R1", "uuid-a", 0.0, nets=("N1", "N2"))
        b = replace(
            make_test_fp("R2", "uuid-b", 10.0, nets=("N3", "N4")),
            angle_deg=30.0, locked=True,
        )
        c = make_test_fp("R3", "uuid-c", 20.0, nets=("N5", "N6"))
        model = _model([a, b, c])
        out = refine_placement(model, {}, strength=1.0, radius_mm=25.0)
        assert "uuid-b" not in out

    def test_cross_layer_excluded_from_neighbors(self):
        a = make_test_fp("R1", "uuid-a", 0.0, nets=("N1", "N2"))
        b = replace(make_test_fp("R2", "uuid-b", 10.0, nets=("N3", "N4")), angle_deg=30.0)
        other_side = replace(
            make_test_fp("R3", "uuid-c", 11.0, nets=("N5", "N6"), layer="B.Cu"),
            angle_deg=90.0,
        )
        model = _model([a, b, other_side])
        out = refine_placement(model, {}, strength=1.0, radius_mm=25.0)
        # Only same-layer F.Cu neighbor counts: median is 0, not skewed by the
        # 90-degree back-side part.
        assert out["uuid-b"][2] == pytest.approx(-30.0, abs=1e-6)
        assert "uuid-c" not in out


@pytest.mark.unit
class TestRefineSpacing:
    def test_gap_variance_shrinks(self):
        a = make_test_fp("R1", "uuid-a", 0.0, nets=("N1", "N2"))
        b = make_test_fp("R2", "uuid-b", 10.0, nets=("N3", "N4"))
        c = make_test_fp("R3", "uuid-c", 30.0, nets=("N5", "N6"))
        model = _model([a, b, c])
        out = refine_placement(model, {}, strength=1.0, radius_mm=60.0)
        assert out, "expected spacing moves on an uneven row"
        moved = apply_deltas(model, out)
        xs = sorted(fp.x_mm for fp in moved.footprints)
        gaps = [xs[1] - xs[0], xs[2] - xs[1]]
        assert abs(gaps[0] - gaps[1]) < abs(10.0 - 20.0)

    def test_result_stays_legal(self):
        fps = [
            make_test_fp(f"R{i}", f"uuid-{i}", float(i) * 8.0, nets=(f"N{i}a", f"N{i}b"))
            for i in range(5)
        ]
        model = _model(fps)
        out = refine_placement(model, {}, strength=1.0, radius_mm=30.0)
        merged = dict(out)
        report = check_legality(model, merged)
        assert report.is_legal

    def test_already_illegal_base_returns_empty(self):
        # Overlapping pair: no refine output can make the base legal, and the
        # gate must roll everything back rather than add motion.
        a = make_test_fp("R1", "uuid-a", 0.0, nets=("N1", "N2"), hw=5.0)
        b = make_test_fp("R2", "uuid-b", 1.0, nets=("N3", "N4"), hw=5.0)
        model = _model([a, b])
        assert refine_placement(model, {}, strength=1.0, radius_mm=25.0) == {}


@pytest.mark.unit
class TestRefineParams:
    def test_defaults_are_off(self):
        p = PlacementParams()
        assert p.refine_strength == 0.0
        assert p.refine_radius_m == pytest.approx(0.020)

    def test_round_trip(self):
        p = PlacementParams.from_dict({"refine_strength": 0.6, "refine_radius_m": 0.03})
        assert p.refine_strength == pytest.approx(0.6)
        assert p.refine_radius_m == pytest.approx(0.03)
        assert p.to_dict()["refine_strength"] == pytest.approx(0.6)

    def test_unknown_keys_still_dropped(self):
        p = PlacementParams.from_dict({"nope": 1.0})
        assert p.refine_strength == 0.0

    @pytest.mark.parametrize("bad", [-0.1, 1.5, float("nan")])
    def test_bad_strength_rejected(self, bad):
        with pytest.raises(ValueError):
            PlacementParams.from_dict({"refine_strength": bad})

    @pytest.mark.parametrize("bad", [0.0, -0.02])
    def test_bad_radius_rejected(self, bad):
        with pytest.raises(ValueError):
            PlacementParams.from_dict({"refine_radius_m": bad})


@pytest.mark.unit
class TestRefineContextAndReport:
    def test_ghost_never_counts_as_neighbor(self):
        a = make_test_fp("R1", "uuid-a", 0.0, nets=("N1", "N2"))
        b = replace(make_test_fp("R2", "uuid-b", 10.0, nets=("N3", "N4")), angle_deg=30.0)
        g = replace(
            make_test_fp("G1", "uuid-g", 15.0, nets=("N7", "N8")),
            angle_deg=90.0, ghost=True,
        )
        model = _model([a, b], ghosts=[g])
        out = refine_placement(model, {}, strength=1.0, radius_mm=25.0)
        # Ghost at 90° must not skew the median: b aligns to a (0°) only.
        assert out["uuid-b"][2] == pytest.approx(-30.0, abs=1e-6)
        assert "uuid-g" not in out

    def test_selection_moves_against_full_board_context(self):
        a, b, c = _row(middle_angle=30.0)
        model = _model([a, b, c])
        out = refine_placement(
            model, {}, strength=1.0, radius_mm=25.0,
            movable_uuids={"uuid-b"},
        )
        assert set(out) == {"uuid-b"}
        assert out["uuid-b"][2] == pytest.approx(-30.0, abs=1e-6)

    def test_report_names_neighborless_parts(self):
        (a,) = [make_test_fp("R1", "uuid-a", 0.0, nets=("N1", "N2"))]
        model = _model([a])
        out, rep = refine_placement_with_report(model, {}, strength=1.0, radius_mm=25.0)
        assert out == {}
        assert rep["no_neighbors"] == ["uuid-a"]
        assert rep["kept"] == []
        assert rep["base_illegal"] is False

    def test_report_keeps_moves_on_illegal_base(self):
        # Touching pair with something to line up: the old touches are
        # left alone, the orientation moves are kept.
        a = make_test_fp("R1", "uuid-a", 0.0, nets=("N1", "N2"), hw=5.0)
        b = replace(
            make_test_fp("R2", "uuid-b", 1.0, nets=("N3", "N4"), hw=5.0),
            angle_deg=30.0,
        )
        model = _model([a, b])
        out, rep = refine_placement_with_report(model, {}, strength=1.0, radius_mm=25.0)
        assert set(out) == {"uuid-a", "uuid-b"}
        assert rep["base_illegal"] is True
        assert rep["base_overlaps"] >= 1
        assert rep["rolled_back"] == []

    def test_fresh_damage_is_blocked(self):
        # Tall thin parts, clean base. b lining up with locked a would
        # land on top of it: only b is dropped, base stays clean.
        a = replace(
            make_test_fp("R1", "uuid-a", 0.0, nets=("N1", "N2"), hw=1.0, hh=5.0),
            locked=True,
        )
        b = replace(
            make_test_fp("R2", "uuid-b", 4.0, nets=("N3", "N4"), hw=1.0, hh=5.0),
            angle_deg=90.0,
        )
        model = _model([a, b])
        out, rep = refine_placement_with_report(model, {}, strength=1.0, radius_mm=25.0)
        assert out == {}
        assert rep["rolled_back"] == ["uuid-b"]
        assert rep["base_illegal"] is False


@pytest.mark.minimal
class TestRefineMinimalFixture:
    def test_minimal_runs_without_crash(self):
        model = board_model(parse_file(MINIMAL_PCB))
        out = refine_placement(model, {}, strength=1.0, radius_mm=25.0)
        assert isinstance(out, dict)
