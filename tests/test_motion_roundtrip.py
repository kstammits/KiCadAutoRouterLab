"""Frames -> SVG round trip: the motion player must show internal poses.

The player (`ui/index.html::applyMotionFrame`) poses each part with an
SVG group transform built from the frame delta vs the committed board::

    translate(dx dy) rotate(-da about base)

where ``base`` is the committed footprint origin from ``/api/footprints``.
Internally the same delta is applied by ``apply_deltas`` (local-frame
round trip: rotate ``R(-da)`` about the footprint origin, then translate).
These are two independent implementations of one rigid motion — the C11
bug class (wrong pivot, flipped rotation sign, translate/rotate order)
is exactly a disagreement between them.

So for every recorded frame (the shear scenario below rotates >160°,
which is what exposes sign/pivot errors):

- ``test_player_transform_matches_apply_deltas``: the player formula in
  Python applied to committed courtyard/pad positions equals
  ``apply_deltas(model, frame)`` to 1e-9,
- ``test_rendered_svg_matches_player_pose``: the actual SVG file for a
  frame's deltas carries those same coordinates (3-decimal formatting),
- ``test_frames_json_preserves_delta_order``: ``proposal_to_json`` serves
  frames as ``[dx, dy, da]`` per uuid, which is what the player consumes.
"""

import math
import re
from pathlib import Path

import pytest

from kicad_autorouter.board_model import (
    BoardModel,
    Point,
    apply_deltas,
)
from kicad_autorouter.placement import PlacementParams, PlacementProposal, run_placement
from kicad_autorouter.svg_render import render_board_svg

from .helpers import make_fp as make_test_fp

UUID_M = "uuid-m"
UUID_L = "uuid-l"


def _shear_model():
    """Locked part grazes one pad of a 2-pad mover -> torque + travel."""
    mover = make_test_fp("M", UUID_M, 2.0, 0.0, ("N1", "N2"),
                         footprint_id="test:R", hw=1.5, hh=1.0,
                         pad_layers=("F.Cu",), pad_shape="circle")
    locked = make_test_fp("L", UUID_L, 1.0, 0.3, ("N9",),
                          footprint_id="test:R", hw=1.5, hh=1.0,
                          pad_layers=("F.Cu",), pad_shape="circle",
                          locked=True)
    edge = 30.0
    return BoardModel(
        footprints=(mover, locked),
        keepout_zones=(),
        nets={},
        by_ref={"M": mover, "L": locked},
        edge_cuts=(
            (Point(-edge, -edge), Point(edge, -edge)),
            (Point(edge, -edge), Point(edge, edge)),
            (Point(edge, edge), Point(-edge, edge)),
            (Point(-edge, edge), Point(-edge, -edge)),
        ),
        footprint_region={UUID_M: 0, UUID_L: 0},
    )


def _run_frames():
    model = _shear_model()
    prop = run_placement(
        model,
        PlacementParams(max_iterations=12, convergence_eps_m=1e-15,
                        repulsion_k_N_m2=1.0000000000000001e-07, attraction_k_N=5e-05,
                        wall_k_N=0.002, wall_halo_m=0.003,
                        boundary_k_N=1e-06),
        movable_uuids={UUID_M},
        record_frames=True, frame_stride=1,
    )
    assert any(abs(fr.get(UUID_M, (0, 0, 0))[2]) > 30.0 for fr in prop.frames), \
        "scenario must rotate (else sign/pivot errors hide)"
    return model, prop


def _player_pose(base, px, py, delta):
    """Python replica of index.html applyMotionFrame.

    SVG ``translate(dx dy) rotate(-da cx cy)`` maps ``p`` to
    ``base + R(-da).(p - base) + d`` (transforms compose left-to-right;
    board coords are used directly as SVG coords by the renderer).
    """
    dx, dy, da = delta
    rad = math.radians(-da)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    qx, qy = px - base[0], py - base[1]
    return (base[0] + qx * cos_a - qy * sin_a + dx,
            base[1] + qx * sin_a + qy * cos_a + dy)


