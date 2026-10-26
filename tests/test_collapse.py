"""collapse_to_two_layer: 4-layer content → genuine 2-layer board.

Synthetic trees only (no new fixtures). Markers: minimal
"""

from pathlib import Path

import pytest

from kicad_autorouter.board_model import copper_layers
from kicad_autorouter.io import collapse_to_two_layer
from kicad_autorouter.sexpr import parse as parse_sexpr, parse_file, to_sexpr
from kicad_autorouter.validate import (
    KicadCliNotFound,
    find_kicad_cli,
    validate_pcb,
)

FIXTURES = Path("tests/fixtures")

FOUR_LAYER = """(kicad_pcb
  (version 20221018) (generator "test")
  (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (3 "B.Cu" signal))
  (setup (stackup
    (layer "F.Cu" (type "copper") (thickness 0.035))
    (layer "dielectric 1" (type "core") (thickness 0.2))
    (layer "In1.Cu" (type "copper") (thickness 0.017))
    (layer "dielectric 2" (type "prepreg") (thickness 1.0))
    (layer "In2.Cu" (type "copper") (thickness 0.017))
    (layer "dielectric 3" (type "core") (thickness 0.2))
    (layer "B.Cu" (type "copper") (thickness 0.035))
    (copper_finish "None")))
  (net 0 "")
  (net 1 "GND")
  (net 2 "SIG")
  (segment (start 0 0) (end 10 0) (width 0.25) (layer "F.Cu") (net 2 "SIG") (uuid "s-outer"))
  (segment (start 0 1) (end 10 1) (width 0.25) (layer "In1.Cu") (net 2 "SIG") (uuid "s-inner"))
  (segment (start 0 2) (end 10 2) (width 0.25) (layer "B.Cu") (net 1 "GND") (uuid "s-back"))
  (via (at 5 5) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1 "GND") (uuid "v-through"))
  (via (at 6 6) (size 0.8) (drill 0.4) (layers "F.Cu" "In1.Cu") (net 2 "SIG") (uuid "v-blind"))
  (via (at 7 7) (size 0.8) (drill 0.4) (layers "In1.Cu" "In2.Cu") (net 2 "SIG") (uuid "v-buried"))
  (zone (net 1 "GND") (layers "F.Cu" "In1.Cu") (uuid "z-multi")
    (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10)))
    (filled_polygon (layer "F.Cu") (pts (xy 0 0) (xy 5 0) (xy 5 5)))
    (filled_polygon (layer "In1.Cu") (pts (xy 0 0) (xy 5 0) (xy 5 5))))
  (zone (net 1 "GND") (layer "In2.Cu") (uuid "z-inner")
    (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10))))
  (zone (net 1 "GND") (layer "B.Cu") (uuid "z-outer")
    (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10))))
  (zone (net 0) (net_name "") (layers "In1.Cu" "In2.Cu") (uuid "z-keepout")
    (keepout (tracks not_allowed) (vias not_allowed) (pads not_allowed)
     (copperpours allowed) (footprints not_allowed))
    (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10))))
)"""


def test_collapse_removes_inner_copper():
    tree = parse_sexpr(FOUR_LAYER)
    new_tree, report = collapse_to_two_layer(tree)
    text = to_sexpr(new_tree)
    assert "In1.Cu" not in text
    assert "In2.Cu" not in text
    assert copper_layers(new_tree) == ("F.Cu", "B.Cu")
    assert report["tracks_removed"] == 1
    assert report["zones_removed"] == 2  # inner-only pour + inner keepout
    assert report["zones_restricted"] == 1  # GND F+In1 → F
    assert report["vias_converted"] == 2  # blind + buried restacked
    assert report["vias_dropped"] == 0  # never-drop: every via survives
    assert report["affected_nets"] == ["GND", "SIG"]
    assert list(report["stackup_before"]) == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert list(report["stackup_after"]) == ["F.Cu", "B.Cu"]
    assert report["impedance_note"]


def test_collapse_restacks_every_via_never_drops():
    from kicad_autorouter.board_model import vias

    new_tree, _ = collapse_to_two_layer(parse_sexpr(FOUR_LAYER))
    ## through + blind + buried = 3 vias, all F.Cu–B.Cu now.
    assert len(vias(new_tree)) == 3
    assert all(v.layers == ("F.Cu", "B.Cu") for v in vias(new_tree))


def test_collapse_zone_restrict_vs_delete_shapes():
    new_tree, _ = collapse_to_two_layer(parse_sexpr(FOUR_LAYER))
    # Multi-layer GND restricted to the singular outer form (KiCad convention).
    multi = next(z for z in new_tree.children("zone")
                 if z.find("uuid") is not None and z.find("uuid").args[0] == "z-multi")
    assert multi.find("layers") is None
    assert multi.find("layer") is not None
    assert multi.find("layer").args[0] == "F.Cu"
    # Inner fill cache dropped, outer fill kept.
    fills = [f.find("layer").args[0] for f in multi.children("filled_polygon")]
    assert fills == ["F.Cu"]
    # Untouched outer zone keeps its singular node object as-is.
    outer = next(z for z in new_tree.children("zone")
                 if z.find("uuid") is not None and z.find("uuid").args[0] == "z-outer")
    assert outer.find("layer").args[0] == "B.Cu"
    assert len(list(new_tree.children("zone"))) == 2


