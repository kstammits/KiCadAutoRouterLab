"""Inlet-anchored DC-resistance screening (report only).

- Resistance math pins (track/via closed forms).
- Graph behavior: direct track (single path -> attention), parallel
  tracks (redundant -> pass), disconnected (fail), no-inlet (fail),
  pour-mediated continuity, cross-region isolation.
"""

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ui"))

from kicad_autorouter.board_model import (
    BoardModel,
    BoardRegion,
    NetConnection,
    Point,
    Track,
    Zone,
)
from kicad_autorouter.routing.grid import create_grid_from_model
from kicad_autorouter.routing.impedance import (
    BridgeSpec,
    InletSpec,
    analyze_power_gnd,
    suggest_header_mates,
    track_resistance_mohm,
    via_resistance_mohm,
)

from .helpers import make_fp

FIXTURES = Path(__file__).parent / "fixtures"


def _zone(uuid, net, layers=("F.Cu",), x0=0.0, y0=0.0, x1=40.0, y1=30.0):
    return Zone(
        uuid=uuid, layers=tuple(layers),
        polygon=(Point(x0, y0), Point(x1, y0), Point(x1, y1), Point(x0, y1)),
        net_name=net, clearance_mm=0.5, min_thickness_mm=0.25,
    )


def _board(fps=(), zones=(), tracks=(), vias=(), edge=50.0, regions=None, region_of=None):
    fps = tuple(fps)
    nets = {}
    for fp in fps:
        for pad in fp.pads:
            if pad.net_name:
                nets.setdefault(pad.net_name, []).append(
                    NetConnection(ref=fp.ref, pad_number=pad.number))
    kwargs = dict(
        footprints=fps,
        keepout_zones=(),
        zones=tuple(zones),
        tracks=tuple(tracks),
        vias=tuple(vias),
        nets={k: tuple(v) for k, v in nets.items()},
        by_ref={fp.ref: fp for fp in fps},
        edge_cuts=(
            (Point(-edge, -edge), Point(edge, -edge)),
            (Point(edge, -edge), Point(edge, edge)),
            (Point(edge, edge), Point(-edge, edge)),
            (Point(-edge, edge), Point(-edge, -edge)),
        ),
        footprint_region={fp.uuid: 0 for fp in fps},
    )
    if regions is not None:
        kwargs["board_regions"] = tuple(regions)
        kwargs["footprint_region"] = dict(region_of or {})
    return BoardModel(**kwargs)


def _track(x1, y1, x2, y2, net, layer="F.Cu", width=1.0):
    return Track(start=Point(x1, y1), end=Point(x2, y2),
                 width_mm=width, layer=layer, net_name=net)


@pytest.mark.unit
class TestResistanceMath:
    def test_track_10mm_025mm(self):
        assert track_resistance_mohm(10.0, 0.25) == pytest.approx(19.2, rel=0.01)

    def test_track_scales_with_geometry(self):
        assert track_resistance_mohm(20.0, 0.25) == pytest.approx(
            2 * track_resistance_mohm(10.0, 0.25))
        assert track_resistance_mohm(10.0, 0.5) == pytest.approx(
            track_resistance_mohm(10.0, 0.25) / 2)

    def test_via_04mm_about_one_mohm(self):
        assert via_resistance_mohm(0.4) == pytest.approx(0.91, rel=0.15)

    def test_degenerate_inputs_zero(self):
        assert track_resistance_mohm(0.0, 0.25) == 0.0
        assert via_resistance_mohm(0.0) == 0.0