@pytest.mark.physics
def test_player_transform_matches_apply_deltas():
    model, prop = _run_frames()
    fp = next(f for f in model.footprints if f.uuid == UUID_M)
    base = (fp.x_mm, fp.y_mm)
    for i, frame in enumerate(prop.frames):
        if UUID_M not in frame:
            continue
        posed = apply_deltas(model, {UUID_M: frame[UUID_M]})
        fp_new = next(f for f in posed.footprints if f.uuid == UUID_M)
        assert (fp_new.x_mm, fp_new.y_mm) == pytest.approx(
            (base[0] + frame[UUID_M][0], base[1] + frame[UUID_M][1]))
        # apply_deltas stores angle + da unnormalized (pads normalize).
        assert fp_new.angle_deg == pytest.approx(fp.angle_deg + frame[UUID_M][2])
        for (a_old, b_old), (a_new, b_new) in zip(fp.courtyard, fp_new.courtyard):
            for old, new in ((a_old, a_new), (b_old, b_new)):
                want = _player_pose(base, old.x_mm, old.y_mm, frame[UUID_M])
                assert (new.x_mm, new.y_mm) == pytest.approx(want, abs=1e-9), \
                    f"frame {i}: player/SVG pose disagrees with internals"
        for p_old, p_new in zip(fp.pads, fp_new.pads):
            want = _player_pose(base, p_old.position.x_mm, p_old.position.y_mm,
                                frame[UUID_M])
            assert (p_new.position.x_mm, p_new.position.y_mm) == \
                pytest.approx(want, abs=1e-9), f"frame {i}: pad pose mismatch"


@pytest.mark.physics
def test_rendered_svg_matches_player_pose():
    """The SVG file for a frame's deltas must carry the player pose."""
    model, prop = _run_frames()
    frame = prop.frames[-1]
    svg = render_board_svg(
        model,
        proposal=PlacementProposal(deltas=dict(frame), iterations=0,
                                   final_max_disp_mm=0.0, elapsed_s=0.0,
                                   params={}),
    )
    fp = next(f for f in model.footprints if f.uuid == UUID_M)
    base = (fp.x_mm, fp.y_mm)
    grp = re.search(
        rf'<g class="[^"]*" data-fp-uuid="{re.escape(UUID_M)}".*?</g>',
        svg, re.DOTALL,
    )
    assert grp, "no SVG group for mover"
    lines = re.findall(
        r'<line x1="([\d.\-]+)" y1="([\d.\-]+)" '
        r'x2="([\d.\-]+)" y2="([\d.\-]+)"', grp.group(0),
    )
    assert len(lines) == len(fp.courtyard)
    for (x1, y1, x2, y2), (a_old, b_old) in zip(lines, fp.courtyard):
        want_a = _player_pose(base, a_old.x_mm, a_old.y_mm, frame[UUID_M])
        want_b = _player_pose(base, b_old.x_mm, b_old.y_mm, frame[UUID_M])
        assert (float(x1), float(y1)) == pytest.approx(want_a, abs=2e-3)
        assert (float(x2), float(y2)) == pytest.approx(want_b, abs=2e-3)


@pytest.mark.server
def test_frames_json_preserves_delta_order():
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ui"))
    import server

    model, prop = _run_frames()
    payload = server.proposal_to_json(prop)
    assert payload["frame_count"] == len(prop.frames) > 1
    assert payload["frame_stride"] == 1
    for fr_json, fr in zip(payload["frames"], prop.frames):
        for uuid, (dx, dy, da) in fr.items():
            assert fr_json[uuid][0] == pytest.approx(dx, abs=5e-4)
            assert fr_json[uuid][1] == pytest.approx(dy, abs=5e-4)
            assert fr_json[uuid][2] == pytest.approx(da, abs=5e-3)
    # basePos source: /api/footprints serves committed origins, which is
    # what the player rotates about.
    for fp in model.footprints:
        assert isinstance(fp.x_mm, float) and isinstance(fp.y_mm, float)
