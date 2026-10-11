"""Movement-trajectory behavior: converge, repel, attract without pathology.

The golden parity test pins exact historical trajectories (brittle under
physics rework); these tests pin *inequalities* a working mover must
satisfy on fast synthetic 2-4 body boards:

- courtyard overlap decays and resolves (no persistent interpenetration),
- per-iteration steps are neither too large (cap) nor too small (strangled),
- paths don't ping-pong (path/net ratio + reversal bound),
- attraction closes distance, repulsion separates without creating overlap,
- settling scenarios stop early at the analytic equilibrium,
- long runs stay finite and inside the board.

Tolerances are ratios/inequalities by design — dynamics retunes must not
churn these; only real regressions (non-convergence, jitter, blowup) go red.
"""

import math

import pytest

from kicad_autorouter.board_model import (
    BoardModel,
    BoardRegion,
    apply_deltas,
)
from kicad_autorouter.placement import PlacementParams, run_placement

from .helpers import make_fp as make_test_fp
from .helpers import overlap_area

UUID_A = "uuid-a"
UUID_B = "uuid-b"
UUID_C = "uuid-c"
UUID_D = "uuid-d"

REGION = BoardRegion(uuid="r0", polygon=(), bbox=(-60.0, 60.0, -60.0, 60.0))


def _board(*fps):
    return BoardModel(
        footprints=tuple(fps),
        keepout_zones=(),
        nets={},
        by_ref={fp.ref: fp for fp in fps},
        board_regions=(REGION,),
        footprint_region={fp.uuid: 0 for fp in fps},
    )


def _overlap_pair(x0=0.0, x1=4.0, y1=0.0):
    """Two 6x4mm-courtyard 2-pad parts, 2mm edge overlap in x."""
    fa = make_test_fp("A1", UUID_A, x0, 0.0, ("N1", "N2"),
                      footprint_id="T:R_0805", hw=3.0, hh=2.0,
                      pad_shape="circle", pad_layers=("F.Cu",))
    fb = make_test_fp("B1", UUID_B, x1, y1, ("N3", "N4"),
                      footprint_id="T:R_0805", hw=3.0, hh=2.0,
                      pad_shape="circle", pad_layers=("F.Cu",))
    return _board(fa, fb)


def _repulse_params(**kw):
    base = dict(max_iterations=60, convergence_eps_mm=1e-9,
                courtyard_repulsion_kc=2000.0, courtyard_halo_mm=3.0)
    base.update(kw)
    return PlacementParams(**base)


def _max_pad_disp(model, deltas):
    """Max over pads of |posed - committed| board displacement."""
    moved = apply_deltas(model, deltas)
    worst = 0.0
    for f_old, f_new in zip(model.footprints, moved.footprints):
        for p_old, p_new in zip(f_old.pads, f_new.pads):
            worst = max(worst, math.hypot(p_new.position.x_mm - p_old.position.x_mm,
                                          p_new.position.y_mm - p_old.position.y_mm))
    return worst


def _frame_origins(model, frames, uuid):
    fp = next(f for f in model.footprints if f.uuid == uuid)
    pts = [(fp.x_mm, fp.y_mm)]
    pts += [(fp.x_mm + fr[uuid][0], fp.y_mm + fr[uuid][1])
            for fr in frames if uuid in fr]
    return pts


@pytest.mark.physics
def test_overlap_decays_and_resolves():
    m = _overlap_pair()
    before = overlap_area(m, {})
    assert before == pytest.approx(8.0)
    prop = run_placement(m, _repulse_params())
    after = overlap_area(m, prop.deltas)
    assert after < 1e-6, f"overlap must resolve, left {after}"
    assert after < 0.05 * before


@pytest.mark.physics
def test_overlap_decay_is_monotonic():
    m = _overlap_pair()
    prop = run_placement(m, _repulse_params(), record_frames=True, frame_stride=1)
    areas = [overlap_area(m, fr) for fr in prop.frames]
    assert len(areas) > 5
    for a, b in zip(areas, areas[1:]):
        assert b <= a + 1e-6, f"overlap grew {a} -> {b} (oscillation)"


@pytest.mark.physics
def test_single_step_capped_at_max_step():
    """Deep overlap must saturate the per-iteration budget, never exceed it."""
    m = _overlap_pair(x1=2.0)  # 4mm deep overlap
    assert overlap_area(m, {}) == pytest.approx(16.0)
    prop = run_placement(m, _repulse_params(max_iterations=1,
                                            convergence_eps_mm=1e-12))
    cap = PlacementParams().max_step_mm
    assert _max_pad_disp(m, prop.deltas) <= cap + 1e-9


@pytest.mark.physics
def test_first_step_not_strangled():
    """With huge forces the first step must use a fair share of the budget."""
    m = _overlap_pair(x1=2.0)
    prop = run_placement(m, _repulse_params(max_iterations=1,
                                            convergence_eps_mm=1e-12))
    cap = PlacementParams().max_step_mm
    assert _max_pad_disp(m, prop.deltas) >= 0.25 * cap


