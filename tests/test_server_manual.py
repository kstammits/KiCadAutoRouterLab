"""End-to-end tests for manual nudge + ghost CRUD server endpoints.

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
    server.STATE.pinned_uuids = set()
    server.STATE.selected_uuids = set()
    server.STATE.undo_stack = []
    server.STATE._last_nudge_at = 0.0
    server.STATE._last_nudge_keys = frozenset()
    server.PARAMS_PATH = tmp_path / "placement.json"
    yield


def _client():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _post(base, path, body):
    req = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def _get(base, path):
    with urllib.request.urlopen(f"{base}{path}") as r:
        return json.load(r)


def _load(base, fixture="ghost_test.kicad_pcb", name="ghost_test"):
    data = (Path(__file__).parent / "fixtures" / fixture).read_bytes()
    req = urllib.request.Request(
        f"{base}/api/load?name={name}", data=data, method="POST"
    )
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def _footprints(base):
    return {f["uuid"]: f for f in _get(base, "/api/footprints")["footprints"]}


@pytest.mark.server
@pytest.mark.minimal
def test_nudge_moves_selected_and_undo_restores():
    httpd, base = _client()
    try:
        _load(base)
        fps = _footprints(base)
        u1 = "aaaaaaaa-1111-2222-3333-444444444444"
        assert fps[u1]["x_mm"] == 50
        # Select U1 then nudge without explicit uuids (defaults to selection).
        _post(base, "/api/placement/select", {"uuid": u1})
        r = _post(base, "/api/placement/nudge", {"dx_mm": 1.0, "dy_mm": 0.0})
        assert r["ok"] and r["moved"] == [u1]
        assert _footprints(base)[u1]["x_mm"] == pytest.approx(51.0)
        # Other part untouched.
        r2 = "bbbbbbbb-1111-2222-3333-444444444444"
        assert _footprints(base)[r2]["x_mm"] == pytest.approx(80.0)
        # Undo restores.
        with urllib.request.urlopen(
            urllib.request.Request(f"{base}/api/placement/undo", method="POST")
        ) as resp:
            assert json.load(resp)["ok"] is True
        assert _footprints(base)[u1]["x_mm"] == pytest.approx(50.0)
    finally:
        httpd.shutdown()


@pytest.mark.server
@pytest.mark.minimal
def test_nudge_rejects_empty_and_noop():
    httpd, base = _client()
    try:
        _load(base)
        with pytest.raises(urllib.error.HTTPError) as ei:
            _post(base, "/api/placement/nudge", {"dx_mm": 1.0, "dy_mm": 0.0})
        assert ei.value.code == 400  # nothing selected
        with pytest.raises(urllib.error.HTTPError) as ei:
            _post(base, "/api/placement/nudge", {"dx_mm": 0.0, "dy_mm": 0.0})
        assert ei.value.code == 400  # no-op
    finally:
        httpd.shutdown()


@pytest.mark.server
@pytest.mark.minimal
def test_nudge_burst_coalesces_into_one_undo(monkeypatch):
    """Holding an arrow key: N rapid nudges share one undo entry, and one
    undo reverts the whole burst (history isn't flushed with micro-steps)."""
    now = [1000.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: now[0])
    httpd, base = _client()
    try:
        _load(base)
        u1 = "aaaaaaaa-1111-2222-3333-444444444444"
        _post(base, "/api/placement/select", {"uuid": u1})
        for i in range(3):
            now[0] += 0.05  # 50ms apart: one burst
            r = _post(base, "/api/placement/nudge", {"dx_mm": 1.0, "dy_mm": 0.0})
            assert r["ok"]
        assert _footprints(base)[u1]["x_mm"] == pytest.approx(53.0)
        assert len(server.STATE.undo_stack) == 1
        with urllib.request.urlopen(
            urllib.request.Request(f"{base}/api/placement/undo", method="POST")
        ):
            pass
        assert _footprints(base)[u1]["x_mm"] == pytest.approx(50.0)
    finally:
        httpd.shutdown()


@pytest.mark.server
@pytest.mark.minimal
def test_nudge_burst_breaks_on_pause_or_reselect(monkeypatch):
    """Window expiry or a changed selection starts a new undo entry."""
    now = [1000.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: now[0])
    httpd, base = _client()
    try:
        _load(base)
        u1 = "aaaaaaaa-1111-2222-3333-444444444444"
        r2 = "bbbbbbbb-1111-2222-3333-444444444444"
        _post(base, "/api/placement/select", {"uuid": u1})
        _post(base, "/api/placement/nudge", {"dx_mm": 1.0, "dy_mm": 0.0})
        assert len(server.STATE.undo_stack) == 1
        # Pause past the window: new entry.
        now[0] += server.NUDGE_COALESCE_S + 0.5
        _post(base, "/api/placement/nudge", {"dx_mm": 1.0, "dy_mm": 0.0})
        assert len(server.STATE.undo_stack) == 2
        # Different selection inside the window: new entry.
        now[0] += 0.05
        _post(base, "/api/placement/select", {"uuid": r2})
        _post(base, "/api/placement/nudge", {"dx_mm": 1.0, "dy_mm": 0.0})
        assert len(server.STATE.undo_stack) == 3
    finally:
        httpd.shutdown()


@pytest.mark.server
@pytest.mark.minimal
def test_ghost_move_burst_coalesces(monkeypatch):
    """Ghost-panel stepper bursts share one undo entry like nudges."""
    now = [2000.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: now[0])
    httpd, base = _client()
    try:
        _load(base)
        r = _post(base, "/api/ghosts", {
            "kind": "wall", "ref": "GHOST_B",
            "x_mm": 10, "y_mm": 10, "w_mm": 20, "h_mm": 5,
        })
        wall_uuid = r["uuid"]
        assert len(server.STATE.undo_stack) == 1  # create checkpoints
        for i in range(3):
            now[0] += 0.05
            _post(base, "/api/ghosts/move", {"uuid": wall_uuid, "dx_mm": 1.0})
        assert _footprints(base)[wall_uuid]["x_mm"] == pytest.approx(13.0)
        assert len(server.STATE.undo_stack) == 2  # create + one burst
        with urllib.request.urlopen(
            urllib.request.Request(f"{base}/api/placement/undo", method="POST")
        ):
            pass
        # Burst reverted as one: back to the created pose.
        assert _footprints(base)[wall_uuid]["x_mm"] == pytest.approx(10.0)
    finally:
        httpd.shutdown()


@pytest.mark.server
@pytest.mark.minimal
def test_nudge_rotates_selection():
    httpd, base = _client()
    try:
        _load(base)
        u1 = "aaaaaaaa-1111-2222-3333-444444444444"
        assert _footprints(base)[u1]["angle_deg"] == 0
        _post(base, "/api/placement/select", {"uuid": u1})
        r = _post(base, "/api/placement/nudge", {"dx_mm": 0.0, "dy_mm": 0.0, "da_deg": 15.0})
        assert r["ok"] and r["moved"] == [u1]
        assert _footprints(base)[u1]["angle_deg"] == pytest.approx(15.0)
        # Position untouched by a pure rotation.
        assert _footprints(base)[u1]["x_mm"] == pytest.approx(50.0)
    finally:
        httpd.shutdown()


@pytest.mark.server
@pytest.mark.minimal
def test_ghost_crud_roundtrip():
    httpd, base = _client()
    try:
        _load(base)
        n_before = sum(1 for f in _footprints(base).values() if f["ghost"])
        # Create wall.
        r = _post(base, "/api/ghosts", {
            "kind": "wall", "ref": "GHOST_T1",
            "x_mm": 10, "y_mm": 10, "w_mm": 20, "h_mm": 5,
        })
        wall_uuid = r["uuid"]
        fps = _footprints(base)
        assert sum(1 for f in fps.values() if f["ghost"]) == n_before + 1
        assert fps[wall_uuid]["ref"] == "GHOST_T1"
        assert fps[wall_uuid]["x_mm"] == pytest.approx(10.0)
        # Move wall.
        _post(base, "/api/ghosts/move", {"uuid": wall_uuid, "dx_mm": 1.0, "dy_mm": 2.0})
        fps = _footprints(base)
        assert fps[wall_uuid]["x_mm"] == pytest.approx(11.0)
        assert fps[wall_uuid]["y_mm"] == pytest.approx(12.0)
        # Create attractor with target + strength visible to the UI.
        r = _post(base, "/api/ghosts", {
            "kind": "attractor", "ref": "GHOST_T2",
            "x_mm": 50, "y_mm": 30, "target_ref": "U1", "ka": 0.2,
        })
        fps = _footprints(base)
        assert fps[r["uuid"]]["ghost_attractions"] == [["U1", 0.2]]
        # Delete wall; attractor stays.
        _post(base, "/api/ghosts/delete", {"uuid": wall_uuid})
        fps = _footprints(base)
        assert wall_uuid not in fps
        assert r["uuid"] in fps
        # Undo restores the wall.
        with urllib.request.urlopen(
            urllib.request.Request(f"{base}/api/placement/undo", method="POST")
        ) as resp:
            assert json.load(resp)["ok"] is True
        assert wall_uuid in _footprints(base)
    finally:
        httpd.shutdown()


@pytest.mark.server
@pytest.mark.minimal
def test_ghost_rotate_and_resize():
    httpd, base = _client()
    try:
        _load(base)
        r = _post(base, "/api/ghosts", {
            "kind": "wall", "ref": "GHOST_RR",
            "x_mm": 10, "y_mm": 10, "w_mm": 20, "h_mm": 4,
        })
        uuid = r["uuid"]
        fp = _footprints(base)[uuid]
        assert fp["angle_deg"] == 0
        # Rotate 90°: wall extents swap in board space.
        _post(base, "/api/ghosts/move", {"uuid": uuid, "da_deg": 90.0})
        fp = _footprints(base)[uuid]
        assert fp["angle_deg"] == pytest.approx(90.0)
        w = fp["bbox"][2] - fp["bbox"][0]
        h = fp["bbox"][3] - fp["bbox"][1]
        assert (w, h) == (pytest.approx(4.0), pytest.approx(20.0))
        # Resize back to a local 20x4 rect (angle preserved).
        _post(base, "/api/ghosts/resize", {"uuid": uuid, "w_mm": 10.0, "h_mm": 6.0})
        fp = _footprints(base)[uuid]
        assert fp["angle_deg"] == pytest.approx(90.0)
        w = fp["bbox"][2] - fp["bbox"][0]
        h = fp["bbox"][3] - fp["bbox"][1]
        assert (w, h) == (pytest.approx(6.0), pytest.approx(10.0))
        # Bad sizes rejected.
        with pytest.raises(urllib.error.HTTPError) as ei:
            _post(base, "/api/ghosts/resize", {"uuid": uuid, "w_mm": 0, "h_mm": 6.0})
        assert ei.value.code == 400
    finally:
        httpd.shutdown()


@pytest.mark.server
@pytest.mark.minimal
def test_ghost_create_rejects_bad_input():
    httpd, base = _client()
    try:
        _load(base)
        # Duplicate ref (U1 is real).
        with pytest.raises(urllib.error.HTTPError) as ei:
            _post(base, "/api/ghosts", {
                "kind": "wall", "ref": "U1",
                "x_mm": 0, "y_mm": 0, "w_mm": 5, "h_mm": 5,
            })
        assert ei.value.code == 400
        # Unknown attractor target.
        with pytest.raises(urllib.error.HTTPError) as ei:
            _post(base, "/api/ghosts", {
                "kind": "attractor", "ref": "GHOST_BAD",
                "x_mm": 0, "y_mm": 0, "target_ref": "NOPE", "ka": 0.1,
            })
        assert ei.value.code == 400
        # Moving a real footprint via the ghost endpoint is rejected.
        with pytest.raises(urllib.error.HTTPError) as ei:
            _post(base, "/api/ghosts/move", {
                "uuid": "aaaaaaaa-1111-2222-3333-444444444444",
                "dx_mm": 1.0, "dy_mm": 0.0,
            })
        assert ei.value.code == 400
    finally:
        httpd.shutdown()
