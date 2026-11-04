"""End-to-end tests for the workflow UI server endpoints.

Markers: server, minimal
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

import server  # noqa: E402
import server_state  # noqa: E402
from tests.helpers import post_fixture


@pytest.fixture(autouse=True)
def _reset_state(tmp_path):
    server.STATE.model = None
    server.STATE.sch = None
    server.STATE.name = ""
    server.STATE.version = 0
    server.STATE.proposal = None
    server.PARAMS_PATH = tmp_path / "placement.json"
    yield


def _client():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


@pytest.mark.server
@pytest.mark.minimal
def test_load_state_and_svg():
    httpd, base = _client()
    try:
        assert post_fixture(base)["ok"] is True
        with urllib.request.urlopen(f"{base}/api/state") as r:
            st = json.load(r)
        assert st["loaded"] and st["name"] == "minimal" and st["version"] >= 1
        with urllib.request.urlopen(f"{base}/api/board.svg?v={st['version']}") as r:
            svg = r.read().decode()
        assert svg.startswith("<svg")
    finally:
        httpd.shutdown()


@pytest.mark.server
@pytest.mark.params
def test_params_roundtrip():
    httpd, base = _client()
    try:
        with urllib.request.urlopen(f"{base}/api/placement/params") as r:
            defaults = json.load(r)
        # SI teardown complete: no legacy repulsion_kr anywhere.
        assert "repulsion_kr" not in defaults
        assert defaults["repulsion_k_N_m2"] == pytest.approx(2e-9)
        custom = dict(defaults, repulsion_k_N_m2=4.2500000000000003e-08)
        req = urllib.request.Request(
            f"{base}/api/placement/params",
            data=json.dumps(custom).encode(),
            method="POST",
        )
        with urllib.request.urlopen(req) as r:
            saved = json.load(r)
        assert saved["repulsion_k_N_m2"] == pytest.approx(4.25e-08)
        with urllib.request.urlopen(f"{base}/api/placement/params") as r:
            reloaded = json.load(r)
        assert reloaded["repulsion_k_N_m2"] == pytest.approx(4.25e-08)
    finally:
        httpd.shutdown()


@pytest.mark.server
@pytest.mark.minimal
def test_run_and_clear_proposal():
    httpd, base = _client()
    try:
        version = post_fixture(base)["version"]
        req = urllib.request.Request(f"{base}/api/placement/run", method="POST")
        with urllib.request.urlopen(req) as r:
            run = json.load(r)
        assert run["ok"] and run["moved"] == 0 and run["version"] > version
        with urllib.request.urlopen(f"{base}/api/placement/proposal") as r:
            prop = json.load(r)
        assert prop["deltas"] == {}
        req = urllib.request.Request(f"{base}/api/placement/clear", method="POST")
        with urllib.request.urlopen(req) as r:
            assert json.load(r)["ok"] is True
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(f"{base}/api/placement/proposal")
        assert ei.value.code == 404
    finally:
        httpd.shutdown()


@pytest.mark.server
@pytest.mark.minimal
def test_sch_load_sets_has_sch():
    httpd, base = _client()
    try:
        with urllib.request.urlopen(f"{base}/api/state") as r:
            assert json.load(r)["has_sch"] is False
        data = (Path(__file__).parent / "fixtures" / "minimal.kicad_sch").read_bytes()
        req = urllib.request.Request(
            f"{base}/api/load?name=minimal&kind=sch", data=data, method="POST"
        )
        with urllib.request.urlopen(req) as r:
            assert json.load(r)["ok"] is True
        with urllib.request.urlopen(f"{base}/api/state") as r:
            st = json.load(r)
        assert st["has_sch"] is True and st["loaded"] is False
    finally:
        httpd.shutdown()


@pytest.mark.server
@pytest.mark.minimal
def test_run_with_json_body_iterations():
    """Test /api/placement/run accepts JSON body with iterations parameter."""
    httpd, base = _client()
    try:
        post_fixture(base)
        req = urllib.request.Request(
            f"{base}/api/placement/run",
            data=json.dumps({"iterations": 10}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as r:
            run = json.load(r)
        assert run["ok"] is True
        assert run["iterations"] <= 10
    finally:
        httpd.shutdown()


@pytest.mark.server
@pytest.mark.minimal
def test_run_with_movable_uuids():
    """Test /api/placement/run accepts movable_uuids to restrict movement."""
    httpd, base = _client()
    try:
        post_fixture(base)
        req = urllib.request.Request(f"{base}/api/placement/run", method="POST")
        with urllib.request.urlopen(req) as r:
            run = json.load(r)
        req = urllib.request.Request(
            f"{base}/api/placement/run",
            data=json.dumps({"movable_uuids": ["fake-uuid"]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as r:
            run = json.load(r)
        assert run["ok"] is True
        assert run["moved"] == 0
    finally:
        httpd.shutdown()


@pytest.mark.server
def test_forces_include_components():
    """POST /api/placement/forces returns per-cause breakdown summing to totals."""
    httpd, base = _client()
    try:
        assert post_fixture(base, "DCCF.sved.kicad_pcb", "dccf")["ok"] is True
        req = urllib.request.Request(
            f"{base}/api/placement/forces",
            data=json.dumps({}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as r:
            body = json.load(r)
        assert body["ok"] is True
        assert body["forces"]
        assert set(body["force_components"]) == set(body["forces"])
        for uuid, total in body["forces"].items():
            comps = body["force_components"][uuid]
            assert set(comps) == {
                "repulsion", "attraction",
                "ghost", "courtyard", "tangential",
            }
            sx = sum(v[0] for v in comps.values())
            sy = sum(v[1] for v in comps.values())
            assert sx == pytest.approx(total[0])
            sy == pytest.approx(total[1])
        with urllib.request.urlopen(f"{base}/api/board.svg?forces=1") as r:
            svg = r.read().decode()
        assert "url(#force-" in svg
    finally:
        httpd.shutdown()


@pytest.mark.server
def test_accept_proposal():
    """Test /api/placement/accept applies a physics proposal to the model."""
    httpd, base = _client()
    try:
        assert post_fixture(base, "DCCF.sved.kicad_pcb", "dccf")["ok"] is True
        req = urllib.request.Request(
            f"{base}/api/placement/run",
            data=json.dumps({"iterations": 5}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as r:
            run = json.load(r)
        assert run["moved"] > 0
        version_before = run["version"]

        req = urllib.request.Request(f"{base}/api/placement/accept", method="POST")
        with urllib.request.urlopen(req) as r:
            accept = json.load(r)
        assert accept["ok"] is True
        assert accept["version"] > version_before

        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(f"{base}/api/placement/proposal")
        assert ei.value.code == 404

        with urllib.request.urlopen(f"{base}/api/state") as r:
            st = json.load(r)
        assert st["version"] == accept["version"]
    finally:
        httpd.shutdown()


@pytest.mark.server
@pytest.mark.minimal
def test_accept_no_proposal_returns_400():
    """Test /api/placement/accept returns 400 when no proposal exists."""
    httpd, base = _client()
    try:
        post_fixture(base)
        req = urllib.request.Request(f"{base}/api/placement/accept", method="POST")
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req)
        assert ei.value.code == 400
    finally:
        httpd.shutdown()


@pytest.mark.server
def test_gnd_islands_tube111_fragmented():
    httpd, base = _client()
    try:
        data = (Path(__file__).parent / "fixtures" / "tube111.kicad_pcb").read_bytes()
        req = urllib.request.Request(f"{base}/api/load?name=tube111", data=data, method="POST")
        with urllib.request.urlopen(req) as r:
            assert json.load(r)["ok"] is True
        with urllib.request.urlopen(f"{base}/api/routing/debug/gnd-islands") as r:
            payload = json.load(r)
        assert payload["ok"] is True
        assert payload["resolution_mm"] == pytest.approx(0.2)
        fcu = payload["layers"]["F.Cu"]
        assert len(fcu) >= 2
        first = fcu[0]
        for key in ("net", "area_mm2", "cx_mm", "cy_mm", "cells",
                    "connected", "bbox_mm", "zones"):
            assert key in first, key
        assert first["net"] == "GND"
    finally:
        httpd.shutdown()

def _get(base, path):
    with urllib.request.urlopen(f"{base}{path}") as r:
        return r.status, r.headers.get_content_type(), r.read()


@pytest.mark.server
@pytest.mark.unit
def test_docs_list_contains_physics_set():
    httpd, base = _client()
    try:
        status, ctype, body = _get(base, "/api/docs/list")
        assert status == 200
        docs = {d["name"]: d for d in json.loads(body)["docs"]}
        for name in (
            "README.md",
            "docs/si_force_laws.md",
            "docs/physics_primer.qmd",
            "docs/roadmap.md",
        ):
            assert name in docs, f"missing {name}"
        # Rendered mapping present for the QMD primers.
        assert docs["docs/physics_primer.qmd"]["rendered_url"] in (
            "/docs-rendered/physics_primer.html",
            None,  # not yet rendered on a fresh checkout
        )
        # Plain markdown has no rendered twin.
        assert docs["docs/si_force_laws.md"]["rendered_url"] is None
    finally:
        httpd.shutdown()


@pytest.mark.server
@pytest.mark.unit
def test_docs_content_unknown_returns_404():
    httpd, base = _client()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{base}/api/docs/content?name=docs/kicad/pcbnew.md")
        assert exc.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as exc2:
            urllib.request.urlopen(f"{base}/api/docs/content?name=../pyproject.toml")
        assert exc2.value.code == 404
    finally:
        httpd.shutdown()


@pytest.mark.unit
def test_accept_report_counts_ripped_arcs():
    """Accepting a placement move reports ripped arcs per net.

    C1 sits on GND/+12V; with nothing protected, moving it rips +12V
    copper. An injected +12V arc must vanish from model and tree and show
    up as ``ripped["+12V"]["arcs"] == 1`` while the GND arc survives.
    """
    from dataclasses import replace
    from types import SimpleNamespace

    from kicad_autorouter.board_model import ArcTrack, Point, board_model
    from kicad_autorouter.sexpr import SExpr, parse_file

    def _arc_node(net):
        return SExpr("arc", (
            SExpr("start", (0.0, 0.0)),
            SExpr("mid", (5.0, 5.0)),
            SExpr("end", (10.0, 0.0)),
            SExpr("width", (0.25,)),
            SExpr("layer", ("F.Cu",)),
            SExpr("net", (net,)),
        ))

    tree = parse_file(Path(__file__).parent / "fixtures" / "tube111.kicad_pcb")
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
    st = server_state.BoardState()
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