@pytest.mark.physics
def test_path_does_not_ping_pong():
    """Path length vs net displacement bounds jitter (halo overshoot ok)."""
    m = _overlap_pair(y1=0.3)  # slight stagger breaks symmetry
    prop = run_placement(m, _repulse_params(), record_frames=True, frame_stride=1)
    for uuid in (UUID_A, UUID_B):
        pts = _frame_origins(m, prop.frames, uuid)
        path = sum(math.hypot(b[0] - a[0], b[1] - a[1])
                   for a, b in zip(pts, pts[1:]))
        net = math.hypot(pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1])
        assert net > 1.0, "parts must actually travel"
        assert path <= 3.0 * net + 0.5, f"{uuid}: path {path:.2f} vs net {net:.2f}"
        # Backward progress (against the net travel direction): one
        # overshoot correction plus decaying settling hunt are fine, but a
        # sustained cycle accumulates many net-lengths of backward travel.
        nx, ny = pts[-1][0] / net, pts[-1][1] / net
        backward = sum(
            max(0.0, -((b[0] - a[0]) * nx + (b[1] - a[1]) * ny))
            for a, b in zip(pts, pts[1:])
        )
        assert backward <= 0.5 * net + 0.2, f"{uuid}: backward {backward:.2f}"


@pytest.mark.physics
def test_attraction_closes_distance():
    fa = make_test_fp("A1", UUID_A, 0.0, 0.0, ("NET",),
                      footprint_id="T:R_0805", hw=1.5, hh=1.0,
                      pad_shape="circle", pad_layers=("F.Cu",))
    fb = make_test_fp("B1", UUID_B, 20.0, 0.0, ("NET",),
                      footprint_id="T:R_0805", hw=1.5, hh=1.0,
                      pad_shape="circle", pad_layers=("F.Cu",))
    m = _board(fa, fb)

    def gap(deltas):
        moved = apply_deltas(m, deltas)
        pa = moved.by_ref["A1"].pads[0].position
        pb = moved.by_ref["B1"].pads[0].position
        return math.hypot(pb.x_mm - pa.x_mm, pb.y_mm - pa.y_mm)

    d0 = gap({})
    prop = run_placement(m, PlacementParams(
        max_iterations=100, attraction_ka=2.0, repulsion_kr=10.0,
        courtyard_repulsion_kc=2000.0, courtyard_halo_mm=3.0))
    assert gap(prop.deltas) < 0.7 * d0
    for fr in prop.frames:
        assert gap(fr) <= d0 + 0.1, "attraction must not fling parts apart"


@pytest.mark.physics
def test_repulsion_separates_without_overlap():
    m = _overlap_pair(x1=8.0)  # 2mm edge gap, inside the 3mm halo
    assert overlap_area(m, {}) == 0.0
    prop = run_placement(m, _repulse_params(max_iterations=10))
    dx = prop.deltas[UUID_B][0] - prop.deltas[UUID_A][0]
    assert dx > 0.0, "halo push must separate the pair"
    assert overlap_area(m, prop.deltas) == 0.0


@pytest.mark.physics
def test_overlap_run_terminates_settled():
    """Friction deadband terminates the Coulomb tail: the separated pair
    must stop on its own, inside the board, with overlap resolved.

    Regression: an 1/8 damping floor pinned a sustained 0.25mm period-2
    flip-flop (overlap flickering across the jump) that ran the full
    budget with statically converged footprints.

    NOTE (2026-10-11, rigid projection): the old Hooke spring's slam
    injected direction reversals that decayed the adaptive step scale, so
    friction zeroed the pair early (~a fast stop, but for the wrong
    reason). Without the slam the pair cruises monotonically down the
    true 1/d^2 tail until per-pad steps clear the 0.05mm deadband
    (~309 iterations, +/-43mm — still inside the +/-60mm region). The
    budget below covers that tail with margin; what this guards is
    termination + containment, not speed.
    """
    m = _overlap_pair()
    prop = run_placement(m, _repulse_params(max_iterations=2000))
    assert prop.iterations < 2000, "settled run must early-stop, not budget out"
    assert overlap_area(m, prop.deltas) < 1e-6
    for uuid in (UUID_A, UUID_B):
        dx, dy, _ = prop.deltas[uuid]
        assert abs(dx) < 60.0 and abs(dy) < 60.0, f"{uuid} left the region"