@pytest.mark.unit
class TestGraphBehavior:
    def _two_pad_board(self, tracks):
        j1 = make_fp("J1", "uuid-j1", 5.0, 15.0, ("GND",))
        j2 = make_fp("J2", "uuid-j2", 25.0, 15.0, ("GND",))
        return _board(fps=[j1, j2], tracks=tracks)

    def test_direct_track_single_path_attention(self):
        model = self._two_pad_board([_track(5.0, 15.0, 25.0, 15.0, "GND")])
        grid = create_grid_from_model(model, resolution_mm=0.5)
        report = analyze_power_gnd(model, grid, gnd_inlets=[InletSpec("J1", "1")])
        net = next(n for n in report.nets if n.net == "GND")
        tgt = next(p for p in net.pads if p.ref == "J2")
        assert tgt.connected
        assert tgt.r_mohm == pytest.approx(track_resistance_mohm(20.0, 1.0), rel=0.2)
        assert tgt.disjoint_paths == 1
        assert tgt.verdict == "attention" and tgt.reason == "single-path"

    def test_parallel_tracks_redundant_pass(self):
        model = self._two_pad_board([
            _track(5.0, 15.0, 25.0, 15.0, "GND"),
            _track(5.0, 15.0, 25.0, 15.0, "GND"),
        ])
        grid = create_grid_from_model(model, resolution_mm=0.5)
        report = analyze_power_gnd(model, grid, gnd_inlets=[InletSpec("J1", "1")])
        net = next(n for n in report.nets if n.net == "GND")
        tgt = next(p for p in net.pads if p.ref == "J2")
        assert tgt.connected and tgt.disjoint_paths == 2 and tgt.verdict == "pass"

    def test_disconnected_pad_fails(self):
        model = self._two_pad_board([])
        grid = create_grid_from_model(model, resolution_mm=0.5)
        report = analyze_power_gnd(model, grid, gnd_inlets=[InletSpec("J1", "1")])
        net = next(n for n in report.nets if n.net == "GND")
        tgt = next(p for p in net.pads if p.ref == "J2")
        assert not tgt.connected and tgt.verdict == "fail" and tgt.reason == "disconnected"

    def test_no_inlet_fails(self):
        model = self._two_pad_board([_track(5.0, 15.0, 25.0, 15.0, "GND")])
        grid = create_grid_from_model(model, resolution_mm=0.5)
        report = analyze_power_gnd(model, grid, gnd_inlets=[])
        net = next(n for n in report.nets if n.net == "GND")
        assert all(p.verdict == "fail" and p.reason == "no-inlet" for p in net.pads)

    def test_over_threshold_fails(self):
        model = self._two_pad_board([_track(5.0, 15.0, 25.0, 15.0, "GND", width=0.1)])
        grid = create_grid_from_model(model, resolution_mm=0.5)
        report = analyze_power_gnd(model, grid, gnd_inlets=[InletSpec("J1", "1")],
                                   gnd_max_mohm=1.0)
        net = next(n for n in report.nets if n.net == "GND")
        tgt = next(p for p in net.pads if p.ref == "J2")
        assert tgt.verdict == "fail" and tgt.reason == "over-threshold"

    def test_pour_mediated_continuity(self):
        j1 = make_fp("J1", "uuid-j1", 10.0, 15.0, ("GND",))
        j2 = make_fp("J2", "uuid-j2", 30.0, 15.0, ("GND",))
        model = _board(fps=[j1, j2], zones=[_zone("z1", "GND")])
        grid = create_grid_from_model(model, resolution_mm=0.5)
        report = analyze_power_gnd(model, grid, gnd_inlets=[InletSpec("J1", "1")])
        net = next(n for n in report.nets if n.net == "GND")
        tgt = next(p for p in net.pads if p.ref == "J2")
        assert tgt.connected and tgt.r_mohm is not None and tgt.r_mohm < 5.0

    def test_cross_region_needs_bridge(self):
        from kicad_autorouter.routing.impedance import BridgeSpec
        ra = BoardRegion(uuid="ra",
                         polygon=(Point(0, 0), Point(40, 0), Point(40, 30), Point(0, 30)),
                         bbox=(0.0, 40.0, 0.0, 30.0))
        rb = BoardRegion(uuid="rb",
                         polygon=(Point(60, 0), Point(100, 0), Point(100, 30), Point(60, 30)),
                         bbox=(60.0, 100.0, 0.0, 30.0))
        j1 = make_fp("J1", "uuid-j1", 5.0, 15.0, ("GND",), tht=True)
        j2 = make_fp("J2", "uuid-j2", 65.0, 15.0, ("GND",), tht=True)
        model = _board(fps=[j1, j2],
                       zones=[_zone("za", "GND"),
                              _zone("zb", "GND", x0=60.0, y0=0.0, x1=100.0, y1=30.0)],
                       regions=[ra, rb], region_of={"uuid-j1": 0, "uuid-j2": 1})
        grid = create_grid_from_model(model, resolution_mm=0.5)
        # Panels are electrically isolated: one inlet cannot reach the sibling.
        report = analyze_power_gnd(model, grid, gnd_inlets=[InletSpec("J1", "1")])
        net = next(n for n in report.nets if n.net == "GND")
        tgt = next(p for p in net.pads if p.ref == "J2")
        assert not tgt.connected and tgt.reason == "disconnected"
        # A declared mated pair (pin header) joins the panels.
        report = analyze_power_gnd(
            model, grid, gnd_inlets=[InletSpec("J1", "1")],
            bridges=[BridgeSpec("J1", "1", "J2", "1", 2.0)])
        net = next(n for n in report.nets if n.net == "GND")
        tgt = next(p for p in net.pads if p.ref == "J2")
        assert tgt.connected and tgt.r_mohm == pytest.approx(2.0)
        # ... and the report records the explicit bridge.
        assert report.to_dict()["bridges"]["explicit"][0]["a_ref"] == "J1"

    def test_overlapping_holes_auto_bridge(self):
        # Stacked mounting holes (same XY, different panels) join automatically.
        ra = BoardRegion(uuid="ra",
                         polygon=(Point(0, 0), Point(40, 0), Point(40, 30), Point(0, 30)),
                         bbox=(0.0, 40.0, 0.0, 30.0))
        rb = BoardRegion(uuid="rb",
                         polygon=(Point(60, 0), Point(100, 0), Point(100, 30), Point(60, 30)),
                         bbox=(60.0, 100.0, 0.0, 30.0))
        j1 = make_fp("J1", "uuid-j1", 5.0, 15.0, ("GND",), tht=True)
        j2 = make_fp("J2", "uuid-j2", 65.0, 15.0, ("GND",), tht=True)
        m1 = make_fp("M1", "uuid-m1", 20.0, 15.0, ("GND",), tht=True)
        m2 = make_fp("M2", "uuid-m2", 20.0, 15.0, ("GND",), tht=True)
        model = _board(fps=[j1, j2, m1, m2],
                       tracks=[_track(5.0, 15.0, 20.0, 15.0, "GND"),
                               _track(65.0, 15.0, 20.0, 15.0, "GND")],
                       regions=[ra, rb],
                       region_of={"uuid-j1": 0, "uuid-m1": 0,
                                  "uuid-j2": 1, "uuid-m2": 1})
        grid = create_grid_from_model(model, resolution_mm=0.5)
        report = analyze_power_gnd(model, grid, gnd_inlets=[InletSpec("J1", "1")])
        net = next(n for n in report.nets if n.net == "GND")
        tgt = next(p for p in net.pads if p.ref == "J2")
        assert tgt.connected, "stacked mounting holes should join the panels"
        assert report.to_dict()["bridges"]["auto"] >= 1

    def test_nearest_inlet_feeds_each_pad(self):
        j1 = make_fp("J1", "uuid-j1", 5.0, 15.0, ("GND",))
        j2 = make_fp("J2", "uuid-j2", 20.0, 15.0, ("GND",))
        j3 = make_fp("J3", "uuid-j3", 25.0, 15.0, ("GND",))
        model = _board(fps=[j1, j2, j3],
                       tracks=[_track(5.0, 15.0, 25.0, 15.0, "GND")])
        grid = create_grid_from_model(model, resolution_mm=0.5)
        report = analyze_power_gnd(
            model, grid, gnd_inlets=[InletSpec("J1", "1"), InletSpec("J3", "1")])
        net = next(n for n in report.nets if n.net == "GND")
        by_ref = {p.ref: p for p in net.pads}
        assert by_ref["J1"].verdict == "pass" and by_ref["J1"].inlet == "J1.1"
        assert by_ref["J3"].verdict == "pass" and by_ref["J3"].inlet == "J3.1"
        # Mid-track T-junction taps the segment: nearer feed wins.
        assert by_ref["J2"].connected and by_ref["J2"].inlet == "J3.1"

    def test_duplicate_refs_stay_distinct(self):
        j1 = make_fp("J1", "uuid-j1", 10.0, 15.0, ("GND",))
        j2 = make_fp("J1", "uuid-j2", 30.0, 15.0, ("GND",))
        model = _board(fps=[j1, j2],
                       tracks=[_track(10.0, 15.0, 30.0, 15.0, "GND")])
        grid = create_grid_from_model(model, resolution_mm=0.5)
        report = analyze_power_gnd(model, grid, gnd_inlets=[InletSpec("J1", "1")])
        net = next(n for n in report.nets if n.net == "GND")
        assert len(net.pads) == 2  # both holes reported, not collapsed


