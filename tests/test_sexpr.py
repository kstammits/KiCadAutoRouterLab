from pathlib import Path

from kicad_autorouter.sexpr import SExpr, parse, parse_file


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