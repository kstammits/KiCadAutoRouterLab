"""Shared live-server HTTP harness for the server test cluster.

One ``ThreadingHTTPServer`` spelling plus JSON/raw POST/GET helpers,
used by ``test_server.py``, ``test_server_manual.py``,
``test_board_mode.py`` (and formerly ``test_server_validation.py``,
folded into ``test_server.py``). State reset stays per-file: the
fixtures reset different ``STATE`` subsets.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from tests.helpers import post_fixture


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


def post_raw(base, path, payload):
    """POST a JSON payload; return ``(status, headers, body)``."""
    req = urllib.request.Request(
        f"{base}{path}", data=json.dumps(payload).encode(), method="POST"
    )
    with urllib.request.urlopen(req) as r:
        return r.status, r.headers, r.read()


def get_raw(base, path):
    """GET an endpoint; return ``(status, headers, body)``."""
    with urllib.request.urlopen(f"{base}{path}") as r:
        return r.status, r.headers, r.read()


def post_code(base, path, payload):
    """POST a JSON payload; return the HTTP status code (200 on success)."""
    try:
        post_json(base, path, payload)
    except urllib.error.HTTPError as exc:
        return exc.code
    return 200


def load_fixture(base, fixture="minimal.kicad_pcb", name=None, data=None):
    """POST a fixture board; return the parsed JSON response."""
    return post_fixture(base, fixture, name if name is not None else fixture,
                        data=data)
