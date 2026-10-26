from pathlib import Path

import pytest

from kicad_autorouter.sexpr import SExpr, Symbol, parse, parse_file, to_sexpr, tokenize


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
    board = parse_file(FIXTURE)
    once = to_sexpr(board)
    assert to_sexpr(parse(once)) == once