"""Shared synthetic boards for the shove/retry test cluster.

One GND-wall board spelling (``wall_board``), the no-GND retry variant
(``retry_wall_board``), the rail variant (``rail_board``), the trivially
routable board (``easy_board``), plus the minimal-tree stitching
(``tree_for``) and the no-DRC/no-strip params shim (``shove_params``).

Shared by ``test_shove.py``, ``test_signal_shove.py``, ``test_retry.py``.
"""

from __future__ import annotations

from dataclasses import replace

from kicad_autorouter.board_model import NetConnection
from kicad_autorouter.io import add_footprint_to_tree
from kicad_autorouter.routing.pipeline import RoutingParams
from kicad_autorouter.sexpr import SExpr, parse_file

from .helpers import make_fp, stamp_edge_cuts
from .test_pour import _board, _track

FIXTURES = "tests/fixtures"


def tree_for(model, footprints=()):
    """Minimal fixture tree + footprints + edge cuts + model tracks."""
    tree = parse_file(f"{FIXTURES}/minimal.kicad_pcb")
    for fp, lib in footprints:
        tree = add_footprint_to_tree(tree, fp, lib)
    tree = stamp_edge_cuts(tree, half=25.0)
    for t in model.tracks:
        tree = SExpr(tree.head, tree.args + (SExpr("segment", (
            SExpr("start", (t.start.x_mm, t.start.y_mm)),
            SExpr("end", (t.end.x_mm, t.end.y_mm)),
            SExpr("width", (t.width_mm,)),
            SExpr("layer", (t.layer,)),
            SExpr("net", (t.net_name,)),
        )),))
    return tree


def wall_board(wall_net="GND", extra_tracks=(), extra_nets=None,
               extra_fps=()):
    """Signal S sealed by full-height walls; GND link across the bottom."""
    fa = make_fp("A", "uuid-a", 3.0, 6.0, ("S",), hw=0.25, hh=0.25)
    fb = make_fp("B", "uuid-b", 17.0, 6.0, ("S",), hw=0.25, hh=0.25)
    g1 = make_fp("G1", "uuid-g1", 2.0, -15.0, ("GND",), hw=0.25, hh=0.25,
                 layer="B.Cu")
    g2 = make_fp("G2", "uuid-g2", 17.0, -15.0, ("GND",), hw=0.25, hh=0.25,
                 layer="B.Cu")
    fps = [fa, fb, g1, g2] + list(extra_fps)
    tracks = [
        _track(5.0, -30.0, 5.0, 30.0, wall_net, layer="F.Cu", width=0.5),
        _track(5.0, -30.0, 5.0, 30.0, wall_net, layer="B.Cu", width=0.5),
        _track(2.0, -15.0, 17.0, -15.0, "GND", layer="B.Cu", width=0.5),
    ] + list(extra_tracks)
    model = _board(fps=fps, tracks=tracks, edge=25.0)
    nets = {"S": (NetConnection("A", "1"), NetConnection("B", "1"))}
    if extra_nets:
        nets.update(extra_nets)
    model = replace(model, nets=nets)
    tree = tree_for(model, [(fp, "Test:R_0805") for fp in fps])
    return model, tree


def retry_wall_board():
    """Walled board without GND footprints/link (retry orchestration)."""
    fa = make_fp("A", "uuid-a", 3.0, 6.0, ("S",), hw=0.25, hh=0.25)
    fb = make_fp("B", "uuid-b", 17.0, 6.0, ("S",), hw=0.25, hh=0.25)
    model = _board(
        fps=[fa, fb],
        tracks=[
            _track(5.0, -30.0, 5.0, 30.0, "GND", layer="F.Cu", width=0.5),
            _track(5.0, -30.0, 5.0, 30.0, "GND", layer="B.Cu", width=0.5),
        ],
        edge=25.0,
    )
    model = replace(model, nets={
        "S": (NetConnection("A", "1"), NetConnection("B", "1")),
    })
    return model, tree_for(model, [(fa, "Test:R_0805"), (fb, "Test:R_0805")])


def easy_board():
    """Trivially routable two-part board (no-retry contract)."""
    fa = make_fp("A", "uuid-a", 3.0, 6.0, ("S",), hw=0.25, hh=0.25)
    fb = make_fp("B", "uuid-b", 17.0, 6.0, ("S",), hw=0.25, hh=0.25)
    model = _board(fps=[fa, fb], edge=25.0)
    model = replace(model, nets={
        "S": (NetConnection("A", "1"), NetConnection("B", "1")),
    })
    return model, tree_for(model, [(fa, "Test:R_0805"), (fb, "Test:R_0805")])


def rail_board():
    """GND continuity on one full-width rail; B.Cu K-wall seals via dives."""
    fa = make_fp("A", "uuid-a", 9.0, 4.0, ("S",), hw=0.25, hh=0.25)
    fb = make_fp("B", "uuid-b", 9.0, 10.0, ("S",), hw=0.25, hh=0.25)
    g1 = make_fp("G1", "uuid-g1", -20.0, 6.0, ("GND",), hw=0.25, hh=0.25)
    g2 = make_fp("G2", "uuid-g2", 20.0, 6.0, ("GND",), hw=0.25, hh=0.25)
    model = _board(
        fps=[fa, fb, g1, g2],
        tracks=[
            _track(-30.0, 6.0, 30.0, 6.0, "GND", layer="F.Cu", width=0.5),
            _track(-30.0, 6.0, 30.0, 6.0, "K", layer="B.Cu", width=0.5),
        ],
        edge=25.0,
    )
    model = replace(model, nets={
        "S": (NetConnection("A", "1"), NetConnection("B", "1")),
    })
    fps = [fa, fb, g1, g2]
    return model, tree_for(model, [(fp, "Test:R_0805") for fp in fps])


def shove_params(**kw):
    """No-DRC, no-strip params shim shared by the cluster."""
    base = dict(grid_resolution_mm=0.5, run_drc=False,
                strip_gnd_blockers=False, stitch_gnd_vias=False)
    base.update(kw)
    return RoutingParams(**base)
