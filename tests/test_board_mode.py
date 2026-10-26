"""Board mode switch: server round-trip, preview, collapse download.

Markers: server, minimal
"""

import json
import sys
import threading
import time
import urllib.request
import urllib.error
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ui"))
import server  # noqa: E402


def _serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


@pytest.fixture()
def base():
    httpd = _serve()
    server.STATE.model = None
    server.STATE.pcb_tree = None
    server.STATE.mode = None
    server.STATE.cost_grid = None
    server.STATE.routing_grid = None
    server.STATE.version = 0
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        server.STATE.model = None
        server.STATE.pcb_tree = None
        server.STATE.mode = None
        server.STATE.cost_grid = None
        server.STATE.routing_grid = None
        server.STATE.version = 0


def _post(url, data=None, payload=None):
    body = (json.dumps(payload).encode() if payload is not None
            else (data or b""))
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req) as r:
        return r.status, r.headers, r.read()


def _get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, r.headers, r.read()


def _load(board_base, name="minimal.kicad_pcb", data=None):
    return _post(f"{board_base}/api/load?name={name}",
                 data=data or (FIXTURES / "minimal.kicad_pcb").read_bytes())


def _grafted_four_layer_bytes():
    """Minimal fixture + 4-layer stackup, one inner segment, one blind via."""
    from kicad_autorouter.board_model import copper_layers
    from kicad_autorouter.sexpr import SExpr, parse as parse_sexpr, parse_file

    tree = parse_file(FIXTURES / "minimal.kicad_pcb")
    four_stackup = parse_sexpr(
        '(stackup (layer "F.Cu" (type "copper") (thickness 0.035))'
        ' (layer "dielectric 1" (type "core") (thickness 1.5))'
        ' (layer "In1.Cu" (type "copper") (thickness 0.017))'
        ' (layer "dielectric 2" (type "prepreg") (thickness 0.2))'
        ' (layer "B.Cu" (type "copper") (thickness 0.035)))'
    )
    graft = [
        SExpr("segment", (SExpr("start", (60.0, 60.0)),
                          SExpr("end", (70.0, 60.0)), SExpr("width", (0.25,)),
                          SExpr("layer", ("In1.Cu",)), SExpr("net", (0,)),
                          SExpr("uuid", ("graft-seg",)))),
        SExpr("via", (SExpr("at", (65.0, 65.0)), SExpr("size", (0.8,)),
                      SExpr("drill", (0.4,)), SExpr("layers", ("F.Cu", "In1.Cu")),
                      SExpr("net", (0,)), SExpr("uuid", ("graft-via",)))),
    ]
    setup = tree.find("setup")
    new_setup = replace(
        setup,
        args=tuple(four_stackup if c is setup.find("stackup") else c
                   for c in setup.args),
    )
    grafted = replace(
        tree,
        args=tuple(new_setup if c is setup else c for c in tree.args) + tuple(graft),
    )
    assert copper_layers(grafted) == ("F.Cu", "In1.Cu", "B.Cu")
    from kicad_autorouter.sexpr import to_sexpr
    return (to_sexpr(grafted) + "\n").encode()


@pytest.mark.server
@pytest.mark.minimal
def test_mode_round_trip_and_state_fields(base):
    _load(base)
    _, _, body = _get(f"{base}/api/mode")
    info = json.loads(body)
    assert info["mode"] == "two-layer"
    assert info["inferred"] == "two-layer"
    assert info["warning"] is None
    assert info["preset"]["allowed_layers"] == ["F.Cu", "B.Cu"]

    _, _, body = _get(f"{base}/api/state")
    st = json.loads(body)
    assert st["mode"] == "two-layer"
    assert st["mode_warning"] is None
    assert "mode_custom" not in st  # Custom badge cut

    status, _, body = _post(f"{base}/api/mode", payload={"mode": "four-layer"})
    assert status == 200
    info = json.loads(body)
    assert info["mode"] == "four-layer"
    assert "as-is" in (info["warning"] or "")

    _, _, body = _get(f"{base}/api/state")
    assert json.loads(body)["mode_warning"] == info["warning"]

    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(f"{base}/api/mode", payload={"mode": "six-layer"})
    assert exc.value.code == 400


@pytest.mark.server
@pytest.mark.minimal
def test_download_two_layer_no_inner_serves_as_is(base):
    _load(base)
    status, headers, _ = _get(f"{base}/api/board/download")
    assert status == 200
    assert "-modified.kicad_pcb" in headers["Content-Disposition"]
    assert "-2layer" not in headers["Content-Disposition"]