@pytest.mark.physics
def test_friction_freezes_weak_motion_exactly():
    """Sub-threshold pushes leave pads bit-identical (static friction)."""
    fa = make_test_fp("A1", UUID_A, 0.0, 0.0, ("N1",),
                      footprint_id="T:R_0805", hw=1.5, hh=1.0,
                      pad_shape="circle", pad_layers=("F.Cu",))
    fb = make_test_fp("B1", UUID_B, 60.0, 0.0, ("N2",),
                      footprint_id="T:R_0805", hw=1.5, hh=1.0,
                      pad_shape="circle", pad_layers=("F.Cu",))
    m = _board(fa, fb)
    params = PlacementParams(max_iterations=10, convergence_eps_mm=1e-12,
                             friction_threshold_mm=50.0)
    prop = run_placement(m, params)
    assert prop.deltas == {}
    off = PlacementParams(max_iterations=10, convergence_eps_mm=1e-12,
                          friction_threshold_mm=0.0)
    assert run_placement(m, off).deltas != {}


@pytest.mark.physics
def test_friction_does_not_strand_deep_overlap():
    m = _overlap_pair(x1=2.0)  # 4mm deep overlap
    prop = run_placement(m, _repulse_params(max_iterations=200))
    assert overlap_area(m, prop.deltas) < 1e-6


@pytest.mark.physics
def test_friction_param_validation():
    with pytest.raises(ValueError):
        PlacementParams.from_dict({"friction_threshold_mm": -0.01})
    assert PlacementParams(friction_threshold_mm=0.0).friction_threshold_mm == 0.0
    assert PlacementParams().friction_threshold_mm == pytest.approx(0.05)
    assert PlacementParams().to_dict()["friction_threshold_mm"] == pytest.approx(0.05)
    assert PlacementParams.from_dict({}).friction_threshold_mm == pytest.approx(0.05)
    assert PlacementParams.from_dict(
        {"friction_threshold_mm": 0.1}).friction_threshold_mm == pytest.approx(0.1)


@pytest.mark.physics
def test_pair_settles_early_at_equilibrium():
    """Analytic balance d=sqrt(kr/ka): run must early-stop, not budget out."""
    kr, ka = 100.0, 1.0
    fa = make_test_fp("A1", UUID_A, 0.0, 0.0, ("NET",),
                      footprint_id="T:R_0805", hw=1.5, hh=1.0,
                      pad_shape="circle", pad_layers=("F.Cu",))
    fb = make_test_fp("B1", UUID_B, 20.0, 0.0, ("NET",),
                      footprint_id="T:R_0805", hw=1.5, hh=1.0,
                      pad_shape="circle", pad_layers=("F.Cu",))
    m = _board(fa, fb)
    prop = run_placement(m, PlacementParams(
        max_iterations=500, repulsion_kr=kr, attraction_ka=ka,
        courtyard_repulsion_kc=1e-9, courtyard_halo_mm=3.0,
        boundary_repulsion_kb=1e-9))
    assert prop.iterations < 500, "settling run must early-stop"
    moved = apply_deltas(m, prop.deltas)
    ax, bx = moved.by_ref["A1"].x_mm, moved.by_ref["B1"].x_mm
    assert abs(bx - ax) == pytest.approx(math.sqrt(kr / ka), rel=0.25)


@pytest.mark.physics
def test_long_run_stays_bounded():
    fa = make_test_fp("A1", UUID_A, 0.0, 0.0, ("N1", "N2"),
                      footprint_id="T:R_0805", hw=3.0, hh=2.0,
                      pad_shape="circle", pad_layers=("F.Cu",))
    fb = make_test_fp("B1", UUID_B, 4.0, 0.3, ("N3", "N4"),
                      footprint_id="T:R_0805", hw=3.0, hh=2.0,
                      pad_shape="circle", pad_layers=("F.Cu",))
    fc = make_test_fp("C1", UUID_C, 0.0, 10.0, ("N1", "N5"),
                      footprint_id="T:R_0805", hw=3.0, hh=2.0,
                      pad_shape="circle", pad_layers=("F.Cu",))
    fd = make_test_fp("D1", UUID_D, 20.0, 0.0, ("N6", "N7"),
                      footprint_id="T:R_0805", hw=3.0, hh=2.0,
                      pad_shape="circle", pad_layers=("F.Cu",))
    m = _board(fa, fb, fc, fd)
    pairs = [("uuid-a", "uuid-b"), ("uuid-a", "uuid-c"), ("uuid-b", "uuid-c"),
             ("uuid-a", "uuid-d"), ("uuid-b", "uuid-d"), ("uuid-c", "uuid-d")]
    before = sum(overlap_area(m, {}, uuids=p) for p in pairs)
    assert before > 0.0
    prop = run_placement(m, _repulse_params(max_iterations=200))
    after = sum(overlap_area(m, prop.deltas, uuids=p) for p in pairs)
    assert after < before
    moved = apply_deltas(m, prop.deltas)
    for fp in moved.footprints:
        assert all(math.isfinite(v)
                   for v in (fp.x_mm, fp.y_mm, fp.angle_deg)), fp.ref
        assert -70.0 <= fp.x_mm <= 70.0 and -70.0 <= fp.y_mm <= 70.0, fp.ref
