"""BoardState placement-accept: ripped-copper report counts arcs."""

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ui"))

from server_state import BoardState

from kicad_autorouter.board_model import ArcTrack, Point, board_model
from kicad_autorouter.sexpr import SExpr, parse_file

FIXTURES = Path(__file__).parent / "fixtures"


def _arc_node(net):
    return SExpr("arc", (
        SExpr("start", (0.0, 0.0)),
        SExpr("mid", (5.0, 5.0)),
        SExpr("end", (10.0, 0.0)),
        SExpr("width", (0.25,)),
        SExpr("layer", ("F.Cu",)),
        SExpr("net", (net,)),
    ))


@pytest.mark.unit
def test_accept_report_counts_ripped_arcs():
    """Accepting a placement move reports ripped arcs per net.

    C1 sits on GND/+12V; with nothing protected, moving it rips +12V
    copper. An injected +12V arc must vanish from model and tree and show
    up as ``ripped["+12V"]["arcs"] == 1`` while the GND arc survives.
    """
    tree = parse_file(FIXTURES / "tube111.kicad_pcb")
    model = board_model(tree)
    fp = next(f for f in model.footprints if f.ref == "C1")
    pad_nets = {p.net_name for p in fp.pads if p.net_name}
    assert {"GND", "+12V"} <= pad_nets

    plus_arc = ArcTrack(start=Point(0.0, 0.0), mid=Point(5.0, 5.0),
                        end=Point(10.0, 0.0),
                        width_mm=0.25, layer="F.Cu", net_name="+12V")
    gnd_arc = ArcTrack(start=Point(0.0, 0.0), mid=Point(5.0, -5.0),
                       end=Point(10.0, 0.0),
                       width_mm=0.25, layer="F.Cu", net_name="GND")
    st = BoardState()
    st.model = replace(model, arcs=model.arcs + (plus_arc, gnd_arc))
    st.pcb_tree = replace(
        tree, args=tree.args + (_arc_node("+12V"), _arc_node("GND")))
    st.protected_nets = {"GND"}
    st.proposal = SimpleNamespace(
        deltas={fp.uuid: (1.0, 0.0, 0.0)},
        iterations=5, final_max_disp_mm=0.5,
    )

    version = st.accept_proposal()

    assert version == 1
    assert [a.net_name for a in st.model.arcs].count("+12V") == 0
    assert [a.net_name for a in st.model.arcs].count("GND") == 1
    tree_arcs = [a.find("net").args[-1] for a in st.pcb_tree.children("arc")]
    assert "+12V" not in tree_arcs
    assert tree_arcs.count("GND") == 1
    ripped = st.last_accept_report["ripped"]["+12V"]
    assert ripped["arcs"] == 1
    assert ripped["tracks"] >= 0 and ripped["vias"] >= 0
