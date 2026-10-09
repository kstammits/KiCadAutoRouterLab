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
def test_board_svg_requires_load():
    httpd, base = _client()
    try:
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(f"{base}/api/board.svg")
        assert ei.value.code == 404
    finally:
        httpd.shutdown()


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
def test_load_bad_body_rejected():
    httpd, base = _client()
    try:
        req = urllib.request.Request(f"{base}/api/load", data=b"((unclosed", method="POST")
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req)
        assert ei.value.code == 400
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
        custom = dict(defaults, repulsion_kr=42.5, ideal_length_mm=33.0)
        req = urllib.request.Request(
            f"{base}/api/placement/params",
            data=json.dumps(custom).encode(),
            method="POST",
        )
        with urllib.request.urlopen(req) as r:
            saved = json.load(r)
        assert saved["repulsion_kr"] == 42.5 and saved["ideal_length_mm"] == 33.0
        with urllib.request.urlopen(f"{base}/api/placement/params") as r:
            reloaded = json.load(r)
        assert reloaded["repulsion_kr"] == 42.5
    finally:
        httpd.shutdown()


@pytest.mark.server
@pytest.mark.params
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
def test_apply_not_implemented():
    httpd, base = _client()
    try:
        req = urllib.request.Request(f"{base}/api/placement/apply", method="POST")
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req)
        assert ei.value.code == 501
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
        # Get the UUID of the unlocked footprint (MH1)
        # First run a proposal to see what UUIDs are available
        req = urllib.request.Request(f"{base}/api/placement/run", method="POST")
        with urllib.request.urlopen(req) as r:
            run = json.load(r)
        # No nets on the minimal fixture, so the proposal is empty — but the
        # endpoint still accepts the movable_uuids param.
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
@pytest.mark.minimal
def test_accept_empty_proposal_rejected():
    """Accepting a no-movement proposal (net-free minimal board) is a 400."""
    httpd, base = _client()
    try:
        _load_minimal(base)
        req = urllib.request.Request(
            f"{base}/api/placement/run",
            data=json.dumps({"iterations": 5}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as r:
            run = json.load(r)
        assert run["moved"] == 0
        req = urllib.request.Request(f"{base}/api/placement/accept", method="POST")
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req)
        assert ei.value.code == 400
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
                "repulsion", "attraction", "rigid",
                "ghost", "courtyard", "boundary",
            }
            sx = sum(v[0] for v in comps.values())
            sy = sum(v[1] for v in comps.values())
            assert sx == pytest.approx(total[0])
            assert sy == pytest.approx(total[1])
        # SVG with forces on must contain per-cause arrows + markers.
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

        # Now accept the proposal
        req = urllib.request.Request(f"{base}/api/placement/accept", method="POST")
        with urllib.request.urlopen(req) as r:
            accept = json.load(r)
        assert accept["ok"] is True
        assert accept["version"] > version_before

        # Proposal should be cleared
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(f"{base}/api/placement/proposal")
        assert ei.value.code == 404

        # Board state should be updated (version increased)
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
@pytest.mark.minimal
def test_run_rejects_bad_movable_uuids():
    """Test /api/placement/run rejects non-list movable_uuids."""
    httpd, base = _client()
    try:
        _load_minimal(base)
        req = urllib.request.Request(
            f"{base}/api/placement/run",
            data=json.dumps({"movable_uuids": "not-a-list"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req)
        assert ei.value.code == 400
    finally:
        httpd.shutdown()


@pytest.mark.server
@pytest.mark.minimal
def test_docs_list_returns_curated_set():
    """GET /api/docs/list returns exactly the curated allowlist."""
    httpd, base = _client()
    try:
        with urllib.request.urlopen(f"{base}/api/docs/list") as r:
            data = json.load(r)
        assert data["ok"] is True
        names = sorted(d["name"] for d in data["docs"])
        assert names == sorted([
            "README.md",
            "PLAN.md",
            "TODO.md",
            "docs/README.md",
            "docs/autorouting_glossary.md",
        ])
    finally:
        httpd.shutdown()


@pytest.mark.server
@pytest.mark.minimal
def test_docs_content_readme():
    """GET /api/docs/content serves README markdown."""
    httpd, base = _client()
    try:
        with urllib.request.urlopen(f"{base}/api/docs/content?name=README.md") as r:
            data = json.load(r)
        assert data["ok"] is True
        assert data["name"] == "README.md"
        assert "KiCad AutoRouter Lab" in data["markdown"]
        assert data["truncated"] is False
    finally:
        httpd.shutdown()


@pytest.mark.server
@pytest.mark.minimal
def test_docs_content_rejects_unknown_and_traversal():
    """Unknown keys and path traversal are 404, never file reads."""
    httpd, base = _client()
    try:
        for bad in ("docs/kicad/pcbnew.md", "../pyproject.toml", "", "nope.md"):
            with pytest.raises(urllib.error.HTTPError) as ei:
                urllib.request.urlopen(
                    f"{base}/api/docs/content?name={urllib.request.quote(bad, safe='')}"
                )
            assert ei.value.code == 404
    finally:
        httpd.shutdown()


@pytest.mark.server
@pytest.mark.minimal
def test_docs_html_served():
    """Standalone /docs.html viewer is served as HTML."""
    httpd, base = _client()
    try:
        with urllib.request.urlopen(f"{base}/docs.html") as r:
            body = r.read().decode()
        assert "<title>KiCad AutoRouter — Docs</title>" in body
    finally:
        httpd.shutdown()
