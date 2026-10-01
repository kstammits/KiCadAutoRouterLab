"""Tests for the placement domain: params validation and the v0 stub runner."""

import math
from pathlib import Path

import pytest

from kicad_autorouter.board_model import Footprint, board_model
from kicad_autorouter.placement import (
    PlacementParams,
    _jitter_for,
    run_placement,
)
from kicad_autorouter.sexpr import parse_file

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL_PCB = FIXTURES / "minimal.kicad_pcb"


@pytest.fixture(scope="module")
def minimal_model():
    return board_model(parse_file(MINIMAL_PCB))


class TestParams:
    def test_defaults(self):
        p = PlacementParams()
        assert p.repulsion_kr == 100.0
        assert p.attraction_ka == 0.05
        assert p.ideal_length_mm == 25.0
        assert p.max_iterations == 1000
        assert p.convergence_eps_mm == 0.01
        assert p.demo_jitter_mm == 0.0

    def test_roundtrip(self):
        p = PlacementParams(repulsion_kr=42.5, demo_jitter_mm=3.0)
        q = PlacementParams.from_dict(p.to_dict())
        assert q == p

    def test_from_dict_fills_missing_and_ignores_unknown(self):
        q = PlacementParams.from_dict({"repulsion_kr": 1.0, "future_param": 9})
        assert q.repulsion_kr == 1.0
        assert q.attraction_ka == 0.05

    @pytest.mark.parametrize(
        "bad",
        [
            {"max_iterations": -1},
            {"max_iterations": 2.5},
            {"repulsion_kr": 0},
            {"attraction_ka": -0.1},
            {"ideal_length_mm": 0},
            {"convergence_eps_mm": 0},
            {"demo_jitter_mm": -1},
            {"repulsion_kr": "big"},
        ],
    )
    def test_from_dict_rejects_bad_values(self, bad):
        with pytest.raises(ValueError):
            PlacementParams.from_dict(bad)


class TestJitter:
    def test_deterministic_per_uuid(self):
        a = _jitter_for("uuid-a", 5.0)
        b = _jitter_for("uuid-a", 5.0)
        assert a == b

    def test_magnitude_bounded(self):
        for i in range(20):
            dx, dy = _jitter_for(f"uuid-{i}", 7.5)
            assert math.hypot(dx, dy) <= 7.5 + 1e-9


class TestStubRunner:
    MH1_UUID = "5c0af984-49c4-40a0-95aa-bb612ff4098b"

    def test_identity_with_default_params(self, minimal_model):
        p = PlacementParams()
        prop = run_placement(minimal_model, p)
        assert prop.deltas == {}
        assert prop.iterations == 0
        assert prop.final_max_disp_mm == 0.0
        assert prop.elapsed_s >= 0.0
        assert prop.params == p.to_dict()

    def test_jitter_moves_only_unlocked_parts(self, minimal_model):
        p = PlacementParams(demo_jitter_mm=5.0)
        prop = run_placement(minimal_model, p)
        # only MH1 is unlocked in the minimal fixture
        assert set(prop.deltas) == {self.MH1_UUID}
        dx, dy = prop.deltas[self.MH1_UUID]
        assert 0.0 < math.hypot(dx, dy) <= 5.0 + 1e-9
        assert prop.final_max_disp_mm == pytest.approx(math.hypot(dx, dy))

    def test_jitter_is_deterministic(self, minimal_model):
        p = PlacementParams(demo_jitter_mm=3.0)
        first = run_placement(minimal_model, p).deltas
        second = run_placement(minimal_model, p).deltas
        assert first == second

    def test_locked_mask_from_fixture(self, minimal_model):
        locked = [fp.locked for fp in minimal_model.footprints]
        assert locked == [True, False, True, True]
