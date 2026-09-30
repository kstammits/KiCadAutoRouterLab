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
def _reset_state():
    server.STATE.model = None
    server.STATE.name = ""
    server.STATE.version = 0
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