class TestFixtureImpedance:
    """Recorded inlet-anchored screening on real boards (characterization).

    tube111 is a 3-panel file: J11.6 feeds panel 3 (region 2) only — the
    sibling panels mate through pin headers, which need declared bridges
    (none declared here, so their pads report disconnected). DCCF's single
    GND zone spans both panels in-file, so Q2.5 reaches everything.
    If a re-layout moves these numbers, update them AND justify — same
    convention as test_pour.py.
    """

    @staticmethod
    def _load(name):
        from kicad_autorouter.sexpr import parse_file
        from kicad_autorouter.board_model import board_model
        return board_model(parse_file(FIXTURES / name))

    @pytest.mark.unit
    def test_tube111_gnd_from_j11_6(self):
        model = self._load("tube111.kicad_pcb")
        grid = create_grid_from_model(model, resolution_mm=0.2)
        inlet_region = model.footprint_region.get(model.by_ref["J11"].uuid)
        report = analyze_power_gnd(model, grid, gnd_inlets=[InletSpec("J11", "6")])
        net = next(n for n in report.nets if n.net == "GND")
        assert len(net.pads) == 69
        local = [p for p in net.pads if p.region == inlet_region]
        assert local and all(p.connected for p in local)
        remote = [p for p in net.pads if not p.connected]
        assert remote and all(p.region != inlet_region for p in remote)
        assert net.worst_mohm == pytest.approx(1.713, abs=0.05)
        assert net.failures == len(remote)
        assert next(p for p in net.pads if p.ref == "J11" and p.pad == "6").verdict == "pass"

    @pytest.mark.unit
    def test_tube111_gnd_with_header_bridges(self):
        # The long 1x08 headers mate panel 2 to panel 3 (same-net pins);
        # with those 8 bridges (default 20 mΩ contact each) the middle
        # panel measures back to J11.6 and only the unlinked faceplate
        # holes stay out.
        pairs = [("J7", "2", "J9", "2"), ("J7", "4", "J9", "4"),
                 ("J7", "5", "J9", "5"), ("J7", "8", "J9", "8"),
                 ("J8", "2", "J10", "2"), ("J8", "4", "J10", "4"),
                 ("J8", "6", "J10", "6"), ("J8", "8", "J10", "8")]
        bridges = [BridgeSpec(a, b, c, d) for a, b, c, d in pairs]
        model = self._load("tube111.kicad_pcb")
        grid = create_grid_from_model(model, resolution_mm=0.2)
        report = analyze_power_gnd(model, grid, gnd_inlets=[InletSpec("J11", "6")],
                                   bridges=bridges)
        net = next(n for n in report.nets if n.net == "GND")
        assert net.failures == 10
        assert all(p.region == 0 for p in net.pads if not p.connected)
        assert net.worst_mohm == pytest.approx(22.113, abs=0.05)
        assert report.to_dict()["bridges"] == {"explicit": [
            {"a_ref": a, "a_pad": b, "b_ref": c, "b_pad": d, "r_mohm": 20.0}
            for a, b, c, d in pairs], "auto": 0, "cross_net": 0}

    @pytest.mark.unit
    def test_cross_net_bridges_join_declared_mates(self):
        # Net names differ deliberately (DRC trick); a declared mate joins
        # them anyway. I1 feeds net A; T1 on net A is reachable only via
        # net-B copper through two declared cross-net bridges. (B pads sit
        # off the A row so nothing joins by snap coincidence.)
        j1 = make_fp("J1", "uuid-j1", 0.0, 15.0, ("NA",))
        t1 = make_fp("T1", "uuid-t1", 30.0, 15.0, ("NA",))
        b1 = make_fp("B1", "uuid-b1", 0.0, 17.0, ("NB",))
        b2 = make_fp("B2", "uuid-b2", 30.0, 17.0, ("NB",))
        model = _board(
            fps=[j1, t1, b1, b2],
            tracks=[_track(0.0, 17.0, 30.0, 17.0, "NB")],
            edge=50.0,
        )
        grid = create_grid_from_model(model, resolution_mm=0.5)
        no_bridge = analyze_power_gnd(model, grid, gnd_inlets=[],
                                      power_inlets={"NA": [InletSpec("J1", "1")]},
                                      power_nets=["NA"])
        net = next(n for n in no_bridge.nets if n.net == "NA")
        assert next(p for p in net.pads if p.ref == "T1").reason == "disconnected"
        bridges = [BridgeSpec("J1", "1", "B1", "1", 2.0),
                   BridgeSpec("T1", "1", "B2", "1", 2.0)]
        report = analyze_power_gnd(model, grid, gnd_inlets=[],
                                   power_inlets={"NA": [InletSpec("J1", "1")]},
                                   power_nets=["NA"], bridges=bridges)
        net = next(n for n in report.nets if n.net == "NA")
        tgt = next(p for p in net.pads if p.ref == "T1")
        assert tgt.connected and tgt.bridges_used == 2
        assert tgt.r_mohm == pytest.approx(
            2.0 + track_resistance_mohm(30.0, 1.0) + 2.0, rel=0.05)
        assert report.to_dict()["bridges"]["cross_net"] == 2

    @pytest.mark.unit
    def test_tube111_cross_net_mates_leave_gnd_untouched(self):
        # All 16 header mate pairs declared (incl. the C1/C2 signal pairs
        # with deliberately different names): GND verdicts must be identical
        # to the same-net-only run — cross-net joins never leak into GND.
        same = [("J7", "2", "J9", "2"), ("J7", "4", "J9", "4"),
                ("J7", "5", "J9", "5"), ("J7", "8", "J9", "8"),
                ("J8", "2", "J10", "2"), ("J8", "4", "J10", "4"),
                ("J8", "6", "J10", "6"), ("J8", "8", "J10", "8")]
        diff = [("J7", "1", "J9", "1"), ("J7", "3", "J9", "3"),
                ("J7", "6", "J9", "6"), ("J7", "7", "J9", "7"),
                ("J8", "1", "J10", "1"), ("J8", "3", "J10", "3"),
                ("J8", "5", "J10", "5"), ("J8", "7", "J10", "7")]
        bridges = [BridgeSpec(a, b, c, d) for a, b, c, d in same + diff]
        model = self._load("tube111.kicad_pcb")
        grid = create_grid_from_model(model, resolution_mm=0.2)
        report = analyze_power_gnd(model, grid, gnd_inlets=[InletSpec("J11", "6")],
                                   bridges=bridges)
        net = next(n for n in report.nets if n.net == "GND")
        assert net.failures == 10
        assert all(p.region == 0 for p in net.pads if not p.connected)
        assert net.worst_mohm == pytest.approx(22.113, abs=0.05)
        assert report.to_dict()["bridges"]["cross_net"] == 8

    @pytest.mark.unit
    def test_tube111_header_mate_suggestions(self):
        model = self._load("tube111.kicad_pcb")
        mates = suggest_header_mates(model)
        by_pair = {}
        for d in mates:
            by_pair.setdefault((d["a"]["ref"], d["b"]["ref"]), []).append(d)
        assert set(by_pair) == {("J7", "J9"), ("J8", "J10")}
        assert all(len(v) == 8 for v in by_pair.values())
        matched = [d for d in mates if d["match"]]
        assert len(matched) == 8  # power+GND pins agree
        warned = [d for d in mates if not d["match"]]
        assert len(warned) == 8  # C1/C2 signal pins need the user's call
        assert {d["a"]["ref"] for d in warned} == {"J7", "J8"}

    @pytest.mark.unit
    def test_tube111_power_rails_are_thin(self):
        # Long thin rails: tens of mΩ from the J7 power header. If the
        # rails get widened, update these bands.
        model = self._load("tube111.kicad_pcb")
        grid = create_grid_from_model(model, resolution_mm=0.2)
        report = analyze_power_gnd(
            model, grid, gnd_inlets=[InletSpec("J11", "6")],
            power_inlets={"+12V": [InletSpec("J7", "2")],
                          "-12V": [InletSpec("J7", "5")]},
            power_nets=["+12V", "-12V"])
        by_net = {n.net: n for n in report.nets}
        assert 40.0 < by_net["+12V"].worst_mohm < 70.0
        assert 30.0 < by_net["-12V"].worst_mohm < 60.0

    @pytest.mark.dccf
    def test_dccf_gnd_from_q2_5(self):
        model = self._load("DCCF.sved.kicad_pcb")
        grid = create_grid_from_model(model, resolution_mm=0.2)
        report = analyze_power_gnd(model, grid, gnd_inlets=[InletSpec("Q2", "5")])
        net = next(n for n in report.nets if n.net == "GND")
        assert len(net.pads) == 66
        assert net.failures == 0
        assert net.worst_mohm == pytest.approx(0.4, abs=0.1)


