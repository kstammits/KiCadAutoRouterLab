"""Server input validation: bad client input yields 400, never 500 or poisoned state.

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

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def base(tmp_path):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    server.STATE.model = None
    server.STATE.pcb_tree = None
    server.STATE.mode = None
    server.STATE.version = 0
    server.PARAMS_PATH = tmp_path / "placement.json"
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        server.STATE.model = None
        server.STATE.pcb_tree = None
        server.STATE.mode = None
        server.STATE.version = 0


def _post(base, path, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(f"{base}{path}", data=body, method="POST")
    with urllib.request.urlopen(req) as r:
        return r.status, r.read()


def _load(base, name="minimal.kicad_pcb"):
    data = (FIXTURES / "minimal.kicad_pcb").read_bytes()
    req = urllib.request.Request(f"{base}/api/load?name={name}", data=data, method="POST")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def _code(base, path, payload):
    try:
        _post(base, path, payload)
    except urllib.error.HTTPError as exc:
        return exc.code
    return 200


@pytest.mark.server
@pytest.mark.minimal
def test_routing_params_rejects_bad_values(base):
    assert _code(base, "/api/routing/params", {"grid_resolution_mm": -1}) == 400
    assert _code(base, "/api/routing/params", {"grid_resolution_mm": 0}) == 400
    assert _code(base, "/api/routing/params", {"max_expansions": "x"}) == 400
    assert _code(base, "/api/routing/params", {"stitch_gnd_vias": "yes"}) == 400
    assert _code(base, "/api/routing/params", {"allowed_layers": "F.Cu"}) == 400
    assert _code(base, "/api/routing/params", {}) == 200


@pytest.mark.server
@pytest.mark.minimal
def test_routing_run_validates_net_names_and_params(base):
    _load(base)
    assert _code(base, "/api/routing/run", {"net_names": "GND"}) == 400
    assert _code(base, "/api/routing/run", {"net_names": []}) == 400
    assert _code(base, "/api/routing/run",
                {"net_names": ["X"], "params": {"grid_resolution_mm": 0}}) == 400


@pytest.mark.server
@pytest.mark.minimal
def test_routing_jobs_rejects_empty_and_bad_params(base):
    _load(base)
    assert _code(base, "/api/routing/jobs", {"net_names": [123]}) == 400
    assert _code(base, "/api/routing/jobs",
                {"net_names": ["X"], "params": {"heuristic_weight": float("nan")}}) == 400


@pytest.mark.server
@pytest.mark.minimal
def test_placement_forces_rejects_bad_params(base):
    _load(base)
    assert _code(base, "/api/placement/forces", {"wall_k_N": "x"}) == 400
    assert _code(base, "/api/placement/forces", {"wall_halo_m": -1}) == 400
    assert _code(base, "/api/placement/forces", {}) == 200


@pytest.mark.server
@pytest.mark.minimal
def test_ghost_create_rejects_nonfinite_geometry(base):
    _load(base)
    wall = {"kind": "wall", "ref": "GHX", "x_mm": 0.0, "y_mm": 0.0,
            "w_mm": 5.0, "h_mm": 5.0}
    assert _code(base, "/api/ghosts", {**wall, "x_mm": float("inf")}) == 400
    assert _code(base, "/api/ghosts", {**wall, "w_mm": -2.0}) == 400
    assert _code(base, "/api/ghosts", {**wall, "ref": "GHOK"}) == 200