@pytest.mark.server
@pytest.mark.minimal
def test_preview_and_collapse_download(base):
    _load(base, name="fourlayer.kicad_pcb", data=_grafted_four_layer_bytes())
    _, _, body = _get(f"{base}/api/mode")
    assert json.loads(body)["inferred"] == "four-layer"

    # Four-layer mode: download serves untouched (warn, never block).
    status, headers, raw = _get(f"{base}/api/board/download")
    assert status == 200
    assert "-modified.kicad_pcb" in headers["Content-Disposition"]
    assert b"graft-seg" in raw

    # Switch to two-layer: preview counts match, download collapses.
    _post(f"{base}/api/mode", payload={"mode": "two-layer"})
    _, _, body = _get(f"{base}/api/board/collapse-preview")
    preview = json.loads(body)
    assert preview["report"]["tracks_removed"] == 1
    assert preview["report"]["vias_converted"] == 1
    assert preview["report"]["vias_dropped"] == 0
    assert "collapse" in (preview["warning"] or "")

    status, headers, raw = _get(f"{base}/api/board/download")
    assert status == 200
    assert "-2layer.kicad_pcb" in headers["Content-Disposition"]
    assert b"graft-seg" not in raw
    from kicad_autorouter.board_model import copper_layers as _cl
    from kicad_autorouter.sexpr import parse as _parse
    assert _cl(_parse(raw.decode("utf-8"))) == ("F.Cu", "B.Cu")

    # In-memory board untouched by the collapsed download.
    _, _, raw2 = _get(f"{base}/api/board/collapse-preview")
    assert json.loads(raw2)["report"]["tracks_removed"] == 1


@pytest.mark.server
@pytest.mark.minimal
def test_download_filename_injection_neutralized(base):
    import urllib.parse
    evil = 'x";\r\nInjected: yes'
    _load(base, name=urllib.parse.quote(evil, safe=""), data=(
        (FIXTURES / "minimal.kicad_pcb").read_bytes()))
    status, headers, _ = _get(f"{base}/api/board/download")
    assert status == 200
    disp = headers["Content-Disposition"]
    assert "\r" not in disp and "\n" not in disp
    assert disp.count('"') == 2  # exactly the wrapping pair
    assert disp.endswith('.kicad_pcb"')


@pytest.mark.server
@pytest.mark.minimal
def test_mode_response_carries_owned_keys(base):
    _load(base)
    _, _, body = _get(f"{base}/api/mode")
    info = json.loads(body)
    assert info["owned"] == ["stitch_gnd_vias", "strip_gnd_blockers",
                             "pour_severance_weight", "allowed_layers"]
    status, _, body = _post(f"{base}/api/mode", payload={"mode": "four-layer"})
    assert status == 200
    assert json.loads(body)["owned"] == [
        "stitch_gnd_vias", "strip_gnd_blockers",
        "pour_severance_weight", "allowed_layers", "refill_zones"]


@pytest.mark.server
@pytest.mark.minimal
def test_download_suffix_less_name_gets_extension(base):
    _load(base, name="board-without-suffix", data=(
        (FIXTURES / "minimal.kicad_pcb").read_bytes()))
    status, headers, _ = _get(f"{base}/api/board/download")
    assert status == 200
    assert headers["Content-Disposition"].endswith('.kicad_pcb"')
    assert "board-without-suffix-modified.kicad_pcb" in headers["Content-Disposition"]


@pytest.mark.server
@pytest.mark.minimal
def test_reset_reinfers_mode(base):
    _load(base, name="fourlayer.kicad_pcb", data=_grafted_four_layer_bytes())
    _, _, body = _get(f"{base}/api/mode")
    assert json.loads(body)["inferred"] == "four-layer"
    _post(f"{base}/api/mode", payload={"mode": "two-layer"})
    status, _, _ = _post(f"{base}/api/board/reset", data=b"")
    assert status == 200
    _, _, body = _get(f"{base}/api/mode")
    info = json.loads(body)
    assert info["mode"] == "four-layer"
    assert info["inferred"] == "four-layer"


@pytest.mark.server
@pytest.mark.minimal
def test_job_submit_attaches_mode_warning(base):
    _load(base)
    _post(f"{base}/api/mode", payload={"mode": "four-layer"})
    status, _, body = _post(f"{base}/api/routing/jobs",
                            payload={"net_names": ["NONET"], "params": {}})
    assert status == 200
    job_id = json.loads(body)["job_id"]
    deadline = time.time() + 60
    while True:
        _, _, polled = _get(f"{base}/api/routing/jobs/{job_id}")
        job = json.loads(polled)
        if job.get("state") in ("done", "failed"):
            break
        assert time.time() < deadline, "routing job did not finish"
        time.sleep(0.2)
    assert job["state"] == "done"
    assert "as-is" in (job.get("mode_warning") or "")
    assert job["result"]["mode_warning"] == job["mode_warning"]