@pytest.mark.server
@pytest.mark.minimal
def test_inlets_roundtrip_and_impedance_on_minimal():
    import server
    server.STATE.model = None
    server.STATE.gnd_inlets = []
    server.STATE.power_inlets = {}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        data = (Path(__file__).parent / "fixtures" / "minimal.kicad_pcb").read_bytes()
        req = urllib.request.Request(f"{base}/api/load?name=minimal", data=data, method="POST")
        with urllib.request.urlopen(req) as r:
            assert json.load(r)["ok"] is True
        with urllib.request.urlopen(f"{base}/api/nets/inlets") as r:
            assert json.load(r)["gnd_list"] == []
        # Unknown ref is rejected, not stored.
        bad = urllib.request.Request(
            f"{base}/api/nets/inlets",
            data=json.dumps({"gnd_list": {"ref": "NOPE", "pad": "1"}}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(bad)
            raise AssertionError("expected 400")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
        with urllib.request.urlopen(f"{base}/api/routing/debug/impedance") as r:
            payload = json.load(r)
        assert payload["ok"] is True and "summary" in payload
        # Bridges roundtrip (minimal has MH1..MH4 mounting holes, no nets).
        req = urllib.request.Request(
            f"{base}/api/nets/inlets",
            data=json.dumps({"bridges": [
                {"a": {"ref": "MH1", "pad": ""}, "b": {"ref": "MH2", "pad": ""}}]}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            stored = json.load(r)
        assert stored["bridges"] == [
            {"a": {"ref": "MH1", "pad": ""}, "b": {"ref": "MH2", "pad": ""}, "r_mohm": 20.0}]
        with urllib.request.urlopen(f"{base}/api/nets/inlets") as r:
            assert json.load(r)["bridges"] == stored["bridges"]
        # Unknown bridge endpoint is rejected.
        bad_bridge = urllib.request.Request(
            f"{base}/api/nets/inlets",
            data=json.dumps({"bridges": [
                {"a": {"ref": "MH1", "pad": ""}, "b": {"ref": "NOPE", "pad": "1"}}]}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(bad_bridge)
            raise AssertionError("expected 400")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
    finally:
        httpd.shutdown()
