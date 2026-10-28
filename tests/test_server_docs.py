"""Docs viewer endpoints: allowlist, rendered HTML, traversal guards.

Markers: server, unit
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
