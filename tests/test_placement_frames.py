"""Trajectory frames for the SVG motion player: downsampling + rounding."""

import pytest

from kicad_autorouter.board_model import BoardModel, BoardRegion
from kicad_autorouter.placement import (
    FRAME_ANGLE_DECIMALS,
    FRAME_XY_DECIMALS,
    PlacementParams,
    _thin_frames,
    run_placement,
)

from .helpers import make_fp as make_test_fp


def _two_close_parts() -> BoardModel:
    """Two 1-pad footprints 2mm apart (inside the 3mm halo) on separate nets.

    Courtyard repulsion guarantees motion over a few iterations.
    """
    fpa = make_test_fp(
        "A1", "aaaaaaaa-0000-0000-0000-000000000001", 0.0, 0.0, ("N1",),
        footprint_id="T:R_0805", hw=1.0, hh=1.0,
        pad_shape="circle", pad_layers=(),
    )
    fpb = make_test_fp(
        "B1", "bbbbbbbb-0000-0000-0000-000000000002", 4.0, 0.0, ("N2",),
        footprint_id="T:R_0805", hw=1.0, hh=1.0,
        pad_shape="circle", pad_layers=(),
    )
    region = BoardRegion(uuid="r0", polygon=(), bbox=(-10.0, 20.0, -10.0, 10.0))
    return BoardModel(
        footprints=(fpa, fpb),
        keepout_zones=(),
        nets={},
        by_ref={"A1": fpa, "B1": fpb},
        board_regions=(region,),
        footprint_region={fpa.uuid: 0, fpb.uuid: 0},
    )


def _params(**kw):
    base = dict(
        max_iterations=5,
        repulsion_kr=1.0,
        courtyard_repulsion_kc=100.0,
        courtyard_halo_mm=3.0,
    )
    base.update(kw)
    return PlacementParams(**base)


def _decimals(v: float) -> int:
    s = repr(float(v))
    if "e" in s or "E" in s:
        return 0  # tiny scientific values (e.g. 0.0) carry no decimals
    if "." not in s:
        return 0
    return len(s.split(".")[1].rstrip("0"))


@pytest.mark.unit
class TestTrajectoryFrames:
    def test_off_by_default(self):
        prop = run_placement(_two_close_parts(), _params())
        assert prop.frames == []
        assert prop.frame_stride == 1

    def test_records_strided_frames(self):
        prop = run_placement(
            _two_close_parts(), _params(), record_frames=True, frame_stride=2,
        )
        # 5 iters, stride 2 -> samples at 2,4 + final = 3 frames
        assert len(prop.frames) == 3
        assert prop.frame_stride == 2
        assert set(prop.frames[0]) == {
            "aaaaaaaa-0000-0000-0000-000000000001",
            "bbbbbbbb-0000-0000-0000-000000000002",
        }

    def test_last_frame_matches_deltas(self):
        prop = run_placement(
            _two_close_parts(), _params(), record_frames=True,
        )
        assert prop.frames, "expected motion over 5 iters"
        last = prop.frames[-1]
        for uuid, (dx, dy, da) in prop.deltas.items():
            fdx, fdy, fda = last[uuid]
            assert fdx == pytest.approx(dx, abs=10 ** -FRAME_XY_DECIMALS)
            assert fdy == pytest.approx(dy, abs=10 ** -FRAME_XY_DECIMALS)
            assert fda == pytest.approx(da, abs=10 ** -FRAME_ANGLE_DECIMALS)

    def test_floats_rounded_short(self):
        prop = run_placement(
            _two_close_parts(), _params(), record_frames=True,
        )
        for frame in prop.frames:
            for dx, dy, da in frame.values():
                assert _decimals(dx) <= FRAME_XY_DECIMALS
                assert _decimals(dy) <= FRAME_XY_DECIMALS
                assert _decimals(da) <= FRAME_ANGLE_DECIMALS

    def test_downsample_caps_long_runs(self):
        prop = run_placement(
            _two_close_parts(),
            _params(max_iterations=50),
            record_frames=True,
            max_frames=10,
        )
        assert len(prop.frames) <= 10
        # First sample + final pose preserved through thinning
        assert prop.frames[-1] == run_placement(
            _two_close_parts(), _params(max_iterations=50),
            record_frames=True, max_frames=1000,
        ).frames[-1]


@pytest.mark.unit
class TestThinFrames:
    def test_within_budget_is_noop(self):
        frames = [{"a": (1.0, 2.0, 3.0)}, {"a": (4.0, 5.0, 6.0)}]
        assert _thin_frames(frames, 10) == frames

    def test_keeps_last_and_uniform(self):
        frames = [{"i": (float(i), 0.0, 0.0)} for i in range(5)]
        thinned = _thin_frames(frames, 3)
        assert thinned[0] == frames[0]
        assert thinned[-1] == frames[-1]
        assert len(thinned) == 3

    def test_single_frame_keeps_final(self):
        frames = [{"i": (float(i), 0.0, 0.0)} for i in range(5)]
        assert _thin_frames(frames, 1) == [frames[-1]]
