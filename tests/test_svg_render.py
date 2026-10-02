"""Tests for the model -> SVG renderer."""

import math
from pathlib import Path

import pytest

from kicad_autorouter.board_model import board_model
from kicad_autorouter.placement import PlacementProposal
from kicad_autorouter.sexpr import parse_file
from kicad_autorouter.svg_render import render_board_svg

FIXTURES = Path(__file__).parent / "fixtures"
MH1_UUID = "5c0af984-49c4-40a0-95aa-bb612ff4098b"


def _minimal_model():
    return board_model(parse_file(FIXTURES / "minimal.kicad_pcb"))


def _proposal(dx: float, dy: float) -> PlacementProposal:
    return PlacementProposal(
        deltas={MH1_UUID: (dx, dy)},
        iterations=0,
        final_max_disp_mm=math.hypot(dx, dy),
        elapsed_s=0.0,
        params={},
    )


def test_renders_valid_document():
    m = board_model(parse_file(FIXTURES / "minimal.kicad_pcb"))
    s = render_board_svg(m, title="t")
    assert s.startswith("<svg") and s.rstrip().endswith("</svg>")
    assert "<title>t</title>" in s


def test_minimal_draws_zones_and_outline():
    m = board_model(parse_file(FIXTURES / "minimal.kicad_pcb"))
    s = render_board_svg(m)
    assert s.count("<polygon") == 2  # keepout zones
    assert "<line" in s              # edge cuts


def test_dccf_draws_zone_and_refs():
    m = board_model(parse_file(FIXTURES / "DCCF.sved.kicad_pcb"))
    s = render_board_svg(m)
    assert "<polygon" in s  # GND zone
    assert "<text " in s    # reference designators


def test_dccf_labels_unnamed_footprints_with_short_uuid():
    m = board_model(parse_file(FIXTURES / "DCCF.sved.kicad_pcb"))
    unnamed = [fp for fp in m.footprints if not fp.ref]
    assert unnamed  # fixture has empty-ref footprints
    s = render_board_svg(m)
    for fp in unnamed:
        assert f">{fp.uuid[:8]}</text>" in s


def test_no_overlay_without_proposal():
    s = render_board_svg(_minimal_model())
    assert 'id="move-arrow"' not in s
    assert 'stroke-dasharray="0.6 0.4"' not in s


def test_empty_deltas_render_plain():
    prop = PlacementProposal(
        deltas={}, iterations=0, final_max_disp_mm=0.0, elapsed_s=0.0, params={}
    )
    s = render_board_svg(_minimal_model(), proposal=prop)
    assert 'id="move-arrow"' not in s


def test_overlay_draws_ghost_and_arrow():
    m = board_model(parse_file(FIXTURES / "DCCF.sved.kicad_pcb"))
    fp = next(f for f in m.footprints if f.courtyard)  # minimal fixture has none
    dx, dy = 2.0, -1.0
    prop = PlacementProposal(
        deltas={fp.uuid: (dx, dy)},
        iterations=0,
        final_max_disp_mm=math.hypot(dx, dy),
        elapsed_s=0.0,
        params={},
    )
    s = render_board_svg(m, proposal=prop)
    assert 'id="move-arrow"' in s
    arrow = (
        f'<line x1="{fp.x_mm:.3f}" y1="{fp.y_mm:.3f}" '
        f'x2="{(fp.x_mm + dx):.3f}" y2="{(fp.y_mm + dy):.3f}" '
        'marker-end="url(#move-arrow)"/>'
    )
    assert arrow in s
    a, b = fp.courtyard[0]
    ghost = (
        f'<line x1="{a.x_mm:.3f}" y1="{a.y_mm:.3f}" '
        f'x2="{b.x_mm:.3f}" y2="{b.y_mm:.3f}"/>'
    )
    # Ghost courtyard at original position (dashed, gray)
    assert ghost in s
    # Shifted courtyard at proposed position - now rendered inside footprint group
    # with state-based styling. Check that the shifted coordinates appear in a line.
    shifted_x1 = f'{(a.x_mm + dx):.3f}'
    shifted_y1 = f'{(a.y_mm + dy):.3f}'
    shifted_x2 = f'{(b.x_mm + dx):.3f}'
    shifted_y2 = f'{(b.y_mm + dy):.3f}'
    # The shifted courtyard is now rendered inside the footprint group with styling
    assert f'x1="{shifted_x1}"' in s
    assert f'y1="{shifted_y1}"' in s
    assert f'x2="{shifted_x2}"' in s
    assert f'y2="{shifted_y2}"' in s


def test_viewbox_grows_with_max_move():
    m = _minimal_model()

    def vb_width(s: str) -> float:
        return float(s.split('viewBox="')[1].split('"')[0].split()[2])

    plain = render_board_svg(m)
    dx, dy = 5.0, 0.0
    moved = render_board_svg(m, proposal=_proposal(dx, dy))
    assert vb_width(moved) == pytest.approx(vb_width(plain) + 2 * math.hypot(dx, dy))
