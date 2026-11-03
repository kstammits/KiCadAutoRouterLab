"""Copper track arcs: parse, round-trip, render, treat-as-copper."""

import math

import pytest

from kicad_autorouter.board_model import (
    ArcTrack,
    Point,
    arcs,
    board_model,
    copper_tracks,
)
from kicad_autorouter.io import rip_up_nets
from kicad_autorouter.routing.output import apply_routes_to_tree
from kicad_autorouter.sexpr import SExpr
from kicad_autorouter.svg_render import render_board_svg


def _arc_node(start, mid, end, width, layer, net):
    return SExpr("arc", (
        SExpr("start", start),
        SExpr("mid", mid),
        SExpr("end", end),
        SExpr("width", (width,)),
        SExpr("layer", (layer,)),
        SExpr("net", (net,)),
    ))


def _board_tree():
    return SExpr("kicad_pcb", (
        SExpr("segment", (
            SExpr("start", (0.0, 0.0)),
            SExpr("end", (10.0, 0.0)),
            SExpr("width", (0.25,)),
            SExpr("layer", ("F.Cu",)),
            SExpr("net", ("SIG",)),
        )),
        _arc_node((10.0, 0.0), (15.0, 5.0), (20.0, 0.0), 0.5, "F.Cu", "PWR"),
        # Malformed arc (no mid) is skipped, like segments missing endpoints.
        SExpr("arc", (
            SExpr("start", (0.0, 0.0)),
            SExpr("end", (5.0, 0.0)),
            SExpr("width", (0.25,)),
            SExpr("layer", ("F.Cu",)),
            SExpr("net", ("SIG",)),
        )),
    ))


def test_parse_copper_arcs():
    """(arc start mid end ...) nodes parse; malformed arcs are skipped."""
    parsed = arcs(_board_tree())
    assert len(parsed) == 1
    arc = parsed[0]
    assert arc.start == Point(10.0, 0.0)
    assert arc.mid == Point(15.0, 5.0)
    assert arc.end == Point(20.0, 0.0)
    assert arc.width_mm == 0.5
    assert arc.layer == "F.Cu"
    assert arc.net_name == "PWR"


def test_board_model_carries_arcs():
    """board_model() wires copper arcs into the model (segments untouched)."""
    model = board_model(_board_tree())
    assert len(model.tracks) == 1
    assert len(model.arcs) == 1
    assert model.arcs[0].net_name == "PWR"


def test_arc_export_round_trip():
    """Emitted (arc) nodes re-parse to the same ArcTrack."""
    arc = ArcTrack(
        start=Point(10.0, 0.0), mid=Point(15.0, 5.0), end=Point(20.0, 0.0),
        width_mm=0.5, layer="F.Cu", net_name="PWR",
    )
    tree = apply_routes_to_tree(SExpr("kicad_pcb", ()), [], [], arcs=[arc])
    node = tree.find("arc")
    assert node is not None
    assert node.find("mid") is not None
    (back,) = arcs(tree)
    assert back == arc


def test_arc_chords_span_the_arc():
    """Analysis chords share the arc endpoints and approximate its length."""
    model = board_model(_board_tree())
    chords = copper_tracks(model)
    # 1 straight track + 16 sampled arc chords.
    assert len(chords) == 1 + 16
    arc_chords = chords[1:]
    # Circle-fit float dust (~1e-16) — approximate endpoints.
    assert arc_chords[0].start.x_mm == pytest.approx(10.0)
    assert arc_chords[0].start.y_mm == pytest.approx(0.0)
    assert arc_chords[-1].end.x_mm == pytest.approx(20.0)
    assert arc_chords[-1].end.y_mm == pytest.approx(0.0)
    straight = math.hypot(20.0 - 10.0, 0.0)
    chained = sum(
        math.hypot(c.end.x_mm - c.start.x_mm, c.end.y_mm - c.start.y_mm)
        for c in arc_chords
    )
    assert chained > straight  # the arc bulges off-chord
    assert all(c.net_name == "PWR" and c.layer == "F.Cu" for c in arc_chords)


def test_rip_up_removes_arcs():
    """Rip-up drops arcs on affected nets and keeps the rest."""
    tree = apply_routes_to_tree(
        SExpr("kicad_pcb", ()),
        [], [],
        arcs=[ArcTrack(
            start=Point(0.0, 0.0), mid=Point(5.0, 5.0), end=Point(10.0, 0.0),
            width_mm=0.25, layer="F.Cu", net_name="SIG",
        )],
    )
    assert len(list(tree.children("arc"))) == 1
    ripped = rip_up_nets(tree, {"SIG"})
    assert list(ripped.children("arc")) == []
    kept = rip_up_nets(tree, {"OTHER"})
    assert len(list(kept.children("arc"))) == 1


def test_svg_renders_arc_copper():
    """Arc copper renders as a selectable path carrying its net."""
    model = board_model(_board_tree())
    svg = render_board_svg(model)
    assert 'class="track arc-track"' in svg
    assert 'data-net="PWR"' in svg
