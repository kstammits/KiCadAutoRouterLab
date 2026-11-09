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
    reset_label_anchors,
    save_pair,
)
from kicad_autorouter.sexpr import SExpr, Symbol, parse, parse_file, to_sexpr, tokenize
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


def test_nudge_moves_only_target_footprint():
    tree = parse_file(PCB_FIXTURE)
    moved = nudge_footprint(tree, "MH1", 1.0, 2.0)
    at = find_footprint(moved, "MH1").find("at")
    assert (float(at.args[0]), float(at.args[1])) == pytest.approx((59.57, 52.55))
    orig_mh2 = find_footprint(tree, "MH2").find("at")
    new_mh2 = find_footprint(moved, "MH2").find("at")
    assert list(new_mh2.args) == list(orig_mh2.args)


def test_find_footprint_by_uuid():
    tree = parse_file(PCB_FIXTURE)
    at = find_footprint_by_uuid(tree, MH1_UUID).find("at")
    assert (float(at.args[0]), float(at.args[1])) == pytest.approx((58.57, 50.55))


def test_nudge_by_uuid_moves_only_target_footprint():
    tree = parse_file(PCB_FIXTURE)
    moved = nudge_footprint_by_uuid(tree, MH1_UUID, 1.0, 2.0)
    at = find_footprint(moved, "MH1").find("at")
    assert (float(at.args[0]), float(at.args[1])) == pytest.approx((59.57, 52.55))
    orig_mh4 = find_footprint(tree, "MH4").find("at")
    new_mh4 = find_footprint(moved, "MH4").find("at")
    assert list(new_mh4.args) == list(orig_mh4.args)


def _ref_texts(tree, ref):
    """(property Reference at-args, fp_text user at-args) for a footprint."""
    fp = find_footprint(tree, ref)
    prop = next(
        c for c in fp.args
        if getattr(c, "head", None) == "property" and c.args[1] == ref
    )
    mirror = next(
        c for c in fp.args
        if getattr(c, "head", None) == "fp_text" and c.args[1] == "${REFERENCE}"
    )
    return prop, mirror


@pytest.mark.unit
def test_reset_label_anchors_restores_filed_positions():
    tree = parse_file(PCB_FIXTURE)
    drifted = nudge_footprint_by_uuid(
        tree, MH1_UUID, 0.0, 0.0, 0.0, label_dx_mm=2.0, label_dy_mm=-1.0)
    prop0, mirr0 = _ref_texts(tree, "MH1")
    prop1, mirr1 = _ref_texts(drifted, "MH1")
    assert float(prop1.find("at").args[0]) == pytest.approx(
        float(prop0.find("at").args[0]) + 2.0)
    fixed, n = reset_label_anchors(drifted, tree)
    assert n > 0
    prop2, mirr2 = _ref_texts(fixed, "MH1")
    assert list(prop2.find("at").args) == pytest.approx(
        list(prop0.find("at").args))
    assert list(mirr2.find("at").args) == pytest.approx(
        list(mirr0.find("at").args))
    # Value text is not a label anchor: untouched throughout.
    value = next(
        c for c in find_footprint(fixed, "MH1").args
        if getattr(c, "head", None) == "property" and c.args[0] == "Value")
    assert list(value.find("at").args)[:2] == [0, 3.35]
    # Idempotent: already-filed input changes nothing.
    _, n2 = reset_label_anchors(fixed, tree)
    assert n2 == 0


def test_fixture_sch_round_trip_tree_equality():
    tree = parse_file(SCH_FIXTURE)
    assert parse(to_sexpr(tree)) == tree


def _zero_nudge_roundtrip(out_dir: Path):
    """Load the fixture pair, apply the zero-delta no-op edit, write new files."""
    pair = load_pair(PCB_FIXTURE, SCH_FIXTURE)
    edited = nudge_footprint(pair.pcb, "MH1", 0.0, 0.0)
    return save_pair(
        replace(pair, pcb=edited),
        out_dir / "minimal_no_op.kicad_pcb",
        out_dir / "minimal_no_op.kicad_sch",
    )


def test_zero_nudge_roundtrip_preserves_trees(tmp_path):
    out_pcb, out_sch = _zero_nudge_roundtrip(tmp_path)
    pair = load_pair(PCB_FIXTURE, SCH_FIXTURE)
    assert parse_file(out_pcb) == pair.pcb
    assert parse_file(out_sch) == pair.sch


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


def test_valid_pcb_passes_drc(cli):
    report = validate_pcb(PCB_FIXTURE, cli=cli)
    assert report.loaded
    assert report.error is None
    assert report.violations == []
    assert report.ok


def test_valid_sch_passes_erc(cli):
    report = validate_sch(SCH_FIXTURE, cli=cli)
    assert report.loaded
    assert report.error is None
    assert report.violations == []
    assert report.ok


# ---------------------------------------------------------------------------
# S-expression layer: parse / serialize round-trips.
#
# (Moved from test_sexpr.py: the sexpr codec is the IO layer's foundation.)


def _reference(fp: SExpr):
    for prop in fp.children("property"):
        if len(prop.args) >= 2 and prop.args[0] == "Reference":
            return prop.args[1]
    return None


def test_parse_minimal_fixture():
    board = parse_file(PCB_FIXTURE)
    assert board.head == "kicad_pcb"

    version = board.find("version")
    assert version is not None and version.args == (20241229,)

    layers = board.find("layers")
    assert layers is not None
    first_layer = layers.args[0]
    assert isinstance(first_layer, SExpr) and first_layer.head is None
    assert first_layer.args[1] == "F.Cu"

    footprints = list(board.children("footprint"))
    assert len(footprints) >= 1
    refs = {r for r in (_reference(fp) for fp in footprints) if r}
    assert "MH1" in refs

    pads = [pad for fp in footprints for pad in fp.children("pad")]
    assert len(pads) >= 1

    zones = list(board.children("zone"))
    assert len(zones) >= 1


def test_digit_led_bare_words_stay_symbols():
    # Malformed-but-harmless tokens (e.g. unquoted 3v3) must not crash
    # the parse; they read back as bare symbols.
    node = parse("(net 3v3)")
    assert node.args[0] == Symbol("3v3")
    assert to_sexpr(parse(to_sexpr(node))) == to_sexpr(node)


def test_unknown_string_escape_round_trips():
    # "\q" keeps its backslash so the serializer re-emits it identically.
    node = parse(r'(gr_text "a\qb" (layer "F.SilkS"))')
    assert node.args[0] == "a\\qb"
    assert to_sexpr(parse(to_sexpr(node))) == to_sexpr(node)


def test_bare_word_token_columns_advance():
    toks = tokenize("(aa bb)")
    cols = [t.col for t in toks if t.kind == "symbol"]
    assert cols == [2, 5]


def test_fixture_serialize_reparse_stable():
    board = parse_file(PCB_FIXTURE)
    once = to_sexpr(board)
    assert to_sexpr(parse(once)) == once