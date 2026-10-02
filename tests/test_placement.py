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
DCCF_PCB = FIXTURES / "DCCF.sved.kicad_pcb"


@pytest.fixture(scope="module")
def minimal_model():
    return board_model(parse_file(MINIMAL_PCB))


@pytest.fixture(scope="module")
def dccf_model():
    return board_model(parse_file(DCCF_PCB))


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
        p = PlacementParams(demo_jitter_mm=5.0, stub=False)
        prop = run_placement(minimal_model, p)
        # only MH1 is unlocked in the minimal fixture
        assert set(prop.deltas) == {self.MH1_UUID}
        dx, dy, da = prop.deltas[self.MH1_UUID]
        assert da == 0.0
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


class TestPhysicsRunner:
    """Tests for the vectorized numpy force-spring simulation (stub=False)."""

    def test_physics_moves_unlocked(self, dccf_model):
        p = PlacementParams(stub=False, max_iterations=10)
        prop = run_placement(dccf_model, p)
        # Should move some footprints
        assert prop.deltas
        # Check delta format is 3-tuple
        for uuid, (dx, dy, da) in prop.deltas.items():
            assert isinstance(dx, float)
            assert isinstance(dy, float)
            assert isinstance(da, float)
        assert prop.iterations > 0
        assert prop.final_max_disp_mm > 0.0

    def test_physics_respects_locked(self, dccf_model):
        p = PlacementParams(stub=False, max_iterations=10)
        prop = run_placement(dccf_model, p)
        # Locked footprints should not appear in deltas
        for uuid in prop.deltas:
            fp = next(fp for fp in dccf_model.footprints if fp.uuid == uuid)
            assert not fp.locked

    def test_selective_movable_uuids(self, dccf_model):
        p = PlacementParams(stub=False, max_iterations=10)
        # Pick a movable footprint UUID
        movable_fp = next(fp for fp in dccf_model.footprints if not fp.locked and fp.uuid)
        prop = run_placement(dccf_model, p, movable_uuids={movable_fp.uuid})
        assert prop.deltas
        assert set(prop.deltas) == {movable_fp.uuid}

    def test_selective_movable_uuids_empty(self, dccf_model):
        p = PlacementParams(stub=False, max_iterations=10)
        # Empty set = nothing can move
        prop = run_placement(dccf_model, p, movable_uuids=set())
        assert prop.deltas == {}

    def test_selective_movable_uuids_excludes_locked(self, dccf_model):
        p = PlacementParams(stub=False, max_iterations=10)
        # Try to move a locked footprint - should be ignored
        locked_fps = [fp for fp in dccf_model.footprints if fp.locked]
        if not locked_fps:
            pytest.skip("No locked footprints in DCCF fixture")
        locked_fp = locked_fps[0]
        prop = run_placement(dccf_model, p, movable_uuids={locked_fp.uuid})
        assert prop.deltas == {}

    def test_convergence_stops_early(self, dccf_model):
        # With very loose convergence, should stop before max_iterations
        p = PlacementParams(stub=False, max_iterations=1000, convergence_eps_mm=100.0)
        prop = run_placement(dccf_model, p)
        assert prop.iterations < 1000

    def test_iterations_param_override(self, dccf_model):
        p = PlacementParams(stub=False, max_iterations=1000)
        prop = run_placement(dccf_model, p)
        # Should run up to max_iterations unless converged
        assert prop.iterations <= 1000
