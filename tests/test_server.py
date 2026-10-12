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
        data = (Path(__file__).parent / "fixtures" / "minimal.kicad_pcb").read_bytes()
        req = urllib.request.Request(f"{base}/api/load?name=minimal", data=data, method="POST")
        with urllib.request.urlopen(req) as r:
            assert json.load(r)["ok"] is True
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
        assert defaults["repulsion_kr"] == 100.0
        custom = dict(defaults, repulsion_kr=42.5)
        req = urllib.request.Request(
            f"{base}/api/placement/params",
            data=json.dumps(custom).encode(),
            method="POST",
        )
        with urllib.request.urlopen(req) as r:
            saved = json.load(r)
        assert saved["repulsion_kr"] == 42.5
        with urllib.request.urlopen(f"{base}/api/placement/params") as r:
            reloaded = json.load(r)
        assert reloaded["repulsion_kr"] == 42.5
    finally:
        httpd.shutdown()


def _load_minimal(base):
    data = (Path(__file__).parent / "fixtures" / "minimal.kicad_pcb").read_bytes()
    req = urllib.request.Request(
        f"{base}/api/load?name=minimal", data=data, method="POST"
    )
    with urllib.request.urlopen(req) as r:
        return json.load(r)["version"]


@pytest.mark.server
@pytest.mark.minimal
def test_run_and_clear_proposal():
    httpd, base = _client()
    try:
        version = _load_minimal(base)
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
        _load_minimal(base)
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
        _load_minimal(base)
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
        data = (Path(__file__).parent / "fixtures" / "DCCF.sved.kicad_pcb").read_bytes()
        req = urllib.request.Request(f"{base}/api/load?name=dccf", data=data, method="POST")
        with urllib.request.urlopen(req) as r:
            assert json.load(r)["ok"] is True
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
                "ghost", "courtyard", "boundary",
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
        data = (Path(__file__).parent / "fixtures" / "DCCF.sved.kicad_pcb").read_bytes()
        req = urllib.request.Request(f"{base}/api/load?name=dccf", data=data, method="POST")
        with urllib.request.urlopen(req) as r:
            assert json.load(r)["ok"] is True
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
        _load_minimal(base)
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