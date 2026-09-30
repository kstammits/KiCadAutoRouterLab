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

from kicad_autorouter.board_model import board_model  # noqa: E402
from kicad_autorouter.pipeline import stages  # noqa: E402
from kicad_autorouter.sexpr import parse  # noqa: E402
from kicad_autorouter.svg_render import render_board_svg  # noqa: E402


class BoardState:
    """In-memory snapshot of the currently loaded board."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.model = None
        self.name = ""
        self.version = 0

    def load(self, data: bytes, name: str) -> int:
        model = board_model(parse(data.decode("utf-8")))
        with self._lock:
            self.model = model
            self.name = name
            self.version += 1
            return self.version

    @property
    def snapshot(self):
        with self._lock:
            return self.model, self.name, self.version


STATE = BoardState()


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
        if parsed.path == "/api/state":
            model, name, version = STATE.snapshot
            body = json.dumps({"loaded": model is not None, "name": name, "version": version})
            self._send(200, body.encode(), "application/json")
            return
        if parsed.path == "/api/board.svg":
            model, name, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            svg = render_board_svg(model, title=name).encode()
            self._send(200, svg, "image/svg+xml")
            return
        rel = Path(self.path.lstrip("/")) or Path("index.html")
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
        if parsed.path != "/api/load":
            self._send(404, b"not found", "text/plain")
            return
        qs = parse_qs(parsed.query)
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
            version = STATE.load(data, name)
        except Exception as exc:
            self._send(400, f"parse error: {exc}".encode(), "text/plain")
            return
        body = json.dumps({"ok": True, "name": name, "version": version}).encode()
        self._send(200, body, "application/json")

    def log_message(self, fmt, *args):  # keep console quiet
        pass


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Workflow UI: http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
