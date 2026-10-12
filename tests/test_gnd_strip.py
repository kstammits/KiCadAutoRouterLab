"""GND-only blocker eviction: signals never rip each other; GND rebuilds via stitcher.

- ``can_rip`` gate: GND victims evictable, signal/power victims never.
- ``_strip_gnd_blockers_from_model`` removes only ground-net copper.
- ``route_nets`` on a board with a GND via wall + an unrelated signal
  track: the other signal's copper survives byte-identical, GND is
  stripped (``gnd_stripped > 0``) and rebuilt by the stitcher.
- ``strip_gnd_blockers=False`` preserves GND copper.
- tube111 helper characterization: strip removes 76 vias + 15 tracks.
"""

from dataclasses import replace
from pathlib import Path

import pytest

from kicad_autorouter.board_model import NetConnection, Point, Via, board_model
from kicad_autorouter.routing.output import apply_routes_to_tree
from kicad_autorouter.routing.pipeline import (
    RoutingParams,
    _strip_gnd_blockers_from_model,
    route_nets,
)
from kicad_autorouter.routing.scheduler import RipUpManager, is_rippupable_victim
from kicad_autorouter.sexpr import SExpr, parse_file

from .helpers import make_fp
from .test_pour import _board, _rect_zone, _track

FIXTURES = Path(__file__).parent / "fixtures"


def _signal_board():
    # S must cross the board; a GND via sits mid-field; unrelated net K
    # owns a track far away that must survive untouched.
    fa = make_fp("A", "uuid-a", 3.0, 6.0, ("S",), hw=0.25, hh=0.25)
    fb = make_fp("B", "uuid-b", 17.0, 6.0, ("S",), hw=0.25, hh=0.25)
    model = _board(
        fps=[fa, fb],
        zones=[_rect_zone("z1", "GND", ("F.Cu", "B.Cu"), x1=20.0, y1=12.0)],
        tracks=[
            _track(10.0, -2.0, 10.0, 14.0, "X"),  # foreign wall severing the pour
            _track(1.0, 11.0, 5.0, 11.0, "K"),  # bystander signal track
        ],
        vias=[Via(position=Point(10.0, 6.0), size_mm=0.8, drill_mm=0.4,
                   layers=("F.Cu", "B.Cu"), net_name="GND")],
        edge=25.0,
    )
    return replace(model, nets={
        "S": (NetConnection("A", "1"), NetConnection("B", "1")),
    })


@pytest.mark.unit
class TestRipGate:
    def test_gnd_victim_evictable(self):
        assert is_rippupable_victim("GND")
        assert is_rippupable_victim("GND_PWR")
        assert RipUpManager().can_rip("S", "GND")

    def test_signal_victim_never(self):
        assert not is_rippupable_victim("S")
        assert not is_rippupable_victim("+12V")
        assert not is_rippupable_victim(None)
        rip = RipUpManager()
        assert not rip.can_rip("S", "K")
        assert not rip.can_rip("S", "+12V")

    def test_same_net_always(self):
        assert RipUpManager().can_rip("S", "S")


@pytest.mark.unit
class TestStripHelper:
    def test_removes_only_gnd(self):
        model = _signal_board()
        stripped, removed = _strip_gnd_blockers_from_model(model)
        assert removed == 1  # the single GND via; X/K tracks kept
        assert len(stripped.vias) == 0
        assert {t.net_name for t in stripped.tracks} == {"X", "K"}

    def test_empty_noop(self):
        model = _board(zones=[_rect_zone("z1", "GND", ("F.Cu",))])
        _, removed = _strip_gnd_blockers_from_model(model)
        assert removed == 0


@pytest.mark.unit
class TestRouteNetsGndOnly:
    def _tree_for(self, model):
        """Minimal (kicad_pcb …) tree carrying the model's copper for writeback."""
        args = []
        for t in model.tracks:
            args.append(SExpr("segment", (
                SExpr("start", (t.start.x_mm, t.start.y_mm)),
                SExpr("end", (t.end.x_mm, t.end.y_mm)),
                SExpr("width", (t.width_mm,)),
                SExpr("layer", (t.layer,)),
                SExpr("net", (t.net_name,)),
            )))
        for v in model.vias:
            args.append(SExpr("via", (
                SExpr("at", (v.position.x_mm, v.position.y_mm)),
                SExpr("size", (v.size_mm,)),
                SExpr("drill", (v.drill_mm,)),
                SExpr("layers", tuple(v.layers)),
                SExpr("net", (v.net_name,)),
            )))
        return SExpr("kicad_pcb", tuple(args))

    def test_signal_routing_preserves_bystander_and_strips_gnd(self):
        model = _signal_board()
        res, _, _ = route_nets(
            model, ["S"], set(), params=RoutingParams(run_drc=False),
            original_pcb_tree=self._tree_for(model),
        )
        assert res.nets_routed == 1
        assert res.gnd_stripped == 1
        final = board_model(res.pcb_tree)
        # Bystander signal copper survives exactly.
        assert [(t.start, t.end, t.net_name) for t in final.tracks if t.net_name == "K"] == \
               [(t.start, t.end, t.net_name) for t in model.tracks if t.net_name == "K"]
        # Foreign wall X survives (never a victim).
        assert any(t.net_name == "X" for t in final.tracks)
        # Old GND via is gone; stitcher rebuilt continuity from scratch.
        assert not any(
            v.net_name == "GND" and v.position.x_mm == pytest.approx(10.0)
            and v.position.y_mm == pytest.approx(6.0)
            for v in final.vias
        )

    def test_strip_disabled_preserves_gnd(self):
        model = _signal_board()
        res, _, _ = route_nets(
            model, ["S"], set(),
            params=RoutingParams(run_drc=False, strip_gnd_blockers=False,
                                 stitch_gnd_vias=False),
            original_pcb_tree=self._tree_for(model),
        )
        assert res.gnd_stripped == 0
        final = board_model(res.pcb_tree)
        assert any(
            v.net_name == "GND" and v.position.x_mm == pytest.approx(10.0)
            for v in final.vias
        )

    def test_params_roundtrip(self):
        p = RoutingParams(strip_gnd_blockers=False)
        assert RoutingParams.from_dict(p.to_dict()).strip_gnd_blockers is False
        assert RoutingParams.from_dict({}).strip_gnd_blockers is True


class TestTube111StripCharacterization:
    """Recorded strip counts on the pre-routed fixture (not a spec).

    tube111 carries 76 GND vias + 15 GND tracks; all are evictable. If a
    copper-model change moves these, update AND justify.
    """

    def test_tube111_gnd_counts(self):
        model = board_model(parse_file(FIXTURES / "tube111.kicad_pcb"))
        _, removed = _strip_gnd_blockers_from_model(model)
        assert removed == 91
