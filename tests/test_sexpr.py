from pathlib import Path

import pytest

from kicad_autorouter.sexpr import SExpr, SExprError, parse, parse_file


def test_simple_list():
    node = parse("(version 20241229)")
    assert node.head == "version"
    assert node.args == (20241229,)


def test_nested_lists_and_scalars():
    node = parse('(footprint "X:Y" (at 58.57 -3.35) (layer "F.Cu"))')
    assert node.head == "footprint"
    at = node.find("at")
    assert at is not None and at.args == (58.57, -3.35)
    layer = node.find("layer")
    assert layer is not None and layer.args == ("F.Cu",)


def test_symbols_vs_strings():
    # Bare symbol becomes head; quoted string stays an arg value.
    node = parse("(fill no)")
    assert node.head == "fill"
    assert node.args == ("no",)

    anon = parse('("F.Cu" 1.6)')
    assert anon.head is None
    assert anon.args == ("F.Cu", 1.6)


def test_number_kinds():
    assert parse("(a 0)").args == (0,)
    assert parse("(a 1.5)").args == (1.5,)
    assert parse("(a -2)").args == (-2,)
    # Hex with underscore separators, as used by layerselection masks.
    hex_node = parse("(layerselection 0x0000_ffff)")
    assert hex_node.args == (65535,)


def test_empty_list_and_string_escapes():
    assert parse("()").args == ()
    node = parse('(name "a\\nb\\"c")')
    assert node.args[0] == 'a\nb"c'


def test_children_iteration_order():
    node = parse("(root (pad 1) (pad 2) (track 3))")
    pads = list(node.children("pad"))
    assert [p.args for p in pads] == [(1,), (2,)]
    assert node.find("track").args == (3,)


def test_error_unterminated_string():
    with pytest.raises(SExprError):
        parse('(name "abc)')


def test_error_trailing_content_after_root():
    with pytest.raises(SExprError):
        parse("(a 1) (b 2)")


def test_error_no_top_level_list():
    with pytest.raises(SExprError):
        parse("just a symbol")


FIXTURE = Path(__file__).parent / "fixtures" / "minimal.kicad_pcb"


def _reference(fp: SExpr):
    for prop in fp.children("property"):
        if len(prop.args) >= 2 and prop.args[0] == "Reference":
            return prop.args[1]
    return None


def test_parse_minimal_fixture():
    board = parse_file(FIXTURE)
    assert board.head == "kicad_pcb"

    version = board.find("version")
    assert version is not None and version.args == (20241229,)

    layers = board.find("layers")
    assert layers is not None
    # Each layer entry is an anonymous list: (id "name" kind).
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
