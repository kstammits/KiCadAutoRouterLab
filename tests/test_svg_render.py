"""Tests for the model -> SVG renderer."""

from pathlib import Path

from kicad_autorouter.board_model import board_model
from kicad_autorouter.sexpr import parse_file
from kicad_autorouter.svg_render import render_board_svg

FIXTURES = Path(__file__).parent / "fixtures"


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
