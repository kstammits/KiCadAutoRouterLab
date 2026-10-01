"""End-to-end tests for the workflow UI server endpoints."""

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


def test_board_svg_requires_load():
    httpd, base = _client()
    try:
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(f"{base}/api/board.svg")
        assert ei.value.code == 404
    finally:
        httpd.shutdown()


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


def test_load_bad_body_rejected():
    httpd, base = _client()
    try:
        req = urllib.request.Request(f"{base}/api/load", data=b"((unclosed", method="POST")
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req)
        assert ei.value.code == 400
    finally:
        httpd.shutdown()


def test_params_roundtrip():
    httpd, base = _client()
    try:
        with urllib.request.urlopen(f"{base}/api/placement/params") as r:
            defaults = json.load(r)
        assert defaults["repulsion_kr"] == 100.0
        custom = dict(defaults, repulsion_kr=42.5, demo_jitter_mm=3.0)
        req = urllib.request.Request(
            f"{base}/api/placement/params",
            data=json.dumps(custom).encode(),
            method="POST",
        )
        with urllib.request.urlopen(req) as r:
            saved = json.load(r)
        assert saved["repulsion_kr"] == 42.5 and saved["demo_jitter_mm"] == 3.0
        with urllib.request.urlopen(f"{base}/api/placement/params") as r:
            reloaded = json.load(r)
        assert reloaded["repulsion_kr"] == 42.5
    finally:
        httpd.shutdown()


def test_params_rejects_bad_values():
    httpd, base = _client()
    try:
        req = urllib.request.Request(
            f"{base}/api/placement/params",
            data=b'{"max_iterations": -1}',
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req)
        assert ei.value.code == 400
    finally:
        httpd.shutdown()


def _load_minimal(base):
    data = (Path(__file__).parent / "fixtures" / "minimal.kicad_pcb").read_bytes()
    req = urllib.request.Request(
        f"{base}/api/load?name=minimal", data=data, method="POST"
    )
    with urllib.request.urlopen(req) as r:
        return json.load(r)["version"]


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


def test_apply_not_implemented():
    httpd, base = _client()
    try:
        req = urllib.request.Request(f"{base}/api/placement/apply", method="POST")
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req)
        assert ei.value.code == 501
    finally:
        httpd.shutdown()


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


def test_wrong_kind_rejected():
    httpd, base = _client()
    try:
        data = (Path(__file__).parent / "fixtures" / "minimal.kicad_pcb").read_bytes()
        req = urllib.request.Request(
            f"{base}/api/load?name=minimal&kind=sch", data=data, method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req)
        assert ei.value.code == 400
    finally:
        httpd.shutdown()