def test_collapse_drops_inner_arcs_annotations_and_top_fills():
    tree = parse_sexpr(
        "(kicad_pcb"
        ' (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (3 "B.Cu" signal))'
        ' (net 0 "")'
        ' (arc (start 0 0) (mid 1 1) (end 2 0) (width 0.25) (layer "In1.Cu") (net 0) (uuid "a-inner"))'
        ' (arc (start 0 0) (mid 1 1) (end 2 0) (width 0.25) (layer "F.Cu") (net 0) (uuid "a-outer"))'
        ' (gr_text "x" (at 0 0) (layer "In1.Cu") (uuid "g-inner"))'
        ' (gr_text "y" (at 0 0) (layer "Cmts.User") (uuid "g-user"))'
        ' (filled_polygon (layer "In1.Cu") (pts (xy 0 0) (xy 1 0))))'
    )
    new_tree, report = collapse_to_two_layer(tree)
    text = to_sexpr(new_tree)
    assert "a-inner" not in text
    assert "a-outer" in text
    assert "g-inner" not in text
    assert "g-user" in text
    assert "filled_polygon" not in text
    assert report["tracks_removed"] == 3
    # Structural-only guarantee: footprints/pads/netclasses untouched.
    assert "net 0" in text


def test_collapse_keeps_layer_less_nodes():
    # Unknown layer is not inner copper: layer-less tracks/arcs survive
    # (same keep-behavior as fills/zones), layer-less vias pass through.
    tree = parse_sexpr(
        "(kicad_pcb"
        ' (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (3 "B.Cu" signal))'
        ' (net 0 "")'
        ' (segment (start 0 0) (end 1 0) (width 0.25) (net 0) (uuid "s-nolayer"))'
        ' (arc (start 0 0) (mid 1 1) (end 2 0) (width 0.25) (net 0) (uuid "a-nolayer"))'
        ' (via (at 5 5) (size 0.8) (drill 0.4) (net 0) (uuid "v-nolayers")))'
    )
    new_tree, report = collapse_to_two_layer(tree)
    text = to_sexpr(new_tree)
    assert "s-nolayer" in text
    assert "a-nolayer" in text
    assert "v-nolayers" in text
    assert report["tracks_removed"] == 0


def test_collapse_idempotent():
    once, report1 = collapse_to_two_layer(parse_sexpr(FOUR_LAYER))
    twice, report2 = collapse_to_two_layer(once)
    assert to_sexpr(twice) == to_sexpr(once)
    assert report2["tracks_removed"] == 0
    assert report2["zones_removed"] == 0
    assert report2["zones_restricted"] == 0
    assert report2["vias_converted"] == 0
    assert report2["affected_nets"] == []
    assert report1["vias_dropped"] == 0


def test_collapse_two_layer_input_byte_identical():
    tree = parse_file(FIXTURES / "minimal.kicad_pcb")
    before = to_sexpr(tree)
    new_tree, report = collapse_to_two_layer(tree)
    assert new_tree is tree
    assert to_sexpr(new_tree) == before
    assert report["impedance_note"] is None
    assert report["stackup_after"] == report["stackup_before"]


@pytest.fixture(scope="module")
def cli() -> str:
    try:
        return find_kicad_cli()
    except KicadCliNotFound:
        pytest.skip("kicad-cli not installed")


def test_collapsed_grafted_board_loads_in_kicad(cli, tmp_path):
    """Minimal fixture + 4-layer stackup/via/segment graft → collapse → loads."""
    from dataclasses import replace

    from kicad_autorouter.sexpr import SExpr

    tree = parse_file(FIXTURES / "minimal.kicad_pcb")
    four_stackup = parse_sexpr(
        '(stackup (layer "F.Cu" (type "copper") (thickness 0.035))'
        ' (layer "dielectric 1" (type "core") (thickness 1.5))'
        ' (layer "In1.Cu" (type "copper") (thickness 0.017))'
        ' (layer "dielectric 2" (type "prepreg") (thickness 0.2))'
        ' (layer "B.Cu" (type "copper") (thickness 0.035)))'
    )
    graft = [
        SExpr("segment", (SExpr("start", (60.0, 60.0)),
                          SExpr("end", (70.0, 60.0)), SExpr("width", (0.25,)),
                          SExpr("layer", ("In1.Cu",)), SExpr("net", (0,)),
                          SExpr("uuid", ("graft-seg",)))),
        SExpr("via", (SExpr("at", (65.0, 65.0)), SExpr("size", (0.8,)),
                      SExpr("drill", (0.4,)), SExpr("layers", ("F.Cu", "In1.Cu")),
                      SExpr("net", (0,)), SExpr("uuid", ("graft-via",)))),
    ]
    setup = tree.find("setup")
    new_setup = replace(
        setup,
        args=tuple(four_stackup if c is setup.find("stackup") else c
                   for c in setup.args),
    )
    grafted = replace(
        tree,
        args=tuple(new_setup if c is setup else c for c in tree.args) + tuple(graft),
    )
    assert copper_layers(grafted) == ("F.Cu", "In1.Cu", "B.Cu")
    collapsed, report = collapse_to_two_layer(grafted)
    assert report["tracks_removed"] == 1
    assert report["vias_converted"] == 1
    out = tmp_path / "collapsed.kicad_pcb"
    out.write_text(to_sexpr(collapsed) + "\n", encoding="utf-8")
    # Footprint pad wildcard templates (e.g. "In1.Cu".."In30.Cu") pass
    # through untouched by design — only structural copper must be clean.
    from kicad_autorouter.board_model import tracks, vias, zones

    assert copper_layers(collapsed) == ("F.Cu", "B.Cu")
    assert all(t.layer in ("F.Cu", "B.Cu") for t in tracks(collapsed))
    assert all(v.layers == ("F.Cu", "B.Cu") for v in vias(collapsed))
    assert all(
        not z.layers or set(z.layers) <= {"F.Cu", "B.Cu"}
        for z in zones(collapsed)
    )
    assert validate_pcb(out, cli=cli).loaded
