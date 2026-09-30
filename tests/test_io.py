"""Tests for the pure-Python pcb+sch pair round trip (io + sexpr serializer).

kicad-cli-dependent checks skip cleanly when it is not installed.
"""

from pathlib import Path

import pytest

from kicad_autorouter.io import (
    RoundtripError,
    load_pair,
    noop_roundtrip,
    no_op_edit,
    save_pair,
    verify_noop,
)
from kicad_autorouter.sexpr import parse, parse_file, to_sexpr
from kicad_autorouter.validate import (
    KicadCliNotFound,
    find_kicad_cli,
    validate_pcb,
    validate_sch,
)

FIXTURES = Path(__file__).parent / "fixtures"
PCB_FIXTURE = FIXTURES / "minimal.kicad_pcb"
SCH_FIXTURE = FIXTURES / "minimal.kicad_sch"


# --- serializer -------------------------------------------------------------


def test_to_sexpr_inline_scalars():
    assert to_sexpr(parse("(version 20241229)")) == "(version 20241229)"


def test_to_sexpr_anonymous_scalar_list():
    assert to_sexpr(parse('(0 "F.Cu" signal)')) == '(0 "F.Cu" signal)'


def test_to_sexpr_multiline_nested():
    node = parse('(footprint "X:Y" (at 58.57 -3.35) (layer "F.Cu"))')
    assert to_sexpr(node) == '(footprint "X:Y"\n\t(at 58.57 -3.35)\n\t(layer "F.Cu")\n)'


def test_to_sexpr_string_escaping_round_trip():
    node = parse('(name "a\\nb\\"c")')
    assert to_sexpr(node) == '(name "a\\nb\\"c")'
    assert parse(to_sexpr(node)) == node


def test_to_sexpr_hex_mask_becomes_decimal_but_equal():
    node = parse("(layerselection 0x0000_ffff)")
    assert to_sexpr(node) == "(layerselection 65535)"
    assert parse(to_sexpr(node)) == node


def test_fixture_pcb_round_trip_tree_equality():
    tree = parse_file(PCB_FIXTURE)
    assert parse(to_sexpr(tree)) == tree


def test_fixture_sch_round_trip_tree_equality():
    tree = parse_file(SCH_FIXTURE)
    assert parse(to_sexpr(tree)) == tree


# --- pair load / save --------------------------------------------------------


def test_load_pair_headers_and_paths():
    pair = load_pair(PCB_FIXTURE, SCH_FIXTURE)
    assert pair.pcb.head == "kicad_pcb"
    assert pair.sch.head == "kicad_sch"
    assert pair.pcb_path == PCB_FIXTURE
    assert pair.sch_path == SCH_FIXTURE


def test_load_pair_rejects_wrong_header():
    with pytest.raises(ValueError, match="kicad_pcb"):
        load_pair(SCH_FIXTURE, SCH_FIXTURE)  # sch file in the pcb slot


def test_no_op_edit_is_identity():
    node = parse("(a 1)")
    assert no_op_edit(node) is node


def test_save_pair_writes_both(tmp_path):
    pair = load_pair(PCB_FIXTURE, SCH_FIXTURE)
    out_pcb, out_sch = save_pair(
        pair, tmp_path / "b.kicad_pcb", tmp_path / "s.kicad_sch"
    )
    assert parse_file(out_pcb) == pair.pcb
    assert parse_file(out_sch) == pair.sch


def test_noop_roundtrip_and_verify(tmp_path):
    out_pcb, out_sch = noop_roundtrip(PCB_FIXTURE, SCH_FIXTURE, tmp_path)
    assert out_pcb.name == "minimal_no_op.kicad_pcb"
    assert out_sch.name == "minimal_no_op.kicad_sch"
    verify_noop(PCB_FIXTURE, SCH_FIXTURE, out_pcb, out_sch)


def test_verify_noop_detects_change(tmp_path):
    out_pcb, out_sch = noop_roundtrip(PCB_FIXTURE, SCH_FIXTURE, tmp_path)
    text = out_pcb.read_text(encoding="utf-8")
    changed = text.replace("(version 20241229)", "(version 99999999)")
    assert changed != text
    out_pcb.write_text(changed, encoding="utf-8")
    with pytest.raises(RoundtripError):
        verify_noop(PCB_FIXTURE, SCH_FIXTURE, out_pcb, out_sch)


# --- kicad-cli checks --------------------------------------------------------


@pytest.fixture(scope="module")
def cli() -> str:
    try:
        return find_kicad_cli()
    except KicadCliNotFound:
        pytest.skip("kicad-cli not installed")


def test_roundtripped_pair_passes_drc_and_erc(cli, tmp_path):
    out_pcb, out_sch = noop_roundtrip(PCB_FIXTURE, SCH_FIXTURE, tmp_path)
    assert validate_pcb(out_pcb, cli=cli).ok
    assert validate_sch(out_sch, cli=cli).ok
