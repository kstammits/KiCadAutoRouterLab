"""Shared live-server HTTP harness for the server test cluster.

One ``ThreadingHTTPServer`` spelling plus JSON POST/GET helpers,
used by ``test_server.py``, ``test_server_manual.py``,
``test_board_mode.py`` (and formerly ``test_server_validation.py``,
folded into ``test_server.py``). State reset stays per-file: the
fixtures reset different ``STATE`` subsets.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer


def serve_client(handler):
    """Boot a live server; return ``(httpd, base_url)`` (caller shuts down)."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def post_json(base, path, body):
    """POST a JSON body; return the parsed JSON response."""
    req = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def get_json(base, path):
    """GET a JSON endpoint; return the parsed response."""
    with urllib.request.urlopen(f"{base}{path}") as r:
        return json.load(r)
