"""Minimal stdlib web server for the autorouter workflow UI."""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

UI_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(UI_DIR.parent / "src"))

from kicad_autorouter.pipeline import stages  # noqa: E402


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

    def log_message(self, fmt, *args):  # keep console quiet
        pass


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Workflow UI: http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
