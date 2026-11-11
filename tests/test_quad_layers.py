"""Load + display all copper layers; route outer-only.

Markers: minimal, server
"""

from dataclasses import replace
from pathlib import Path

import pytest

from kicad_autorouter.board_model import (
    BoardModel,
    Point,
    Track,
    Via,
    Zone,
    board_model,
    copper_layers,
)
from kicad_autorouter.routing.grid import create_grid_from_model
from kicad_autorouter.routing.obstacles import BLOCKED, build_occupancy_grid
from kicad_autorouter.sexpr import parse as parse_sexpr
from kicad_autorouter.svg_render import render_board_svg

FIXTURES = Path(__file__).parent / "fixtures"


def _minimal_tree():
    from kicad_autorouter.sexpr import parse_file

    return parse_file(FIXTURES / "minimal.kicad_pcb")


@pytest.mark.minimal
def test_copper_layers_two_layer_fixture():
    tree = _minimal_tree()
    assert copper_layers(tree) == ("F.Cu", "B.Cu")
    assert board_model(tree).copper_layers == ("F.Cu", "B.Cu")


@pytest.mark.minimal
def test_copper_layers_stackup_order():
    tree = parse_sexpr(
        '(kicad_pcb (setup (stackup'
        ' (layer "B.Cu" (type "copper"))'
        ' (layer "In2.Cu" (type "copper"))'
        ' (layer "dielectric 1" (type "core"))'
        ' (layer "In1.Cu" (type "copper"))'
        ' (layer "F.Cu" (type "copper"))'
        ' (layer "F.SilkS" (type "Top Silk Screen"))'
        ')) (layers (0 "F.Cu" signal) (1 "In1.Cu" signal)'
        ' (2 "In2.Cu" signal) (3 "B.Cu" signal)))'
    )
    assert copper_layers(tree) == ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")


@pytest.mark.minimal
def test_copper_layers_header_fallback():
    tree = parse_sexpr(
        '(kicad_pcb (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (3 "B.Cu" signal)))'
    )
    assert copper_layers(tree) == ("F.Cu", "In1.Cu", "B.Cu")


@pytest.mark.minimal
def test_inner_track_ignored_through_via_blocks():
    model = board_model(_minimal_tree())
    grid = create_grid_from_model(model, resolution_mm=0.5, margin_mm=2.0)
    base = build_occupancy_grid(model, grid)
    inner_track = Track(
        start=Point(60.0, 100.0), end=Point(70.0, 100.0),
        width_mm=0.25, layer="In1.Cu", net_name="SIG",
    )
    with_inner = build_occupancy_grid(replace(model, tracks=model.tracks + (inner_track,)), grid)
    assert (with_inner == base).all()
    outer_track = replace(inner_track, layer="F.Cu")
    with_outer = build_occupancy_grid(replace(model, tracks=model.tracks + (outer_track,)), grid)
    assert (with_outer != base).any()
    # Inner-only blind via: no outer blockage. Through via: blocks F/B.
    blind = Via(position=Point(65.0, 102.0), size_mm=0.8, drill_mm=0.4,
                layers=("In1.Cu", "In2.Cu"), net_name="SIG")
    assert (build_occupancy_grid(replace(model, vias=model.vias + (blind,)), grid) == base).all()
    through = replace(blind, layers=("F.Cu", "B.Cu"))
    assert (build_occupancy_grid(replace(model, vias=model.vias + (through,)), grid) >= BLOCKED).any()


@pytest.mark.minimal
def test_svg_renders_all_copper_layers_with_tints():
    model = board_model(_minimal_tree())
    quad = replace(
        model,
        copper_layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"),
        tracks=model.tracks + (
            Track(start=Point(60.0, 150.0), end=Point(70.0, 150.0),
                  width_mm=0.25, layer="F.Cu", net_name="SIG"),
            Track(start=Point(60.0, 151.0), end=Point(70.0, 151.0),
                  width_mm=0.25, layer="In1.Cu", net_name="SIG"),
        ),
        zones=model.zones + (
            Zone(uuid="in2-test", layers=("In2.Cu",),
                 polygon=(Point(60, 150), Point(70, 150), Point(70, 160), Point(60, 160)),
                 net_name="GND"),
        ),
        vias=model.vias + (
            Via(position=Point(65.0, 152.0), size_mm=0.8, drill_mm=0.4,
                layers=("F.Cu", "B.Cu"), net_name="SIG"),
        ),
    )
    svg = render_board_svg(quad)
    assert 'id="layer-In1.Cu-tracks"' in svg
    assert 'id="layer-In2.Cu-zones"' in svg
    # Outer styles unchanged (2-layer boards render pixel-identical).
    assert 'id="layer-F.Cu-tracks" fill="none" stroke-linecap="round" opacity="1.0"' in svg
    assert 'data-layers="F.Cu B.Cu"' in svg


@pytest.mark.minimal
@pytest.mark.server
@pytest.mark.minimal
def test_server_state_reports_copper_layers():
    import json
    import sys
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ui"))
    import server

    server.STATE.model = None
    server.STATE.version = 0
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        data = (FIXTURES / "minimal.kicad_pcb").read_bytes()
        req = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_address[1]}/api/load?name=minimal",
            data=data, method="POST")
        with urllib.request.urlopen(req):
            pass
        with urllib.request.urlopen(
                f"http://127.0.0.1:{httpd.server_address[1]}/api/state") as r:
            st = json.load(r)
        assert st["copper_layers"] == ["F.Cu", "B.Cu"]
    finally:
        httpd.shutdown()
        server.STATE.model = None
        server.STATE.version = 0


@pytest.mark.minimal
@pytest.mark.server
def test_board_svg_colors_param_accepted():
    import json
    import sys
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ui"))
    import server

    server.STATE.model = None
    server.STATE.version = 0
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        data = (FIXTURES / "minimal.kicad_pcb").read_bytes()
        req = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_address[1]}/api/load?name=minimal",
            data=data, method="POST")
        with urllib.request.urlopen(req):
            pass
        for colors in ("net", "layer"):
            with urllib.request.urlopen(
                f"http://127.0.0.1:{httpd.server_address[1]}/api/board.svg?colors={colors}"
            ) as r:
                assert r.status == 200
                assert r.headers.get("Content-Type") == "image/svg+xml"
    finally:
        httpd.shutdown()
        server.STATE.model = None
        server.STATE.version = 0


@pytest.mark.minimal
def test_board_model_preserves_copper_layers_on_ripup():
    from kicad_autorouter.routing.pipeline import _rip_up_nets_from_model

    model = replace(
        board_model(_minimal_tree()),
        copper_layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"),
    )
    assert _rip_up_nets_from_model(model, ["NONEXISTENT"]).copper_layers == (
        "F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
