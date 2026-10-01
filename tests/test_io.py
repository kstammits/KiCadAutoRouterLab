"""Tests for the pure-Python pcb+sch pair read/modify/write pass (io + sexpr).

The no-op edit is ``nudge_footprint`` with zero deltas, per io.py's docstring.
kicad-cli-dependent checks skip cleanly when it is not installed.
"""

from dataclasses import replace
from pathlib import Path

import pytest

from kicad_autorouter.io import (
    find_footprint,
    find_footprint_by_uuid,
    load_pair,
    nudge_footprint,
    nudge_footprint_by_uuid,
    save_pair,
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
MH1_UUID = "5c0af984-49c4-40a0-95aa-bb612ff4098b"


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


# --- nudge_footprint (the no-op edit) ----------------------------------------


def test_zero_nudge_is_identity():
    tree = parse_file(PCB_FIXTURE)
    assert nudge_footprint(tree, "MH1", 0.0, 0.0) == tree


def test_nudge_moves_only_target_footprint():
    tree = parse_file(PCB_FIXTURE)
    moved = nudge_footprint(tree, "MH1", 1.0, 2.0)
    at = find_footprint(moved, "MH1").find("at")
    assert (float(at.args[0]), float(at.args[1])) == pytest.approx((59.57, 52.55))
    orig_mh2 = find_footprint(tree, "MH2").find("at")
    new_mh2 = find_footprint(moved, "MH2").find("at")
    assert list(new_mh2.args) == list(orig_mh2.args)


def test_nudge_unknown_ref_raises():
    tree = parse_file(PCB_FIXTURE)
    with pytest.raises(KeyError):
        nudge_footprint(tree, "NOPE", 1.0, 0.0)


# --- UUID-based addressing (duplicate / missing refs) ------------------------


def test_find_footprint_by_uuid():
    tree = parse_file(PCB_FIXTURE)
    at = find_footprint_by_uuid(tree, MH1_UUID).find("at")
    assert (float(at.args[0]), float(at.args[1])) == pytest.approx((58.57, 50.55))


def test_find_footprint_by_uuid_unknown_raises():
    tree = parse_file(PCB_FIXTURE)
    with pytest.raises(KeyError):
        find_footprint_by_uuid(tree, "nope")


def test_nudge_by_uuid_moves_only_target_footprint():
    tree = parse_file(PCB_FIXTURE)
    moved = nudge_footprint_by_uuid(tree, MH1_UUID, 1.0, 2.0)
    at = find_footprint(moved, "MH1").find("at")
    assert (float(at.args[0]), float(at.args[1])) == pytest.approx((59.57, 52.55))
    orig_mh4 = find_footprint(tree, "MH4").find("at")
    new_mh4 = find_footprint(moved, "MH4").find("at")
    assert list(new_mh4.args) == list(orig_mh4.args)


def test_nudge_by_uuid_unknown_raises():
    tree = parse_file(PCB_FIXTURE)
    with pytest.raises(KeyError):
        nudge_footprint_by_uuid(tree, "nope", 1.0, 0.0)


# --- round trip --------------------------------------------------------------


def _zero_nudge_roundtrip(out_dir: Path):
    """Load the fixture pair, apply the zero-delta no-op edit, write new files."""
    pair = load_pair(PCB_FIXTURE, SCH_FIXTURE)
    edited = nudge_footprint(pair.pcb, "MH1", 0.0, 0.0)
    return save_pair(
        replace(pair, pcb=edited),
        out_dir / "minimal_no_op.kicad_pcb",
        out_dir / "minimal_no_op.kicad_sch",
    )


def test_save_pair_writes_both(tmp_path):
    pair = load_pair(PCB_FIXTURE, SCH_FIXTURE)
    out_pcb, out_sch = save_pair(
        pair, tmp_path / "b.kicad_pcb", tmp_path / "s.kicad_sch"
    )
    assert parse_file(out_pcb) == pair.pcb
    assert parse_file(out_sch) == pair.sch


def test_zero_nudge_roundtrip_preserves_trees(tmp_path):
    out_pcb, out_sch = _zero_nudge_roundtrip(tmp_path)
    pair = load_pair(PCB_FIXTURE, SCH_FIXTURE)
    assert parse_file(out_pcb) == pair.pcb
    assert parse_file(out_sch) == pair.sch


def test_roundtrip_detects_change(tmp_path):
    out_pcb, _ = _zero_nudge_roundtrip(tmp_path)
    text = out_pcb.read_text(encoding="utf-8")
    changed = text.replace("(version 20241229)", "(version 99999999)")
    assert changed != text
    out_pcb.write_text(changed, encoding="utf-8")
    pair = load_pair(PCB_FIXTURE, SCH_FIXTURE)
    assert parse_file(out_pcb) != pair.pcb


# --- kicad-cli checks --------------------------------------------------------


@pytest.fixture(scope="module")
def cli() -> str:
    try:
        return find_kicad_cli()
    except KicadCliNotFound:
        pytest.skip("kicad-cli not installed")


def test_roundtripped_pair_passes_drc_and_erc(cli, tmp_path):
    out_pcb, out_sch = _zero_nudge_roundtrip(tmp_path)
    assert validate_pcb(out_pcb, cli=cli).ok
    assert validate_sch(out_sch, cli=cli).ok
