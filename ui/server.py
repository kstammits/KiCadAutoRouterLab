"""Minimal stdlib web server for the autorouter workflow UI."""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

UI_DIR = Path(__file__).resolve().parent
REPO_ROOT = UI_DIR.parent.resolve()
sys.path.insert(0, str(UI_DIR.parent / "src"))

from kicad_autorouter.board_model import apply_deltas, board_model  # noqa: E402
from kicad_autorouter.io import nudge_footprint_by_uuid  # noqa: E402
from kicad_autorouter.pipeline import stages  # noqa: E402
from kicad_autorouter.placement import (  # noqa: E402
    PlacementParams,
    run_placement,
)
from kicad_autorouter.sexpr import parse  # noqa: E402
from kicad_autorouter.svg_render import render_board_svg  # noqa: E402

# User-tuned placement parameters persist here (gitignored).
PARAMS_PATH = REPO_ROOT / "placement.json"


class BoardState:
    """In-memory snapshot of the currently loaded board pair + proposal + undo stack."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.model = None
        self.sch = None
        self.pcb_tree = None  # Original S-expression tree for writeback
        self.original_pcb_tree = None  # Original tree for reset
        self.name = ""
        self.version = 0
        self.proposal = None
        self.pinned_uuids: set[str] = set()  # User-pinned (pseudo-locked)
        self.selected_uuids: set[str] = set()  # Currently selected for move
        self.undo_stack: list[tuple] = []  # Stack of (model, pcb_tree) for undo
        self.max_undo_depth = 20

    def load(self, data: bytes, name: str, kind: str) -> int:
        tree = parse(data.decode("utf-8"))
        expected = "kicad_sch" if kind == "sch" else "kicad_pcb"
        if tree.head != expected:
            raise ValueError(f"expected ({expected} ...), got head {tree.head!r}")
        with self._lock:
            if kind == "sch":
                self.sch = tree
            else:
                self.model = board_model(tree)
                self.pcb_tree = tree
                self.original_pcb_tree = tree
                self.name = name
                self.sch = None  # a new board invalidates any stale sibling
                self.proposal = None  # a new board invalidates any stale proposal
                self.pinned_uuids.clear()
                self.selected_uuids.clear()
                self.undo_stack.clear()
            self.version += 1
            return self.version

    @property
    def snapshot(self):
        with self._lock:
            return self.model, self.sch is not None, self.name, self.version

    @property
    def current_proposal(self):
        with self._lock:
            return self.proposal

    def set_proposal(self, proposal) -> int:
        with self._lock:
            self.proposal = proposal
            self.version += 1
            return self.version

    def get_pinned_uuids(self) -> set[str]:
        with self._lock:
            return self.pinned_uuids.copy()

    def get_selected_uuids(self) -> set[str]:
        with self._lock:
            return self.selected_uuids.copy()

    def set_pinned_uuids(self, uuids: set[str]) -> int:
        with self._lock:
            self.pinned_uuids = uuids.copy()
            self.version += 1
            return self.version

    def set_selected_uuids(self, uuids: set[str]) -> int:
        with self._lock:
            self.selected_uuids = uuids.copy()
            self.version += 1
            return self.version

    def toggle_pinned(self, uuid: str) -> int:
        with self._lock:
            if uuid in self.pinned_uuids:
                self.pinned_uuids.remove(uuid)
            else:
                self.pinned_uuids.add(uuid)
            self.version += 1
            return self.version

    def toggle_selected(self, uuid: str) -> int:
        with self._lock:
            if uuid in self.selected_uuids:
                self.selected_uuids.remove(uuid)
            else:
                self.selected_uuids.add(uuid)
            self.version += 1
            return self.version

    def clear_selection(self) -> int:
        with self._lock:
            self.selected_uuids.clear()
            self.version += 1
            return self.version

    def select_all_unlocked(self, model) -> int:
        """Select all unlocked, non-pinned footprints."""
        with self._lock:
            self.selected_uuids = {
                fp.uuid for fp in model.footprints
                if fp.uuid and not fp.locked and fp.uuid not in self.pinned_uuids
            }
            self.version += 1
            return self.version

    def push_undo(self) -> None:
        """Push current state to undo stack (called before mutating)."""
        with self._lock:
            if self.model is not None and self.pcb_tree is not None:
                self.undo_stack.append((self.model, self.pcb_tree))
                if len(self.undo_stack) > self.max_undo_depth:
                    self.undo_stack.pop(0)

    def undo(self) -> int:
        """Revert to previous state from undo stack."""
        with self._lock:
            if not self.undo_stack:
                return self.version
            self.model, self.pcb_tree = self.undo_stack.pop()
            self.proposal = None
            self.selected_uuids.clear()
            self.version += 1
            return self.version

    def reset_to_original(self) -> int:
        """Reset to the originally loaded board."""
        with self._lock:
            if self.original_pcb_tree is not None:
                self.model = board_model(self.original_pcb_tree)
                self.pcb_tree = self.original_pcb_tree
                self.proposal = None
                self.selected_uuids.clear()
                self.undo_stack.clear()
                self.version += 1
            return self.version

    def accept_proposal(self) -> int:
        """Apply current proposal to both model and PCB tree, push to undo stack."""
        with self._lock:
            if self.proposal is None or self.model is None or self.pcb_tree is None:
                raise ValueError("no proposal to accept")
            # Push current state to undo before applying
            self.push_undo()
            # Apply to BoardModel (for physics/rendering)
            self.model = apply_deltas(self.model, self.proposal.deltas)
            # Apply to S-expression tree (for writeback/download)
            for uuid, (dx, dy, da) in self.proposal.deltas.items():
                if dx != 0.0 or dy != 0.0 or da != 0.0:
                    self.pcb_tree = nudge_footprint_by_uuid(self.pcb_tree, uuid, dx, dy)
            self.proposal = None
            self.version += 1
            return self.version

    def get_pcb_tree(self):
        """Get the current PCB S-expression tree for download."""
        with self._lock:
            return self.pcb_tree


STATE = BoardState()


def load_params() -> PlacementParams:
    """Effective params: defaults overlaid with placement.json when present."""
    if PARAMS_PATH.is_file():
        try:
            return PlacementParams.from_dict(
                json.loads(PARAMS_PATH.read_text(encoding="utf-8"))
            )
        except (ValueError, json.JSONDecodeError):
            pass  # corrupt file falls back to defaults; next save overwrites it
    return PlacementParams()


def save_params(params: PlacementParams) -> None:
    PARAMS_PATH.write_text(
        json.dumps(params.to_dict(), indent=2) + "\n", encoding="utf-8"
    )


def proposal_to_json(proposal) -> dict:
    return {
        "deltas": {u: [dx, dy, da] for u, (dx, dy, da) in proposal.deltas.items()},
        "iterations": proposal.iterations,
        "final_max_disp_mm": proposal.final_max_disp_mm,
        "elapsed_s": proposal.elapsed_s,
        "params": proposal.params,
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/api/stages":
            self._send(200, json.dumps(stages()).encode(), "application/json")
            return
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/api/state":
            model, has_sch, name, version = STATE.snapshot
            body = json.dumps(
                {
                    "loaded": model is not None,
                    "name": name,
                    "version": version,
                    "has_sch": has_sch,
                }
            )
            self._send(200, body.encode(), "application/json")
            return
        if parsed.path == "/api/board.svg":
            model, _, name, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            overlay_on = qs.get("overlay", ["1"])[0] != "0"
            proposal = STATE.current_proposal if overlay_on else None
            pinned = STATE.get_pinned_uuids()
            selected = STATE.get_selected_uuids()
            svg = render_board_svg(model, title=name, proposal=proposal,
                                   pinned_uuids=pinned, selected_uuids=selected).encode()
            self._send(200, svg, "image/svg+xml")
            return
        if parsed.path == "/api/placement/params":
            defaults_only = qs.get("defaults", ["0"])[0] != "0"
            params = PlacementParams() if defaults_only else load_params()
            self._send(200, json.dumps(params.to_dict()).encode(), "application/json")
            return
        if parsed.path == "/api/placement/proposal":
            proposal = STATE.current_proposal
            if proposal is None:
                self._send(404, b"no proposal", "text/plain")
                return
            body = json.dumps(proposal_to_json(proposal)).encode()
            self._send(200, body, "application/json")
            return
        if parsed.path == "/api/placement/pinned":
            pinned = STATE.get_pinned_uuids()
            self._send(200, json.dumps({"ok": True, "pinned": list(pinned)}).encode(), "application/json")
            return
        if parsed.path == "/api/placement/selected":
            selected = STATE.get_selected_uuids()
            self._send(200, json.dumps({"ok": True, "selected": list(selected)}).encode(), "application/json")
            return
        if parsed.path == "/api/board/download":
            tree = STATE.get_pcb_tree()
            if tree is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            from kicad_autorouter.sexpr import to_sexpr
            filename = STATE.name.replace(".kicad_pcb", "-modified.kicad_pcb")
            content = to_sexpr(tree) + "\n"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(content.encode())))
            self.end_headers()
            self.wfile.write(content.encode())
            return
        rel_path = parsed.path.lstrip("/")
        rel = Path(rel_path) if rel_path else Path("index.html")
        full = (UI_DIR / rel).resolve()
        if not str(full).startswith(str(UI_DIR.resolve())):
            self._send(403, b"forbidden", "text/plain")
            return
        if not full.is_file():
            self._send(404, b"not found", "text/plain")
            return
        ctype = "text/html" if full.suffix == ".html" else "application/octet-stream"
        self._send(200, full.read_bytes(), ctype)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/api/load":
            kind = "sch" if (qs.get("kind") or ["pcb"])[0] == "sch" else "pcb"
            name = (qs.get("name") or ["board.kicad_pcb"])[0]
            path_q = (qs.get("path") or [None])[0]
            if path_q:
                full = (REPO_ROOT / path_q).resolve()
                if not str(full).startswith(str(REPO_ROOT)) or not full.is_file():
                    self._send(400, b"bad path", "text/plain")
                    return
                data = full.read_bytes()
            else:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    self._send(400, b"empty body", "text/plain")
                    return
                data = self.rfile.read(length)
            try:
                version = STATE.load(data, name, kind)
            except Exception as exc:
                self._send(400, f"parse error: {exc}".encode(), "text/plain")
                return
            payload = {"ok": True, "name": name, "kind": kind, "version": version}
            self._send(200, json.dumps(payload).encode(), "application/json")
            return
        if parsed.path == "/api/placement/params":
            length = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(length) if length > 0 else b""
            try:
                params = PlacementParams.from_dict(json.loads(data.decode("utf-8")))
            except ValueError as exc:
                self._send(400, f"bad params: {exc}".encode(), "text/plain")
                return
            save_params(params)
            self._send(200, json.dumps(params.to_dict()).encode(), "application/json")
            return
        if parsed.path == "/api/placement/run":
            model, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            # Read JSON body for optional parameters
            length = int(self.headers.get("Content-Length") or 0)
            body_data = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req = json.loads(body_data.decode("utf-8"))
            except json.JSONDecodeError:
                self._send(400, b"invalid JSON", "text/plain")
                return

            iterations = req.get("iterations")
            movable_uuids = req.get("movable_uuids")
            if isinstance(movable_uuids, list):
                movable_uuids = set(movable_uuids)
            elif movable_uuids is not None:
                self._send(400, b"movable_uuids must be a list", "text/plain")
                return

            params = load_params()
            if iterations is not None:
                if not isinstance(iterations, int) or iterations < 1:
                    self._send(400, b"iterations must be a positive integer", "text/plain")
                    return
                # Create params with overridden max_iterations, preserve other settings
                # Use request body for stub if provided, else fall back to saved params
                req_stub = req.get("stub", params.stub)
                params = PlacementParams(
                    repulsion_kr=params.repulsion_kr,
                    attraction_ka=params.attraction_ka,
                    ideal_length_mm=params.ideal_length_mm,
                    max_iterations=iterations,
                    convergence_eps_mm=params.convergence_eps_mm,
                    rigid_stiffness=params.rigid_stiffness,
                    courtyard_repulsion_kc=params.courtyard_repulsion_kc,
                    demo_jitter_mm=params.demo_jitter_mm,
                    stub=req_stub,
                )

            # If movable_uuids not provided, compute as all unlocked except pinned
            if movable_uuids is None:
                movable_uuids = {
                    fp.uuid for fp in model.footprints
                    if fp.uuid and not fp.locked and fp.uuid not in STATE.get_pinned_uuids()
                }
            

            try:
                proposal = run_placement(model, params, movable_uuids)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                self._send(500, f"placement failed: {exc}".encode(), "text/plain")
                return
            version = STATE.set_proposal(proposal)
            payload = {
                "ok": True,
                "moved": len(proposal.deltas),
                "iterations": proposal.iterations,
                "final_max_disp_mm": proposal.final_max_disp_mm,
                "elapsed_s": proposal.elapsed_s,
                "version": version,
            }
            self._send(200, json.dumps(payload).encode(), "application/json")
            return
        if parsed.path == "/api/placement/accept":
            try:
                version = STATE.accept_proposal()
            except ValueError as exc:
                self._send(400, f"{exc}".encode(), "text/plain")
                return
            self._send(
                200,
                json.dumps({"ok": True, "version": version}).encode(),
                "application/json",
            )
            return
        if parsed.path == "/api/placement/clear":
            version = STATE.set_proposal(None)
            self._send(
                200,
                json.dumps({"ok": True, "version": version}).encode(),
                "application/json",
            )
            return
        if parsed.path == "/api/placement/undo":
            version = STATE.undo()
            self._send(
                200,
                json.dumps({"ok": True, "version": version}).encode(),
                "application/json",
            )
            return
        if parsed.path == "/api/board/reset":
            version = STATE.reset_to_original()
            self._send(
                200,
                json.dumps({"ok": True, "version": version}).encode(),
                "application/json",
            )
            return
        if parsed.path == "/api/placement/apply":
            # Writeback (nudge by UUID + save_pair + DRC) lands in PLAN item 3.
            self._send(
                501, b"writeback not implemented yet (PLAN item 3)", "text/plain"
            )
            return
        if parsed.path == "/api/placement/pin":
            model, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            body_data = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req = json.loads(body_data.decode("utf-8"))
            except json.JSONDecodeError:
                self._send(400, b"invalid JSON", "text/plain")
                return
            uuid = req.get("uuid")
            if not uuid:
                self._send(400, b"uuid required", "text/plain")
                return
            version = STATE.toggle_pinned(uuid)
            pinned = STATE.get_pinned_uuids()
            self._send(200, json.dumps({"ok": True, "pinned": list(pinned), "version": version}).encode(), "application/json")
            return
        if parsed.path == "/api/placement/select":
            model, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            body_data = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req = json.loads(body_data.decode("utf-8"))
            except json.JSONDecodeError:
                self._send(400, b"invalid JSON", "text/plain")
                return
            uuid = req.get("uuid")
            if not uuid:
                self._send(400, b"uuid required", "text/plain")
                return
            version = STATE.toggle_selected(uuid)
            selected = STATE.get_selected_uuids()
            self._send(200, json.dumps({"ok": True, "selected": list(selected), "version": version}).encode(), "application/json")
            return
        if parsed.path == "/api/placement/select_all":
            model, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            version = STATE.select_all_unlocked(model)
            selected = STATE.get_selected_uuids()
            self._send(200, json.dumps({"ok": True, "selected": list(selected), "version": version}).encode(), "application/json")
            return
        if parsed.path == "/api/placement/clear_selection":
            version = STATE.clear_selection()
            self._send(200, json.dumps({"ok": True, "version": version}).encode(), "application/json")
            return
        if parsed.path == "/api/placement/pinned":
            pinned = STATE.get_pinned_uuids()
            self._send(200, json.dumps({"ok": True, "pinned": list(pinned)}).encode(), "application/json")
            return
        if parsed.path == "/api/placement/selected":
            selected = STATE.get_selected_uuids()
            self._send(200, json.dumps({"ok": True, "selected": list(selected)}).encode(), "application/json")
            return
        if parsed.path == "/api/placement/apply":
            # Writeback (nudge by UUID + save_pair + DRC) lands in PLAN item 3.
            self._send(
                501, b"writeback not implemented yet (PLAN item 3)", "text/plain"
            )
            return
        self._send(404, b"not found", "text/plain")

    def log_message(self, fmt, *args):  # keep console quiet
        pass


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Workflow UI: http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
