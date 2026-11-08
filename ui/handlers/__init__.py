"""Domain handler modules for the workflow UI HTTP API.

Each submodule exports ``handle_get(path, qs, handler)`` and
``handle_post(path, qs, body, handler)`` returning ``(code, body, content_type)``
or ``None`` when it does not own ``path``. ``ui/server.py`` owns the
:class:`~http.server.BaseHTTPRequestHandler`` subclass, dispatch, and all shared
state/helpers; the handler modules contain only the per-endpoint logic moved
per-endpoint logic.
"""
